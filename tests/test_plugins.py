# -*- coding: utf-8 -*-
"""tests/test_plugins.py — 语言插件单测（assert 式，直接 python 运行）

用法：python tests/test_plugins.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from plugins import python_plugin as py, js_plugin as js, go_plugin as go, java_plugin as ja


def test_python():
    imps = py.extract_imports(
        "from services.auth import verify_jwt\nimport os, json as _json\nfrom ..common import helper\n"
    )
    assert imps == ["services.auth", "os", "json", "..common"], f"py imps: {imps}"
    syms = py.extract_symbols("class A:\n    def m(self): pass\n\ndef top(): pass")
    assert syms == [{"name": "A", "line": 1}, {"name": "m", "line": 2}, {"name": "top", "line": 4}], f"py syms: {syms}"


def test_js():
    syms = js.extract_symbols(
        "const orderSvc = async (req) => {};\nexport function createOrder(req) {}"
    )
    assert syms == [{"name": "orderSvc", "line": 1}, {"name": "createOrder", "line": 2}], f"js syms: {syms}"
    imps = js.extract_imports("import { verify } from './auth';\nrequire('dotenv').config();\n")
    assert imps == ["./auth", "dotenv"], f"js imps: {imps}"


def test_go():
    syms = go.extract_symbols("type Order struct { ID int }\nfunc (o *Order) Create() {}\nfunc main() {}")
    assert syms == [{"name": "Order", "line": 1}, {"name": "Create", "line": 2}, {"name": "main", "line": 3}], f"go syms: {syms}"
    imps = go.extract_imports('import (\n"fmt"\n"github.com/x/y/ordersvc"\n)')
    assert imps == ["fmt", "github.com/x/y/ordersvc"], f"go imps: {imps}"


def test_java():
    syms = ja.extract_symbols("public class Main { public static void main(String[] args) {} }")
    assert syms == [{"name": "Main", "line": 1}], f"java syms: {syms}"
    imps = ja.extract_imports("import com.example.services.OrderService;")
    assert imps == ["com.example.services.OrderService"], f"java imps: {imps}"


if __name__ == "__main__":
    test_python()
    test_js()
    test_go()
    test_java()
    print("ALL PLUGIN TESTS PASSED")
