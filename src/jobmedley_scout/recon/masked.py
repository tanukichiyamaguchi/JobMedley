"""媒体が「値を伏せた」ことを表す文字列を、**値ではなく不在として扱う**。純粋。

2026-08-22、運用者から実画面のレジュメを見せてもらって分かった。**欄は在るが
値が伏せられている項目がある。**

::

    会員番号   01613058
    氏名(ふりがな)   （未応募のため非表示）
    電話番号        （未応募のため非表示）
    自己PR          未入力

**これは 6.4 の事故が起きる形そのものである。** キーパスを当てて素直に写せば、
``display_name`` に「（未応募のため非表示）」が入る。空ではないので
:mod:`generation.facts` は「非公開」に落とさず、そのまま値として渡す。モデルは
それを名前と見なして::

    （未応募のため非表示）様

と書く。取り消せない (13.6)。``自己PR: 未入力`` も同じで、渡せばモデルは
「自己PRに『未入力』とご記入されており」と書きうる。

**空文字にするのではなく None にする。** 空文字は「観測したが空だった」で、
None は「値が無い」である。:mod:`generation.facts` は None を
:data:`~generation.facts.UNDISCLOSED` (「非公開」) に落とすので、モデルは
その項目に言及できなくなる -- 嘘の発生経路が構造的に塞がる (8.3 対策1)。

**判定は完全一致ではなく、括弧と空白を落としたうえでの一致にしてある。** 媒体が
全角括弧を付けたり外したりしても効き続ける必要があるからである。逆に部分一致には
していない -- 「非表示」を含む正当な自由記述 (「前職では非表示設定の…」) を
消してしまう。
"""

from __future__ import annotations

import unicodedata

#: 伏せ字・未入力を表す表記。**実画面で確認したものだけを入れる。**
#:
#: 増やすときは推測で足さないこと (原則3)。ここに無い表記はそのまま値として
#: 通るが、それは「観測していないものを勝手に消さない」という正しい既定である。
WITHHELD_MARKERS: frozenset[str] = frozenset(
    {
        "未応募のため非表示",
        "未入力",
        "非公開",
        "非表示",
        "-",
        "ー",
        "—",
    }
)

#: 落としてから比べる飾り。括弧と空白だけで、文字は落とさない。
_TRIM = "（）()〔〕[]【】 　\t\r\n"


def _fold(value: str) -> str:
    """Normalise width and strip decoration, without changing the words."""
    return unicodedata.normalize("NFKC", value).strip(_TRIM).strip()


def is_withheld(value: str | None) -> bool:
    """Whether the platform is saying "there is no value here". **Pure.**"""
    if value is None:
        return False
    folded = _fold(value)
    if not folded:
        return True
    # NFKC は全角括弧を半角にするので、畳んだあとにもう一度剥がす。
    return folded.strip(_TRIM).strip() in WITHHELD_MARKERS


def unmask(value: str | None) -> str | None:
    """``None`` when the platform withheld the value, otherwise the value.

    **取り込み側はこれを通してからモデルへ渡すこと。** 通さなければ
    「（未応募のため非表示）様」が候補者に届く。
    """
    if value is None:
        return None
    return None if is_withheld(value) else value


def unmask_all(values: tuple[str, ...]) -> tuple[str, ...]:
    """Drop withheld entries from a list of values. **Pure.**"""
    return tuple(item for item in values if not is_withheld(item))


__all__ = ["WITHHELD_MARKERS", "is_withheld", "unmask", "unmask_all"]
