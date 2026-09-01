# -*- coding: utf-8 -*-
"""python_plugin.py — Python 事实提取（V1 正则版）

只提取【硬事实】：函数/类符号名 + import 声明（含相对导入）。
刻意不做语义推断；被引用的符号属于 AI 语义层。
"""
import re

_SYMBOL_RE = re.compile(
    r"^\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)|^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)
_IMPORT_RE = re.compile(
    r"^\s*from\s+([.\w]+)\s+import\s+([\w \t,()*]+)|^\s*import\s+([\w \t,.\w]+)",
    re.MULTILINE,
)


def _clean_module(mod: str) -> str:
    """去掉 as 别名段，如 'pkg.sub as s' -> 'pkg.sub'"""
    return mod.split(" as ")[0].strip()


def extract_symbols(text: str) -> list[dict]:
    """返回带行号的符号表：[{name, line}]（行号 1-based，事实字段）。"""
    out = []
    for m in _SYMBOL_RE.finditer(text):
        out.append({"name": m.group(1) or m.group(2),
                    "line": text.count("\n", 0, m.start()) + 1})
    return out


def extract_imports(text: str) -> list[str]:
    out = []
    for m in _IMPORT_RE.finditer(text):
        if m.group(1):  # from X import Y
            mod = _clean_module(m.group(1))
            for name in m.group(2).split(","):
                name = name.strip()
                if not name:
                    continue
                # from X import Y → 目标是 X 模块（忽略符号级导入，V1 粒度=文件）
                out.append(mod)
                break
        elif m.group(3):  # import A, B
            for name in m.group(3).split(","):
                name = _clean_module(name)
                if name:
                    out.append(name)
    return out


PLUGIN = {
    "language": "python",
    "extract_symbols": extract_symbols,
    "extract_imports": extract_imports,
}
