"""Every rule must answer UNDETERMINABLE when its input is missing (7.1).

現状のジョブメドレーはレジュメのキー写像が1つも確定していないため、これは
理論上の話ではなく **今の本番の入力そのもの** である。どれか1つでも「値が無い
から合格」と答えるルールがあれば、その日から静かな誤送信が始まる。
"""

from __future__ import annotations

import pytest

from jobmedley_scout.config.schema import UndeterminablePolicy
from jobmedley_scout.targeting.determination import Determination
from jobmedley_scout.targeting.filter import apply_targeting
from jobmedley_scout.targeting.registry import ALL_RULE_IDS, ALL_RULES, Rule
from tests.targeting.factories import make_candidate, make_targeting_config


@pytest.mark.parametrize(("rule_id", "rule"), ALL_RULES, ids=[r for r, _ in ALL_RULES])
def test_rule_is_undeterminable_on_an_empty_resume(rule_id: str, rule: Rule) -> None:
    outcome = rule(make_candidate(), make_targeting_config())
    assert outcome.determination is Determination.UNDETERMINABLE, outcome.evidence
    assert outcome.rule_id == rule_id
    assert outcome.evidence, "判定不能の理由が空だと運用者が原因を追えない"


def test_empty_resume_leaves_every_rule_undeterminable() -> None:
    result = apply_targeting(make_candidate(), make_targeting_config())
    assert result.undeterminable_rules == ALL_RULE_IDS
    assert result.matched_values == ()
    # exclude 方針のルールが1つでもあれば対象外になる。
    assert result.is_target is False


def test_undeterminable_exclusions_are_reported_as_rejection_reasons() -> None:
    """「判定不能で除外した」ことが理由として残らないと、7.1 の再発を検知できない。

    以前は4ルール分をまとめて確認していたが、それらは削除された。
    **消えた行を .get() などで生き延びさせず、走っているルールで書き直す。**
    通っているだけの緑のアサーションは、消えたアサーションより悪い。
    """
    result = apply_targeting(make_candidate(), make_targeting_config())
    reasons = "\n".join(result.rejection_reasons)

    assert "membership_status" in reasons
    assert "判定不能" in reasons
    # 方針も理由文に出る (どちら向きの判断で落ちたのかが後から分かるように)。
    assert "方針=exclude" in reasons


def test_all_undeterminable_and_all_include_makes_a_target() -> None:
    # 方針を全部 include にすると、何も分からない候補者が対象になってしまう。
    # これは設定の帰結であって実装のバグではない -- その非対称が YAML の diff に
    # 出ることが 7.1 の狙いなので、挙動として明示的に固定しておく。
    cfg = make_targeting_config(
        undeterminable_policy=dict.fromkeys(ALL_RULE_IDS, UndeterminablePolicy.INCLUDE)
    )
    result = apply_targeting(make_candidate(), cfg)
    assert result.is_target is True
    assert result.undeterminable_rules == ALL_RULE_IDS
