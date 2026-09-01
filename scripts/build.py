# -*- coding: utf-8 -*-
"""build.py — 最终交付构建（借鉴 archify deliver：原子替换 + 失败保留旧产物）

功能：
  1. 将 final_manifest.json 注入模板（替换 demo 的 window.__MANIFEST__ 示例数据）
  2. 注入 window.__PROJECT_ROOT__（IDE 跳转的真实绝对路径）
  3. --echarts <path>：内联 ECharts 压缩版（内网/离线交付，对应 config.bundle.inline_echarts）
  4. 原子导出（tmp + os.replace），任何失败保留上一版输出

用法：
  python scripts/build.py --manifest <final_manifest.json> \
      [--template demo/dashboard_proto.html] --out <dashboard.html> \
      [--echarts echarts.min.js] [--report build-report.json]
"""
import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

MANIFEST_RE = re.compile(r"window\.__MANIFEST__\s*=\s*\{.*?\};", re.DOTALL)
PROJECT_ROOT_PLACEHOLDER = "window.__PROJECT_ROOT__"
ECHARTS_CDN_RE = re.compile(
    r"<script src=\"https://cdn\.jsdelivr\.net/npm/echarts[^>]*></script>", re.DOTALL
)


def inject_manifest(template: str, manifest: dict, project_root: str) -> str:
    payload = json.dumps(manifest, ensure_ascii=False, indent=1)
    new_block = "window.__MANIFEST__ = " + payload + ";"
    if not MANIFEST_RE.search(template):
        raise ValueError("模板中未找到 window.__MANIFEST__ 占位")
    html = MANIFEST_RE.sub(lambda _: new_block, template, count=1)
    root_block = f"window.__PROJECT_ROOT__ = {json.dumps(project_root)};"
    if re.search(r"window\.__PROJECT_ROOT__\s*=\s*", html):
        html = re.sub(r"window\.__PROJECT_ROOT__\s*=\s*[\s\S]*?;", root_block, html, count=1)
    else:
        html = html.replace("window.__MANIFEST__ =", root_block + "\n" + "window.__MANIFEST__ =", 1)
    return html


def inline_echarts(template: str, echarts_path: Path) -> tuple[str, str]:
    """ECharts CDN script 标签 → 内联脚本。返回 (html, digest)。"""
    if not echarts_path.is_file():
        raise FileNotFoundError(f"ECharts 文件不存在: {echarts_path}")
    code = echarts_path.read_text(encoding="utf-8")
    inline = f"<script>/* echarts inlined by build.py --echarts */{code}</script>"
    if not ECHARTS_CDN_RE.search(template):
        raise ValueError("模板中未找到 ECharts CDN script 标签")
    html = ECHARTS_CDN_RE.sub(inline, template, count=1)
    import hashlib
    return html, hashlib.sha256(code.encode("utf-8")).hexdigest()[:16]


def collect_snapshots(snap_dir: Path, manifest: dict) -> list[dict]:
    """扫描快照目录，计算每份快照相对【当前 manifest】的 diff（路径级交集比较）。

    只注入 delta 元数据（路径列表 + 统计），不含全量节点 → 控制单文件体积。
    """
    cur = {n["file_path"]: n for n in manifest.get("nodes", [])}
    out = []
    if not snap_dir.is_dir():
        return out
    for p in sorted(snap_dir.glob("*.json"), key=lambda q: q.stat().st_mtime):
        try:
            snap = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        old = {n["file_path"]: n for n in snap.get("nodes", [])}
        added, removed, moved, updated = [], [], [], []
        for fp, n in cur.items():
            if fp not in old:
                added.append(fp)
            elif n.get("old_path") and n["old_path"] in old:
                moved.append(fp)
            elif old[fp].get("hash") != n.get("hash"):
                updated.append(fp)
        for fp in old:
            if fp not in cur:
                removed.append(fp)
        out.append({
            "id": p.stem,
            "at": snap.get("generated_at", ""),
            "stats": {"added": len(added), "removed": len(removed),
                      "moved": len(moved), "updated": len(updated)},
            "added": added, "removed": removed, "moved": moved, "updated": updated,
        })
    return out


def inject_snapshots(template: str, snapshots: list[dict]) -> str:
    """注入 window.__SNAPSHOTS__（无快照时不注入）。"""
    if not snapshots:
        return template
    block = "window.__SNAPSHOTS__ = " + json.dumps(snapshots, ensure_ascii=False, indent=1) + ";"
    if re.search(r"window\.__SNAPSHOTS__\s*=\s*", template):
        return re.sub(r"window\.__SNAPSHOTS__\s*=\s*[\s\S]*?;", block, template, count=1)
    return template.replace("window.__MANIFEST__ =", block + "\n" + "window.__MANIFEST__ =", 1)


def deliver(out_path: Path, payload: str, report: dict) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(out_path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, out_path)
        report["delivered"] = True
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Codebase Insight 交付构建")
    ap.add_argument("--manifest", required=True, help="final_manifest.json")
    ap.add_argument("--template", default=None, help="dashboard 模板（默认 demo/dashboard_proto.html）")
    ap.add_argument("--out", required=True, help="输出 dashboard HTML 路径")
    ap.add_argument("--echarts", default=None, help="本地 echarts.min.js（--bundle 等价，内联）")
    ap.add_argument("--snapshots-dir", default=None, help="快照目录（inject window.__SNAPSHOTS__ 差异元数据）")
    ap.add_argument("--report", default=None, help="构建报告 JSON 输出路径")
    args = ap.parse_args(argv)

    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    report = {"ok": False, "delivered": False, "errors": [], "warnings": [],
              "echarts_inlined": False, "echarts_digest": None,
              "manifest_nodes": 0, "manifest_edges": 0, "out_bytes": 0,
              "snapshots": 0}

    script_dir = Path(__file__).resolve().parent
    template_path = Path(args.template) if args.template else script_dir.parent / "demo" / "dashboard_proto.html"

    try:
        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        template = template_path.read_text(encoding="utf-8")
        root = manifest.get("repository", {}).get("root") or ""
        html = inject_manifest(template, manifest, root)
        if args.snapshots_dir:
            snaps = collect_snapshots(Path(args.snapshots_dir), manifest)
            html = inject_snapshots(html, snaps)
            report["snapshots"] = len(snaps)
            if not snaps:
                report["warnings"].append(f"快照目录无有效快照: {args.snapshots_dir}")
        if args.echarts:
            html, digest = inline_echarts(html, Path(args.echarts))
            report["echarts_inlined"] = True
            report["echarts_digest"] = digest
        out_path = Path(args.out).resolve()
        deliver(out_path, html, report)
        report["manifest_nodes"] = len(manifest.get("nodes", []))
        report["manifest_edges"] = len(manifest.get("edges", []))
        report["out_bytes"] = Path(out_path).stat().st_size
        report["ok"] = True
        print(f"✔ 构建完成: {out_path}  ({report['out_bytes'] / 1024:.0f} KB)"
              f"  节点 {report['manifest_nodes']}  边 {report['manifest_edges']}"
              + (f"  快照 {report['snapshots']}" if report["snapshots"] else "")
              + ("  [ECharts 已内联]" if args.echarts else ""))
    except Exception as e:  # noqa: BLE001 — 构建失败保留旧产物
        report["errors"].append(str(e))
        print(f"✘ 构建失败，保留上一版输出: {e}", file=sys.stderr)
        if args.report:
            Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return 1

    if args.report:
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
