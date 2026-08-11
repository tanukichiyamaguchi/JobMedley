"""原則2 -- 最も危険な失敗は例外ではなく「静かなゼロ件」。

判定表を1件ずつ表明する。とくに ``(targets>0, sent=0, failures=0)`` は **全滅では
ない** (全件が正当に除外された日と区別できない) が、要注意としては必ず出す。
"""

from __future__ import annotations

import pytest

from jobmedley_scout.errors import WipeoutDetected
from jobmedley_scout.state.wipeout import detect_wipeout, raise_if_wipeout


def test_targets_with_no_sends_and_a_failure_is_a_wipeout() -> None:
    verdict = detect_wipeout(targets=20, sent=0, failures=20)
    assert verdict.detected is True
    assert "静かなゼロ件" in verdict.reason


def test_one_target_one_failure_zero_sent_is_already_a_wipeout() -> None:
    """閾値は「1件でも」。件数を待ってから騒ぐ設計にすると数日気づかれない (6.6)。"""
    assert detect_wipeout(targets=1, sent=0, failures=1).detected is True


def test_targets_with_no_sends_and_no_failures_is_not_a_wipeout() -> None:
    """全件が除外条件に当たった正常な日と区別できないので、異常にはしない。"""
    verdict = detect_wipeout(targets=20, sent=0, failures=0)
    assert verdict.detected is False
    # ただし「対象があるのに0件送信」は運用上の注意対象として必ず報告する (12.5)。
    assert verdict.noteworthy is True
    assert "除外条件" in verdict.reason


def test_any_successful_send_rules_out_a_wipeout() -> None:
    verdict = detect_wipeout(targets=20, sent=1, failures=19)
    assert verdict.detected is False
    assert verdict.noteworthy is False


def test_no_targets_is_not_a_wipeout() -> None:
    for failures in (0, 3):
        verdict = detect_wipeout(targets=0, sent=0, failures=failures)
        assert verdict.detected is False
        assert verdict.noteworthy is False


def test_the_verdict_carries_the_counts_for_the_report() -> None:
    verdict = detect_wipeout(targets=7, sent=0, failures=7)
    assert (verdict.targets, verdict.sent, verdict.failures) == (7, 0, 7)
    assert "対象7件・送信0件・失敗7件" in verdict.describe()


def test_a_detected_wipeout_raises_and_a_clean_one_does_not() -> None:
    with pytest.raises(WipeoutDetected) as excinfo:
        raise_if_wipeout(detect_wipeout(targets=5, sent=0, failures=5))
    assert "対象5件" in str(excinfo.value)  # 件数を添えたまま落とす (12.5)
    raise_if_wipeout(detect_wipeout(targets=5, sent=5, failures=0))


def test_negative_counts_are_rejected() -> None:
    with pytest.raises(ValueError):
        detect_wipeout(targets=-1, sent=0, failures=0)
    with pytest.raises(ValueError):
        detect_wipeout(targets=1, sent=-1, failures=0)
    with pytest.raises(ValueError):
        detect_wipeout(targets=1, sent=0, failures=-1)


@pytest.mark.parametrize(
    ("targets", "sent", "failures", "expected"),
    [
        (0, 0, 0, False),
        (0, 0, 1, False),
        (1, 0, 0, False),
        (1, 0, 1, True),
        (1, 1, 0, False),
        (1, 1, 1, False),
        (5, 0, 2, True),
        (5, 2, 3, False),
    ],
)
def test_the_full_matrix(targets: int, sent: int, failures: int, expected: bool) -> None:
    assert detect_wipeout(targets, sent, failures).detected is expected
