# -*- coding: utf-8 -*-
"""manifest_builder.py — 数据缝合与质检层（V2.0 架构第 4 层）

职责（借鉴 archify 两阶段交付契约）：
  1. validate：白名单过滤 AI 输出（越界字段/编造节点/编造边/flows 无背书 → drop + 记录）
     + 坐标校验（文件存在、引用一致性）+ 增量合并（unchanged 旧语义原样保留）
  2. deliver：校验全部通过才原子替换输出（tmp + os.replace）；
     **任何失败都保留上一版 final_manifest.json**，退出码非 0。

用法：
  python scripts/manifest_builder.py --fact-graph <fact_graph.json> \
      [--ai-output <ai.json>] [--old-manifest <final_manifest.json>] \
      --out <final_manifest.json> [--project <root>] [--ai-files a.py,b.py] \
      [--time-window-min 120] [--report report.json]

AI 输出格式（见 instructions.md）：完整 manifest 形态的 JSON（纯 JSON、无 markdown 包裹）。
非法/缺失 AI 输出 → 不中止：用事实骨架 + 旧语义生成"降级版"，并明示。
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SEMANTIC_FIELDS = {"tech_stack", "summary", "design_reason", "alternatives",
                   "known_risks", "code_snippet", "story"}
STORY_FIELDS = {"t", "d", "note"}
SEMANTIC_RELATIONS = {"动态调用", "消息", "接口实现", "配置引用"}
MAX_SOUL_QUESTIONS = 5
STRING_FIELDS = {"tech_stack", "summary", "design_reason", "code_snippet"}
LIST_FIELDS = {"alternatives", "known_risks"}


def load_json(path: Path):
    """读取 JSON；文件缺失/非法 → None（AI 输出非法 = 降级，不中断）。"""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _clean_str(v):
    return v.strip() if isinstance(v, str) and v.strip() else None


def _clean_list(v):
    if not isinstance(v, list):
        return None
    out = [x for x in v if isinstance(x, str) and x.strip()]
    return out or None


def sanitize_ai_output(ai, facts, report):
    """白名单过滤：只抽允许的语义字段；drop 编造节点/边/flows（静默丢弃+记录）。"""
    sem = {"nodes": {}, "edges": [], "flows": [], "soul_questions": []}
    fact_paths = {n["file_path"] for n in facts["nodes"]}

    for an in (ai.get("nodes") or []):
        if not isinstance(an, dict):
            continue
        fp = an.get("file_path")
        if fp not in fact_paths:
            report["dropped_nodes"].append(fp or "<?>")
            continue
        clean = {}
        for k in STRING_FIELDS:
            v = _clean_str(an.get(k))
            if v:
                clean[k] = v
        for k in LIST_FIELDS:
            v = _clean_list(an.get(k))
            if v:
                clean[k] = v
        story = []
        for s in (an.get("story") or []) if isinstance(an.get("story"), list) else []:
            if isinstance(s, dict) and isinstance(s.get("t"), str) and s["t"].strip():
                story.append({k: s[k] for k in STORY_FIELDS if k in s and isinstance(s[k], str)})
        if story:
            clean["story"] = story
        sem["nodes"][fp] = clean

    node_ids = {n["id"] for n in facts["nodes"]}
    for ae in (ai.get("edges") or []):
        if not isinstance(ae, dict):
            continue
        src, tgt, rel = ae.get("source"), ae.get("target"), ae.get("relation")
        if src in node_ids and tgt in node_ids and rel in SEMANTIC_RELATIONS:
            sem["edges"].append({"source": src, "target": tgt, "relation": rel, "factual": False})
        else:
            report["dropped_edges"].append(ae)

    fact_edges = {(e["source"], e["target"]) for e in facts["edges"]}
    for fl in (ai.get("flows") or []):
        if not isinstance(fl, dict) or not isinstance(fl.get("steps"), list) or len(fl["steps"]) < 2:
            report["dropped_flows"].append(fl or "<?>")
            continue
        ok, steps = True, []
        for st in fl["steps"]:
            if not isinstance(st, dict) or st.get("node") not in node_ids or not isinstance(st.get("order"), int) or st["order"] < 1:
                ok = False
                break
            steps.append({"node": st["node"], "order": st["order"],
                          **({"action": str(st["action"])} if isinstance(st.get("action"), str) and st["action"] else {})})
        if not ok:
            report["dropped_flows"].append(fl.get("id", "<?>"))
            continue
        for i in range(len(steps) - 1):  # 相邻步骤背书检查（事实边或 gap_hint）
            a, b = steps[i]["node"], steps[i + 1]["node"]
            if (a, b) not in fact_edges and not str(fl.get("gap_hint") or "").strip():
                ok = False
                report["dropped_flows"].append(f"{fl.get('id')}/step{i + 1}({a}→{b} 无背书)")
                break
        if not ok:
            continue
        out = {"id": str(fl["id"]), "name": str(fl["name"]), "entry": str(fl["entry"]),
               "steps": steps}
        if str(fl.get("gap_hint") or "").strip():
            out["gap_hint"] = str(fl["gap_hint"]).strip()
        sem["flows"].append(out)

    for q in (ai.get("soul_questions") or []):
        if isinstance(q, str) and q.strip():
            sem["soul_questions"].append(q.strip())
    sem["soul_questions"] = sem["soul_questions"][:MAX_SOUL_QUESTIONS]
    return sem


def old_semantics(old_manifest) -> dict:
    """旧 manifest → {file_path: {语义字段..., is_ai_generated, ai_evidence}}"""
    out = {}
    for n in (old_manifest or {}).get("nodes", []) or []:
        fp = n.get("file_path")
        if not fp:
            continue
        entry = {}
        for k in SEMANTIC_FIELDS:
            if isinstance(n.get(k), (str, list, dict)) and n.get(k):
                entry[k] = n[k]
        for k in ("is_ai_generated", "ai_evidence"):
            if n.get(k):
                entry[k] = n[k]
        if entry:
            out[fp] = entry
    return out


def merge(facts, old_manifest, sem, report) -> dict:
    """事实为骨；unchanged(hash 相同) 旧语义原样；new/updated/moved 新语义覆盖；deleted 保旧故事。"""
    old_sem = old_semantics(old_manifest)
    old_by_path = {n["file_path"]: n for n in (old_manifest or {}).get("nodes", []) if n.get("file_path")}
    node_ids = {n["id"] for n in facts["nodes"]}

    nodes = []
    for fn in sorted(facts["nodes"], key=lambda n: n["file_path"]):
        fp = fn["file_path"]
        st = fn["status"]
        node = {k: fn[k] for k in ("id", "name", "type", "file_path", "start_line", "hash",
                                   "language", "symbols", "status") if k in fn}
        if fn.get("old_path"):
            node["old_path"] = fn["old_path"]

        old_meta = old_by_path.get(fp)
        old = old_sem.get(fp, {})
        fresh = sem["nodes"].get(fp, {})
        is_unchanged = (st == "active" and old_meta is not None
                        and old_meta.get("hash") == node.get("hash")
                        and not fn.get("old_path"))
        is_ai = bool(old.get("is_ai_generated"))

        if st == "deleted":
            node.update({k: v for k, v in old.items() if k != "story"})
            node["story"] = old.get("story") or []
        elif is_unchanged:
            node.update({k: v for k, v in old.items() if k != "story"})
            node["story"] = old.get("story") or []
        else:  # new / updated / moved：AI 语义覆盖，旧故事追加去重
            node.update(fresh)
            merged_story = []
            for s in (old.get("story") or []):
                merged_story.append(s)
            for s in (fresh.get("story") or []):
                if not any(m.get("t") == s.get("t") for m in merged_story):
                    merged_story.append(s)
            if merged_story:
                node["story"] = merged_story
            if not fn.get("old_path"):
                is_ai = False  # 新语义节点由三重判定决定
        node["is_ai_generated"] = is_ai
        nodes.append(node)

    # 语义边 / 事实边合并（语义边仅保留引用存在的）
    edges = [dict(e) for e in facts["edges"]] + [
        e for e in sem["edges"] if e["source"] in node_ids and e["target"] in node_ids
    ]

    return {
        "schema_version": 1,
        "manifest_type": "codebase-insight",
        "project_name": facts["project_name"],
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repository": facts["repository"],
        "stats": {
            "total_nodes": len(nodes),
            "ai_contributed_nodes": sum(1 for n in nodes if n.get("is_ai_generated")),
            "total_files": facts["stats"].get("total_files", len(nodes)),
            "changed_nodes": sum(1 for n in nodes
                                 if n["status"] == "deleted" or n.get("old_path")
                                 or (n["status"] == "active" and n["file_path"] not in old_sem)),
        },
        "nodes": sorted(nodes, key=lambda n: n["file_path"]),
        "edges": edges,
        "flows": sem["flows"],
        "soul_questions": sem["soul_questions"],
    }


def ai_judgement(project_root: Path, paths: set[str], user_ai_files: set[str],
                 window_min: int, agent_files: dict | None = None) -> dict:
    """is_ai_generated 判定（四重证据，满足其二）。

    证据渠道：
      1. Git 状态（新增/未追踪）—— commit 后失效
      2. 时间窗（mtime 近 window_min 分钟）—— 最弱，人类编辑也命中
      3. 用户确认（--ai-files）
      4. AI 会话记录（agent_evidence：宿主会话 JSONL 审计，Agent 自动写文件，
         跨会话依然可信；复制粘贴无法捕获，只能靠渠道 3 补充）

    返回 {path: (bool, evidence[])}。
    """
    result = {}
    agent_files = agent_files or {}

    git_new = set()
    try:
        r = subprocess.run(["git", "-C", str(project_root), "status", "--porcelain", "--untracked-files=all"],
                           capture_output=True, text=True, timeout=10, encoding="utf-8", errors="replace")
        for ln in (r.stdout or "").splitlines():
            if len(ln) >= 4 and ln[:2].strip() in ("??", "A", "AM"):
                git_new.add(ln[3:].strip().replace("\\", "/"))
    except (OSError, subprocess.TimeoutExpired):
        pass

    now = datetime.now().timestamp()
    for p in paths:
        evidence = []
        if p in git_new:
            evidence.append("Git 状态：新增/未追踪")
        try:
            st = (project_root / p).stat()
            if (now - st.st_mtime) <= window_min * 60:
                evidence.append(f"时间窗：近 {window_min} 分钟内修改")
        except OSError:
            pass
        if p in user_ai_files:
            evidence.append("用户确认：已勾选")
        if p in agent_files:
            evidence.append(f"AI 会话记录：{agent_files[p]} 自动写入")
        result[p] = (len(evidence) >= 2, evidence)
    return result


def validate_manifest(m, project_root, report) -> tuple[bool, list[str]]:
    """结构/引用/坐标校验。返回 (ok, errors)；warnings 进 report。"""
    errors = []
    ids = set()
    for n in m["nodes"]:
        if n["status"] not in ("active", "deleted", "moved"):
            errors.append(f"node 状态非法: {n['file_path']}")
        if not re.fullmatch(r"[a-fA-F0-9]{64}", n.get("hash", "")):
            errors.append(f"hash 非法: {n['file_path']}")
        ids.add(n["id"])
        if n["status"] != "deleted":
            fp = project_root / n["file_path"]
            if not fp.is_file():
                errors.append(f"坐标校验失败: 文件不存在 {n['file_path']}")
        for risk in (n.get("known_risks") or []):
            if not re.search(r":\d+", risk):
                report["warnings"].append(f"known_risk 缺少行号坐标: {n['file_path']}")
    for e in m["edges"]:
        if e["source"] not in ids or e["target"] not in ids:
            errors.append(f"边引用不存在的节点: {e['source']}→{e['target']}")
    return (not errors), errors


def deliver(out_path: Path, payload: dict) -> None:
    """原子替换：tmp + os.replace（deliver 语义：失败保留上一版）。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(out_path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, out_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Codebase Insight 数据缝合与质检")
    ap.add_argument("--fact-graph", required=True, help="scanner.py 产出的 fact_graph.json")
    ap.add_argument("--ai-output", default=None, help="AI 语义层输出 JSON（可选；缺失→降级）")
    ap.add_argument("--old-manifest", default=None, help="上一版 final_manifest.json（增量基础）")
    ap.add_argument("--out", required=True, help="final_manifest.json 输出路径")
    ap.add_argument("--project", default=None, help="项目根（默认取 fact repository.root）")
    ap.add_argument("--ai-files", default="", help="用户确认的 AI 生成文件（逗号分隔相对路径）")
    ap.add_argument("--time-window-min", type=int, default=None, help="时间窗分钟（默认 120，config.ai.time_window_min 覆盖）")
    ap.add_argument("--config", default=None, help="config.yaml 路径")
    ap.add_argument("--no-agent-evidence", action="store_true",
                    help="禁用 AI 会话日志证据通道（默认启用）")
    ap.add_argument("--report", default=None, help="校验报告 JSON 输出路径")
    args = ap.parse_args(argv)

    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    cfg = {}
    if args.config:
        from scanner import load_config
        cfg = load_config(Path(args.config))
    time_window = args.time_window_min or (cfg.get("ai") or {}).get("time_window_min") or 120

    report = {"ok": False, "errors": [], "warnings": [], "dropped_nodes": [],
              "dropped_edges": [], "dropped_flows": [], "ai_evidence": {},
              "delivered": False, "degraded": False}

    facts = load_json(Path(args.fact_graph))
    if not facts or facts.get("manifest_type") != "codebase-insight":
        print("error: fact_graph 缺失或非法", file=sys.stderr)
        report["errors"].append("fact_graph 缺失或非法")
        return 2

    project_root = Path(args.project or facts["repository"]["root"]).resolve()
    old_manifest = load_json(Path(args.old_manifest)) if args.old_manifest else None
    ai = load_json(Path(args.ai_output)) if args.ai_output else None
    if args.ai_output and ai is None:
        report["degraded"] = True
        report["warnings"].append("AI 输出缺失/非法 JSON：本次语义丢弃，生成降级版")
    elif ai is None:
        report["degraded"] = True

    sem = sanitize_ai_output(ai or {}, facts, report)
    final = merge(facts, old_manifest, sem, report)

    # is_ai_generated 判定（候选：new/updated/moved = 有 old_path 或 hash 与旧 manifest 不同或不在旧清单）
    old_by_path = {n["file_path"]: n for n in (old_manifest or {}).get("nodes", []) if n.get("file_path")}
    candidates = set()
    for n in final["nodes"]:
        if n["status"] == "deleted":
            continue
        on = old_by_path.get(n["file_path"])
        if n.get("old_path") or on is None or on.get("hash") != n.get("hash"):
            candidates.add(n["file_path"])
    user_files = {p.strip().replace("\\", "/") for p in args.ai_files.split(",") if p.strip()}
    # 第 4 证据通道：宿主会话日志审计（reasonix/codex/claude-code）
    agent_files: dict = {}
    if not args.no_agent_evidence:
        try:
            from agent_evidence import AgentEvidence, HOSTS
            hosts = tuple((cfg.get("agent_evidence") or {}).get("hosts") or HOSTS)
            ev = AgentEvidence(project_root, time_window).scan_all(hosts=hosts)
            agent_files = ev.files
            agent_report = ev.report()
            agent_report["enabled"] = True
            report["agent_evidence"] = agent_report
            if agent_report.get("missed_write_file"):
                report["warnings"].append(
                    f"reasonix write_file 返回纯内容无法取路径（{agent_report['missed_write_file']} 处），"
                    "复制粘贴/未归档会话请用 --ai-files 补充")
        except Exception as e:  # noqa: BLE001 — 证据通道故障不阻断主流程
            report["warnings"].append(f"AI 会话证据不可用: {e}")
    judgement = ai_judgement(project_root, candidates, user_files, time_window, agent_files)
    for n in final["nodes"]:
        if n["file_path"] in judgement:
            flag, evidence = judgement[n["file_path"]]
            if flag:
                n["is_ai_generated"] = True
            if evidence:
                n["ai_evidence"] = evidence
                report["ai_evidence"][n["file_path"]] = evidence
    final["stats"]["ai_contributed_nodes"] = sum(1 for n in final["nodes"] if n.get("is_ai_generated"))

    ok, errors = validate_manifest(final, project_root, report)
    report["ok"] = ok
    report["errors"].extend(errors)

    out_path = Path(args.out).resolve()
    if ok:
        deliver(out_path, final)
        report["delivered"] = True
        print(f"✔ 缝合完成: {out_path}")
        print(f"  节点 {final['stats']['total_nodes']}  AI {final['stats']['ai_contributed_nodes']}"
              f"  边 {len(final['edges'])}  流程 {len(final['flows'])}"
              f"  灵魂拷问 {len(final['soul_questions'])}")
    else:
        print(f"✘ 校验失败，保留上一版输出（{out_path} 未动）", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)

    if report["dropped_nodes"]:
        print(f"  [过滤] 编造节点 {len(report['dropped_nodes'])}: "
              f"{', '.join(map(str, report['dropped_nodes'][:5]))}...", file=sys.stderr)
    if report["dropped_edges"]:
        print(f"  [过滤] 无法背书的边 {len(report['dropped_edges'])}", file=sys.stderr)
    if report["dropped_flows"]:
        print(f"  [过滤] 无背书的流程/步骤 {len(report['dropped_flows'])}", file=sys.stderr)

    if args.report:
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
