"""渡した住所が本文に出ないこと。

運用者の指示は「住所・アクセスは必要ないです」だった。渡さなければ守られるが、
渡さないとプロンプトの STEP1 (通勤時間の見立て) が走らない。**渡したうえで
守らせる** -- 保証はプロンプトの文言ではなく検査が持つ (8.5)。
"""

from __future__ import annotations

import pytest

from jobmedley_scout.generation.clinic import load_clinic_facts
from jobmedley_scout.generation.scout_body import (
    APPLY_BUTTON,
    HEADLINE,
    BodyViolationKind,
    address_markers,
    validate_body,
)
from tests.generation.test_clinic_facts import CLINIC_PATH

CODE = "01613058"


def _body(extra: str) -> str:
    """検査を通る最小の本文に、試したい一文だけを足す。"""
    filler = "当院は患者担当制です。" * 50
    return (
        f"{HEADLINE}\n{CODE}様（システム上お名前が表示されず、会員番号での"
        f"ご挨拶となる失礼をお許しください）\n{filler}{extra}\n"
        f"ご興味をお持ちいただけましたら、{APPLY_BUTTON}ボタンを押してください。\n"
        f"ヤガサキ歯科医院 院長 矢ケ崎 隆信"
    )


REAL_ADDRESS = "〒214-0001 神奈川県川崎市多摩区菅４丁目３−３２ ２階"


def test_the_markers_are_the_postal_code_and_the_street_number_only() -> None:
    """**市区町村は含めない。** 通勤の話で自然に出るし、それは書きすぎではない。"""
    markers = address_markers(REAL_ADDRESS)
    assert "214-0001" in markers
    assert "2140001" in markers
    assert "4丁目3-32" in markers
    assert not any("多摩区" in m or "川崎" in m or "神奈川" in m for m in markers)


def test_the_postal_code_is_not_mistaken_for_a_street_number() -> None:
    """``214-0001`` は ``\\d+-\\d+`` にも見える。**先に取り除いてある。**"""
    assert "0001" not in address_markers("〒214-0001 神奈川県川崎市多摩区菅４丁目３−３２")


@pytest.mark.parametrize(
    "written",
    [
        "当院は〒214-0001にあります。",
        "当院は2140001です。",
        "住所は菅4丁目3-32です。",
        "住所は菅４丁目３−３２です。",  # 全角でもすり抜けない
        "住所は菅４丁目３ー３２です。",  # 長音記号をハイフンに使われても
        "住所は 菅 4丁目 3-32 です。",  # 空白で割られても
    ],
)
def test_the_address_written_into_the_body_is_caught(written: str) -> None:
    """**表記の揺れですり抜けない。** 全角・長音記号・空白を揃えてから見る。"""
    kinds = [
        v.kind for v in validate_body(_body(written), member_code=CODE, clinic_address=REAL_ADDRESS)
    ]
    assert BodyViolationKind.STREET_ADDRESS_LEAKED in kinds


def test_the_commute_sentence_the_prompt_asks_for_is_not_a_violation() -> None:
    """**STEP1 が書かせる文が落ちてはいけない。**

    市区町村を禁じると、プロンプトが必ず書けと言っている通勤の一文が毎回
    違反になる。禁じているのは番地まで書き込むことである。
    """
    ok = "宮前区からですと、お車でおよそ30分から40分前後かと思います。"
    kinds = [
        v.kind for v in validate_body(_body(ok), member_code=CODE, clinic_address=REAL_ADDRESS)
    ]
    assert BodyViolationKind.STREET_ADDRESS_LEAKED not in kinds
    assert not kinds, kinds


def test_no_address_configured_means_no_address_check() -> None:
    """空文字なら検査しない。**検査対象が無いのに落とさない。**"""
    kinds = [v.kind for v in validate_body(_body("菅4丁目3-32"), member_code=CODE)]
    assert BodyViolationKind.STREET_ADDRESS_LEAKED not in kinds


def test_the_evidence_is_the_marker_not_the_whole_body() -> None:
    """13.2: 違反の証拠に本文そのものを入れない。"""
    found = [
        v
        for v in validate_body(_body("〒214-0001"), member_code=CODE, clinic_address=REAL_ADDRESS)
        if v.kind is BodyViolationKind.STREET_ADDRESS_LEAKED
    ]
    assert found[0].evidence == "214-0001"
    assert CODE not in found[0].evidence


def test_the_real_configured_address_is_what_gets_checked() -> None:
    """設定の実物で通ること。**試験用の住所だけで通しても意味がない。**"""
    configured = load_clinic_facts(CLINIC_PATH)["CLINIC_ADDRESS"]
    assert "4丁目3-32" in address_markers(configured)
