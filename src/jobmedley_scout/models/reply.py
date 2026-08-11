"""Reply-detection models.

10.4 の要点: 誤検知が一度DBに書かれると手作業では消せない。したがって検知集合は
**run スコープで全量置換できる (自己修復できる) データモデル** にしてある。
``DetectionRun`` が一世代分の検知結果を丸ごと保持し、``active`` な run は常に
1つだけ (DB の部分ユニークインデックスで保証)。誤検知が判明したら、その run を
捨てて新しい run を active にすればよい。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class MatchKind(StrEnum):
    """How a reply was matched back to a send record."""

    EXACT = "exact"
    PREFIX35 = "prefix35"


class MatchOutcome(StrEnum):
    """The result of trying to match one observed subject.

    ``AMBIGUOUS`` を ``NO_MATCH`` と区別しているのは、曖昧は「照合しない」で
    あって「返信が無い」ではないため (10.2)。曖昧だった件数は記録して、
    件名生成の一意性が劣化していないかを監視できるようにする。
    """

    MATCHED = "matched"
    NO_MATCH = "no_match"
    AMBIGUOUS = "ambiguous"
    TOO_SHORT = "too_short"


class RunStatus(StrEnum):
    BUILDING = "building"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    ABORTED = "aborted"


@dataclass(frozen=True)
class SubjectMatch:
    """The outcome of matching one observed inbox subject."""

    outcome: MatchOutcome
    candidate_id: str | None = None
    send_record_id: int | None = None
    match_kind: MatchKind | None = None
    #: 曖昧だった場合に、どの候補者たちに当たったか。診断用。
    ambiguous_candidate_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReplyDetection:
    """One detected reply, scoped to a detection run."""

    run_id: str
    candidate_id: str
    send_record_id: int | None
    matched_subject_norm: str
    match_kind: MatchKind
    provenance: str
    replied_at: datetime | None = None


@dataclass(frozen=True)
class DetectionRun:
    """One full pass over the inbox.

    ``signature_chain`` はページングの署名列 (10.5)。「進んでいない」で終了した
    のか、末尾に到達して終了したのかを後から判別できるようにするため、
    ``stop_reason`` とあわせて保持する。収集範囲のバグはグラフの形を見て初めて
    気づくことがあるので、実行時に痕跡を残しておく。
    """

    run_id: str
    started_at: datetime
    status: RunStatus
    page_count: int
    row_count: int
    signature_chain: tuple[str, ...]
    stop_reason: str | None
    detections: tuple[ReplyDetection, ...]
    ambiguous_subjects: tuple[str, ...]
