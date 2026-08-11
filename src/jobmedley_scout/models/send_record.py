"""Send records and send results.

二つの不変条件がここに集約されている。

**9.4 -- 送信枠は後から絶対に復元できない。** 参照実装は分析基盤を後から入れた
ため、それ以前の送信は送信枠が記録されておらず「内訳不明」が積み上がった。
よって :class:`SendResult` は ``endpoint_id`` と ``slot`` を必ず持つ。``UNKNOWN``
を独立したカテゴリとして残し、「合計 == 有料 + 無料 + 不明」の恒等式が
成り立つ形にしてある (テストで表明する)。

**13.3 -- 件名は復元できない。** 件名を失うと、その対象の返信は恒久的に検知
不可能になる (返信検知の突合キーが件名だから、10.2)。よって ``subject`` は
送信記録の必須項目であり、DB でも NOT NULL である。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class SendSlot(StrEnum):
    """The platform's send quota that a message consumes.

    ``UNKNOWN`` は一級市民。「不明」を既定値に寄せて有料/無料のどちらかに
    畳むと、恒等式が壊れ、後から分析軸を足したときに嘘の内訳が出る (9.4)。
    実際の枠がジョブメドレーに何種類あるかは段階3の偵察で確定する座標。
    """

    FREE = "free"
    PAID = "paid"
    UNKNOWN = "unknown"


class SendStatus(StrEnum):
    """Lifecycle of one send record.

    ``SENDING`` は「送ったか不明」を表す唯一の状態であり、9.2 の冪等キー再利用
    判定の要。``SENT`` だけが既送信とみなされる (:mod:`state.dedupe`) ので、
    ``GENERATED`` / ``SKIPPED`` / ``FAILED`` は再試行可能なまま残る。
    """

    GENERATED = "generated"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"


class MessageKind(StrEnum):
    """First contact or a follow-up."""

    FIRST_CONTACT = "first_contact"
    FOLLOW_UP = "follow_up"


@dataclass(frozen=True)
class ReservedSend:
    """A send that has been committed to the database *before* the network call.

    9.2: 「送信API成功 → DBに記録」の間にプロセスが落ちると、DB上は未送信の
    ままになり次回二重送信する。CIのキャンセルでも普通に起きる。したがって
    実送信の直前に、状態を ``SENDING`` にしつつ冪等キーを書いてコミットする。
    """

    record_id: int
    candidate_id: str
    idempotency_key: str
    message_kind: MessageKind
    followup_seq: int
    slot: SendSlot
    subject: str
    reserved_at: datetime


@dataclass(frozen=True)
class SendResult:
    """The outcome of one send attempt.

    ``endpoint_id`` と ``slot`` を必ず載せる (9.4)。派生ではなく実測値として
    記録すること -- endpoint→slot の写像自体が変わりうる座標なので、読み取り時に
    導出すると過去行が黙って書き換わる。
    """

    candidate_id: str
    endpoint_id: str
    slot: SendSlot
    succeeded: bool
    http_status: int | None
    platform_message_id: str | None
    failure_reason: str | None
    idempotency_key: str


@dataclass(frozen=True)
class SendRecord:
    """A persisted send record, as read back from the database."""

    record_id: int
    candidate_id: str
    idempotency_key: str
    message_kind: MessageKind
    followup_seq: int
    slot: SendSlot
    endpoint_id: str
    subject: str
    status: SendStatus
    reserved_at: datetime
    sent_at: datetime | None
    failure_reason: str | None
