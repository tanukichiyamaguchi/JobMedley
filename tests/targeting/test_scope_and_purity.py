"""Structural guards on the targeting package itself.

コメントや docstring ではなく **AST** を見る。コメントは「そう書いてあるだけ」で
守られないが、AST の検査は書き換えた瞬間に赤くなる。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[2] / "src" / "jobmedley_scout" / "targeting"
MODULES = sorted(PACKAGE.glob("*.py"))

#: 純粋性を壊す入口。判定に必要なものは全部引数で受け取る。
FORBIDDEN_IMPORTS = {
    "os",
    "io",
    "sys",
    "time",
    "random",
    "datetime",
    "pathlib",
    "sqlite3",
    "socket",
    "subprocess",
    "httpx",
    "requests",
    "urllib",
    "anthropic",
    "playwright",
}


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_modules_were_found() -> None:
    assert MODULES, "targeting パッケージが見つからない"


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_module_is_pure(path: Path) -> None:
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in FORBIDDEN_IMPORTS, alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            assert root not in FORBIDDEN_IMPORTS, node.module


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_no_module_reads_the_summary_field(path: Path) -> None:
    """7.2: 「ネイティブ」判定を語学欄の外へ広げる経路を作らせない。

    要約や職歴本文に広げた瞬間、「ネイティブ広告」「クラウドネイティブ」が一致して
    日本人候補者が除外される。複合語の除外リストで戦うのではなく、**そもそも
    参照しない** ことが対処である。
    """
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Attribute):
            assert node.attr != "summary", f"{path.name}: resume.summary を参照している"


def test_the_language_rule_reads_only_the_language_field() -> None:
    tree = _tree(PACKAGE / "rules.py")
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "detect_foreign_native_detail"
    ]
    assert calls, "語学判定の呼び出しが見つからない"
    for call in calls:
        assert ast.unparse(call.args[0]) == "candidate.resume.language_text"
