# -*- coding: utf-8 -*-
"""scanner.py — Codebase Insight 确定性事实底座（V2.0 架构第 2 层）

用法：
    python scripts/scanner.py --project <path> [--cache-dir DIR] [--no-cache] [--out FILE]

产出 fact_graph.json（pure facts）：
    - nodes：文件级节点（id/name/type/file_path/start_line/hash/language/symbols/status/old_path）
    - edges：import 关系（factual: true），AI 无权覆盖
    - repository：{url, revision, root} 仓库证据绑定（借鉴 archify）
    - status：由 lifecycle.compare 对旧缓存做 delta 判定（new/updated/unchanged/moved/deleted）

约定：非 git 或解析失败 → revision 全 0 / parse_ok=False 降级为文件级节点（不中断）。
"""
import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from posixpath import normpath

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Windows 控制台默认 GBK，UTF-8 重配避免中文/特殊符号输出崩溃
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from fingerprint import hash_file  # noqa: E402
from lifecycle import FileRec, compare, write_snapshot  # noqa: E402
from plugins import get_plugin  # noqa: E402

SCHEMA_VERSION = 1
MANIFEST_TYPE = "codebase-insight"
MAX_TEXT_BYTES = 2 * 1024 * 1024          # 超过按二进制处理
READ_HEAD_BYTES = 8192

DEFAULT_IGNORE = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env", ".env",
    ".idea", ".vscode", "dist", "build", "target", "site-packages", ".cache",
    # 本工具产物（扫描器必须自我排除，防止把上轮输出扫成节点）
    "fact_graph.json", "final_manifest.json", "report.json",
    "*.pyc", "*.pyo", "*.so", "*.dll", "*.dylib", "*.exe", "*.o", "*.a",
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.svg", "*.ico", "*.webp", "*.pdf",
    "*.lock", "*.db", "*.sqlite", "*.sqlite3", "*.log", "*.min.js", "*.map",
    "*.zip", "*.tar", "*.gz", "*.7z", "*.whl",
}


def default_cache_dir(project_root: str) -> Path:
    """~/.cache/project_viz/<root 绝对路径 hash[:12]>（V2.0：全局缓存 + 路径隔离）"""
    import hashlib
    tag = hashlib.sha256(str(project_root).encode("utf-8")).hexdigest()[:12]
    return Path.home() / ".cache" / "project_viz" / tag


def load_config(path: Path) -> dict:
    """极简配置加载：剥离 # 注释 → json.loads（YAML 兼容 JSON 语法，零依赖）。

    config.yaml 中允许行内/整行 # 注释；解析失败返回空 dict（不阻塞扫描）。"""
    if not path or not Path(path).is_file():
        return {}
    try:
        text = Path(path).read_text(encoding="utf-8")
        lines = [ln.split("#", 1)[0] if not ln.lstrip().startswith("#") else ""
                 for ln in text.splitlines()]
        return json.loads("\n".join(lines)) or {}
    except (OSError, json.JSONDecodeError):
        print(f"  [warn] config 无法解析，忽略: {path}", file=sys.stderr)
        return {}


def load_gitignore(project_root: Path) -> list[str]:
    """读取 .gitignore（V1 简化：支持行模式，忽略 ! 反选并提示）。"""
    try:
        lines = (project_root / ".gitignore").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines:
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        if ln.startswith("!"):
            print(f"  [warn] .gitignore 反选规则暂不支持, 忽略: {ln}", file=sys.stderr)
            continue
        out.append(ln.lstrip("/"))
    return out


def ignored(rel: str, is_dir: bool, patterns: list[str]) -> bool:
    """rel: posix 相对路径。目录按前缀匹配，文件按 fnmatch。"""
    for pat in patterns:
        pat = pat.rstrip("/")
        if not pat:
            continue
        if fnmatch.fnmatch(rel, pat) or rel.startswith(pat + "/"):
            return True
        if "/" not in pat and fnmatch.fnmatch(rel.split("/")[0], pat):
            return True
    return False


def iter_project_files(project_root: Path, patterns: list[str], include_hidden: bool) -> list[Path]:
    files = []
    for dirpath, dirnames, filenames in os.walk(project_root):
        rel_dir = Path(dirpath).relative_to(project_root).as_posix()
        if rel_dir == ".":
            rel_dir = ""
        dirnames[:] = [
            d for d in dirnames
            if (include_hidden or not d.startswith("."))
            and not ignored((rel_dir + "/" + d).strip("/"), True, patterns)
        ]
        for fn in filenames:
            rel = (rel_dir + "/" + fn).strip("/")
            if not include_hidden and fn.startswith("."):
                continue
            if ignored(rel, False, patterns):
                continue
            files.append(Path(dirpath) / fn)
    return files


def is_binary(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return b"\x00" in f.read(READ_HEAD_BYTES)
    except OSError:
        return True


def resolve_python(rel_path: str, spec: str, paths: set[str], stems: dict[str, list[str]] | None = None) -> str | None:
    """解析 Python import spec → 仓库内相对路径；相对导入按目录解析。

    stems 兜底：模块名不带包前缀的扁平项目（如 scripts/ 下 from fingerprint import ...），
    按 spec 最后段匹配文件 stem（V1 局限：同名不同目录时取首个，README 已声明）。
    """
    base_dir = rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""
    parts = spec.split(".")
    if spec.startswith("."):
        dots = len(spec) - len(spec.lstrip("."))
        tail = [p for p in parts[dots:] if p]
        target = normpath("/".join([base_dir] + [".."] * (dots - 1) + tail) if dots > 1 else
                          "/".join([base_dir] + tail))
        target = target.lstrip("./")
    else:
        target = "/".join(parts)
    for cand in (target + ".py", target + ".pyi", target + "/__init__.py"):
        if cand in paths:
            return cand
    if stems:  # 兜底：模块名 → 任意目录下的同名文件
        last = parts[-1]
        for cand in stems.get(last, []):
            if cand.endswith((".py", ".pyi")):
                return cand
    return None


def resolve_js(rel_path: str, spec: str, paths: set[str], stems: dict[str, list[str]]) -> str | None:
    base_dir = rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""
    cands = []
    if spec.startswith("."):
        t = normpath("/".join([base_dir, spec])).lstrip("./")
        cands += [t + e for e in (".ts", ".tsx", ".js", ".jsx")] + [t + "/index.ts", t + "/index.js"]
    else:
        t = spec.lstrip("./")
        cands += [t + e for e in (".ts", ".tsx", ".js", ".jsx")] + [t + "/index.ts", t + "/index.js"]
    for c in cands:
        if c in paths:
            return c
    stem = spec.rsplit("/", 1)[-1]
    for c in stems.get(stem, []):  # 兜底：按文件名匹配
        return c
    return None


def resolve_go(spec: str, paths: set[str], stems: dict[str, list[str]]) -> str | None:
    seg = spec.rsplit("/", 1)[-1]
    for c in stems.get(seg, []):
        return c
    return None


def resolve_java(spec: str, paths: set[str]) -> str | None:
    cand = spec.replace(".", "/") + ".java"
    if cand in paths:
        return cand
    seg = spec.rsplit(".", 1)[-1]
    for p in paths:
        if p.endswith("/" + seg + ".java") or p == seg + ".java":
            return p
    return None


def build_edges(files: dict[str, FileRec], paths: set[str], stems: dict[str, list[str]]) -> list[dict]:
    edges, seen = [], set()
    for rel, rec in files.items():
        resolvers = {
            "python": lambda spec: resolve_python(rel, spec, paths, stems),
            "javascript/typescript": lambda spec: resolve_js(rel, spec, paths, stems),
            "go": lambda spec: resolve_go(spec, paths, stems),
            "java": lambda spec: resolve_java(spec, paths),
        }
        res = resolvers.get(rec.language)
        if not res:
            continue
        for spec in rec.imports:
            target = res(spec)
            if target and target != rel and (rel, target) not in seen:
                seen.add((rel, target))
                edges.append({"source": rel, "target": target, "relation": "import", "factual": True})
    return edges


def git_evidence(project_root: Path) -> dict:
    """仓库证据（借鉴 archify meta.repository）：git HEAD 40 位 sha + origin url。"""
    def run(*args):
        try:
            return subprocess.run(
                ["git", "-C", str(project_root), *args], capture_output=True,
                text=True, timeout=10, check=False,
            ).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return ""
    rev = run("rev-parse", "HEAD")
    url = run("remote", "get-url", "origin")
    return {
        "url": url or "",
        "revision": rev if re.fullmatch(r"[a-fA-F0-9]{40}", rev) else "0" * 40,
        "root": str(project_root.resolve()),
    }


def scan(project_root: Path, cache_dir: Path, no_cache: bool, include_hidden: bool,
         out_path: Path | None = None, excludes: tuple = (), extra_ignores: tuple = ()) -> dict:
    """扫描 + 缓存读写一体（测试可直接调用；main 仅做 CLI 包装）。"""
    patterns = DEFAULT_IGNORE | set(load_gitignore(project_root)) | set(extra_ignores)
    files_paths = iter_project_files(project_root, list(patterns), include_hidden)

    files: dict[str, FileRec] = {}
    symbol_meta: dict[str, list[dict]] = {}
    unknown = 0
    exclude_set = {Path(x).resolve() for x in excludes}
    for p in files_paths:
        if p.resolve() in exclude_set:
            continue
        rel = p.relative_to(project_root).as_posix()
        if p.stat().st_size > MAX_TEXT_BYTES or is_binary(p):
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        plugin = get_plugin(rel)
        if plugin:
            syms_meta = plugin["extract_symbols"](text)
            imports = plugin["extract_imports"](text)
            language = plugin["language"]
        else:  # 未知语言：降级为文件级节点（parse_ok=False 语义，edges 留空）
            syms_meta, imports, language = [], [], "unknown"
            unknown += 1
        files[rel] = FileRec(rel, language, hash_file(p), {s["name"] for s in syms_meta}, imports)
        symbol_meta[rel] = syms_meta

    previous = {}
    if not no_cache:
        old = cache_dir / "fact_graph.json"
        if old.exists():
            try:
                old_graph = json.loads(old.read_text(encoding="utf-8"))
                for n in old_graph.get("nodes", []):
                    previous[n["file_path"]] = FileRec.from_json({
                        "path": n["file_path"], "language": n.get("language"),
                        "hash": n.get("hash"), "symbols": n.get("symbols", []),
                        "imports": [],
                    })
            except (OSError, json.JSONDecodeError, KeyError):
                print("  [warn] 旧缓存不可读, 视为首次扫描", file=sys.stderr)

    delta = compare(files, previous) if previous else {
        "status_by_path": {p: "new" for p in files}, "old_path_by_path": {}, "stats": {"new": len(files)},
    }

    paths = set(files)
    stems: dict[str, list[str]] = {}
    for rel in files:
        stem = rel.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        stems.setdefault(stem, []).append(rel)
    edges = build_edges(files, paths, stems)

    status_map = delta["status_by_path"]
    nodes = []
    is_ai = False  # scanner 不定 AI 标记；由 manifest_builder 的三重判定规则决定
    for rel, rec in sorted(files.items()):
        st = status_map.get(rel, "new")
        meta = symbol_meta.get(rel, [])
        nodes.append({
            "id": rel,
            "name": rel.rsplit("/", 1)[-1].rsplit(".", 1)[0],
            "type": "module",
            "file_path": rel,
            "start_line": meta[0]["line"] if meta else 1,
            "is_ai_generated": is_ai,
            "status": "active" if st in ("new", "updated", "unchanged") else st,
            "hash": rec.hash or ("0" * 64),
            "language": rec.language,
            "symbols": meta,
            **({"old_path": delta["old_path_by_path"][rel]} if rel in delta["old_path_by_path"] else {}),
        })
    # deleted 历史存档：旧缓存中的节点保留为 status=deleted（dashboard 灰虚线 + 决策故事挂靠）
    for old_path, st in status_map.items():
        if st != "deleted" or old_path in files:
            continue
        orec = previous.get(old_path)
        if not orec:
            continue
        nodes.append({
            "id": old_path,
            "name": old_path.rsplit("/", 1)[-1].rsplit(".", 1)[0],
            "type": "module",
            "file_path": old_path,
            "start_line": 1,
            "is_ai_generated": is_ai,
            "status": "deleted",
            "hash": orec.hash or ("0" * 64),
            "language": orec.language,
            "symbols": [{"name": n, "line": 1} for n in sorted(orec.symbols)],
        })

    graph = {
        "schema_version": SCHEMA_VERSION,
        "manifest_type": MANIFEST_TYPE,
        "project_name": project_root.name,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repository": git_evidence(project_root),
        "stats": {
            "total_nodes": len(nodes),
            "ai_contributed_nodes": 0,
            "total_files": len(files_paths),
            "changed_nodes": sum(1 for rel, st in status_map.items() if st in ("new", "updated", "moved", "deleted")),
        },
        "nodes": nodes,
        "edges": edges,
        "soul_questions": [],
    }
    # 缓存写入（读+写同源，确保增量链路闭环）
    dest = out_path or (cache_dir / "fact_graph.json")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")
    write_snapshot(cache_dir / "snapshots", graph)
    return {"graph": graph, "delta": delta, "unknown": unknown, "out_path": dest}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Codebase Insight 确定性事实底座扫描器")
    ap.add_argument("--project", required=True, help="项目根目录路径")
    ap.add_argument("--cache-dir", default=None, help="缓存目录（默认 ~/.cache/project_viz/<root-hash>）")
    ap.add_argument("--no-cache", action="store_true", help="不读旧缓存（全部视为新节点）")
    ap.add_argument("--include-hidden", action="store_true", help="包含 . 开头隐藏文件/目录")
    ap.add_argument("--out", default=None, help="fact_graph.json 输出路径（默认缓存目录内）")
    ap.add_argument("--config", default=None, help="config.yaml 路径（忽略规则/缓存目录等）")
    args = ap.parse_args(argv)

    cfg = load_config(Path(args.config)) if args.config else {}
    project_root = Path(args.project).resolve()
    if not project_root.is_dir():
        print(f"error: 目录不存在: {project_root}", file=sys.stderr)
        return 2

    cfg_cache = (cfg.get("cache") or {}).get("dir")
    cache_dir = Path(args.cache_dir or cfg_cache or default_cache_dir(str(project_root)))
    cache_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.monotonic()
    extra_ignores = tuple(cfg.get("ignore_extra") or [])
    result = scan(project_root, cache_dir, args.no_cache, args.include_hidden,
                  out_path=Path(args.out) if args.out else None,
                  extra_ignores=extra_ignores)
    graph, delta, unknown = result["graph"], result["delta"], result["unknown"]

    elapsed = time.monotonic() - t0
    print(f"✔ 扫描完成: {project_root.name}  用时 {elapsed:.2f}s")
    print(f"  文件: {graph['stats']['total_files']}  节点: {graph['stats']['total_nodes']}"
          f"  边: {len(graph['edges'])}  未知语言文件级节点: {unknown}")
    print(f"  delta: {delta['stats']}")
    print(f"  输出: {result['out_path']}")
    print(f"  repository: {graph['repository']['url'] or '(无 git 远程)'} @ {graph['repository']['revision'][:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
