# -*- coding: utf-8 -*-
"""js_plugin.py — JavaScript/TypeScript 事实提取（V1 正则版）

提取：函数/类/组件符号 + import/require 声明。
注：tree-sitter 版本（v1.5 可选增强）将在此处通过 import 探测无缝替换。
"""
import re

_SYMBOL_RE = re.compile(
    r"(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)"
    r"|(?:export\s+(?:default\s+)?)?class\s+([A-Za-z_$][\w$]*)"
    r"|(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\(|function)"
    r"|(?:export\s+default\s+)?([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\(|function)",
)
_IMPORT_RE = re.compile(
    r"import\s+(?:[^'\"\n]+?\s+from\s+)?['\"]([^'\"]+)['\"]"
    r"|import\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"
    r"|require\(\s*['\"]([^'\"]+)['\"]\s*\)",
)


def extract_symbols(text: str) -> list[dict]:
    """返回带行号的符号表：[{name, line}]。"""
    seen, out = set(), []
    for m in _SYMBOL_RE.finditer(text):
        name = next((g for g in m.groups() if g), None)
        if name and name not in seen:
            seen.add(name)
            out.append({"name": name, "line": text.count("\n", 0, m.start()) + 1})
    return out


def extract_imports(text: str) -> list[str]:
    out = []
    for m in _IMPORT_RE.finditer(text):
        spec = next((g for g in m.groups() if g), None)
        if spec:
            out.append(spec)
    return out


PLUGIN = {
    "language": "javascript/typescript",
    "extract_symbols": extract_symbols,
    "extract_imports": extract_imports,
}
