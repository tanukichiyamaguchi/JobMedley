"""Deciding when to stop paging the inbox (10.5).

終了判定は **ページの内容そのもの** で行う。ページごとに行の一意な目印から署名を
作り、署名が空なら末尾、直前と同じなら進んでいない、と判定する。

**「次ページのリンク/ボタンがDOMにあるか」という分岐は意図的に持たない。**
受信箱はSPAで、ページャは初期DOMに存在しない。その判定を入れた実装は必ず1ページ目で
停止し、過去週の返信率が恒久的に0のまま固定された -- しかも実行は成功扱いなので、
数値がおかしいと気付くまで誰も見に行かない (原則2の静かなゼロ件)。この分岐が
無いのは書き忘れではない。**追加しないこと。** 座標
``inbox.next_page_control`` は「ページを進める操作」であって、
「進めるかどうかの判定」ではない。

もうひとつの帰結として、末尾を越えたページの行は **捨てる**
(:func:`keeps_rows`)。空ページや直前と同じページを収集結果に混ぜると、
同じ返信を二重に数えるか、行数だけが増えて分母が壊れる。
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from enum import StrEnum
from typing import Final

from jobmedley_scout.reply.extract import InboxRow

#: 署名の桁数。DBの ``signature_chain`` に並べて目視で比較するので、
#: 衝突しない範囲で短く保つ。
SIGNATURE_HEX_LENGTH: Final[int] = 32

#: 目印が1つも取れなかったページの署名。「末尾」を表す唯一の値。
EMPTY_SIGNATURE: Final[str] = ""

_MARKER_SEPARATOR: Final[str] = "\x1f"


class PageDecision(StrEnum):
    """What to do after fetching one page."""

    CONTINUE = "continue"
    PAST_END = "past_end"
    NOT_ADVANCING = "not_advancing"


def marker_signature(markers: Iterable[str]) -> str:
    """Hash a page's unique markers into a stable signature.

    目印は集合として扱う (重複除去 + 整列)。行の並び順は媒体側の都合で揺れる
    ことがあり、順序込みで署名すると **同じページを別ページと誤認して**
    上限ページ数まで回り続ける。

    ハッシュにするのは長さを揃えるためだけでなく、署名がそのままログとDBに
    残るため -- 件名や氏名を平文で残さない (13.2)。
    """
    unique = sorted({marker for marker in markers if marker.strip()})
    if not unique:
        return EMPTY_SIGNATURE
    joined = _MARKER_SEPARATOR.join(unique)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:SIGNATURE_HEX_LENGTH]


def page_signature(rows: Iterable[InboxRow]) -> str:
    """The signature of one page of inbox rows."""
    return marker_signature(row.signature_marker() for row in rows)


def decide_pagination(signature: str, history: Sequence[str]) -> PageDecision:
    """Decide whether to fetch another page, given this page's signature.

    ``history`` is the signatures of the pages already accepted, oldest first.

    引数はこの2つだけである。DOM由来の手掛かりを受け取らないこと自体が
    このモジュールの仕様 -- 冒頭の説明を参照 (10.5)。
    """
    if not signature:
        # 目印が1つも無い = 末尾を越えている。このページの行は使わない。
        return PageDecision.PAST_END
    if signature in history:
        # 直前と同じだけでなく、過去のいずれかと同じでも停止する。SPAのページャは
        # 進めなくなると1ページ目を返し続けることがあり、直前だけを見ていると
        # 1ページ目と2ページ目を交互に取り続けて終わらない。
        return PageDecision.NOT_ADVANCING
    return PageDecision.CONTINUE


def keeps_rows(decision: PageDecision) -> bool:
    """Whether the rows of the page that produced ``decision`` may be collected.

    ``CONTINUE`` のときだけ真。``PAST_END`` は末尾越え、``NOT_ADVANCING`` は
    既に取り込んだページの再取得なので、どちらも行を捨てる (10.5)。
    """
    return decision is PageDecision.CONTINUE


def stop_reason(decision: PageDecision) -> str | None:
    """The value to store in ``DetectionRun.stop_reason``; ``None`` while continuing.

    「進んでいないので止めた」のか「末尾に達したので止めた」のかを後から
    区別できるようにする。収集範囲のバグはグラフの形を見て初めて気付く。
    """
    return None if decision is PageDecision.CONTINUE else decision.value
