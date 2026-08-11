"""Matching an inbox subject back to the candidate it was sent to (10.2).

返信検知の要点はここにある。受信箱の行に出ているのは **返信者の実名だけ** で、
候補者IDは行にも詳細にも出てこない。画面に出ていないIDを探し続けるより、
画面に必ず出ている「自分が作った一意文字列」を突合キーにするほうが早い --
返信は「Re: <送信した件名>」として現れ、件名は候補者ごとに個別生成される
一点物なので、件名そのものが結合キーになる。

したがって送信時に件名を記録し損ねた対象の返信は恒久的に検知できない。
件名の払い出しと保存は :mod:`generation.subject` と 13.3 を参照。

ガードは3つあり、どれも外すと誤検知が生まれる。**誤検知は一度DBに入ると
手作業では消せない** (10.4) ので、迷ったら照合しない側に倒す:

1. 送信時と同じ関数で正規化する (:func:`models.text_norm.normalize_subject`)。
   ``Re:`` / ``返信:`` の剥がしもここに含まれる。
2. 正規化後に :data:`MIN_SUBJECT_MATCH_LENGTH` 文字未満の件名は
   :data:`MatchOutcome.TOO_SHORT` として捨てる。短い件名は他人の返信に当たる。
3. 1つの件名が2人以上の候補者に当たったら :data:`MatchOutcome.AMBIGUOUS` とし、
   **どちらにも紐づけない**。当たった候補者は診断のために返す。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

from pydantic import BaseModel, ConfigDict, field_validator

from jobmedley_scout.errors import ConfigError
from jobmedley_scout.generation.subject import (
    DEFAULT_SUBJECT_PREFIX_LENGTH,
    MIN_SUBJECT_NORM_LENGTH,
)
from jobmedley_scout.models.ids import CandidateId
from jobmedley_scout.models.reply import MatchKind, MatchOutcome, SubjectMatch
from jobmedley_scout.models.text_norm import normalize_subject

_STRICT = ConfigDict(extra="forbid", frozen=True)

#: 照合側の下限。**生成側の定数をそのまま参照している。** ここに 12 と直接書くと、
#: 片方だけ変更されたときに「生成は通したが照合が短すぎると捨てる」= 返信が静かに
#: 失われる状態になる (8.6 と同じ失敗の形)。この import はまとめ直さないこと。
MIN_SUBJECT_MATCH_LENGTH: Final[int] = MIN_SUBJECT_NORM_LENGTH

#: 前方一致に使う長さの既定値。生成側と同じ値を参照する (理由は上と同じ)。
DEFAULT_PREFIX_LENGTH: Final[int] = DEFAULT_SUBJECT_PREFIX_LENGTH


class SubjectEntry(BaseModel):
    """One subject we sent, as it was persisted at send time (13.3)."""

    model_config = _STRICT

    candidate_id: CandidateId
    #: 送信記録が特定できない経路 (移行データなど) では ``None`` になる。
    send_record_id: int | None
    subject_norm: str
    #: 保存済みの前方一致キー。索引はこれを **使わず** ``subject_norm`` から
    #: 導出する (:class:`SubjectIndex` の説明を参照)。保持しているのは、
    #: 保存列と導出値の食い違いを診断できるようにするため。
    subject_prefix35: str

    @field_validator("subject_norm")
    @classmethod
    def _normalize(cls, value: str) -> str:
        # 正規化は冪等なので二度通しても結果は変わらない。DBの値が古い正規化で
        # 書かれていた場合に、ここで照合側と同じ形に揃う (8.6)。
        return normalize_subject(value)


class SubjectIndex:
    """The subjects we sent, indexed by both matching keys.

    前方一致キーは保存列ではなく ``subject_norm`` から毎回導出する。生成時の
    長さと照合時の長さがずれた場合、保存列だけが古い長さのまま残り、前方一致が
    例外も警告も出さずにただ当たらなくなるため (8.6)。
    """

    def __init__(
        self,
        entries: Iterable[SubjectEntry],
        *,
        prefix_length: int = DEFAULT_PREFIX_LENGTH,
    ) -> None:
        if prefix_length <= 0:
            raise ConfigError(f"件名の前方一致長は正の値である必要があります: {prefix_length}")
        self._prefix_length = prefix_length
        by_norm: dict[str, list[SubjectEntry]] = {}
        by_prefix: dict[str, list[SubjectEntry]] = {}
        kept: list[SubjectEntry] = []
        ignored: list[SubjectEntry] = []
        mismatched: list[SubjectEntry] = []
        for entry in entries:
            if not entry.subject_norm:
                # 件名を失った行を索引に入れない。空キーは何にも当たらないが、
                # 「入っているのに当たらない」より「入っていない」ほうが診断が早い。
                ignored.append(entry)
                continue
            derived_prefix = entry.subject_norm[:prefix_length]
            if entry.subject_prefix35 != derived_prefix:
                mismatched.append(entry)
            by_norm.setdefault(entry.subject_norm, []).append(entry)
            by_prefix.setdefault(derived_prefix, []).append(entry)
            kept.append(entry)
        self._by_norm = {key: tuple(value) for key, value in by_norm.items()}
        self._by_prefix = {key: tuple(value) for key, value in by_prefix.items()}
        self._entries = tuple(kept)
        self._ignored = tuple(ignored)
        self._prefix_mismatches = tuple(mismatched)

    @property
    def prefix_length(self) -> int:
        """The prefix length this index was built with."""
        return self._prefix_length

    @property
    def entries(self) -> tuple[SubjectEntry, ...]:
        """Every indexed entry, in insertion order."""
        return self._entries

    @property
    def ignored_entries(self) -> tuple[SubjectEntry, ...]:
        """Entries dropped because they carried no normalized subject (13.3)."""
        return self._ignored

    @property
    def prefix_mismatches(self) -> tuple[SubjectEntry, ...]:
        """Entries whose stored prefix column disagrees with the derived key.

        照合の挙動には影響しない (導出値を使うため)。件数が増えていたら、
        生成側と設定の前方一致長がずれた合図である。
        """
        return self._prefix_mismatches

    def __len__(self) -> int:
        return len(self._entries)

    def by_norm(self, subject_norm: str) -> tuple[SubjectEntry, ...]:
        return self._by_norm.get(subject_norm, ())

    def by_prefix(self, prefix: str) -> tuple[SubjectEntry, ...]:
        return self._by_prefix.get(prefix, ())


def _resolve(entries: tuple[SubjectEntry, ...], kind: MatchKind) -> SubjectMatch:
    """Turn the entries a key hit into one outcome."""
    candidate_ids = sorted({entry.candidate_id for entry in entries})
    if len(candidate_ids) > 1:
        # 2人以上に当たった件名は **どちらにも紐づけない** (10.2)。誤って紐づけた
        # 検知はDBに入ると手作業で消せない (10.4)。件数は記録して、件名生成の
        # 一意性が劣化していないかを監視する。
        return SubjectMatch(
            outcome=MatchOutcome.AMBIGUOUS,
            ambiguous_candidate_ids=tuple(candidate_ids),
        )
    send_record_ids = {entry.send_record_id for entry in entries}
    # 同一候補者に複数の送信記録が同じキーで当たることはありうる (同日の再送)。
    # 候補者は一意なので照合は成立させるが、**どの送信への返信かは決められない**
    # ので送信記録は空にする。適当に1件選ぶと追客の判定が狂う。
    send_record_id = send_record_ids.pop() if len(send_record_ids) == 1 else None
    return SubjectMatch(
        outcome=MatchOutcome.MATCHED,
        candidate_id=candidate_ids[0],
        send_record_id=send_record_id,
        match_kind=kind,
    )


def match_subject(
    observed_subject: str,
    index: SubjectIndex,
    *,
    min_length: int = MIN_SUBJECT_MATCH_LENGTH,
    prefix_length: int = DEFAULT_PREFIX_LENGTH,
) -> SubjectMatch:
    """Match one observed inbox subject against the subjects we sent.

    Returns a :class:`models.reply.SubjectMatch` describing *why* the subject did
    or did not resolve -- callers need the distinction between "no reply",
    "ambiguous" and "too short to be safe" (10.2).
    """
    if index.prefix_length != prefix_length:
        # 生成側 (索引の構築長) と照合側で長さが違うと、前方一致は例外も警告も
        # 出さずにただ当たらなくなる。静かに壊れるくらいなら実行を落とす (8.6)。
        raise ConfigError(
            f"件名索引の前方一致長 ({index.prefix_length}) と照合の前方一致長 "
            f"({prefix_length}) が一致しません。設定 reply.subject_prefix_length を"
            "生成側と揃えてください (10.2)。"
        )

    # ガード1: 送信側と同じ正規化。"Re: " や "Re: Re: " はここで剥がれる。
    normalized = normalize_subject(observed_subject)

    # ガード2: 短い件名は突合キーとして使い物にならない。他人の返信に当たる。
    if len(normalized) < min_length:
        return SubjectMatch(outcome=MatchOutcome.TOO_SHORT)

    exact = index.by_norm(normalized)
    if exact:
        return _resolve(exact, MatchKind.EXACT)

    # 媒体や受信箱で件名が途中で切られる / 末尾に何か付く場合の受け皿。
    # **「保存件名が観測件名で始まるか」という開いた前方一致にはしない。**
    # 一意性が保証されているのは生成時に衝突を弾いた prefix_length 文字までで
    # あって、それより短い前方一致は別の候補者に当たりうる (10.2)。
    prefix_hits = index.by_prefix(normalized[:prefix_length])
    if prefix_hits:
        return _resolve(prefix_hits, MatchKind.PREFIX35)

    return SubjectMatch(outcome=MatchOutcome.NO_MATCH)
