"""Foreign-native detection, scoped to the language field only.

7.2 の事故: 「ネイティブ」を **職務要約や職歴本文にも** 適用していたため、

* 「ネイティブ広告の運用経験」
* 「ネイティブアプリ開発」
* 「クラウドネイティブ環境への移行」

が軒並み一致し、日本人候補者を外国語ネイティブとして除外しかけた。対処として
複合語の除外リスト (「ネイティブ広告」「ネイティブアプリ」…) を足す案が出たが、
**適用範囲を語学欄だけに絞ったことで除外リスト自体が不要になった。** リストは
新しい複合語が生まれるたびに破れるが、適用範囲は破れない。したがって本モジュールに
denylist は **無い。足さないこと。**

その代わり「語学欄が実際に取得できているか」が新しい前提になる。取得できていない
(=``None``) ときに「日本語話者だろう」と推測してはならない。``None`` は
:class:`Determination.UNDETERMINABLE` であり、方針 (設定は ``include``) に委ねる。

7.3 は :mod:`jobmedley_scout.targeting.attribution` を参照。
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict

from jobmedley_scout.config.schema import ForeignLanguageConfig
from jobmedley_scout.models.text_norm import fold_width
from jobmedley_scout.targeting.attribution import Attribution, attribute_nearest, iter_spans
from jobmedley_scout.targeting.determination import Determination

_STRICT = ConfigDict(extra="forbid", frozen=True)

#: ひらがな・カタカナ・漢字。英語優勢判定の分母/分子に使う。
#: 範囲はエスケープで書く -- 生の文字で範囲を書くと、エディタや正規化で
#: 見た目の変わらない別文字に化けても誰も気づけない。
_JAPANESE = re.compile(r"[\u3040-\u309f\u30a0-\u30ff\u3400-\u4dbf\u4e00-\u9fff\u3005]")
_LATIN = re.compile(r"[A-Za-z]")
_NON_SPACE = re.compile(r"\S")


class ForeignNativeDetection(BaseModel):
    """Detection verdict plus the evidence behind it."""

    model_config = _STRICT

    determination: Determination
    evidence: str
    marker: str | None = None
    attribution: Attribution | None = None


def _normalize_for_match(text: str) -> str:
    """Fold width and case without changing character offsets meaningfully.

    語学欄は「英語」「English」「english」が混在する。設定のトークンと本文の
    **両方** を同じ関数に通すこと (8.6: 片方だけだと静かに不一致する)。
    空白は畳まない -- 畳むと距離が変わり、7.3 の距離比較の意味が動くため。
    """
    return fold_width(text).casefold()


def _marker_spans(text: str, markers: tuple[str, ...]) -> tuple[tuple[str, tuple[int, int]], ...]:
    found: list[tuple[str, tuple[int, int]]] = []
    for marker in markers:
        for span in iter_spans(text, _normalize_for_match(marker)):
            found.append((marker, span))
    # 出現位置順。evidence に出る「どの出現か」を安定させるため。
    return tuple(sorted(found, key=lambda item: item[1][0]))


def detect_foreign_native_detail(
    language_text: str | None, cfg: ForeignLanguageConfig
) -> ForeignNativeDetection:
    """Detect a foreign-native claim in the **language field** text.

    Never pass a summary or work-history body here -- see the module docstring.
    """
    if language_text is None:
        # 6.4: 写像が未確定なら空のまま。空を「日本語話者」と読み替えない (7.1)。
        return ForeignNativeDetection(
            determination=Determination.UNDETERMINABLE,
            evidence="語学欄が未取得 (レジュメのキー写像が未確定の可能性)",
        )
    text = _normalize_for_match(language_text)
    if not text.strip():
        # 空欄は「外国語ネイティブではない」の根拠にならない。記入していないだけ。
        return ForeignNativeDetection(
            determination=Determination.UNDETERMINABLE,
            evidence="語学欄が空",
        )

    markers = _marker_spans(text, cfg.native_markers)
    if not markers:
        # ここは判定できる: 語学欄が取れていて、ネイティブ表記が無い。
        return ForeignNativeDetection(
            determination=Determination.NO_MATCH,
            evidence="語学欄にネイティブ表記なし",
        )

    foreign = tuple(_normalize_for_match(token) for token in cfg.foreign_languages)
    japanese = tuple(_normalize_for_match(token) for token in cfg.japanese_tokens)
    attribution = attribute_nearest(
        text=text,
        marker_positions=tuple(span for _, span in markers),
        foreign_tokens=foreign,
        japanese_tokens=japanese,
        max_distance=cfg.proximity_max_distance,
    )
    marker_label = _marker_label(markers, attribution)
    if attribution.attributed_to_foreign:
        return ForeignNativeDetection(
            determination=Determination.MATCH,
            evidence=(
                f"「{marker_label}」の最近傍が外国語 "
                f"「{attribution.foreign_token}」(距離{attribution.foreign_distance}) "
                f"日本語側は{_side_label(attribution.japanese_token, attribution.japanese_distance)}"
            ),
            marker=marker_label,
            attribution=attribution,
        )
    return ForeignNativeDetection(
        determination=Determination.NO_MATCH,
        evidence=(
            f"「{marker_label}」は外国語に帰属しない "
            f"(日本語側={_side_label(attribution.japanese_token, attribution.japanese_distance)} / "
            f"外国語側={_side_label(attribution.foreign_token, attribution.foreign_distance)})"
        ),
        marker=marker_label,
        attribution=attribution,
    )


def _marker_label(
    markers: tuple[tuple[str, tuple[int, int]], ...], attribution: Attribution
) -> str:
    for label, span in markers:
        if span == attribution.marker_span:
            return label
    return markers[0][0]


def _side_label(token: str | None, distance: int | None) -> str:
    if token is None or distance is None:
        return "範囲内になし"
    return f"「{token}」(距離{distance})"


def detect_foreign_native(language_text: str | None, cfg: ForeignLanguageConfig) -> Determination:
    """Three-valued foreign-native verdict for the language field."""
    return detect_foreign_native_detail(language_text, cfg).determination


def count_latin_chars(text: str) -> int:
    """Number of Latin letters in ``text``."""
    return len(_LATIN.findall(fold_width(text)))


def japanese_char_ratio(text: str) -> float:
    """Ratio of Japanese characters among non-whitespace characters.

    分母を非空白文字にしているのは、記号や数字だけの行で比率が跳ねないようにする
    ため。空文字は 0.0 とする (「日本語ではない」ではなく「材料が無い」なので、
    呼び出し側が文字数条件と AND で使うことを前提にしている)。
    """
    folded = fold_width(text)
    total = len(_NON_SPACE.findall(folded))
    if total == 0:
        return 0.0
    return len(_JAPANESE.findall(folded)) / total


def is_english_dominant(text: str, cfg: ForeignLanguageConfig) -> bool:
    """Whether ``text`` is dominated by English.

    ラテン文字が ``latin_min_chars`` 文字以上 **かつ** 日本語文字比率が
    ``japanese_ratio_threshold`` 未満。

    **これは :func:`detect_foreign_native` に接続していない。** 接続すると
    「英語で書かれた語学欄」= 外国語ネイティブ という別経路の誤検知を作ることに
    なり、7.2 で適用範囲を絞った意味が消える。補助シグナルが必要な呼び出し側
    (偵察・レポート) が明示的に使うこと。
    """
    return count_latin_chars(text) >= cfg.latin_min_chars and (
        japanese_char_ratio(text) < cfg.japanese_ratio_threshold
    )
