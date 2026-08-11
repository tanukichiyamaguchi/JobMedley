"""Parsing the inbox listing (10.3).

**DOMではなく、一覧を返す非同期通信の応答本文を解析する。** 受信箱はSPAで描画され、
行の要素構造は媒体の都合でいつでも変わる。応答本文を対象にしておくと
マークアップ変更にも強くなるうえ、失敗したときに何が返ってきたのかを
そのままログに残せる (:mod:`reply.diagnostics`)。

件名の位置 (``inbox.subject_json_path``) は段階3で確定する **未確定の座標** である。
確定するまでこの機能を止めてしまうと座標を確定する材料も採れないので、
座標が無くても動く経路を用意してある:

* 座標がある → その位置から件名を取り出す (:data:`RowSource.JSON_PATH`)。
* 座標が無い / 座標が実際の応答に当たらない → 構造走査に落ちて、
  **行の存在と拾えた値だけ** を返す (:data:`RowSource.STRUCTURAL_SCAN`)。

構造走査は **件名を推測しない**。件名は返信検知の突合キーであり (10.2)、
それらしい文字列を件名として当てはめると、他人の返信を別人に紐づける。
拾えなかったものは ``None`` のままにする。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, TypeAlias

from jobmedley_scout.errors import ConfigError
from jobmedley_scout.models.text_norm import normalize_subject

#: 構造走査で降りる最大の深さ。無制限にすると相互参照を含むJSONで戻ってこない。
MAX_SCAN_DEPTH: Final[int] = 8

#: 1フィールドあたりの保持文字数。診断用の値なので長い本文は要らない。
MAX_FIELD_CHARS: Final[int] = 120

_SEGMENT = re.compile(r"\A(?P<name>[^.\[\]]*)(?P<subscripts>(?:\[\d*\])*)\Z")
_SUBSCRIPT = re.compile(r"\[(\d*)\]")


class RowSource(StrEnum):
    """How a row was obtained -- which decides whether its subject is trustworthy."""

    JSON_PATH = "json_path"
    STRUCTURAL_SCAN = "structural_scan"


@dataclass(frozen=True)
class InboxRow:
    """One row of the inbox listing, with whatever was recoverable.

    ``subject`` が ``None`` なのは「件名が無い行」ではなく「件名を取り出せなかった
    行」である。突合は :mod:`reply.subject_match` が件名の有る行だけに対して行う。
    """

    row_index: int
    source: RowSource
    subject: str | None
    subject_norm: str | None
    #: 拾えた走査可能な値を ``(キー, 値)`` で保持したもの。キー順に並ぶ。
    #: 座標が未確定の段階では、ここを見て件名らしきキーを特定する (10.6)。
    fields: tuple[tuple[str, str], ...]

    def signature_marker(self) -> str:
        """The most identifying string this row has, for the page signature (10.5).

        件名があればそれが一意 (10.2)。無い段階では拾えた値の連結で代用する --
        署名はハッシュ化されるので中身が何であっても記録上は安全 (13.2)。
        """
        if self.subject_norm:
            return self.subject_norm
        return "\x1e".join(f"{key}={value}" for key, value in self.fields)


@dataclass(frozen=True)
class _Key:
    name: str


@dataclass(frozen=True)
class _Index:
    position: int


@dataclass(frozen=True)
class _Iterate:
    """``[]`` -- the level at which the listing repeats, i.e. one row per element."""


_Step: TypeAlias = _Key | _Index | _Iterate


def _parse_json_path(path: str) -> tuple[_Step, ...]:
    """Parse ``a.b[].c`` / ``a.b[0].c`` into steps.

    ``[]`` は「ここが行の繰り返し」を表す。座標の記法を1つに固定しておかないと、
    座標を書く人と読む人で解釈がずれ、確定したはずの座標が当たらなくなる。
    """
    steps: list[_Step] = []
    for raw_segment in path.split("."):
        match = _SEGMENT.match(raw_segment.strip())
        if match is None:
            raise ConfigError(
                f"JSONパス '{path}' の区間 '{raw_segment}' を解釈できません。"
                "記法は 'data.items[].subject' 形式です。"
            )
        name = match.group("name")
        if name:
            steps.append(_Key(name))
        for subscript in _SUBSCRIPT.findall(match.group("subscripts")):
            steps.append(_Iterate() if subscript == "" else _Index(int(subscript)))
    if not steps:
        raise ConfigError(f"JSONパスが空です: {path!r}")
    return tuple(steps)


def _split_at_iterate(
    steps: tuple[_Step, ...],
) -> tuple[tuple[_Step, ...], tuple[_Step, ...]] | None:
    """Split ``steps`` around the first ``[]``; ``None`` when there is none."""
    for position, step in enumerate(steps):
        if isinstance(step, _Iterate):
            before = steps[:position]
            after = steps[position + 1 :]
            if any(isinstance(later, _Iterate) for later in after):
                raise ConfigError(
                    "JSONパスに '[]' が2つ以上あります。行の繰り返しは1段だけ対応します。"
                )
            return before, after
    return None


def _resolve(value: object, steps: Sequence[_Step]) -> object | None:
    """Walk ``steps`` from ``value``; ``None`` when the path does not apply."""
    current: object = value
    for step in steps:
        if isinstance(step, _Key):
            if not isinstance(current, Mapping) or step.name not in current:
                return None
            current = current[step.name]
        elif isinstance(step, _Index):
            if not isinstance(current, list) or not 0 <= step.position < len(current):
                return None
            current = current[step.position]
        else:  # pragma: no cover - 呼び出し前に _split_at_iterate で除いてある
            return None
    return current


def _scalar_text(value: object) -> str | None:
    """Render a JSON scalar for the diagnostic field list, or refuse it."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value[:MAX_FIELD_CHARS]
    if isinstance(value, int | float):
        return str(value)
    return None


def _flatten(
    value: Mapping[str, object], prefix: str = "", depth: int = 0
) -> Iterator[tuple[str, str]]:
    """Yield the scalar leaves of ``value`` as dotted ``(key, text)`` pairs."""
    if depth > MAX_SCAN_DEPTH:
        return
    for raw_key, raw_value in value.items():
        key = f"{prefix}{raw_key}"
        if isinstance(raw_value, Mapping):
            yield from _flatten(raw_value, prefix=f"{key}.", depth=depth + 1)
            continue
        text = _scalar_text(raw_value)
        if text is not None:
            yield key, text


def _row_fields(element: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(element, Mapping):
        text = _scalar_text(element)
        return (("value", text),) if text is not None else ()
    return tuple(sorted(_flatten(element)))


def _make_row(row_index: int, source: RowSource, element: object, subject: object) -> InboxRow:
    # 件名として受け付けるのは文字列だけ。数値やnullを str() で件名に仕立てると
    # 突合キーが偽物になる (10.2)。取れなかったものは None のままにする。
    text = subject if isinstance(subject, str) and subject.strip() else None
    return InboxRow(
        row_index=row_index,
        source=source,
        subject=text,
        # 8.6: 突合キーの正規化は生成側と同じ関数を通す。ここで通しておくと、
        # 呼び出し側が正規化を忘れた経路を作れない。
        subject_norm=normalize_subject(text) if text is not None else None,
        fields=_row_fields(element),
    )


def _rows_from_json_path(parsed: object, subject_json_path: str) -> tuple[InboxRow, ...] | None:
    """Rows located by the coordinate; ``None`` when the coordinate does not apply.

    空タプル (座標は当たったが行が0件) と ``None`` (座標が当たらない) を区別する
    のは意図的である。最終ページの0件を「座標が外れた」と誤解して構造走査に
    落とすと、無関係な配列を行として拾い、終端判定 (10.5) が壊れる。
    """
    steps = _parse_json_path(subject_json_path)
    split = _split_at_iterate(steps)
    if split is None:
        # 繰り返しの無いパスは「1応答に1件名」を指す。届いた形が違えば当たらない。
        subject = _resolve(parsed, steps)
        if subject is None:
            return None
        return (_make_row(0, RowSource.JSON_PATH, parsed, subject),)
    before, after = split
    container = _resolve(parsed, before)
    if not isinstance(container, list):
        return None
    return tuple(
        _make_row(row_index, RowSource.JSON_PATH, element, _resolve(element, after))
        for row_index, element in enumerate(container)
    )


def _lists_of_mappings(value: object, depth: int = 0) -> Iterator[tuple[int, list[object]]]:
    if depth > MAX_SCAN_DEPTH:
        return
    if isinstance(value, Mapping):
        for nested in value.values():
            yield from _lists_of_mappings(nested, depth + 1)
        return
    if isinstance(value, list):
        if value and all(isinstance(element, Mapping) for element in value):
            yield depth, list(value)
        for nested in value:
            yield from _lists_of_mappings(nested, depth + 1)


def _rows_from_structural_scan(parsed: object) -> tuple[InboxRow, ...]:
    """The largest list of objects in the body, as rows without a subject.

    座標が未確定でも「何行あるか」「どんなキーがあるか」は分かる。ここが空を
    返すのと、受信箱が本当に空なのとは区別できないが、**件名を推測しない**
    ので誤検知には育たない (10.2)。
    """
    found = list(_lists_of_mappings(parsed))
    if not found:
        return ()
    # 件数が最大のものを行の配列とみなす。同数なら浅いほうを採る -- 深いほうは
    # たいてい行の内側の入れ子 (添付・タグなど) で、行数を水増しする。
    depth, container = max(found, key=lambda item: (len(item[1]), -item[0]))
    return tuple(
        _make_row(row_index, RowSource.STRUCTURAL_SCAN, element, None)
        for row_index, element in enumerate(container)
    )


def parse_body(body: str) -> object | None:
    """Parse ``body`` as JSON, or ``None`` when it is not JSON at all.

    応答がJSONとは限らない。例外を投げずに ``None`` を返すのは、呼び出し側が
    :func:`reply.diagnostics.fallback_tokens` に切り替えて **同じ実行の中で**
    中身を確認できるようにするため (10.6)。
    """
    try:
        parsed: object = json.loads(body)
    except ValueError:
        return None
    return parsed


def extract_rows(body: str, subject_json_path: str | None) -> tuple[InboxRow, ...]:
    """Extract the inbox rows from one listing response body.

    ``subject_json_path`` is the (stage-3) coordinate for where the subject sits.
    When it is ``None`` -- or when it does not apply to this body -- the rows come
    back from a structural scan with ``subject=None`` rather than a guess.
    """
    parsed = parse_body(body)
    if parsed is None:
        # JSONでないものを行に仕立てない。診断は diagnostics 側で採る (10.6)。
        return ()
    if subject_json_path is not None:
        rows = _rows_from_json_path(parsed, subject_json_path)
        if rows is not None:
            return rows
        # 座標が古くなって当たらなくなった場合。ここで静かに0件を返すと
        # 「返信ゼロ」に見えてしまう (原則2の静かなゼロ件) ため、行の存在だけは
        # 構造走査で拾って返す。source を見れば件名が無い理由が分かる。
    return _rows_from_structural_scan(parsed)
