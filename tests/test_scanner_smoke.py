# -*- coding: utf-8 -*-
"""tests/test_scanner_smoke.py — scanner 端到端冒烟（v1 → v2 全生命周期场景）

场景：首次扫描（全量）→ 修改代码（删除/纯移动/内容修改/新增）→ 二次扫描（增量 delta）。
"""
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from scanner import scan  # noqa: E402

V1 = {
    "app.py": "from services.order.handler import handle_order\nfrom services.auth import verify\ndef main():\n    return handle_order(1)\n",
    "services/__init__.py": "",
    "services/order/handler.py": (
        "from services.auth import verify\nfrom services.kafka import publish\n"
        "def handle_order(req):\n    return {'ok': True}\n"
    ),
    "services/order/processor.py": (
        "from ..order.handler import handle_order\n"
        "def process():\n    return handle_order(2)\n"
    ),
    "services/auth.py": "def verify(token):\n    return True\n",
    "services/kafka.py": "def publish(topic, msg):\n    return True\n",
    "utils/config.py": "def load_cfg():\n    return {}\n",
    "legacy/payment.py": "def pay():\n    return True\n",
    "services/pay/engine.py": "def billing():\n    return 0\n",
}

V2_CHANGES = {
    "services/order/handler.py": (
        "from services.auth import verify\nfrom services.kafka import publish\n"
        "def handle_order(req):\n    return {'ok': True}\n"
        "def retry(req):\n    return handle_order(req)\n"
    ),
    "services/report.py": "def report():\n    return 'ok'\n",
}
V2_DELETE = ["legacy/payment.py"]
V2_MOVE = {"services/pay/engine.py": "services/billing/engine.py"}  # 内容不变：纯移动


def write_tree(root: Path, tree: dict):
    for rel, content in tree.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


class TestScannerSmoke(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cache = self.root / ".." / "_cache" / self.root.name
        write_tree(self.root, V1)

    def tearDown(self):
        self._tmp.cleanup()

    def _scan(self):
        return scan(self.root, self.cache, no_cache=False, include_hidden=False)["graph"]

    def test_first_scan_full(self):
        g = self._scan()
        self.assertEqual(g["schema_version"], 1)
        self.assertEqual(g["manifest_type"], "codebase-insight")
        self.assertEqual(g["repository"]["revision"], "0" * 40, "非 git 项目应约定全 0 revision")
        paths = {n["file_path"] for n in g["nodes"]}
        self.assertEqual(len(g["nodes"]), 9, "v1 全量节点")
        # 断言边：绝对导入 + 相对导入都要解析成功
        edges = {(e["source"], e["target"]) for e in g["edges"]}
        self.assertIn(("services/order/handler.py", "services/auth.py"), edges)
        self.assertIn(("services/order/handler.py", "services/kafka.py"), edges)
        self.assertIn(("app.py", "services/order/handler.py"), edges)
        self.assertIn(("services/order/processor.py", "services/order/handler.py"),
                      edges, "相对导入 ..order.handler 应解析")
        self.assertTrue(all(e["factual"] for e in g["edges"]))
        # 符号级行号：首次扫描应带 {name, line}，start_line = 首个符号行
        handler = next(n for n in g["nodes"] if n["file_path"] == "services/order/handler.py")
        self.assertEqual(handler["start_line"], 3, "V1 handler.py 首个符号 handle_order 在 L3")
        syms = {(s["name"], s["line"]) for s in handler["symbols"]}
        self.assertIn(("handle_order", 3), syms, "handle_order 应带行号 3")

    def test_second_scan_delta(self):
        g1 = self._scan()
        # 应用 v2 变更：修改 / 删除 / 移动 / 新增
        write_tree(self.root, V2_CHANGES)
        for rel in V2_DELETE:
            (self.root / rel).unlink()
        for old, new in V2_MOVE.items():
            (self.root / new).parent.mkdir(parents=True, exist_ok=True)
            (self.root / old).rename(self.root / new)
        g2 = self._scan()

        by_path = {n["file_path"]: n for n in g2["nodes"]}
        self.assertEqual(by_path["services/order/handler.py"]["status"], "active")
        self.assertEqual(by_path["services/report.py"]["status"], "active")
        moved = by_path.get("services/billing/engine.py")
        self.assertIsNotNone(moved, "移动后的节点应存在")
        self.assertEqual(moved["status"], "moved", "纯移动（hash 相同）应判定 moved")
        self.assertEqual(moved["old_path"], "services/pay/engine.py", "old_path 应指向旧路径")
        deleted = by_path.get("legacy/payment.py")
        self.assertIsNotNone(deleted, "deleted 节点应保留为历史存档")
        self.assertEqual(deleted["status"], "deleted")
        # unchanged 校验：auth.py 未变
        self.assertEqual(by_path["services/auth.py"]["status"], "active")
        self.assertNotIn("old_path", by_path["services/auth.py"], "unchanged 不应带 old_path")
        # delta 统计
        self.assertEqual(g2["stats"]["changed_nodes"], 4, "new(1)+moved(1)+updated(1)+deleted(1)")
        # v1 9 节点 → v2：8 现存(1 删除 1 新增 1 移动后)+1 deleted 历史存档 = 10
        self.assertEqual(g2["stats"]["total_nodes"], 10)

    def test_root_artifact_is_ignored(self):
        """回归：根目录的产物/隐藏文件不会被扫进来（./ 前缀 bug）。"""
        (self.root / "fact_graph.json").write_text("{}", encoding="utf-8")
        (self.root / "final_manifest.json").write_text("{}", encoding="utf-8")
        (self.root / ".env").write_text("SECRET=1", encoding="utf-8")
        g = self._scan()
        paths = {n["file_path"] for n in g["nodes"]}
        self.assertNotIn("fact_graph.json", paths)
        self.assertNotIn("final_manifest.json", paths)
        self.assertNotIn(".env", paths)
        self.assertEqual(g["stats"]["total_nodes"], 9)

    def test_src_layout_import_resolution(self):
        """回归：src 布局项目（import pkg.x 但文件在 src/pkg/x.py）+ 同名 stem 不误连。"""
        import tempfile
        import scanner
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "src" / "pkg").mkdir(parents=True)
            (root / "src" / "pkg" / "utils.py").write_text("def helper(): pass\n", encoding="utf-8")
            (root / "src" / "pkg" / "mod.py").write_text("import pkg.utils\ndef run(): pass\n", encoding="utf-8")
            (root / "tools").mkdir()
            (root / "tools" / "utils.py").write_text("OTHER = 1\n", encoding="utf-8")
            g = scanner.scan(root, root / ".cache", no_cache=False, include_hidden=False)["graph"]
            edges = {(e["source"], e["target"]) for e in g["edges"]}
            self.assertIn(("src/pkg/mod.py", "src/pkg/utils.py"), edges,
                          "import pkg.utils 应连到 src/pkg/utils.py")
            self.assertFalse(any(t.endswith("tools/utils.py") for _, t in edges),
                             "同名 stem 不得误连 tools/utils.py")


class TestScannerCLI(unittest.TestCase):
    """CLI 冒烟：subprocess 完整链路（exit 0 + 增量第二跑）。"""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        write_tree(self.root, V1)

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, cache):
        import subprocess
        return subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parent.parent / "scripts" / "scanner.py"),
             "--project", str(self.root), "--cache-dir", str(cache)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )

    def test_cli_runs_and_second_run_is_stable(self):
        cache = self.root.parent / "_cli_cache"
        r1 = self._run(cache)
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertIn("扫描完成", r1.stdout)
        r2 = self._run(cache)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertIn("unchanged", r2.stdout, "第二跑应命中缓存增量")

        moved = Path(self.root) / "services"
        self.assertTrue((cache / "fact_graph.json").exists())


class TestConfig(unittest.TestCase):
    """config.yaml 加载：注释剥离 + ignore_extra 生效。"""

    def test_load_config_strips_comments_and_ignores(self):
        import tempfile
        from scanner import load_config, scan
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "a.py").write_text("def a(): pass\n", encoding="utf-8")
            (root / "docs").mkdir()
            (root / "docs" / "note.md").write_text("# hi\n", encoding="utf-8")
            cfg_path = root / "config.yaml"
            cfg_path.write_text(
                '{ "ignore_extra": ["docs/"], "cache": {"dir": null} }  # 行内注释\n',
                encoding="utf-8")
            cfg = load_config(cfg_path)
            self.assertEqual(cfg["ignore_extra"], ["docs/"])
            g = scan(root, root / ".cache", no_cache=False, include_hidden=False,
                     extra_ignores=tuple(cfg.get("ignore_extra") or []))["graph"]
            paths = {n["file_path"] for n in g["nodes"]}
            self.assertIn("a.py", paths)
            self.assertNotIn("docs/note.md", paths, "ignore_extra 应排除 docs/ 目录")


if __name__ == "__main__":
    unittest.main()
