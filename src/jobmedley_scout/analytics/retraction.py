"""Bulk retraction by provenance (11.2).

**レコードには「誰が書いたか」ではなく「何を根拠に書いたか」を残す。**

参照実装は取り消し可否を「自動／手動」で判断していた。シート経由で手動扱いに
なった値は自動側から触れなくなり、誤検知が固定化した。区分が権限の話になって
いたからである。

由来なら自己修復に使える。「件名の前方一致で書いた」と記録しておけば、前方一致の
ロジックに欠陥が見つかったとき、**その根拠で書かれた行だけ** を一括で取り消せる。
完全一致で書かれた行は無傷で残る。誰が実行したかは関係ない。

取り消しは検知集合の全量置換 (10.4) と組み合わせて使う。ここは純粋な選別だけを
行い、残す側と外す側の **両方** を返す。外した件数を数えられないと、「効いたのか
何も当たらなかったのか」が区別できない。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from jobmedley_scout.errors import ConfigError
from jobmedley_scout.models.provenance import ALL_PROVENANCES, matches_origin
from jobmedley_scout.models.reply import ReplyDetection


@dataclass(frozen=True)
class RetractionResult:
    """What survived, what was retracted, and on what grounds."""

    #: 取り消しの根拠にした由来の接頭辞。
    prefix: str
    kept: tuple[ReplyDetection, ...]
    retracted: tuple[ReplyDetection, ...]

    @property
    def retracted_count(self) -> int:
        return len(self.retracted)

    @property
    def changed(self) -> bool:
        """Whether anything was actually retracted.

        0件は「対象が無かった」であって成功ではない。呼び出し側がログに残せる
        よう、真偽値ではなく件数と一緒に返している。
        """
        return bool(self.retracted)

    @property
    def retracted_candidate_ids(self) -> tuple[str, ...]:
        return tuple(sorted({detection.candidate_id for detection in self.retracted}))


def retract_by_provenance(
    detections: Iterable[ReplyDetection],
    prefix: str,
) -> RetractionResult:
    """Split detections into those written on ``prefix`` grounds and the rest.

    比較は :func:`~models.provenance.matches_origin` に任せる。接頭辞の前方一致を
    自前で書くと ``auto/subject-match`` を指定したつもりで
    ``auto/subject-match-v2`` まで巻き込む。
    """
    if prefix not in ALL_PROVENANCES:
        # 打鍵ミスは「0件取り消し」という静かな成功になる。誤検知を消したつもりで
        # 消えていない状態が一番まずいので、定数を経由していない指定は落とす。
        raise ConfigError(
            f"未知の由来です: {prefix!r} -- models/provenance.py の定数を使ってください。"
            f"既知の由来: {list(ALL_PROVENANCES)}"
        )

    kept: list[ReplyDetection] = []
    retracted: list[ReplyDetection] = []
    for detection in detections:
        target = retracted if matches_origin(detection.provenance, prefix) else kept
        target.append(detection)
    return RetractionResult(prefix=prefix, kept=tuple(kept), retracted=tuple(retracted))
