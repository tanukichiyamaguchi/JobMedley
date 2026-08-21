"""手順書が、存在しないコマンドを指していないこと。

**同じ形の事故が2回起きている。**

1. ``read-bundle`` がワークフローの選択肢にあったのに、対応する実行手順が無く、
   選ぶとジョブは「成功」して何もせずに終わった (実測)
2. ``docs/ladder.md`` の段階4が ``scout recon dryrun-send`` を指していたが、
   そんなサブコマンドは存在しない。運用者が打つと argparse が
   ``invalid choice`` で終了コード2を返す

どちらも **静かな失敗ではなく、運用者の時間の損失** である。クラウドでしか媒体へ
到達できない運用では、往復1回が最も高価な資源なので、往復を空振りさせる記述は
コードの誤りと同じ重さで扱う。

この試験は手順書とCLIを突き合わせる。**手順書に書けるのは、実在するコマンドだけ。**
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

from jobmedley_scout.cli import _build_parser

DOCS = (Path("docs/ladder.md"), Path("README.md"))

#: 手順書の中の ``scout ...`` を拾う。行頭・コード塊のどちらでも。
_INVOCATION = re.compile(r"\bscout\s+([a-z][a-z0-9-]*(?:\s+[a-z][a-z0-9-]*)?)")

#: 説明文の中で「コマンドの名前」ではなく普通の語として現れるもの。
_NOT_A_COMMAND = frozenset({"の", "は", "を", "が", "で", "に", "と"})


def _subcommands(parser: argparse.ArgumentParser) -> dict[str, frozenset[str]]:
    """``{コマンド名: サブコマンド名の集合}``. サブコマンドが無ければ空集合。"""
    found: dict[str, frozenset[str]] = {}
    for action in parser._actions:  # noqa: SLF001 -- argparse に公開APIが無い
        if not isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            continue
        for name, sub in action.choices.items():
            inner: set[str] = set()
            for sub_action in sub._actions:  # noqa: SLF001
                if isinstance(sub_action, argparse._SubParsersAction):  # noqa: SLF001
                    inner |= set(sub_action.choices)
            found[name] = frozenset(inner)
    return found


def _invocations(text: str) -> set[tuple[str, str]]:
    """``scout X [Y]`` を ``(X, Y)`` の集合にする (Y が無ければ空文字)。"""
    found: set[tuple[str, str]] = set()
    for match in _INVOCATION.finditer(text):
        parts = match.group(1).split()
        head = parts[0]
        tail = parts[1] if len(parts) > 1 and parts[1] not in _NOT_A_COMMAND else ""
        found.add((head, tail))
    return found


@pytest.mark.parametrize("path", DOCS)
def test_every_command_the_docs_tell_the_operator_to_run_exists(path: Path) -> None:
    """**手順書が指すコマンドは、全部 CLI に在る。**

    在らなければ、運用者は往復を1回失う。クラウドでしか媒体へ到達できない運用
    では、それが最も高価な損失である。
    """
    if not path.exists():
        pytest.skip(f"{path} がありません")
    commands = _subcommands(_build_parser())
    missing: list[str] = []
    for head, tail in sorted(_invocations(path.read_text(encoding="utf-8"))):
        if head not in commands:
            missing.append(f"scout {head}")
            continue
        # サブコマンドを持つコマンドは、名前まで一致していること。
        if commands[head] and tail and tail not in commands[head]:
            missing.append(f"scout {head} {tail}")
    assert not missing, (
        f"{path} が存在しないコマンドを指しています: {missing}。"
        f"運用者が打つと argparse が invalid choice で終了します "
        f"(往復を1回失う)。"
    )


def test_a_command_that_never_sends_does_not_require_send_response_coordinates() -> None:
    """**送信せずには埋められない座標を、送信しないコマンドの前提にしない。**

    梯子が閉じるからである。``api.send.paid.success_statuses`` と
    ``api.auth_failure_codes`` は段階4で **実測して** 埋める座標なのに、それを
    段階5の空振り (``scout dryrun``) が要求すると、段階4に進むための確認手段が
    段階4の成果物待ちになる。

    ``config/coordinates.py`` 自身の docstring が戒めている「鶏と卵」である。
    """
    from jobmedley_scout.config.coordinates import REQUIRED_BY_COMMAND

    only_a_real_send_can_fill = {
        "api.send.paid.success_statuses",
        "api.send.free.success_statuses",
        "api.auth_failure_codes",
    }
    for command in ("dryrun", "ingest", "generate"):
        required = REQUIRED_BY_COMMAND.get(command, frozenset())
        overlap = required & only_a_real_send_can_fill
        assert not overlap, (
            f"コマンド '{command}' は一通も送らないのに、送信の応答を解釈する"
            f"ための座標を要求しています: {sorted(overlap)}。"
            f"これを要求すると梯子が閉じます。"
        )


def test_dryrun_still_requires_what_it_needs_to_build_a_send() -> None:
    """**要求を減らしすぎない。**

    「送信直前で止める」は、止まる直前まで組み立てるということである。
    組み立てられない状態で「空振り成功」と報告したら、それは原則2 の
    「静かなゼロ件」を手順書の側で作ることになる。
    """
    from jobmedley_scout.config.coordinates import REQUIRED_BY_COMMAND

    required = REQUIRED_BY_COMMAND["dryrun"]
    assert "api.send.paid.url_pattern" in required
    assert "api.send.paid.payload_template" in required
