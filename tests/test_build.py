# -*- coding: utf-8 -*-
"""tests/test_build.py — build.py 注入/内联/失败保旧 单测"""
import json
import re
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from build import main  # noqa: E402


def sample_manifest(root: str) -> dict:
    return {
        "schema_version": 1,
        "manifest_type": "codebase-insight",
        "project_name": "demo",
        "generated_at": "2026-08-31T12:00:00Z",
        "repository": {"url": "", "revision": "0" * 40, "root": root},
        "stats": {"total_nodes": 1, "ai_contributed_nodes": 1,
                  "total_files": 1, "changed_nodes": 1},
        "nodes": [{"id": "app.py", "name": "app", "type": "module",
                   "file_path": "app.py", "start_line": 1, "is_ai_generated": True,
                   "status": "active", "hash": "a" * 64, "language": "python",
                   "symbols": ["main"], "design_reason": "示例语义"}],
        "edges": [],
        "flows": [],
        "soul_questions": ["示例问题？"],
    }


class TestBuild(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.manifest = self.root / "final_manifest.json"
        self.manifest.write_text(json.dumps(sample_manifest(str(self.root))), encoding="utf-8")
        self.out = self.root / "dashboard.html"

    def tearDown(self):
        self._tmp.cleanup()

    def test_inject_manifest_and_root(self):
        rc = main(["--manifest", str(self.manifest), "--out", str(self.out)])
        self.assertEqual(rc, 0)
        html = self.out.read_text(encoding="utf-8")
        self.assertIn('"示例语义"', html, "真实 manifest 语义应注入")
        self.assertIn("示例问题？", html)
        self.assertNotIn("电商平台", html, "demo 示例数据应被替换")
        self.assertIn(f'__PROJECT_ROOT__ = "{json.dumps(str(self.root))[1:-1]}"', html,
                      "项目根应注入 IDE 跳转（注意 Windows 路径反斜杠转义）")
        self.assertIn("cdn.jsdelivr.net/npm/echarts", html, "默认保持 CDN 加载")

    def test_inline_echarts(self):
        echarts = self.root / "echarts.min.js"
        echarts.write_text("window.__FAKE_ECHARTS__=true;", encoding="utf-8")
        rc = main(["--manifest", str(self.manifest), "--out", str(self.out),
                   "--echarts", str(echarts)])
        self.assertEqual(rc, 0)
        html = self.out.read_text(encoding="utf-8")
        self.assertIn("__FAKE_ECHARTS__", html, "ECharts 应内联")
        self.assertNotIn("cdn.jsdelivr.net/npm/echarts", html, "CDN 标签应被替换")

    def test_failure_preserves_previous(self):
        self.assertEqual(main(["--manifest", str(self.manifest), "--out", str(self.out)]), 0)
        good = self.out.read_bytes()
        bad = self.root / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        rc = main(["--manifest", str(bad), "--out", str(self.out)])
        self.assertEqual(rc, 1)
        self.assertEqual(self.out.read_bytes(), good, "失败必须保留上一版产物")

    def test_snapshots_injected(self):
        """快照注入：diff 元数据（relative 当前 manifest）+ 空快照目录不阻断。"""
        snap = self.root / "snapshots"
        snap.mkdir()
        # 构造一份"过去"的快照：没有 app.py（当前有）→ added: ["app.py"]
        (snap / "20260901_120000_deadbeef.json").write_text(json.dumps({
            "schema_version": 1, "manifest_type": "codebase-insight",
            "generated_at": "2026-09-01T12:00:00Z",
            "repository": {"url": "", "revision": "0" * 40, "root": str(self.root)},
            "stats": {"total_nodes": 0, "ai_contributed_nodes": 0},
            "nodes": [], "edges": [],
        }), encoding="utf-8")
        rc = main(["--manifest", str(self.manifest), "--out", str(self.out),
                   "--snapshots-dir", str(snap)])
        self.assertEqual(rc, 0)
        html = self.out.read_text(encoding="utf-8")
        self.assertIsNotNone(re.search(r"window\.__SNAPSHOTS__\s*=\s*\[", html),
                             "有快照时应注入快照赋值块")
        self.assertIn('"added": [', html, "diff 应含 added 数组")
        self.assertIn("deadbeef", html)
        # 空目录 → 不注入也不失败
        empty = self.root / "empty_snaps"
        empty.mkdir()
        rc2 = main(["--manifest", str(self.manifest), "--out", str(self.out),
                    "--snapshots-dir", str(empty)])
        self.assertEqual(rc2, 0, "空快照目录不应失败")
        html2 = self.out.read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"window\.__SNAPSHOTS__\s*=\s*\[", html2),
                          "无快照时不应注入赋值块（模板中的读取引用不算）")


if __name__ == "__main__":
    unittest.main()
