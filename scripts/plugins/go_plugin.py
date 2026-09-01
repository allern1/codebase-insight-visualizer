# -*- coding: utf-8 -*-
"""go_plugin.py — Go 事实提取（V1 正则版）"""
import re

_SYMBOL_RE = re.compile(
    r"^func\s+(?:\([^)]*\)\s*)?([A-Za-z_][A-Za-z0-9_]*)"
    r"|^type\s+([A-Za-z_][A-Za-z0-9_]*)\s+(?:struct|interface)",
    re.MULTILINE,
)
_IMPORT_RE = re.compile(r'^\s*"([^"]+)"', re.MULTILINE)


def extract_symbols(text: str) -> list[dict]:
    """返回带行号的符号表：[{name, line}]。"""
    out = []
    for m in _SYMBOL_RE.finditer(text):
        out.append({"name": m.group(1) or m.group(2),
                    "line": text.count("\n", 0, m.start()) + 1})
    return out


def extract_imports(text: str) -> list[str]:
    # 保留完整路径（可用于精确匹配），同时提供短名供段匹配
    return [m.group(1) for m in _IMPORT_RE.finditer(text)]


PLUGIN = {
    "language": "go",
    "extract_symbols": extract_symbols,
    "extract_imports": extract_imports,
}
