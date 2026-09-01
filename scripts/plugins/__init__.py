"""plugins/__init__.py — 语言解析插件注册表（V1：正则起步，可替换为 tree-sitter）

设计原则（V2.0 架构）：
- 插件只产出【事实】（符号名、导入声明），绝不产出语义判断。
- 未知语言 / 解析失败 → 返回 None / parse_ok=False，调用方降级为"文件级节点，edges 留空"，
  而不是报错中断整个扫描。
"""
import re

from . import python_plugin, js_plugin, go_plugin, java_plugin

# extension -> plugin dict
PLUGINS = {
    ".py": python_plugin.PLUGIN,
    ".pyi": python_plugin.PLUGIN,
    ".js": js_plugin.PLUGIN,
    ".ts": js_plugin.PLUGIN,
    ".jsx": js_plugin.PLUGIN,
    ".tsx": js_plugin.PLUGIN,
    ".go": go_plugin.PLUGIN,
    ".java": java_plugin.PLUGIN,
}

# 语音插件声明：extensions, extract_symbols(text)->list[str], extract_imports(text)->list[str]
def get_plugin(path: str):
    """按扩展名返回插件 dict；未知语言返回 None（降级为文件级节点）。"""
    for ext, plugin in PLUGINS.items():
        if path.lower().endswith(ext):
            return plugin
    return None
