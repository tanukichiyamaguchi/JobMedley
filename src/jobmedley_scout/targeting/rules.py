"""The targeting rules. Pure predicates over a candidate and the config.

Every rule has the same shape ``(candidate, cfg) -> RuleOutcome`` and returns a
three-valued :class:`Determination` (7.1)。``MATCH`` は常に「この候補者はこの
ルールを満たす」を意味する。

rule_id は ``config/config.yaml`` の ``targeting.undeterminable_policy`` のキーと
一致していなければならない。一致の検査は
:func:`jobmedley_scout.targeting.registry.assert_policies_complete`。

なぜルールが1本しか無いのか
---------------------------

2026-08-12、運用者の判断で **年齢・学歴・勤続年数・転職回数・外国語ネイティブ・
海外大学の各ルールを全廃した**。それらは指示書15章に載っていたビズリーチ参照実装の
値をそのまま持ち込んだもので、本番の対象職種 (保育士・栄養士・調理師・事務) とは
母集団が違う。「大学卒以上」を残せば対象がほぼ消えるなど、**引き継いだこと自体が
事故** だった。

対象の定義は媒体側の検索条件 (座標 ``nav.candidate_list_url``) が持つ。年齢も
検索セットごとに変わるため、こちらでは絞らない。**情報源を1つにするための削除で
あり、緩めたのではない。**

閾値を寛容な値へ書き換える (``age_min: 0``、``job_change_threshold: 999``) 形は
採らなかった。それは 7.6 の事故そのもので、設定を読んだ人に「意図して外した」と
「打鍵ミス」の区別が付かず、外したはずのルールが除外レポートと監査出力に
**有効な安全弁として出続ける**。フィールドごと消せば、無いものは無いとしか
読めない。

判定ロジックと経緯は git に残っている。復活させる場合は ``rules.py`` と
``undeterminable_policy`` の **両方** に足すこと -- 片方だけでは
:func:`~jobmedley_scout.targeting.registry.assert_policies_complete` が起動時に
落ちる (それが仕様である)。
"""

from __future__ import annotations

from typing import Final

from jobmedley_scout.config.schema import TargetingConfig
from jobmedley_scout.models.candidate import Candidate
from jobmedley_scout.models.text_norm import normalize_identifier
from jobmedley_scout.targeting.determination import (
    RuleOutcome,
    matched,
    not_matched,
    undeterminable,
)

RULE_MEMBERSHIP_STATUS: Final = "membership_status"


def rule_membership_status(
    candidate: Candidate,
    cfg: TargetingConfig,
    *,
    qualifying: tuple[str, ...] | None = None,
) -> RuleOutcome:
    """Membership status is one of the qualifying values.

    ``qualifying`` は **媒体座標であり、まだ確定していない**。実値を観測して
    いない以上、コードにも設定にも列挙しない (原則3・6.4)。``None`` のまま
    呼ばれたら UNDETERMINABLE を返す -- 推測した値と比較して「不一致だから除外」と
    するのが最悪の振る舞いであり、既定値を置かないことでそれを構造的に防ぐ。
    """
    del cfg  # 会員ステータスの対象値は設定ではなく座標側にある。
    if qualifying is None:
        return undeterminable(
            RULE_MEMBERSHIP_STATUS,
            evidence="対象となる会員ステータスの値が未確定 (媒体座標)",
        )
    status = candidate.resume.membership_status
    if status is None or not status.strip():
        return undeterminable(RULE_MEMBERSHIP_STATUS, evidence="会員ステータスが未取得")
    normalized = normalize_identifier(status)
    wanted = {normalize_identifier(value) for value in qualifying}
    if normalized in wanted:
        return matched(
            RULE_MEMBERSHIP_STATUS,
            evidence=f"会員ステータス '{status}' は対象",
            matched_values=(status,),
        )
    return not_matched(RULE_MEMBERSHIP_STATUS, evidence=f"会員ステータス '{status}' は対象外")
