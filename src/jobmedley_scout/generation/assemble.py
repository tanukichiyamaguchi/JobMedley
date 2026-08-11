"""Deterministic assembly of the final message.

8.1 の分割: 本文の12要素のうち **LLM が書くのは中核の5要素と件名だけ** で、
宛名・注記・定型文・署名・フッターはコードが付ける。さらに **書式の決定も
コード側に置く** -- 1人1ブロックの並べ方、区切り、URLの位置まで含めて、
モデルに委ねる余地を作らない。委ねた分だけ実行ごとに揺れ、検証も差分比較も
できなくなる。

このモジュールは LLM を呼ばない。呼びたくなったら、それは 8.1 の分割線を
跨いでいる合図である。
"""

from __future__ import annotations

import re
from typing import Final

from pydantic import BaseModel, ConfigDict

from jobmedley_scout.errors import GenerationError
from jobmedley_scout.generation.subject import SubjectKeys
from jobmedley_scout.models.message import AssembledMessage, GeneratedCore

_STRICT = ConfigDict(extra="forbid", frozen=True)

#: 紹介1件の先頭に付ける記号。
INTRO_BULLET: Final[str] = "◎"
#: 区切りの罫線。
SECTION_RULE_CHAR: Final[str] = "─"
SECTION_RULE_WIDTH: Final[int] = 24
#: URL の直前に置く矢印。
URL_ARROW: Final[str] = "→"

#: **システム自身が付ける装飾記号の全体。**
#:
#: :mod:`generation.validators` の絵文字検査はこの集合を除外する。参照実装は
#: 自前のフッターに含まれる ◎ で自分のバリデータを踏み、**全メッセージで
#: 修正リトライが走っていた** (8.5)。ここを「余計な除外だ」と見て消すと、
#: その事故がそのまま再発する。装飾記号を足すときは必ずここにも足すこと
#: (tests/generation/test_assemble.py が突き合わせている)。
SYSTEM_GLYPHS: Final[frozenset[str]] = frozenset({INTRO_BULLET, SECTION_RULE_CHAR, URL_ARROW})

_BLANK_RUN: Final[re.Pattern[str]] = re.compile(r"\n{3,}")
_TRAILING_SPACE: Final[re.Pattern[str]] = re.compile(r"[ \t]+$", re.MULTILINE)


class AssemblyContext(BaseModel):
    """Everything the code -- not the model -- contributes to the body.

    ここに文面素材を集約しているのは、定型文や署名が実行ごとに変わらないことを
    差分で確認できるようにするため。LLM に書かせると毎回揺れ、「今日の文面は
    昨日と同じか」が誰にも言えなくなる。
    """

    model_config = _STRICT

    recipient_name: str
    #: :func:`generation.subject.build_subject` が払い出した件名と突合キー。
    subject: SubjectKeys
    intro_heading: str = "ご紹介したいメンバー"
    #: 注記 (返信方法や連絡先の断り書きなど)。
    notes: tuple[str, ...] = ()
    #: 定型文。
    boilerplate: tuple[str, ...] = ()
    signature_lines: tuple[str, ...] = ()
    footer_lines: tuple[str, ...] = ()
    #: 本文に載せる URL。**許可リストの検証は最終本文に対して行う** (8.7)。
    footer_url: str | None = None
    footer_url_label: str = "詳細はこちら"


def _clean_generated(text: str, *, field: str) -> str:
    """Trim one LLM-written element and flatten its blank-line runs.

    空行の数まで LLM に委ねない (8.1)。3行以上の連続改行は2行に畳む。
    """
    cleaned = _TRAILING_SPACE.sub("", text).strip()
    cleaned = _BLANK_RUN.sub("\n\n", cleaned)
    if not cleaned:
        raise GenerationError(f"生成された本文要素 {field} が空です。組み立てを中止します。")
    return cleaned


def _rule_line() -> str:
    return SECTION_RULE_CHAR * SECTION_RULE_WIDTH


def assemble(core: GeneratedCore, ctx: AssemblyContext) -> AssembledMessage:
    """Build the exact text that will be sent.

    ブロックは空行1つで区切り、紹介は **1人1ブロック** で並べる。人物の内部ID
    (``Introduction.person_id``) は本文に出さない -- 突合用の識別子であって、
    受信者に見せるものではない。
    """
    recipient = ctx.recipient_name.strip().removesuffix("様").strip()
    if not recipient:
        raise GenerationError("宛名が空です。組み立てを中止します。")

    blocks: list[str] = [
        f"{recipient}様",
        _clean_generated(core.opening, field="opening"),
        _clean_generated(core.motivation, field="motivation"),
    ]

    if core.introductions:
        # 紹介が0件のときは見出しごと出さない。見出しだけが残ると
        # 「紹介するはずだった誰か」が抜け落ちた文面に見える。
        blocks.append(f"{ctx.intro_heading}\n{_rule_line()}")
        for index, introduction in enumerate(core.introductions, start=1):
            blurb = _clean_generated(introduction.blurb, field=f"introductions[{index}].blurb")
            blocks.append(f"{INTRO_BULLET} {blurb}")

    blocks.append(_clean_generated(core.closing, field="closing"))

    for note in ctx.notes:
        cleaned = note.strip()
        if cleaned:
            blocks.append(cleaned)

    for line in ctx.boilerplate:
        cleaned = line.strip()
        if cleaned:
            blocks.append(cleaned)

    footer: list[str] = [_rule_line()]
    footer.extend(line for line in (item.strip() for item in ctx.signature_lines) if line)
    footer.extend(line for line in (item.strip() for item in ctx.footer_lines) if line)
    if ctx.footer_url is not None:
        url = ctx.footer_url.strip()
        if url:
            # URL の位置もコードが決める (8.1)。本文中に散らさずフッターに集約
            # することで、許可リスト検査 (8.7) の対象が一箇所に定まる。
            footer.append(f"{URL_ARROW} {ctx.footer_url_label}: {url}")
    blocks.append("\n".join(footer))

    body = "\n\n".join(blocks)
    return AssembledMessage(
        subject=ctx.subject.subject,
        body=body,
        subject_norm=ctx.subject.subject_norm,
        subject_prefix35=ctx.subject.subject_prefix35,
    )
