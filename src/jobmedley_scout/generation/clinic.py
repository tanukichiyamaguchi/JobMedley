"""医院情報をプロンプトへ差し込む。純粋 (ファイル読み込みを除く)。

**この層が守っているのは1つだけ** -- 差し込み忘れた欄が、そのまま候補者へ
届かないようにすること。プロンプトの ``{{CLINIC_ADDRESS}}`` が埋まらないまま
モデルへ渡ると、モデルは「所在地は {{CLINIC_ADDRESSS}} です」と読むのではなく、
**知っている風に書く**。医院名は実在するので、それらしい住所が出てしまう。
原則3 が禁じている推測が、こちらの取りこぼしから始まる形である。

値の状態は3つあり、**混同すると事故の向きが変わる**。

``UNRESOLVED``
    まだ確認していない。差し込みは :class:`ConfigError` で止まる。
    座標と同じ扱いである (:mod:`config.placeholders` の思想)。

``NOT_REQUIRED``
    運用者が「必要ない」と明示した欄。止めない。``記載なし`` として渡す。
    **空文字では渡さない** -- 空文字は「その欄が無い」ようにも「渡し忘れた」
    ようにも読め、後者と解釈された瞬間にモデルの補完が始まる
    (:mod:`generation.facts` の ``UNDISCLOSED`` と同じ理由)。

通常の値
    運用者が書いた事実。そのまま渡す。

**「聞き忘れ」と「聞いた上で要らないと言われた」を分けてある。** 前者は埋めに
行く宿題で、後者は宿題ではない。1つの状態に畳むと、宿題の一覧が嘘になる。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import yaml

from jobmedley_scout.errors import ConfigError

#: まだ確認していない欄。差し込みを止める。
UNRESOLVED_TOKEN: Final[str] = "UNRESOLVED"

#: 運用者が「必要ない」と明示した欄。止めずに、無いと明示して渡す。
NOT_REQUIRED_TOKEN: Final[str] = "NOT_REQUIRED"

#: NOT_REQUIRED の欄がプロンプトへ渡るときの表記。
#:
#: **空文字にしない。** 空文字はモデルから見て「渡し忘れ」と区別がつかない。
NOT_REQUIRED_TEXT: Final[str] = "記載なし"

#: ``{{SLOT}}`` の記法。名前は大文字・数字・下線のみ。
SLOT_PATTERN: Final[re.Pattern[str]] = re.compile(r"\{\{([A-Z0-9_]+)\}\}")


def slots_in(template: str) -> tuple[str, ...]:
    """Slot names appearing in a prompt template, in order, de-duplicated."""
    seen: dict[str, None] = {}
    for match in SLOT_PATTERN.finditer(template):
        seen.setdefault(match.group(1), None)
    return tuple(seen)


def load_clinic_facts(path: Path) -> dict[str, str]:
    """Read ``config/clinic.yaml``. Values stay raw -- tokens are not expanded here.

    展開をここで行わないのは、**どの欄がどの状態だったかを呼び出し側が
    見られるようにする** ためである。展開してしまうと ``記載なし`` と
    「運用者が『記載なし』と書いた」が区別できなくなる。
    """
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ConfigError(f"{path}: 医院情報はキーと値の対応表である必要があります。")
    facts: dict[str, str] = {}
    for key, value in loaded.items():
        if not isinstance(key, str):
            raise ConfigError(f"{path}: 欄の名前が文字列ではありません: {key!r}")
        if not isinstance(value, str):
            raise ConfigError(
                f"{path}: 欄 '{key}' の値が文字列ではありません ({type(value).__name__})。"
                f"未確定なら {UNRESOLVED_TOKEN} と書いてください。"
            )
        facts[key] = value.strip()
    return facts


def unresolved_slots(facts: dict[str, str]) -> tuple[str, ...]:
    """Slots that are still ``UNRESOLVED``. **These are the homework.**"""
    return tuple(sorted(k for k, v in facts.items() if v == UNRESOLVED_TOKEN))


def not_required_slots(facts: dict[str, str]) -> tuple[str, ...]:
    """Slots the operator declared unnecessary. **These are not homework.**"""
    return tuple(sorted(k for k, v in facts.items() if v == NOT_REQUIRED_TOKEN))


def fill(template: str, values: dict[str, str], *, used_by: str) -> str:
    """Substitute every slot. Raises rather than emitting a half-filled prompt.

    止める条件は2つある。どちらも **静かに通すと候補者へ届く** 種類の穴である。

    1. 値が ``UNRESOLVED`` の欄がある -- 確認していない事実を書かせることになる
    2. 差し込んだ後も ``{{...}}`` が残っている -- 渡し忘れた欄がある

    2 が独立しているのは、``values`` に無い名前がテンプレート側に増えたときに
    1 では捕まらないからである。プロンプトを差し替えたときに効く。
    """
    unresolved = unresolved_slots(values)
    if unresolved:
        raise ConfigError(
            f"{used_by}: 医院情報の欄が未確定です: {', '.join(unresolved)}。"
            f"**推測で埋めないでください** -- 埋めた内容がそのまま候補者へ届きます。"
            f"運用者に確認して config/clinic.yaml を更新してください。"
        )

    expanded = {
        key: (NOT_REQUIRED_TEXT if value == NOT_REQUIRED_TOKEN else value)
        for key, value in values.items()
    }
    filled = SLOT_PATTERN.sub(
        lambda m: expanded.get(m.group(1), m.group(0)),
        template,
    )

    leftover = slots_in(filled)
    if leftover:
        raise ConfigError(
            f"{used_by}: プロンプトに差し込まれなかった欄が残っています: "
            f"{', '.join(leftover)}。値を渡し忘れています。"
            f"**このまま送るとモデルは記法を読まず、知っている風に書きます。**"
        )
    return filled
