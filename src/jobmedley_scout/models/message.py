"""Message models.

8.1 の分割が型に現れている: :class:`GeneratedCore` が LLM の生成領域、
:class:`AssembledMessage` がコードの決定的な組み立て結果。参照実装では本文12要素
のうち LLM が生成するのは中核の5要素と件名だけで、宛名・注記・定型文・署名・
フッターはすべてコードが付与している。**書式 (1件1ブロックの並べ方・区切り・
URLの付与) も LLM に任せずコードで組む。**
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

_STRICT = ConfigDict(extra="forbid", frozen=True)


class Introduction(BaseModel):
    """One person introduced in the message body."""

    model_config = _STRICT

    #: LLM が返す識別子。生成側と参照側の両方が
    #: :func:`models.text_norm.normalize_identifier` を通すこと (8.6)。
    person_id: str
    #: この人物を紹介する短文。共通点の有無で断定語の許容が変わる。
    blurb: str


class GeneratedCore(BaseModel):
    """The parts of the message the LLM actually writes.

    これ以外の要素 (宛名・注記・定型文・署名・フッター) と、すべての書式は
    :mod:`generation.assemble` がコードで組む。
    """

    model_config = _STRICT

    subject: str
    opening: str
    motivation: str
    introductions: tuple[Introduction, ...]
    closing: str


class AssembledMessage(BaseModel):
    """The final message, exactly as it will be sent.

    バリデータはこの **最終成果物** に掛ける (8.5)。LLM の出力ではなく、
    システムが組み立て終わった本文全体が検証対象である。
    """

    model_config = _STRICT

    subject: str
    body: str
    #: 突合キーの正準形。送信直後に永続化する (13.3) -- 件名を失うと
    #: その対象の返信は恒久的に検知不可能になる。
    subject_norm: str
    subject_prefix35: str
