"""7.3: nearest-token attribution, language-independent."""

from __future__ import annotations

from jobmedley_scout.targeting.attribution import attribute_nearest, iter_spans, span_distance

FOREIGN = ("f",)
JAPANESE = ("j",)


def _attribute(text: str, max_distance: int = 10) -> bool:
    markers = tuple(iter_spans(text, "M"))
    return attribute_nearest(text, markers, FOREIGN, JAPANESE, max_distance).attributed_to_foreign


def test_foreign_must_be_strictly_nearer() -> None:
    # "fM j": 外国語まで0、日本語まで1。
    assert _attribute("fM j") is True


def test_a_tie_falls_to_japanese() -> None:
    # "f M j": どちらも距離1。**安全側に倒す** = 日本語に帰属させ、除外しない。
    assert _attribute("f M j") is False


def test_japanese_nearer_is_not_foreign() -> None:
    assert _attribute("f jM") is False


def test_only_foreign_within_range_is_foreign() -> None:
    assert _attribute("f M") is True


def test_nothing_within_range_is_not_foreign() -> None:
    result = attribute_nearest("...M...", tuple(iter_spans("...M...", "M")), FOREIGN, JAPANESE, 10)
    assert result.attributed_to_foreign is False
    assert result.winning_token is None
    assert result.winning_distance is None


def test_max_distance_is_enforced() -> None:
    # 上限が無いと長文では必ず何かに当たる (7.3)。
    assert _attribute("f" + " " * 10 + "M") is True
    assert _attribute("f" + " " * 11 + "M") is False


def test_any_marker_occurrence_may_trigger() -> None:
    # 2つ目の「ネイティブ」だけが外国語に隣接している場合も検知すること。
    assert _attribute("jM ... fM") is True


def test_result_names_the_winning_token_and_distance() -> None:
    text = "fM j"
    result = attribute_nearest(text, tuple(iter_spans(text, "M")), FOREIGN, JAPANESE, 10)
    assert result.attributed_to_foreign is True
    assert result.winning_token == "f"
    assert result.winning_distance == 0
    # 反対側の距離も残す -- 同点だったのか大差だったのかを運用者が読めるように。
    assert result.japanese_distance == 1


def test_distance_is_the_gap_not_the_offset() -> None:
    # 始点差で測ると長い語が不利になる。隙間で測る。
    assert span_distance((0, 6), (7, 8)) == 1
    assert span_distance((7, 8), (0, 6)) == 1
    assert span_distance((0, 6), (3, 8)) == 0


def test_empty_token_never_matches_everything() -> None:
    # 設定の書き損じで空文字が混ざっても「全位置に一致」にはしない。
    assert list(iter_spans("abc", "")) == []
