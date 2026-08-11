"""Building the subject line.

件名は装飾ではなく **返信検知の突合キー** である (10.2)。受信箱側には
「Re: <送信した件名>」しか手掛かりが無く、送信時に件名を記録し損ねた対象の
返信は恒久的に検知できない。したがってここは文面生成の一部ではなく、
識別子の払い出しとして扱う。

そのため生成時に3つ確認する。どれも「後で気付く」ことができない性質のもの
なので、満たせないなら送らない (:class:`errors.GenerationError` を送出する)。

1. 正規化後の長さが :data:`MIN_SUBJECT_NORM_LENGTH` 文字以上あること。
   短い件名は他人の返信と誤って突合する。
2. 当該実行で払い出した件名と衝突しないこと。正規化後の完全一致だけでなく
   **前方一致キーの衝突も** 弾く -- 前方一致で突合したときに複数該当となり、
   その返信は AMBIGUOUS として捨てられる (10.2)。
3. 形式が :data:`SUBJECT_PATTERN` に合うこと。同じ形式検査を最終本文にも
   掛ける (:mod:`generation.validators`)。

日付タグを入れているのは見栄えのためではない。同一候補者に初回と
フォローアップを送ると件名が似通うため、日付が無いと2通の返信の区別が
付かなくなる。
"""

from __future__ import annotations

import re
from collections.abc import Collection
from typing import Final

from pydantic import BaseModel, ConfigDict

from jobmedley_scout.clock import Clock, jst_date
from jobmedley_scout.errors import GenerationError
from jobmedley_scout.models.candidate import Candidate
from jobmedley_scout.models.message import GeneratedCore
from jobmedley_scout.models.text_norm import normalize_subject, normalize_ws

_STRICT = ConfigDict(extra="forbid", frozen=True)

#: 正規化後にこれ未満の件名は突合キーとして使い物にならない (10.2)。
#: 設定の ``reply.subject_min_length`` はこの値以上にすること -- 生成側が
#: 通した件名を照合側が「短すぎる」と捨てると、返信が静かに失われる。
MIN_SUBJECT_NORM_LENGTH: Final[int] = 12

#: 観測済みの既定値。**既定引数としては使わない** -- 生成側と照合側で長さが
#: ずれると前方一致が静かに壊れるため、呼び出し側に設定値を渡させる (8.6)。
DEFAULT_SUBJECT_PREFIX_LENGTH: Final[int] = 35

#: 件名の区切り。本文側の値に混ざると形式検査が壊れるので、素材からは除去する。
SUBJECT_SEPARATOR: Final[str] = "｜"

#: LLM が書く部分の上限。媒体や受信箱で件名が途中で切られると、完全一致が
#: 効かなくなり前方一致だけが頼りになる。長い件名を作らないこと自体が防御。
MAX_CORE_SUBJECT_CHARS: Final[int] = 60

#: ``<氏名>様｜<LLMの件名>｜<月>/<日>``
SUBJECT_PATTERN: Final[re.Pattern[str]] = re.compile(
    rf"\A[^{SUBJECT_SEPARATOR}]+様{SUBJECT_SEPARATOR}"
    rf"[^{SUBJECT_SEPARATOR}]+{SUBJECT_SEPARATOR}\d{{1,2}}/\d{{1,2}}\Z"
)


class SubjectKeys(BaseModel):
    """A subject and the two keys reply detection matches on.

    3つをまとめて返すのは、件名だけを保存して正規化を後回しにする経路を
    作らないため。送信直後にこの3つを永続化する (13.3)。
    """

    model_config = _STRICT

    subject: str
    #: 完全一致用。``models.text_norm.normalize_subject`` の結果そのもの。
    subject_norm: str
    #: 前方一致用。名前の 35 は ``models.message.AssembledMessage`` の
    #: フィールド名に揃えてあるだけで、長さ自体は設定から渡る。
    subject_prefix35: str


def subject_keys(subject: str, prefix_len: int) -> SubjectKeys:
    """Derive the matching keys for ``subject``.

    ``prefix_len`` に既定値を置いていないのは意図的である。生成側が35文字、
    照合側が40文字で切ると、前方一致は例外も警告も出さずにただ当たらなくなる。
    """
    if prefix_len <= 0:
        raise GenerationError(f"件名の前方一致長は正の値である必要があります: {prefix_len}")
    normalized = normalize_subject(subject)
    return SubjectKeys(
        subject=subject,
        subject_norm=normalized,
        subject_prefix35=normalized[:prefix_len],
    )


def validate_subject_format(subject: str) -> bool:
    """Whether ``subject`` has the shape :func:`build_subject` produces."""
    return SUBJECT_PATTERN.match(subject) is not None


def _clean_part(value: str) -> str:
    """Whitespace-normalize and strip the separator out of one part.

    区切り文字が素材側に混ざると、件名の形式検査もログの読み取りも壊れる。
    """
    return normalize_ws(value.replace(SUBJECT_SEPARATOR, " "))


def build_subject(
    candidate: Candidate,
    core: GeneratedCore,
    clock: Clock,
    *,
    already_used_subjects: Collection[str],
    prefix_len: int,
) -> str:
    """Build the subject line for one message, or refuse to send.

    ``already_used_subjects`` は省略できない。既定値を空にすると、必ずどこかの
    経路が渡し忘れ、衝突検知が黙って無効になる (8.4 と同じ失敗の形)。当該実行で
    払い出し済みの件名を渡すこと。
    """
    name = _clean_part(candidate.display_name)
    # 媒体側の氏名に敬称が付いて返ることがある。「様様」を避ける。
    name = name.removesuffix("様").strip()
    if not name:
        raise GenerationError(
            f"候補者 {candidate.candidate_id} の氏名が空です。件名を組み立てられません。"
        )

    # 切り詰めた位置が語の途中や空白だと末尾に空白が残る。正規化キーには影響
    # しないが、送信される件名に見えるので落としておく。
    core_subject = _clean_part(core.subject)[:MAX_CORE_SUBJECT_CHARS].strip()
    if not core_subject:
        raise GenerationError(f"候補者 {candidate.candidate_id} の件名が生成されていません。")

    today = jst_date(clock.now())
    subject = SUBJECT_SEPARATOR.join((f"{name}様", core_subject, f"{today.month}/{today.day}"))

    if not validate_subject_format(subject):  # pragma: no cover - 組み立て側の破損検知
        raise GenerationError(f"組み立てた件名が想定の形式ではありません: {subject!r}")

    keys = subject_keys(subject, prefix_len)
    if len(keys.subject_norm) < MIN_SUBJECT_NORM_LENGTH:
        raise GenerationError(
            f"件名が短すぎます (正規化後 {len(keys.subject_norm)} 文字 < "
            f"{MIN_SUBJECT_NORM_LENGTH} 文字): {subject!r}。"
            "短い件名は他人の返信と誤って突合するため送信しません (10.2)。"
        )

    for used in already_used_subjects:
        used_keys = subject_keys(used, prefix_len)
        if used_keys.subject_norm == keys.subject_norm:
            raise GenerationError(
                f"件名が当該実行の既存の件名と完全に重複しています: {subject!r}。"
                "返信の突合先が一意に決まらなくなります (10.2)。"
            )
        if used_keys.subject_prefix35 == keys.subject_prefix35:
            raise GenerationError(
                f"件名の前方一致キー ({prefix_len}文字) が既存の件名と衝突しています: "
                f"{subject!r}。件名が途中で切られた返信を突合できなくなります (10.2)。"
            )

    return subject
