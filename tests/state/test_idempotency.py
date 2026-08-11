"""9.2 -- the most important unit test in ``state/``.

冪等キーを再利用してよいのは前回が ``SENDING`` (送ったか不明) のときだけ。
* 再利用しなさすぎ → 前回が実は届いていた場合に二重送信
* 再利用しすぎ    → 正当な再試行がサーバの重複排除に弾かれて永久に未送信
どちらも実害が出るので、状態ごとに1件ずつ表明する。
"""

from __future__ import annotations

import uuid

import pytest

from jobmedley_scout.models.send_record import SendStatus
from jobmedley_scout.state.idempotency import (
    REUSABLE_STATUSES,
    KeyDecision,
    decide_key,
    new_idempotency_key,
    plan_key,
)
from tests.state.factories import make_send_record


def test_sending_is_the_only_status_that_reuses_the_key() -> None:
    """「送信中」は「送信済み」ではない。送ったか不明なので同じキーで送り直す。"""
    prior = make_send_record(status=SendStatus.SENDING)
    assert decide_key(prior) is KeyDecision.REUSE


@pytest.mark.parametrize(
    "status",
    [SendStatus.GENERATED, SendStatus.SENT, SendStatus.FAILED, SendStatus.SKIPPED],
    ids=lambda s: str(s),
)
def test_every_other_prior_status_issues_a_new_key(status: SendStatus) -> None:
    """確定した状態からは新しいキー -- 正当な再試行がサーバに弾かれないため。"""
    assert decide_key(make_send_record(status=status)) is KeyDecision.NEW


def test_no_prior_record_issues_a_new_key() -> None:
    assert decide_key(None) is KeyDecision.NEW


def test_the_reusable_set_contains_only_sending() -> None:
    """集合そのものを表明する。ここに ``SENT`` を足すことは二重送信を許すこと。"""
    assert frozenset({SendStatus.SENDING}) == REUSABLE_STATUSES


def test_every_status_is_covered_by_the_decision() -> None:
    """状態が増えたときに判定漏れで落ちるようにしておく (7.1: 黙って通さない)。"""
    for status in SendStatus:
        decision = decide_key(make_send_record(status=status))
        expected = KeyDecision.REUSE if status is SendStatus.SENDING else KeyDecision.NEW
        assert decision is expected, status


def test_plan_reuses_the_stored_key_verbatim_when_sending() -> None:
    prior = make_send_record(status=SendStatus.SENDING, idempotency_key="key-from-last-run")
    plan = plan_key(prior, fresh_key="key-brand-new")
    assert plan.decision is KeyDecision.REUSE
    assert plan.reused is True
    # 前回そのままのキーでなければサーバ側の重複排除が効かない。
    assert plan.key == "key-from-last-run"
    assert "重複排除" in plan.reason


@pytest.mark.parametrize(
    "status",
    [SendStatus.GENERATED, SendStatus.FAILED, SendStatus.SKIPPED, SendStatus.SENT],
    ids=lambda s: str(s),
)
def test_plan_issues_the_fresh_key_from_settled_states(status: SendStatus) -> None:
    prior = make_send_record(status=status, idempotency_key="key-from-last-run")
    plan = plan_key(prior, fresh_key="key-brand-new")
    assert plan.decision is KeyDecision.NEW
    assert plan.key == "key-brand-new"
    assert str(status) in plan.reason


def test_plan_without_a_prior_record_uses_the_fresh_key() -> None:
    plan = plan_key(None, fresh_key="key-brand-new")
    assert plan.decision is KeyDecision.NEW
    assert plan.key == "key-brand-new"


def test_the_decision_itself_needs_no_randomness() -> None:
    """判定は純粋。乱数は :func:`new_idempotency_key` の一箇所に閉じ込めてある。"""
    prior = make_send_record(status=SendStatus.FAILED)
    first = plan_key(prior, fresh_key="deterministic")
    second = plan_key(prior, fresh_key="deterministic")
    assert first == second


def test_generated_keys_are_distinct_uuid4() -> None:
    keys = {new_idempotency_key() for _ in range(64)}
    assert len(keys) == 64
    for key in keys:
        assert uuid.UUID(key).version == 4
