"""Send de-duplication (9.1).

**「送った」と数えてよいのは :attr:`SendStatus.SENT` だけ。**

9.1/9.2 の要点をひとつの述語に閉じ込めてある。``GENERATED`` は文面を作っただけ、
``SKIPPED`` は送らないと決めたもの、``FAILED`` は届かなかったことが確定したもの、
``SENDING`` は **送ったか不明** なもの -- どれも「送信済み」ではないので、いずれも
再試行対象として残さなければならない。

とくに ``SENDING`` を「送信済み」に丸めたくなる誘惑には抵抗すること。丸めると、
送信APIの直後にプロセスが落ちた対象 (CIのキャンセルで普通に起こる) が二度と
送られず、しかもエラーは一切出ない。代わりに 9.2 の冪等キー再利用
(:mod:`state.idempotency`) が、再送してもサーバ側で重複排除される形で守る。

逆向きの危険 (``SENT`` を再試行対象に含める) は二重送信そのものなので、この
述語は **``SENT`` に対してだけ真** という一点だけを守ればよい。
"""

from __future__ import annotations

from collections.abc import Iterable

from jobmedley_scout.models.send_record import MessageKind, SendRecord, SendStatus

#: 「送った」と数える状態。9.1: ここに要素を足すことは二重送信を許すことと同義。
SENT_STATUSES: frozenset[SendStatus] = frozenset({SendStatus.SENT})

#: 再試行してよい状態。``SENDING`` が入っているのは意図的
#: (送ったか不明 = 未送信として扱い、冪等キーの再利用で守る、9.2)。
RETRYABLE_STATUSES: frozenset[SendStatus] = frozenset(
    {
        SendStatus.GENERATED,
        SendStatus.SENDING,
        SendStatus.FAILED,
        SendStatus.SKIPPED,
    }
)


def _matching(
    records: Iterable[SendRecord], kind: MessageKind, followup_seq: int
) -> tuple[SendRecord, ...]:
    """Records for one (kind, followup_seq) slot.

    フォローアップは通番ごとに独立した送信単位。通番を無視して突合すると、
    1通目を送った相手に2通目が永久に送られない (9.1)。
    """
    return tuple(
        record
        for record in records
        if record.message_kind is kind and record.followup_seq == followup_seq
    )


def is_already_sent(
    records: Iterable[SendRecord], kind: MessageKind, followup_seq: int = 0
) -> bool:
    """Whether this (kind, followup_seq) has actually been sent.

    **``SENT`` のみが真。** ``GENERATED`` / ``SKIPPED`` / ``FAILED`` / ``SENDING``
    は未送信として扱い、再試行の対象に残す (9.1)。
    """
    return any(record.status in SENT_STATUSES for record in _matching(records, kind, followup_seq))


def sent_records(
    records: Iterable[SendRecord], kind: MessageKind, followup_seq: int = 0
) -> tuple[SendRecord, ...]:
    """The records that count as sent.

    件数を返すのは、二重送信の調査で「何行が SENT になっているか」を根拠として
    示せるようにするため (12.8)。2件以上あれば重複送信が起きている。
    """
    return tuple(
        record
        for record in _matching(records, kind, followup_seq)
        if record.status in SENT_STATUSES
    )


def retryable_records(
    records: Iterable[SendRecord], kind: MessageKind, followup_seq: int = 0
) -> tuple[SendRecord, ...]:
    """The records that leave this (kind, followup_seq) eligible for another attempt.

    既に ``SENT`` が1件でもあれば再試行対象は無い -- 二重送信になるため、
    ここは空を返す。
    """
    matching = _matching(records, kind, followup_seq)
    if any(record.status in SENT_STATUSES for record in matching):
        return ()
    return tuple(record for record in matching if record.status in RETRYABLE_STATUSES)
