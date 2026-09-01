# -*- coding: utf-8 -*-
"""tests/test_agent_evidence.py — AI 会话日志证据解析器单测（三宿主 fixture）"""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from agent_evidence import AgentEvidence  # noqa: E402


def iso(ts_offset_min: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=ts_offset_min)).isoformat()


class TestAgentEvidence(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "project"
        self.root.mkdir()
        (self.root / "src").mkdir()
        (self.root / "src" / "a.py").write_text("x=1\n", encoding="utf-8")
        self.ev = AgentEvidence(self.root, window_min=300000)  # 大窗口便于 fixture

    def tearDown(self):
        self._tmp.cleanup()

    def test_reasonix_edit_file_protocol(self):
        """reasonix：tool content 'edited <path>' 头解析 + 项目内外路径过滤。"""
        archive = self.root.parent / "archive"
        archive.mkdir()
        log = archive / "20260807-120000.jsonl"
        abs_in = str(self.root / "src" / "a.py")
        abs_out = str(self.root.parent / "elsewhere.py")
        log.write_text(
            json.dumps({"role": "tool", "content": f"edited {abs_in}\nActual replacement..."}, ensure_ascii=False)
            + "\n" +
            json.dumps({"role": "tool", "content": f"edited {abs_out}\nzzz"}, ensure_ascii=False)
            + "\n" +
            json.dumps({"role": "tool", "content": "def foo():\n    pass\n"}, ensure_ascii=False)
            + "\n", encoding="utf-8")
        ev = AgentEvidence(self.root, 300000).scan_reasonix(archive)
        self.assertEqual(ev.files, {"src/a.py": "reasonix"})
        self.assertGreaterEqual(ev.missed_write_file, 1, "纯内容 write_file 应计为 missed")

    def test_reasonix_archive_out_of_window(self):
        """文件级时间窗：归档日期超窗的日志整体跳过。"""
        import os
        archive = self.root.parent / "archive"
        archive.mkdir()
        log = archive / "20200101-120000.jsonl"  # 文件名日期在 300000 分钟外
        log.write_text(json.dumps({"role": "tool", "content": "edited junk\nx"}, ensure_ascii=False), encoding="utf-8")
        # 覆盖 mtime 为现在（文件名日期优先 → min 取旧）
        ev = AgentEvidence(self.root, 300000).scan_reasonix(archive)
        self.assertEqual(ev.files, {})

    def test_codex_apply_patch(self):
        """codex：function_call apply_patch arguments 中 Begin Patch 块。"""
        sessions = self.root.parent / ".codex" / "sessions"
        (sessions / "202609").mkdir(parents=True)
        log = sessions / "202609" / "abc.jsonl"
        payload = {
            "type": "function_call",
            "createdAt": iso(5),
            "payload": {"name": "apply_patch", "arguments":
                        "*** Begin Patch\n*** Update File: src/a.py\n@@\n-x=1\n+x=2\n*** End Patch\n"},
        }
        logs = [payload]
        log.write_text("\n".join(json.dumps(x) for x in logs) + "\n", encoding="utf-8")
        ev = AgentEvidence(self.root, 300000).scan_codex(sessions)
        self.assertIn("src/a.py", ev.files, "apply_patch 应提取 Update File 路径")
        self.assertEqual(ev.files["src/a.py"], "codex")

    def test_claude_code_tool_use(self):
        """claude-code：assistant tool_use Write/Edit 的 input.file_path。"""
        projects = self.root.parent / ".claude" / "projects"
        (projects / "prj").mkdir(parents=True)
        log = projects / "prj" / "session.jsonl"
        ev_line = {
            "type": "assistant", "timestamp": iso(3),
            "message": {"content": [
                {"type": "tool_use", "name": "Write",
                 "input": {"file_path": str(self.root / "src" / "a.py"), "content": "x=2"}},
                {"type": "tool_use", "name": "Edit", "input": {"file_path": "src/a.py"}},
                {"type": "tool_use", "name": "Read", "input": {"file_path": "src/a.py"}},
            ]},
        }
        log.write_text(json.dumps(ev_line) + "\n", encoding="utf-8")
        ev = AgentEvidence(self.root, 300000).scan_claude_code(projects)
        self.assertEqual(ev.files.get("src/a.py"), "claude-code", "Write/Edit 命中，Read 忽略")

    def test_window_filter_old_events(self):
        """codex 行级时间窗：超窗事件不记录。"""
        sessions = self.root.parent / ".codex" / "sessions"
        (sessions / "d").mkdir(parents=True)
        log = sessions / "d" / "old.jsonl"
        log.write_text(json.dumps({
            "type": "function_call", "createdAt": iso(10_000_000),
            "payload": {"name": "apply_patch", "arguments": "*** Begin Patch\n*** Add File: src/a.py\n*** End Patch\n"},
        }) + "\n", encoding="utf-8")
        ev = AgentEvidence(self.root, 30).scan_codex(sessions)
        self.assertEqual(ev.files, {}, "超窗事件应被过滤（宁可漏不误标）")


if __name__ == "__main__":
    unittest.main()
