"""Nearest-token attribution for a marker word.

7.3 の要点: **共起では判定できない。** 語学欄で最も多い日本語話者の書き方は

    日本語：ネイティブ、英語：ビジネスレベル

であり、「ネイティブ」と「英語」は同じ欄に共起する。共起だけを見ると日本人が
外国語ネイティブとして除外される。そこで「ネイティブ」の各出現について、
**最も近い外国語トークンと最も近い日本語トークンの距離を比べる。**

本モジュールは言語に依存しない (トークンは引数で受け取る)。語学欄という適用範囲の
限定は :mod:`jobmedley_scout.targeting.language` 側の責務である。
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

from pydantic import BaseModel, ConfigDict

_STRICT = ConfigDict(extra="forbid", frozen=True)

#: 探索範囲の外を表す値。``None`` で「見つからなかった」を表す。
Span = tuple[int, int]


class Attribution(BaseModel):
    """Which side a marker occurrence was attributed to, and why.

    ``winning_token`` と ``winning_distance`` は evidence 文字列の材料である。
    「なぜ除外したのか / なぜ除外しなかったのか」を運用者が1行で読めるようにする。
    """

    model_config = _STRICT

    attributed_to_foreign: bool
    #: 報告対象にした「ネイティブ」出現位置。どのトークンも範囲内に無ければ ``None``。
    marker_span: Span | None = None
    foreign_token: str | None = None
    foreign_distance: int | None = None
    japanese_token: str | None = None
    japanese_distance: int | None = None

    @property
    def winning_token(self) -> str | None:
        """The token the marker was attributed to, if any."""
        return self.foreign_token if self.attributed_to_foreign else self.japanese_token

    @property
    def winning_distance(self) -> int | None:
        """Distance in characters to :attr:`winning_token`."""
        return self.foreign_distance if self.attributed_to_foreign else self.japanese_distance


def iter_spans(text: str, token: str) -> Iterator[Span]:
    """Yield ``(start, end)`` for every occurrence of ``token`` in ``text``."""
    if not token:
        # 空トークンは全位置に一致してしまう。設定の書き損じを黙って
        # 「全部ネイティブ」に変えないため、ここで捨てる。
        return
    start = text.find(token)
    while start != -1:
        yield (start, start + len(token))
        start = text.find(token, start + 1)


def span_distance(left: Span, right: Span) -> int:
    """Number of characters strictly between two spans (0 when they touch/overlap).

    始点同士の差ではなく **隙間** で測る。長い語 (「ポルトガル語」) が短い語
    (「国語」) より不利になるのを避けるため。
    """
    if left[1] <= right[0]:
        return right[0] - left[1]
    if right[1] <= left[0]:
        return left[0] - right[1]
    return 0


def _token_spans(text: str, tokens: Sequence[str]) -> tuple[tuple[str, Span], ...]:
    return tuple((token, span) for token in tokens for span in iter_spans(text, token))


def _drop_contained(
    spans: tuple[tuple[str, Span], ...], others: tuple[tuple[str, Span], ...]
) -> tuple[tuple[str, Span], ...]:
    """Drop occurrences that are merely a substring of a longer opposing token.

    「中国語」「韓国語」は日本語トークン「国語」を部分文字列として含む。素朴に
    数えると「中国語：ネイティブ」で日本語側にも距離0の一致が立ち、同点扱いで
    日本語に帰属してしまう -- 中国語ネイティブを取りこぼす。より長い語の内側に
    完全に収まる一致は、その語の一部であって独立した出現ではない
    (8.3 対策3 の「営業」が「法人営業」に一致した事故と同じ形)。
    """
    kept: list[tuple[str, Span]] = []
    for token, span in spans:
        contained = any(
            other[0] <= span[0] and span[1] <= other[1] and (other[1] - other[0]) > len(token)
            for _, other in others
        )
        if not contained:
            kept.append((token, span))
    return tuple(kept)


def _nearest(
    marker: Span, spans: tuple[tuple[str, Span], ...], max_distance: int
) -> tuple[str | None, int | None]:
    """Nearest occurrence to ``marker`` within ``max_distance``."""
    best_token: str | None = None
    best_distance: int | None = None
    for token, span in spans:
        distance = span_distance(marker, span)
        # 7.3: 上限が無いと長文では必ず何かに当たる。範囲外は
        # 「近くに無い」であって「遠くにある」ではないので、候補にしない。
        if distance > max_distance:
            continue
        if best_distance is None or distance < best_distance:
            best_token = token
            best_distance = distance
    return best_token, best_distance


def attribute_nearest(
    text: str,
    marker_positions: Sequence[Span],
    foreign_tokens: Sequence[str],
    japanese_tokens: Sequence[str],
    max_distance: int,
) -> Attribution:
    """Attribute the marker occurrences to the foreign side or the Japanese side.

    Returns an attribution whose ``attributed_to_foreign`` is true only when some
    marker occurrence has a foreign token *strictly* nearer than every Japanese
    token within ``max_distance``.

    同点は日本語に倒す (**安全側に倒す**)。「英語：ネイティブ、日本語：日常会話」の
    ように両側が等距離になる書き方も同点として日本語側になり、外国語ネイティブを
    取りこぼす (=送ってしまう) 方向に落ちる。これは意図した非対称である --
    7.2 の事故は「日本人を誤って除外した」ことであり、こちらの誤りの方が高くつく。
    """
    raw_foreign = _token_spans(text, foreign_tokens)
    raw_japanese = _token_spans(text, japanese_tokens)
    # 部分文字列の一致 (「中国語」の中の「国語」) を先に落としてから距離を測る。
    foreign_spans = _drop_contained(raw_foreign, raw_japanese)
    japanese_spans = _drop_contained(raw_japanese, raw_foreign)

    fallback: Attribution | None = None
    for marker in marker_positions:
        foreign_token, foreign_distance = _nearest(marker, foreign_spans, max_distance)
        japanese_token, japanese_distance = _nearest(marker, japanese_spans, max_distance)
        # 厳密に近いときだけ外国語側。等距離・日本語のみ・両方不在はすべて日本語側。
        is_foreign = foreign_distance is not None and (
            japanese_distance is None or foreign_distance < japanese_distance
        )
        attribution = Attribution(
            attributed_to_foreign=is_foreign,
            marker_span=marker,
            foreign_token=foreign_token,
            foreign_distance=foreign_distance,
            japanese_token=japanese_token,
            japanese_distance=japanese_distance,
        )
        if is_foreign:
            # 1箇所でも外国語に帰属すれば外国語ネイティブとみなす。
            return attribution
        if fallback is None and (foreign_token is not None or japanese_token is not None):
            # 報告用に「最初に何かが近くにあった出現」を残す。判定は変わらない。
            fallback = attribution
    return fallback if fallback is not None else Attribution(attributed_to_foreign=False)
