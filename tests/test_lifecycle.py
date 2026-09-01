# -*- coding: utf-8 -*-
"""tests/test_lifecycle.py — fingerprint/lifecycle 生命周期状态机单测（标准库 unittest）"""
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from fingerprint import file_fingerprint, hash_file, jaccard
from lifecycle import FileRec, compare, rotate_snapshots, write_snapshot


def rec(path, h, symbols=None, imports=None):
    return FileRec(path, "python" if path.endswith(".py") else "go",
                   h, set(symbols or []), list(imports or []))


class TestFingerprint(unittest.TestCase):
    def test_hash_changes_with_content(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.py"
            p.write_text("x = 1", encoding="utf-8")
            h1 = hash_file(p)
            p.write_text("x = 2", encoding="utf-8")
            h2 = hash_file(p)
            self.assertIsNotNone(h1)
            self.assertNotEqual(h1, h2)
            self.assertEqual(hash_file(Path(d) / "missing.py"), None)

    def test_normalize_and_similarity(self):
        f1 = file_fingerprint("services/order_service.py", ["create", "cancel"])
        f2 = file_fingerprint("services.OrderService.go", ["create", "cancel"])
        f3 = file_fingerprint("utils/misc.py", ["random_thing"])
        self.assertGreaterEqual(jaccard(f1, f2), 0.7, "同骨架不同命名应视为相似")
        self.assertLess(jaccard(f1, f3), 0.7, "完全不同应低于阈值")


class TestLifecycleCompare(unittest.TestCase):
    def setUp(self):
        # 旧缓存（快照 1）
        self.prev = {
            "a.py": rec("a.py", "H1", ["create", "cancel"]),
            "b.py": rec("b.py", "H2", ["handle"]),
            "old/legacy.py": rec("old/legacy.py", "H3", ["pay"]),
            "services/pay/engine.py": rec("services/pay/engine.py", "H4", ["billing", "aggregate"]),
        }
        # 当前扫描（快照 2）
        self.cur = {
            "a.py": rec("a.py", "H1", ["create", "cancel"]),                         # unchanged
            "b.py": rec("b.py", "H22", ["handle", "retry"]),                         # updated
            "services/billing/engine.py": rec("services/billing/engine.py", "H4",
                                              ["billing", "aggregate"]),             # moved（hash 相同）
            "new_module.py": rec("new_module.py", "H5", ["fresh"]),                  # new
        }

    def test_compare_statuses(self):
        r = compare(self.cur, self.prev)
        self.assertEqual(r["status_by_path"]["a.py"], "unchanged")
        self.assertEqual(r["status_by_path"]["b.py"], "updated")
        self.assertEqual(r["status_by_path"]["services/billing/engine.py"], "moved")
        self.assertEqual(r["status_by_path"]["new_module.py"], "new")
        self.assertEqual(r["status_by_path"]["old/legacy.py"], "deleted")
        self.assertEqual(r["old_path_by_path"]["services/billing/engine.py"], "services/pay/engine.py")
        self.assertEqual(r["stats"], {"new": 1, "deleted": 1, "moved": 1, "updated": 1, "unchanged": 1})

    def test_moved_by_symbol_similarity(self):
        # hash 不同但符号骨架相近（重命名 + 小改）→ 仍判定 moved
        prev = {"old_name.py": rec("old_name.py", "A", ["alpha", "beta", "gamma"])}
        cur = {"new_name.py": rec("new_name.py", "B", ["alpha", "beta"])}
        r = compare(cur, prev)
        self.assertEqual(r["status_by_path"]["new_name.py"], "moved")
        self.assertEqual(r["old_path_by_path"]["new_name.py"], "old_name.py")


class TestSnapshotRotation(unittest.TestCase):
    def test_rotate_keeps_recent(self):
        with tempfile.TemporaryDirectory() as d:
            snap = Path(d) / "snapshots"
            snap.mkdir()
            for i in range(35):
                (snap / f"snap_{i:02d}.json").write_text("{}", encoding="utf-8")
                (snap / f"snap_{i:02d}.json").touch()  # 递增 mtime
            kept = rotate_snapshots(snap, keep=30)
            self.assertEqual(len(kept), 30)

    def test_write_snapshot_and_rotate(self):
        with tempfile.TemporaryDirectory() as d:
            snap = Path(d) / "snapshots"
            payload = {"repository": {"root": "G:/demo"}, "nodes": []}
            path = write_snapshot(snap, payload)
            self.assertIsNotNone(path)
            self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
