# -*- coding: utf-8 -*-
"""fingerprint.py — 确定性指纹与相似度（生命周期状态机的判据）

设计原则（V2.0 + archify delta 借鉴）：
- hash_file()：内容 SHA-256，任何内容变化必然改变哈希 → "语义变化"判据。
- file_fingerprint()：结构化指纹 = 归一化符号集合 ∪ 归一化文件名。
  与纯文本相似度不同：重命名/大改结构只要符号骨架变化不大，指纹依然相近 →
  支持"moved（重命名/移动）"判定，且不会因整文件重写判定失败。
- 判定阈值统一集中，便于 golden 测试调参。
"""
import hashlib
import re
from pathlib import Path

# 内容指纹相似度阈值：≥ 此值且路径变化 → 判定为 moved（保留旧决策故事）
SIMILARITY_MOVE_THRESHOLD = 0.7

_SEP_RE = re.compile(r"[^A-Za-z0-9]+")


def hash_file(path: str | Path) -> str | None:
    """返回文件内容的 SHA-256（64 位 hex）；读取失败（二进制/权限）返回 None。"""
    p = Path(path)
    h = hashlib.sha256()
    try:
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return None
    h.update(str(p).encode("utf-8", "replace"))  # 路径参与哈希：同名不同目录内容不同
    return h.hexdigest()


def normalize_name(name: str) -> str:
    """归一化：小写、去所有非字母数字分隔符。OrderService / order_service / Order-Service 视为同骨架。"""
    return _SEP_RE.sub("", str(name).lower())


def file_stem(rel_path: str) -> str:
    """归一化文件名主干（最后一段，去目录与包前缀）。"""
    parts = str(rel_path).replace("\\", "/").split("/")
    fname = parts[-1]
    stem = fname.rsplit(".", 1)[0] if "." in fname else fname
    return normalize_name(stem.split(".")[-1])


def fingerprint_symbols(symbols: list[str]) -> frozenset[str]:
    """归一化符号集合。"""
    return frozenset(normalize_name(s) for s in symbols if s)


def file_fingerprint(rel_path: str, symbols: list[str]) -> frozenset[str]:
    """结构化指纹：符号名（归一化）+ 文件名最后一段（归一化）。"""
    return frozenset([file_stem(rel_path)]) | fingerprint_symbols(symbols)


def jaccard(a: frozenset, b: frozenset) -> float:
    """Jaccard 相似度：|A∩B| / |A∪B|；两个空集视为完全相似。"""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def containment(a: frozenset, b: frozenset) -> float:
    """Coverage 相似度：|A∩B| / min(|A|,|B|)。"""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def moved_score(rel_a: str, syms_a: list[str], rel_b: str, syms_b: list[str]) -> float:
    """moved 判定得分：只比较符号骨架（重命名/小改宽容）；
    两侧符号皆空时回退对比文件名主干（如 __init__.py 的移动）。"""
    sa, sb = fingerprint_symbols(syms_a), fingerprint_symbols(syms_b)
    if sa or sb:
        return containment(sa, sb)
    return containment(frozenset([file_stem(rel_a)]), frozenset([file_stem(rel_b)]))


def similar(a: frozenset, b: frozenset) -> float:
    """兼容入口：结构化指纹相似度（containment）。"""
    return containment(a, b)
