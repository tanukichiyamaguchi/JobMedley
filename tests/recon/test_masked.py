"""伏せ字を **値として扱わない**。

2026-08-22、運用者の実画面のレジュメで分かった。**欄は在るが値が伏せられている
項目がある。**

::

    氏名(ふりがな)   （未応募のため非表示）
    電話番号        （未応募のため非表示）
    自己PR          未入力

素直に写せば ``display_name`` に「（未応募のため非表示）」が入る。空ではないので
:mod:`generation.facts` は「非公開」に落とさず値として渡し、モデルはそれを名前と
見なして「（未応募のため非表示）様」と書く。取り消せない (13.6)。
"""

from __future__ import annotations

import pytest

from jobmedley_scout.recon.masked import is_withheld, unmask, unmask_all


@pytest.mark.parametrize(
    "value",
    [
        "（未応募のため非表示）",
        "(未応募のため非表示)",
        "未応募のため非表示",
        "未入力",
        "（未入力）",
        "非公開",
        "非表示",
        "-",
        "ー",
        "",
        "   ",
        "　",
    ],
)
def test_the_platforms_withheld_markers_are_absence(value: str) -> None:
    assert is_withheld(value)
    assert unmask(value) is None


@pytest.mark.parametrize(
    "value",
    [
        "歯科衛生士",
        "神奈川県川崎市多摩区",
        "歯科衛生士科（専門学校）",
        # **部分一致にしていない。** 「非表示」を含む正当な自由記述を消さない。
        "前職では非表示設定の運用を担当していました",
        "未入力欄の設計を担当",
        "24歳",
    ],
)
def test_a_real_value_survives(value: str) -> None:
    assert not is_withheld(value)
    assert unmask(value) == value


def test_none_stays_none_and_is_not_called_withheld() -> None:
    """``None`` は「媒体が伏せた」ではなく「そもそも来ていない」。

    区別を潰すと、報告が「伏せられている」と「取れていない」を混ぜる。
    """
    assert is_withheld(None) is False
    assert unmask(None) is None


def test_a_list_drops_only_the_withheld_entries() -> None:
    assert unmask_all(("歯科衛生士", "未入力", "自動車運転免許", "-")) == (
        "歯科衛生士",
        "自動車運転免許",
    )


def test_the_marker_list_is_what_was_actually_seen() -> None:
    """**推測で足さない** (原則3)。

    ここに無い表記はそのまま値として通るが、それは「観測していないものを
    勝手に消さない」という正しい既定である。
    """
    from jobmedley_scout.recon.masked import WITHHELD_MARKERS

    assert "未応募のため非表示" in WITHHELD_MARKERS
    assert "未入力" in WITHHELD_MARKERS


def test_the_exact_string_from_the_screen_never_reaches_the_model() -> None:
    """**「（未応募のため非表示）様」を送らない。**

    実画面の氏名欄の値をそのまま取り込み経路へ流したときに、モデルへ渡る facts
    が「非公開」になることを端から端まで固定する。
    """
    from jobmedley_scout.generation.facts import UNDISCLOSED, build_facts
    from jobmedley_scout.models.candidate import Candidate

    observed_name = "（未応募のため非表示）"
    candidate = Candidate(
        candidate_id="01613058",
        raw_id_observed="01613058",
        display_name=unmask(observed_name),
    )
    assert candidate.display_name is None
    facts = build_facts(candidate)
    assert facts.display_name.rendered == UNDISCLOSED
    assert observed_name not in facts.render_for_prompt()
