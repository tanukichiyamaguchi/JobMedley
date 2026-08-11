"""Structural guard: the decision half of ``state/`` stays pure.

``state/`` は永続化 (sqlite3) と判定が同居するパッケージなので、判定側だけを名指し
で検査する。コメントで「純粋に保つこと」と書いても守られないが、AST の検査は
壊した瞬間に赤くなる (13.4: 純粋な判定だけが単体テストの対象になりうる)。

乱数が許されるのは :mod:`state.idempotency` の ``new_idempotency_key`` だけ (9.2)。
時刻は引数で受け取る -- :mod:`jobmedley_scout.clock` 以外が壁時計を読まないという
全体規約の一部。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[2] / "src" / "jobmedley_scout" / "state"

#: 判定側 (純粋) のモジュール。永続化側 (db, *_repo, migrations) は対象外。
PURE_MODULES = (
    "idempotency.py",
    "dedupe.py",
    "rotation.py",
    "caps.py",
    "guards.py",
    "wipeout.py",
)

#: 純粋性を壊す入口。判定に必要なものは全部引数で受け取る。
FORBIDDEN_IMPORTS = {
    "os",
    "io",
    "sys",
    "time",
    "random",
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

#: 壁時計とスリープの読み出し口。``datetime`` の import 自体は型のために許すが、
#: ``datetime.now()`` を呼んだ瞬間にこのテストが落ちる。
FORBIDDEN_CALL_ATTRS = {"now", "utcnow", "today", "sleep", "monotonic", "perf_counter"}


def _tree(name: str) -> ast.Module:
    path = PACKAGE / name
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_the_pure_modules_all_exist() -> None:
    for name in PURE_MODULES:
        assert (PACKAGE / name).is_file(), name


@pytest.mark.parametrize("name", PURE_MODULES)
def test_module_imports_nothing_impure(name: str) -> None:
    for node in ast.walk(_tree(name)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in FORBIDDEN_IMPORTS, f"{name}: {alias.name}"
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            assert root not in FORBIDDEN_IMPORTS, f"{name}: {node.module}"


@pytest.mark.parametrize("name", PURE_MODULES)
def test_module_never_reads_the_wall_clock(name: str) -> None:
    """時刻は引数で受け取る。読んだ瞬間に判定が単体テスト不能になる (13.4)。"""
    for node in ast.walk(_tree(name)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in FORBIDDEN_CALL_ATTRS, f"{name}: {ast.unparse(node)}"


@pytest.mark.parametrize("name", PURE_MODULES)
def test_randomness_lives_only_in_the_key_generator(name: str) -> None:
    """9.2: 乱数は :func:`new_idempotency_key` の一箇所に閉じ込める。"""
    imports_uuid = any(
        isinstance(node, ast.Import) and any(alias.name == "uuid" for alias in node.names)
        for node in ast.walk(_tree(name))
    )
    assert imports_uuid == (name == "idempotency.py"), name


@pytest.mark.parametrize("name", PURE_MODULES)
def test_module_declares_future_annotations(name: str) -> None:
    tree = _tree(name)
    assert any(
        isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
        for node in ast.walk(tree)
    ), name
