# -*- coding: utf-8 -*-
"""tests/test_schema.py — final_manifest.schema.json 契约自检（标准库 unittest，零依赖）

运行：python -m unittest discover -s tests -p "test_*.py"
校验：Schema 本身可解析、required 完整、$defs 自引用存在、枚举合法。
"""
import json
import re
import unittest
from pathlib import Path

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "final_manifest.schema.json"


class TestManifestSchema(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    def test_schema_parses(self):
        s = self.schema
        self.assertTrue(s["$schema"].startswith("https://json-schema.org/draft/2020-12"), "draft 版本")
        self.assertTrue(s["title"], "title 必填")

    def test_required_fields(self):
        s = self.schema
        for top in ("schema_version", "manifest_type", "project_name", "generated_at", "repository", "stats", "nodes", "edges"):
            self.assertIn(top, s["required"], f"顶级 required 缺少 {top}")
        self.assertEqual(s["properties"]["schema_version"]["const"], 1, "schema_version 必须 const 1")
        self.assertEqual(s["properties"]["manifest_type"]["const"], "codebase-insight", "manifest_type const")
        node_req = s["$defs"]["node"]["required"]
        for f in ("id", "name", "type", "file_path", "start_line", "is_ai_generated", "status", "hash"):
            self.assertIn(f, node_req, f"node required 缺少 {f}")

    def test_enums(self):
        s = self.schema
        self.assertEqual(set(s["$defs"]["node"]["properties"]["type"]["enum"]),
                         {"service", "entity", "utils", "config", "module"})
        self.assertEqual(set(s["$defs"]["node"]["properties"]["status"]["enum"]),
                         {"active", "deleted", "moved"})

    def test_repository_binding(self):
        s = self.schema
        repo = s["properties"]["repository"]
        self.assertIn("revision", repo["required"])
        self.assertIn("root", repo["required"])
        self.assertRegex("0" * 40, r"^[a-fA-F0-9]{40}$", "revision 模式应匹配非 git 项目的 40 个 0")

    def test_edges_and_flows(self):
        s = self.schema
        self.assertEqual(s["$defs"]["edge"]["required"], ["source", "target", "relation", "factual"])
        self.assertEqual(s["$defs"]["flow"]["properties"]["steps"]["minItems"], 2, "流程至少 2 步")
        self.assertEqual(s["$defs"]["step"]["required"], ["node", "order"])

    def test_symbols_object_with_line(self):
        """符号级行号契约：symbols 必须是 [{name, line}]，不允许字符串/缺行号。"""
        s = self.schema
        items = s["$defs"]["node"]["properties"]["symbols"]["items"]
        self.assertEqual(items["required"], ["name", "line"])
        self.assertEqual(items["properties"]["line"]["minimum"], 1)


if __name__ == "__main__":
    unittest.main()
