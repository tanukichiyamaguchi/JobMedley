"""12.5: リトライ方針は層ごとに違う。**送信APIにはリトライしない。**

「親切にリトライを足す」は二重送信事故に直結する。方針をデータとして固定し、
送信APIについては ``should_retry`` が **呼ばれた時点で例外** になることを表明する。
"""

from __future__ import annotations

import pytest

from jobmedley_scout.errors import PermanentAuthError, SendFailed, TransientError
from jobmedley_scout.runtime.retry import (
    LLM_POLICY,
    PLATFORM_READ_POLICY,
    SAAS_WRITE_POLICY,
    SEND_API_POLICY,
    NoRetry,
    RetryForbidden,
    RetryPolicy,
    RetryTrigger,
    backoff_seconds,
    classify_status,
    should_retry,
)

NAMED_POLICIES = (LLM_POLICY, SAAS_WRITE_POLICY, PLATFORM_READ_POLICY)


# --- 送信API: 呼んだら止まる ------------------------------------------------
def test_should_retry_raises_for_the_send_policy() -> None:
    """将来の呼び出し側が黙って送信をリトライ対象にできないようにする。

    「呼べるが常に False」だと、条件分岐を1つ足すだけでリトライが有効化される。
    """
    with pytest.raises(RetryForbidden):
        should_retry(SEND_API_POLICY, attempt=1, status=500)


def test_backoff_also_raises_for_the_send_policy() -> None:
    """待ち時間を計算できてしまうと、リトライ実装の半分が出来上がってしまう。"""
    with pytest.raises(RetryForbidden):
        backoff_seconds(SEND_API_POLICY, attempt=1)


def test_send_policy_is_a_single_attempt_and_says_why() -> None:
    assert isinstance(SEND_API_POLICY, NoRetry)
    assert SEND_API_POLICY.max_attempts == 1
    assert SEND_API_POLICY.retry_on == frozenset()
    assert "二重送信" in SEND_API_POLICY.reason


def test_a_no_retry_policy_cannot_be_declared_with_more_attempts() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        NoRetry(
            name="sneaky",
            max_attempts=2,
            backoff_base_seconds=1.0,
            retry_on=frozenset(),
            reason="送信をこっそりリトライしようとした",
        )


# --- SaaS書き込み: 429 と 5xx のみ ------------------------------------------
@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_saas_write_retries_rate_limits_and_server_errors(status: int) -> None:
    assert should_retry(SAAS_WRITE_POLICY, attempt=1, status=status) is True


@pytest.mark.parametrize("status", [400, 403, 404, 409, 422])
def test_saas_write_never_retries_client_errors(status: int) -> None:
    """4xx は要求そのものが誤り。再試行しても直らず、二重書き込みの危険だけ残る。"""
    assert should_retry(SAAS_WRITE_POLICY, attempt=1, status=status) is False


def test_saas_write_does_not_retry_on_success() -> None:
    assert should_retry(SAAS_WRITE_POLICY, attempt=1, status=200) is False


def test_no_named_policy_retries_client_errors() -> None:
    """4xx を再試行する方針は宣言の時点で存在しない。"""
    assert all(RetryTrigger.CLIENT_ERROR not in policy.retry_on for policy in NAMED_POLICIES)


def test_declaring_a_policy_that_retries_4xx_is_rejected() -> None:
    with pytest.raises(ValueError, match="4xx"):
        RetryPolicy(
            name="dangerous",
            max_attempts=3,
            backoff_base_seconds=1.0,
            retry_on=frozenset({RetryTrigger.CLIENT_ERROR}),
            reason="4xx も再試行してみたかった",
        )


# --- 試行回数 ---------------------------------------------------------------
def test_attempts_are_exhausted_at_max_attempts() -> None:
    assert should_retry(SAAS_WRITE_POLICY, attempt=2, status=503) is True
    assert should_retry(SAAS_WRITE_POLICY, attempt=3, status=503) is False


def test_attempt_numbering_starts_at_one() -> None:
    with pytest.raises(ValueError, match="1始まり"):
        should_retry(SAAS_WRITE_POLICY, attempt=0, status=503)


# --- LLM: 指数バックオフ ----------------------------------------------------
def test_llm_backoff_grows_exponentially() -> None:
    """副作用が無いので指数バックオフを許す層。"""
    base = LLM_POLICY.backoff_base_seconds

    assert backoff_seconds(LLM_POLICY, 1) == base
    assert backoff_seconds(LLM_POLICY, 2) == base * 2
    assert backoff_seconds(LLM_POLICY, 3) == base * 4


def test_backoff_does_not_sleep() -> None:
    """純粋関数。眠るのは browser/waits.py だけ (5.2)。

    テストが実時間ぶん遅くなると、やがて誰も回さなくなる。
    """
    assert isinstance(backoff_seconds(LLM_POLICY, 5), float)


def test_llm_retries_transient_exceptions() -> None:
    assert should_retry(LLM_POLICY, attempt=1, exc=TransientError("接続断")) is True


# --- 例外による判断 ---------------------------------------------------------
def test_permanent_errors_are_never_retried() -> None:
    """認証切れを再試行すると、媒体側にロック要因を作りかねない (6.6)。"""
    assert should_retry(LLM_POLICY, attempt=1, exc=PermanentAuthError("失効", status=401)) is False


def test_status_beats_the_exception_type() -> None:
    """通信層が 404 を TransientError に包んでも、404 は再試行してはならない。"""
    wrapped = SendFailed("404 を一時エラーとして包んだ")

    assert should_retry(LLM_POLICY, attempt=1, status=404, exc=wrapped) is False


def test_saas_write_does_not_retry_bare_transient_exceptions() -> None:
    """層ごとに違う: 書き込みは「材料が無いなら再試行しない」。"""
    assert should_retry(SAAS_WRITE_POLICY, attempt=1, exc=TransientError("接続断")) is False


def test_no_evidence_means_no_retry() -> None:
    """「よく分からないので念のためもう一度」は、外向き操作では二重実行と同義。"""
    assert should_retry(LLM_POLICY, attempt=1) is False


# --- 媒体の読み取り ---------------------------------------------------------
def test_platform_read_requires_skip_counts_to_be_reported() -> None:
    """12.5 / 原則2: 黙って対象が減るのを防ぐ。件数はレポートに出す。"""
    assert PLATFORM_READ_POLICY.skips_must_be_reported is True
    assert "スキップ" in PLATFORM_READ_POLICY.reason


def test_only_the_skip_absorbing_layer_is_marked_as_such() -> None:
    assert SAAS_WRITE_POLICY.skips_must_be_reported is False
    assert LLM_POLICY.skips_must_be_reported is False


# --- 分類 -------------------------------------------------------------------
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (429, RetryTrigger.RATE_LIMITED),
        (500, RetryTrigger.SERVER_ERROR),
        (599, RetryTrigger.SERVER_ERROR),
        (400, RetryTrigger.CLIENT_ERROR),
        (404, RetryTrigger.CLIENT_ERROR),
        (200, None),
        (201, None),
        (302, None),
    ],
)
def test_classify_status(status: int, expected: RetryTrigger | None) -> None:
    assert classify_status(status) is expected


def test_every_named_policy_explains_itself() -> None:
    """方針の理由が書かれていないと、後から善意で書き換えられる (8.5)。"""
    assert all(policy.reason.strip() for policy in (*NAMED_POLICIES, SEND_API_POLICY))
