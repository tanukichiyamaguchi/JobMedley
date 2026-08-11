"""Idempotency-key decisions (9.2).

参照実装の事故:

> 「送信API成功 → DBに記録」の間にプロセスが落ちると、DB上は未送信のままになり、
> 次回二重送信する。**CIのキャンセルでも普通に起こる。**

対処は「実送信の直前に ``SENDING`` としてコミットし、そのとき生成した冪等キーを
一緒に永続化する」こと (:mod:`state.send_repo`)。本モジュールはその **次回実行側**
を受け持つ -- 前回の記録を見て、キーを再利用するか新しく発行するかを決める。

**再利用は前回が ``SENDING`` のときだけ。** ``SENDING`` は「送信済み」ではなく
「送ったか不明」を表す唯一の状態である。だから重複判定 (:mod:`state.dedupe`) は
通さず再試行対象として残り、そのうえで **同じ冪等キー** で送り直す。前回が実は
サーバに届いていた場合は、サーバ側の重複排除が二重送信を防いでくれる。

逆に ``FAILED`` / ``SKIPPED`` / ``GENERATED`` / 記録なしからは **必ず新しいキー**
を発行する。これらは「届いていない」ことが確定している状態であり、古いキーを
使い回すと **正当な再試行がサーバの重複排除に弾かれて**、永久に送信されない
対象が生まれるため。

このモジュールで乱数を使うのは :func:`new_idempotency_key` の一箇所だけ。判定
そのもの (:func:`decide_key` / :func:`plan_key`) は純粋関数なので、キーを引数で
渡せばテストから完全に決定的に検証できる。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum

from jobmedley_scout.models.send_record import SendRecord, SendStatus


class KeyDecision(StrEnum):
    """Whether the prior idempotency key may be reused."""

    #: 前回は ``SENDING`` = 送ったか不明。同じキーで送り直し、サーバ側の重複排除に委ねる。
    REUSE = "reuse"
    #: 未送信が確定している。新しいキーを発行する。
    NEW = "new"


#: キーの再利用が許される唯一の状態。集合にしてあるのは、後から状態を増やした
#: ときに「ここへ足すべきか」を書き手に必ず考えさせるため。
#: 9.2: ``SENT`` を入れてはならない -- 既送信は :mod:`state.dedupe` が弾く側であって、
#: 再送の対象ではない。
REUSABLE_STATUSES: frozenset[SendStatus] = frozenset({SendStatus.SENDING})


@dataclass(frozen=True)
class KeyPlan:
    """The key to use for the next attempt, and why.

    真偽値ではなく理由つきの結果を返すのは、この判定が **ログに出したときだけ**
    検証できる種類のものだから (12.8)。「再利用した/しなかった」だけでは、
    後から二重送信を調べるときに根拠が残らない。
    """

    decision: KeyDecision
    key: str
    reason: str

    @property
    def reused(self) -> bool:
        return self.decision is KeyDecision.REUSE


def decide_key(prior: SendRecord | None) -> KeyDecision:
    """Decide whether to reuse the prior idempotency key.

    Reuse **only** when the prior attempt was left in ``SENDING``.

    9.2: 「送信中」は「送信済み」ではない。だから重複判定は通さず再試行対象として
    残し、そのうえで同じキーで送り直してサーバ側の重複排除に守らせる。
    確定失敗 (``FAILED``) や未送信 (``GENERATED`` / ``SKIPPED`` / 記録なし) から
    キーを使い回すと、正当な再試行がサーバに弾かれて永久に送られなくなる。
    """
    if prior is None:
        return KeyDecision.NEW
    if prior.status in REUSABLE_STATUSES:
        return KeyDecision.REUSE
    return KeyDecision.NEW


def plan_key(prior: SendRecord | None, *, fresh_key: str) -> KeyPlan:
    """Pure form of the decision: caller supplies the freshly generated key.

    乱数を引数で受け取るので、この関数はテストから完全に決定的に検証できる。
    実運用の呼び出し側は ``plan_key(prior, fresh_key=new_idempotency_key())``。
    """
    decision = decide_key(prior)
    if decision is KeyDecision.REUSE and prior is not None:
        return KeyPlan(
            decision=KeyDecision.REUSE,
            key=prior.idempotency_key,
            reason=(
                f"前回が {SendStatus.SENDING} のまま残っている (送ったか不明) ため "
                f"同じ冪等キーで再送し、サーバ側の重複排除に委ねる (9.2)"
            ),
        )
    if prior is None:
        return KeyPlan(
            decision=KeyDecision.NEW,
            key=fresh_key,
            reason="送信記録が無いため新しい冪等キーを発行する (9.2)",
        )
    return KeyPlan(
        decision=KeyDecision.NEW,
        key=fresh_key,
        reason=(
            f"前回は {prior.status} で未送信が確定しているため新しい冪等キーを発行する。"
            f"古いキーを使い回すと正当な再試行がサーバの重複排除に弾かれる (9.2)"
        ),
    )


def new_idempotency_key() -> str:
    """Generate a fresh idempotency key.

    **この関数がこのパッケージで唯一の乱数源。** 判定ロジックから切り離して
    最小の関数にしてあるのは、テストが決定的なキーを差し込めるようにするため
    (13.4: 純粋な判定だけを単体テストの対象にする)。
    """
    return str(uuid.uuid4())
