# -*- coding: utf-8 -*-
"""tests/test_manifest_builder.py — 数据缝合层单测（零依赖 unittest）

覆盖：全量合法输入 / 编造节点+无背书边+无背书 flow 过滤 / AI 非法 JSON 降级 /
增量 unchanged 旧语义原样保留 / 三重判定 / deliver 失败保留上一版。
"""
import json
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from manifest_builder import main  # noqa: E402


def mk_facts(root: Path) -> dict:
    """构造与 test_scanner_smoke 一致的 fact_graph 形态。"""
    files = {
        "app.py": "from services.auth import verify\ndef main(): return 1\n",
        "services/__init__.py": "",
        "services/auth.py": "def verify(t): return True\n",
        "services/kafka.py": "def publish(t, m): return True\n",
        "services/order/handler.py": "from services.auth import verify\nfrom services.kafka import publish\ndef handle_order(r): return r\n",
        "services/order/processor.py": "from ..order.handler import handle_order\ndef process(): return 1\n",
        "legacy/payment.py": "def pay(): return True\n",
    }
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    import scanner  # 复用真实扫描器（同一缓存目录内二次调用即增量场景）
    graph = scanner.scan(root, root / ".cache", no_cache=False, include_hidden=False)["graph"]
    return graph


def ai_output_for(fp: str, **kw) -> dict:
    """构造单节点 AI 语义输出。"""
    base = {
        "schema_version": 1,
        "manifest_type": "codebase-insight",
        "project_name": "demo",
        "nodes": [{"file_path": fp,
                   "tech_stack": "Python", "summary": "核心服务",
                   "design_reason": "事件驱动保证最终一致性，替代 2PC。",
                   "alternatives": ["曾考虑 RabbitMQ，运维成本高。"],
                   "known_risks": ["缺兜底重试 services/order/handler.py:5"],
                   "code_snippet": "def handle_order(r):\n    return r",
                   "story": [{"t": "AI 新写入", "d": "2026-08-31", "note": "事件驱动替代 2PC"}]}],
        "edges": [], "flows": [], "soul_questions": [],
    }
    base["nodes"][0].update(kw)
    return base


class TestManifestBuilder(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.facts = mk_facts(self.root)
        self.facts_path = self.root / "fact_graph.json"
        self.facts_path.write_text(json.dumps(self.facts, ensure_ascii=False), encoding="utf-8")
        self.out = self.root / "final_manifest.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, *extra, **flags):
        args = ["--fact-graph", str(self.facts_path), "--out", str(self.out),
                "--project", str(self.root), "--report", str(self.root / ".cache" / "report.json")]
        args += list(extra)
        for k, v in flags.items():
            args += [f"--{k.replace('_', '-')}", str(v)]
        return main(args)

    def test_full_first_run(self):
        ai = self.root / ".cache" / "ai.json"
        ai.write_text(json.dumps(ai_output_for("services/order/handler.py"), ensure_ascii=False), encoding="utf-8")
        rc = self._run("--ai-output", str(ai))
        self.assertEqual(rc, 0)
        m = json.loads(self.out.read_text(encoding="utf-8"))
        n = next(x for x in m["nodes"] if x["file_path"] == "services/order/handler.py")
        self.assertEqual(n["design_reason"], "事件驱动保证最终一致性，替代 2PC。")
        self.assertEqual(n["alternatives"], ["曾考虑 RabbitMQ，运维成本高。"])
        self.assertEqual(len(m["nodes"]), 7)
        self.assertEqual(m["stats"]["total_nodes"], 7)
        # 事实边保留 + 语义边与 flows 为空
        self.assertTrue(all(e["factual"] for e in m["edges"]))

    def test_filters_invented_node_edge_flow(self):
        ai = ai_output_for("services/order/handler.py")
        ai["nodes"].append({"file_path": "invented/ghost.py", "design_reason": "我编造的"})
        ai["edges"].append({"source": "invented/ghost.py", "target": "services/auth.py",
                            "relation": "动态调用"})
        ai["flows"].append({"id": "bad", "name": "坏流程", "entry": "services/auth.py",
                            "steps": [{"node": "services/auth.py", "order": 1},
                                      {"node": "invented/ghost.py", "order": 2}]})
        ai["flows"].append({"id": "nogap", "name": "无背书", "entry": "services/auth.py",
                            "steps": [{"node": "services/auth.py", "order": 1},
                                      {"node": "services/kafka.py", "order": 2}]})
        ai_path = self.root / ".cache" / "ai.json"
        ai_path.write_text(json.dumps(ai, ensure_ascii=False), encoding="utf-8")
        rc = self._run("--ai-output", str(ai_path))
        self.assertEqual(rc, 0)
        m = json.loads(self.out.read_text(encoding="utf-8"))
        paths = {n["file_path"] for n in m["nodes"]}
        self.assertNotIn("invented/ghost.py", paths, "编造节点应被过滤")
        self.assertFalse(any(e["source"] == "invented/ghost.py" for e in m["edges"]), "编造边应被过滤")
        self.assertEqual(m["flows"], [], "无背书的流程应被过滤")
        report = json.loads((self.root / ".cache" / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(len(report["dropped_nodes"]), 1)
        self.assertEqual(len(report["dropped_edges"]), 1)
        self.assertEqual(len(report["dropped_flows"]), 2)

    def test_degraded_on_invalid_ai_json(self):
        (self.root / ".cache" / "ai.json").write_text("not a json {{{", encoding="utf-8")
        rc = self._run("--ai-output", str(self.root / ".cache" / "ai.json"))
        self.assertEqual(rc, 0, "降级不应失败")
        m = json.loads(self.out.read_text(encoding="utf-8"))
        n = next(x for x in m["nodes"] if x["file_path"] == "services/auth.py")
        self.assertNotIn("design_reason", n, "降级版应无语义字段")
        report = json.loads((self.root / ".cache" / "report.json").read_text(encoding="utf-8"))
        self.assertTrue(report["degraded"])

    def test_incremental_keeps_unchanged_semantics(self):
        # 第一轮：AI 语义入库
        ai = self.root / ".cache" / "ai.json"
        ai.write_text(json.dumps(ai_output_for("services/order/handler.py"), ensure_ascii=False), encoding="utf-8")
        self.assertEqual(self._run("--ai-output", str(ai)), 0)
        old_manifest = json.loads(self.out.read_text(encoding="utf-8"))

        # 第二轮：修改 handler.py（updated），同时删除 legacy/payment.py
        (self.root / "services/order/handler.py").write_text(
            "from services.auth import verify\nfrom services.kafka import publish\n"
            "def handle_order(r):\n    return r\ndef retry(r):\n    return handle_order(r)\n",
            encoding="utf-8")
        (self.root / "legacy/payment.py").unlink()
        self.facts = self.root / "fact_graph.json"
        import scanner
        g2 = scanner.scan(self.root, self.root / ".cache", no_cache=False, include_hidden=False)["graph"]
        self.facts.write_text(json.dumps(g2, ensure_ascii=False), encoding="utf-8")

        ai2 = ai_output_for("services/order/handler.py", design_reason="新版本：增加重试兜底。",
                            story=[{"t": "AI 更新", "d": "2026-09-01", "note": "加 retry 兜底"}])
        ai2_path = self.root / ".cache" / "ai2.json"
        ai2_path.write_text(json.dumps(ai2, ensure_ascii=False), encoding="utf-8")
        rc = self._run("--ai-output", str(ai2_path), "--old-manifest", str(self.out),
                       "--ai-files", "services/order/handler.py")
        self.assertEqual(rc, 0)
        m2 = json.loads(self.out.read_text(encoding="utf-8"))
        by_path = {n["file_path"]: n for n in m2["nodes"]}

        # unchanged（auth.py）：语义保持原规则——第一轮没有它的语义，本轮不得凭空生成
        self.assertNotIn("design_reason", by_path["services/auth.py"])
        # updated（handler.py）：采纳新语义
        self.assertIn("重试兜底", by_path["services/order/handler.py"]["design_reason"])
        # deleted（legacy/payment.py）：保留历史存档
        self.assertEqual(by_path["legacy/payment.py"]["status"], "deleted")
        # 三重判定：updated 且用户确认+时间窗 → is_ai_generated
        self.assertTrue(by_path["services/order/handler.py"]["is_ai_generated"])
        ev = by_path["services/order/handler.py"]["ai_evidence"]
        self.assertTrue(any("用户确认" in e for e in ev), f"证据应含用户确认: {ev}")
        self.assertEqual(len(m2["nodes"]), 7, "增量后节点数应为 6 现存 + 1 deleted 存档")

    def test_deliver_preserves_previous_on_validation_failure(self):
        # 先交付一版合法产物
        self.assertEqual(self._run(), 0)
        good = self.out.read_bytes()
        # 构造非法 fact（hash 非法 → 校验失败）
        bad_facts = json.loads(self.facts_path.read_text(encoding="utf-8"))
        bad_facts["nodes"][0]["hash"] = "short"
        self.facts_path.write_text(json.dumps(bad_facts), encoding="utf-8")
        rc = self._run()
        self.assertEqual(rc, 1, "校验失败应退出非 0")
        self.assertEqual(self.out.read_bytes(), good, "失败必须保留上一版输出")
        report = json.loads((self.root / ".cache" / "report.json").read_text(encoding="utf-8"))
        self.assertFalse(report["delivered"])


if __name__ == "__main__":
    unittest.main()
