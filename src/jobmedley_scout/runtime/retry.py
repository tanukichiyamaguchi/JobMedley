"""Retry policy, declared per layer -- because "retry" is not one decision.

12.5: **リトライ方針は層ごとに違う。**

* LLM呼び出し -- 指数バックオフでよい (副作用がないため)
* SaaS書き込み -- 429 と 5xx のみ再試行。4xx のクライアントエラーは再試行しない
* 媒体の読み取り -- 失敗は警告してスキップしてよいが、**スキップ件数を必ず報告する**
  (:class:`runtime.report.RunReport` の ``read_errors_skipped``)。黙って対象が
  減るのが最も危険 (原則2)
* **送信API -- リトライしない** (:data:`SEND_API_POLICY`)

方針を「呼び出し側の気分」ではなくデータとして置いてあるのは、後から善意で足される
のを防ぐためである。特に送信APIについては、``should_retry`` が **呼ばれた時点で
例外** になる (:class:`RetryForbidden`)。

ステータスの数値をここで解釈していることについて: :mod:`api.success` に集約されて
いるのは「**このエンドポイントにとっての成功**」の判定であり、エンドポイントごとに
違う (6.2)。一方こちらは「429/5xx はプロトコル上いつでも一時障害」という、
エンドポイントに依存しない分類である。別の問題なので別の場所に置き、数値は
名前付き定数にしてある。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from jobmedley_scout.errors import PermanentError, TransientError

#: 名前付きにしてあるのは可読性のためだけではない。生の数値比較は
#: :mod:`api.success` 以外で禁止されており (ガードレールテスト)、その規約が
#: 守っている「成功判定を散らさない」という意図とも衝突しない書き方にしてある。
TOO_MANY_REQUESTS: Final[int] = 429
CLIENT_ERROR_FLOOR: Final[int] = 400
SERVER_ERROR_FLOOR: Final[int] = 500
SERVER_ERROR_CEILING: Final[int] = 600


class RetryTrigger(StrEnum):
    """Why a retry might be considered."""

    #: 429。待てば通る。
    RATE_LIMITED = "rate_limited"
    #: 5xx。相手側の一時障害。
    SERVER_ERROR = "server_error"
    #: 429 以外の 4xx。**再試行してはならない。** 同じ要求は何度出しても同じ結果で、
    #: 書き込み系では二重書き込みの危険だけが増える。
    CLIENT_ERROR = "client_error"
    #: :class:`errors.TransientError` (通信断・タイムアウト等)。
    TRANSIENT_EXCEPTION = "transient_exception"


class RetryForbidden(PermanentError):
    """Raised when a caller asks whether a no-retry operation may be retried.

    「握りつぶして False を返す」ではなく **例外にする** のが要点。False を返すと
    呼び出し側は「今回はリトライ不可だったのだな」と解釈して、条件次第で
    リトライするコードを書いてしまう。
    """


@dataclass(frozen=True)
class RetryPolicy:
    """How one layer is allowed to retry."""

    name: str
    #: 初回を含む試行回数。1 は「リトライしない」。
    max_attempts: int
    backoff_base_seconds: float
    retry_on: frozenset[RetryTrigger]
    #: なぜこの方針なのか。レポートとレビューのために文字列で持つ。
    reason: str
    #: 12.5 / 原則2: 失敗をスキップで吸収する層は、スキップ件数の報告が **必須**。
    skips_must_be_reported: bool = False

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError(f"max_attempts は1以上 (初回を含む): {self.name}")
        if self.backoff_base_seconds < 0:
            raise ValueError(f"backoff_base_seconds は0以上: {self.name}")
        # 4xx を再試行対象に入れた方針は、書き込み系で二重書き込みを作る。
        # 宣言の時点で落とす (レビューで気づく前に落ちる)。
        if RetryTrigger.CLIENT_ERROR in self.retry_on:
            raise ValueError(
                f"方針 '{self.name}' が 4xx を再試行対象にしています。"
                f"クライアントエラーは何度送っても同じ結果です (12.5)。"
            )


@dataclass(frozen=True)
class NoRetry(RetryPolicy):
    """A policy that forbids retrying at all -- and says so loudly.

    ``max_attempts=1`` を持つだけの :class:`RetryPolicy` と区別して型にしてある
    のは、``should_retry`` が **型で** 拒否できるようにするため。数値の比較で
    済ませると、後から ``max_attempts`` を2にするだけで送信のリトライが有効化される。
    """

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.max_attempts != 1:
            raise ValueError(f"NoRetry の max_attempts は1固定: {self.name}")
        if self.retry_on:
            raise ValueError(f"NoRetry に再試行条件を書いてはいけない: {self.name}")


#: 文面生成。**副作用がない** ので指数バックオフを許す。
LLM_POLICY: Final[RetryPolicy] = RetryPolicy(
    name="llm",
    max_attempts=3,
    backoff_base_seconds=2.0,
    retry_on=frozenset(
        {RetryTrigger.RATE_LIMITED, RetryTrigger.SERVER_ERROR, RetryTrigger.TRANSIENT_EXCEPTION}
    ),
    reason="生成は副作用を持たないため、同じ入力で再実行しても害がない (12.5)",
)

#: スプレッドシート等への書き込み。429 と 5xx のみ。
SAAS_WRITE_POLICY: Final[RetryPolicy] = RetryPolicy(
    name="saas_write",
    max_attempts=3,
    backoff_base_seconds=1.0,
    retry_on=frozenset({RetryTrigger.RATE_LIMITED, RetryTrigger.SERVER_ERROR}),
    reason=(
        "429/5xx は待てば通る一時障害。4xx は要求そのものが誤りで、"
        "再試行しても直らず二重書き込みの危険だけが残る (12.5)"
    ),
)

#: 媒体の読み取り。失敗は警告してスキップしてよい -- **ただし件数を報告すること。**
PLATFORM_READ_POLICY: Final[RetryPolicy] = RetryPolicy(
    name="platform_read",
    max_attempts=2,
    backoff_base_seconds=1.0,
    retry_on=frozenset(
        {RetryTrigger.RATE_LIMITED, RetryTrigger.SERVER_ERROR, RetryTrigger.TRANSIENT_EXCEPTION}
    ),
    reason=(
        "読み取り失敗は1件スキップして続行してよいが、スキップ件数を必ず報告する。"
        "黙って対象が減るのが最も危険な失敗 (原則2 / 12.5)"
    ),
    skips_must_be_reported=True,
)

# ---------------------------------------------------------------------------
# 送信API。**意図的にリトライしていない。**
#
# 送信APIへ「親切にリトライを足す」と、二重送信事故に直結する。送信が成功したのに
# 応答が届かなかった場合、リトライは同じ相手へ2通目を送る。取り消せない外向き操作で
# あり、相手にも媒体上の評価にも実害が出る。
#
# 代わりに拠るのは次の2つである:
#   * 冪等キーを **送信直前に永続化** しておくこと (9.2)
#   * 失敗は次回実行に委ねること (次回は状態を見て、送ったかどうかを判断できる)
#
# この行を「1回くらい再試行しても」と書き換えたくなったら、まず 9.2 の冪等キーが
# 本番で機能していることを証明すること。証明できていないなら、答えは「しない」。
# ---------------------------------------------------------------------------
SEND_API_POLICY: Final[NoRetry] = NoRetry(
    name="send_api",
    max_attempts=1,
    backoff_base_seconds=0.0,
    retry_on=frozenset(),
    reason=(
        "送信APIへのリトライは二重送信に直結する。意図的にリトライしていない。"
        "冪等キーの事前永続化 (9.2) と次回実行に委ねること (12.5)"
    ),
)


def classify_status(status: int) -> RetryTrigger | None:
    """Classify an HTTP status for retry purposes. ``None`` means "not a failure"."""
    if status == TOO_MANY_REQUESTS:
        return RetryTrigger.RATE_LIMITED
    if SERVER_ERROR_FLOOR <= status < SERVER_ERROR_CEILING:
        return RetryTrigger.SERVER_ERROR
    if CLIENT_ERROR_FLOOR <= status < SERVER_ERROR_FLOOR:
        return RetryTrigger.CLIENT_ERROR
    return None


def should_retry(
    policy: RetryPolicy,
    attempt: int,
    status: int | None = None,
    exc: BaseException | None = None,
) -> bool:
    """Whether attempt number ``attempt`` (1-based) may be followed by another.

    Raises :class:`RetryForbidden` for a :class:`NoRetry` policy.
    """
    # **将来の呼び出し側が黙って送信をリトライ対象にできないようにする。**
    # 「呼べるが常に False」ではなく「呼んだら止まる」でなければ、条件分岐を
    # 足すだけでリトライが有効化されてしまう。
    if isinstance(policy, NoRetry):
        raise RetryForbidden(f"方針 '{policy.name}' はリトライを許していません: {policy.reason}")
    if attempt < 1:
        raise ValueError(f"attempt は1始まり: {attempt}")
    if attempt >= policy.max_attempts:
        return False

    # 恒久エラーは何回試しても同じ。特に認証切れ (6.6) を再試行すると、
    # 媒体側にロック要因を作りかねない。ステータスより先に見る。
    if isinstance(exc, PermanentError):
        return False

    # ステータスがあるなら、それが最も確かな証拠。例外の種類より優先する
    # (通信層が 404 を TransientError に包んでも、404 は再試行してはならない)。
    if status is not None:
        trigger = classify_status(status)
        return trigger is not None and trigger in policy.retry_on

    if isinstance(exc, TransientError):
        return RetryTrigger.TRANSIENT_EXCEPTION in policy.retry_on

    # 判断材料が無いときは再試行しない。「よく分からないので念のためもう一度」は、
    # 外向き操作では二重実行と同義である。
    return False


def backoff_seconds(policy: RetryPolicy, attempt: int) -> float:
    """Seconds to wait after attempt ``attempt`` (1-based). **Does not sleep.**

    実際に眠るのは ``browser/waits.py`` だけ (5.2)。ここが眠ると、リトライ方針の
    テストが実時間ぶん遅くなり、やがて誰も回さなくなる。
    """
    if isinstance(policy, NoRetry):
        raise RetryForbidden(
            f"方針 '{policy.name}' には待ち時間の概念がありません: {policy.reason}"
        )
    if attempt < 1:
        raise ValueError(f"attempt は1始まり: {attempt}")
    return policy.backoff_base_seconds * float(2 ** (attempt - 1))
