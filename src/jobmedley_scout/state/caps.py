"""Per-cost-unit caps and truncation reporting (9.7).

参照実装の事故が二つ、どちらも「上限をひとつの数で兼用した」ことに由来する:

1. **送信枠ごとの上限が共有されていた。** 無料枠の処理が有料枠の送信上限に
   食われ、その日の無料枠の対象が未処理のまま残った。上限は **費用単位ごとに
   独立** でなければならない。
2. **取り込み上限と送信上限が同じ値だった。** 取り込みで切られた対象は送信の
   候補にすら上がらないので、「取り込まれなかったから送られない」が **静かに**
   起きる。二つは別の値である (:class:`config.schema.SafetyConfig.ingest_cap` と
   :class:`config.schema.SendConfig` の枠別上限)。

したがって本モジュールの不変条件は:

* 各枠の付与数は **その枠の上限だけ** から決まる。他の枠の消費は一切影響しない。
* 切り捨ては **必ず件数と対処を添えて報告する**。黙って切ると、原則2の
  「静かなゼロ件」の小型版になる。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar

from jobmedley_scout.models.send_record import SendSlot

ItemT = TypeVar("ItemT")

#: 処理順の優先度 (高い順)。**上限そのものには一切影響しない** -- 枠ごとの上限が
#: 独立であることが 9.7 の対処であり、優先度は「実行時間が尽きたときにどちらが
#: 未処理として残るか」を決めるだけ。
#: 無料枠を先に置くのは、事故で未処理のまま残ったのが無料枠側だったことと、
#: 費用のかかる有料枠より先に無償の到達手段を使い切るのが妥当なため。
#: ``UNKNOWN`` が最後なのは、費用が読めないものを先に消費しないため (9.4)。
SLOT_PRIORITY: tuple[SendSlot, ...] = (SendSlot.FREE, SendSlot.PAID, SendSlot.UNKNOWN)


def priority_order(slots: Iterable[SendSlot]) -> tuple[SendSlot, ...]:
    """The given slots, highest-priority segment first, de-duplicated.

    未知の枠が増えたときは :data:`SLOT_PRIORITY` の末尾扱いになる (費用が読めない
    ものを先に消費しない)。
    """
    ranked: list[tuple[int, int, SendSlot]] = []
    seen: set[SendSlot] = set()
    for index, slot in enumerate(slots):
        if slot in seen:
            continue
        seen.add(slot)
        rank = SLOT_PRIORITY.index(slot) if slot in SLOT_PRIORITY else len(SLOT_PRIORITY)
        ranked.append((rank, index, slot))
    return tuple(slot for _, _, slot in sorted(ranked))


def remaining_capacity(
    sent_by_slot: Mapping[SendSlot, int], caps: Mapping[SendSlot, int]
) -> Mapping[SendSlot, int]:
    """Remaining sends per slot, computed **independently for each slot**.

    9.7: ここで枠をまたいで足し引きしてはならない。有料枠を使い切っても無料枠の
    残量は減らない。

    9.4 の恒等式を保てるよう、全ての枠をキーとして返す (0件でも省略しない)。
    上限が設定されていない枠は 0 -- 上限不明を「無制限」に寄せると、想定外の枠が
    現れた日に無制限に送ってしまう。安全側は「送らない」。
    """
    remaining: dict[SendSlot, int] = {}
    for slot in SendSlot:
        cap = caps.get(slot, 0)
        sent = sent_by_slot.get(slot, 0)
        if cap < 0:
            raise ValueError(f"{slot} の上限が負の値です: {cap}")
        if sent < 0:
            raise ValueError(f"{slot} の送信済み件数が負の値です: {sent}")
        remaining[slot] = max(0, cap - sent)
    return remaining


@dataclass(frozen=True)
class SlotAllocation(Generic[ItemT]):
    """What one slot was granted, and what it lost to its own cap."""

    slot: SendSlot
    cap: int
    already_sent: int
    granted: tuple[ItemT, ...]
    requested: int
    truncated: int

    @property
    def granted_count(self) -> int:
        return len(self.granted)

    @property
    def was_truncated(self) -> bool:
        return self.truncated > 0

    def describe(self) -> str:
        line = (
            f"{self.slot}: 対象{self.requested}件・付与{self.granted_count}件 "
            f"(上限{self.cap}件・送信済み{self.already_sent}件)"
        )
        if self.was_truncated:
            line += f"・上限で保留{self.truncated}件"
        return line


@dataclass(frozen=True)
class CapAllocation(Generic[ItemT]):
    """The full per-slot allocation for one run.

    切り捨ては必ず件数と対処つきで報告する (:meth:`describe`)。無言の切り捨ては
    「上限に当たっていることに誰も気づかないまま毎日同じ対象が積み残る」形の
    事故になる (9.7)。
    """

    per_slot: Mapping[SendSlot, SlotAllocation[ItemT]]

    @property
    def ordered_slots(self) -> tuple[SendSlot, ...]:
        return priority_order(self.per_slot.keys())

    def granted(self, slot: SendSlot) -> tuple[ItemT, ...]:
        allocation = self.per_slot.get(slot)
        return () if allocation is None else allocation.granted

    def granted_count(self, slot: SendSlot) -> int:
        return len(self.granted(slot))

    def truncated_count(self, slot: SendSlot) -> int:
        allocation = self.per_slot.get(slot)
        return 0 if allocation is None else allocation.truncated

    @property
    def total_granted(self) -> int:
        return sum(allocation.granted_count for allocation in self.per_slot.values())

    @property
    def total_truncated(self) -> int:
        return sum(allocation.truncated for allocation in self.per_slot.values())

    @property
    def was_truncated(self) -> bool:
        return self.total_truncated > 0

    def ordered_items(self) -> tuple[ItemT, ...]:
        """Every granted item, higher-priority segment first.

        優先度は順序だけの話。枠ごとの付与数は既に独立に決まっている。
        """
        items: list[ItemT] = []
        for slot in self.ordered_slots:
            items.extend(self.granted(slot))
        return tuple(items)

    def describe(self) -> str:
        lines = [self.per_slot[slot].describe() for slot in self.ordered_slots]
        if self.was_truncated:
            lines.append(
                f"上限で保留した合計{self.total_truncated}件は破棄ではなく次回実行に回る。"
                f"恒常的に保留が出るなら send.per_run_cap_* を見直すこと "
                f"(取り込み上限 safety.ingest_cap とは別の値、9.7)"
            )
        return "\n".join(lines)


def allocate(
    candidates_by_slot: Mapping[SendSlot, Sequence[ItemT]],
    caps: Mapping[SendSlot, int],
    *,
    sent_by_slot: Mapping[SendSlot, int] | None = None,
) -> CapAllocation[ItemT]:
    """Grant sends per slot, each slot bounded **only by its own cap**.

    9.7: 無料枠の処理が有料枠の上限に食われた事故の再発防止がこの関数の全て。
    ある枠が上限に達しても、他の枠の付与数は 1 件も減らない。
    """
    remaining = remaining_capacity(sent_by_slot or {}, caps)
    per_slot: dict[SendSlot, SlotAllocation[ItemT]] = {}
    for slot in priority_order(candidates_by_slot.keys()):
        items = tuple(candidates_by_slot[slot])
        allowance = remaining.get(slot, 0)
        granted = items[:allowance]
        per_slot[slot] = SlotAllocation(
            slot=slot,
            cap=caps.get(slot, 0),
            already_sent=(sent_by_slot or {}).get(slot, 0),
            granted=granted,
            requested=len(items),
            truncated=len(items) - len(granted),
        )
    return CapAllocation(per_slot=per_slot)


@dataclass(frozen=True)
class IngestCapResult(Generic[ItemT]):
    """Items kept by the ingest cap, plus what it held back.

    **取り込み上限は送信上限とは別の値。** ひとつの数で兼用すると
    「取り込まれなかったから送られない」が静かに起きる -- 送信側のログには
    「対象0件」としか出ず、上限に当たったことが見えない (9.7)。
    """

    kept: tuple[ItemT, ...]
    total: int
    truncated: int
    cap: int

    @property
    def was_truncated(self) -> bool:
        return self.truncated > 0

    def describe(self) -> str:
        line = f"取り込み: 発見{self.total}件・取り込み{len(self.kept)}件 (上限{self.cap}件)"
        if self.was_truncated:
            line += (
                f"・取り込み上限で保留{self.truncated}件。"
                f"保留分は送信の候補にも上がらないので、送信側には「対象が少ない」と"
                f"しか見えない。件数が慢性的に足りないなら safety.ingest_cap を"
                f"見直すこと (送信上限 send.per_run_cap_* とは別の値、9.7)"
            )
        return line


def apply_ingest_cap(items: Sequence[ItemT], ingest_cap: int) -> IngestCapResult[ItemT]:
    """Truncate an ingest batch, always reporting how much was held back.

    9.7: 取り込み上限と送信上限を同じ値で兼用しないこと。取り込みで切られた対象は
    送信の候補にすら上がらないため、送信側からは「対象が元から少なかった」のと
    区別がつかない。だから切った件数はここで必ず返す。
    """
    if ingest_cap < 0:
        raise ValueError(f"取り込み上限が負の値です: {ingest_cap}")
    kept = tuple(items[:ingest_cap])
    return IngestCapResult(
        kept=kept,
        total=len(items),
        truncated=len(items) - len(kept),
        cap=ingest_cap,
    )
