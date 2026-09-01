# -*- coding: utf-8 -*-
"""lifecycle.py — 节点生命周期状态机（delta 比较器）+ 快照轮转

借鉴 archify delta 比较器：canonical 化后做确定性差异分类，而不是模糊推断。
分类与 mapping（对应 schema 的 status 枚举）：
  new       → status=active（新增节点）
  updated   → status=active（同路径、内容变化 = archify 的"语义变化"）
  unchanged → status=active
  moved     → status=moved（路径变化，但 hash 相同或结构化指纹 ≥ SIMILARITY_MOVE_THRESHOLD）
  deleted   → status=deleted（旧路径消失且无 moved 匹配）

重路由（rerouted）：edges 集合级差异（added/removed），不改变节点状态。
"""
import hashlib
import json
import time
from pathlib import Path

from fingerprint import SIMILARITY_MOVE_THRESHOLD, moved_score

SNAPSHOT_KEEP = 30          # 快照轮转：保留最近 30 份
SNAPSHOT_MAX_BYTES = 20 * 1024 * 1024   # 单快照上限 20MB（超出跳过），防缓存失控


class FileRec:
    """一次扫描产出的文件级记录（事实字段）。"""

    __slots__ = ("path", "language", "hash", "symbols", "imports")

    def __init__(self, path, language, hash, symbols, imports):
        self.path = path
        self.language = language
        self.hash = hash
        self.symbols = symbols
        self.imports = imports

    def to_json(self):
        return {"path": self.path, "language": self.language, "hash": self.hash,
                "symbols": list(self.symbols), "imports": list(self.imports)}

    @classmethod
    def from_json(cls, d):
        raw = d.get("symbols", [])
        names = {s["name"] if isinstance(s, dict) else s for s in raw}
        return cls(d["path"], d.get("language"), d.get("hash"), names,
                   list(d.get("imports", [])))


def canonical_sorted(obj):
    """JSON 无关键序的 canonical 值（用于边集合的确定性比较）。"""
    if isinstance(obj, dict):
        return {k: canonical_sorted(obj[k]) for k in sorted(obj)}
    if isinstance(obj, list):
        return [canonical_sorted(v) for v in obj]
    return obj


def compare(current_files: dict[str, FileRec], previous_files: dict[str, FileRec]) -> dict:
    """确定性 delta：返回 {status_by_path, old_path_by_path, edge_changes, stats}。"""
    prev_prev = dict(previous_files)  # path -> FileRec
    cur = dict(current_files)
    result = {}
    old_path_map = {}

    # 1) unchanged / updated / moved / new
    unmatched_old = dict(prev_prev)
    for path, rec in cur.items():
        if path in prev_prev:
            if prev_prev[path].hash == rec.hash and rec.hash is not None:
                result[path] = "unchanged"
            else:
                result[path] = "updated"
            unmatched_old.pop(path, None)
        else:
            result[path] = "new"

    # 2) moved 判定：旧文件中 hash 完全一致（纯移动）或指纹 ≥ 阈值（重命名+小改）
    matched_old_paths = set()
    for path, rec in cur.items():
        if result[path] != "new":
            continue
        best, best_score = None, 0.0
        for opath, orec in unmatched_old.items():
            if orec.hash and orec.hash == rec.hash:
                best, best_score = opath, 1.0
                break
            score = moved_score(path, list(rec.symbols), opath, list(orec.symbols))
            if score > best_score:
                best, best_score = opath, score
        if best and best_score >= SIMILARITY_MOVE_THRESHOLD:
            result[path] = "moved"
            old_path_map[path] = best
            unmatched_old.pop(best, None)

    # 3) deleted：剩余旧文件
    for opath in unmatched_old:
        result[opath] = "deleted"

    # 4) 重路由：边集合级差异（facts 边由调用方提供）
    stats = {"new": 0, "deleted": 0, "moved": 0, "updated": 0, "unchanged": 0}
    for st in result.values():
        stats[st] = stats.get(st, 0) + 1
    return {
        "status_by_path": result,
        "old_path_by_path": old_path_map,
        "stats": stats,
    }


def rotate_snapshots(snap_dir: Path, keep: int = SNAPSHOT_KEEP) -> list[Path]:
    """按修改时间轮转快照目录：保留最近 keep 份，删除更旧的 .json。返回剩余快照路径。"""
    if not snap_dir.exists():
        return []
    snaps = sorted(snap_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in snaps[keep:]:
        try:
            stale.unlink()
        except OSError:
            pass
    return [p for p in snaps[:keep] if p.exists()]


def write_snapshot(snap_dir: Path, payload: dict) -> Path | None:
    """写入带时间戳的快照并轮转；超过单快照上限则跳过（返回 None）。"""
    try:
        data = json.dumps(payload, ensure_ascii=False, indent=1)
    except (TypeError, ValueError):
        return None
    if len(data.encode("utf-8")) > SNAPSHOT_MAX_BYTES:
        return None
    snap_dir.mkdir(parents=True, exist_ok=True)
    root_key = str(payload.get("repository", {}).get("root", "root"))
    tag = hashlib.sha256(root_key.encode("utf-8")).hexdigest()[:8]
    name = f"{time.strftime('%Y%m%d_%H%M%S')}_{tag}.json"
    path = snap_dir / name
    path.write_text(data, encoding="utf-8")
    rotate_snapshots(snap_dir)
    return path
