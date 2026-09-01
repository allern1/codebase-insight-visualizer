# -*- coding: utf-8 -*-
"""java_plugin.py — Java 事实提取（V1 正则版）"""
import re

_SYMBOL_RE = re.compile(
    r"\b(?:public\s+|private\s+|protected\s+|static\s+|abstract\s+|final\s+)*"
    r"(?:class|interface|enum|record)\s+([A-Za-z_][A-Za-z0-9_]*)"
)
_IMPORT_RE = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+);", re.MULTILINE)


def extract_symbols(text: str) -> list[dict]:
    """返回带行号的符号表：[{name, line}]。"""
    out = []
    for m in _SYMBOL_RE.finditer(text):
        out.append({"name": m.group(1),
                    "line": text.count("\n", 0, m.start()) + 1})
    return out


def extract_imports(text: str) -> list[str]:
    return [m.group(1) for m in _IMPORT_RE.finditer(text)]


PLUGIN = {
    "language": "java",
    "extract_symbols": extract_symbols,
    "extract_imports": extract_imports,
}
