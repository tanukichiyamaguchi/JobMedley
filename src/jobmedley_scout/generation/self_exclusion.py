"""Excluding the sender from the people the message introduces.

8.4 の事故: 紹介候補のマスタには送信者本人も載っている。参照実装は本人を
除外していなかったため、**マスタの先頭にいた送信者本人が常に選ばれ**、
「◯◯という者がおります」と自分を三人称で紹介するメールを送り続けた。

再発を防ぐ条件は2つあり、どちらか片方だけでは必ず漏れる。

1. **除外は1つの関数に集約する。** マッチング経路 (共通点で選ぶ) と
   フォールバック/穴埋め経路 (共通点が足りず件数を満たすために足す) の
   両方が :func:`exclude_self` を通ること。片方だけに書くと、もう片方から
   本人が滑り込む。これが参照実装で実際に起きた形である。
2. **IDだけで突き合わせない。** マスタを作り直すと連番IDが振り直され、
   設定に書かれた送信者IDだけが古いまま残り、除外が **黙って無効になる**。
   氏名 (正規化済み) でも突き合わせることで、どちらか一方が生きていれば
   除外が効く。

氏名の正規化は :func:`jobmedley_scout.models.text_norm.normalize_name` を使う。
ここで独自の正規化を書かないこと -- 生成側と参照側で正規化がずれると静かに
不一致する (8.6)。
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import Protocol, TypeVar

from pydantic import BaseModel, ConfigDict

from jobmedley_scout.models.text_norm import normalize_identifier, normalize_name

_STRICT = ConfigDict(extra="forbid", frozen=True)


class PersonLike(Protocol):
    """Anything that can be introduced in a message.

    プロトコルにしてあるのは、マッチング経路が「人物+共通点」の組を、
    穴埋め経路が素のマスタ行を扱うため。**両方が同じ関数を通れること**が
    8.4 の再発防止そのものなので、片方のために別関数を作らないこと。
    """

    @property
    def person_id(self) -> str: ...

    @property
    def display_name(self) -> str: ...


PersonT = TypeVar("PersonT", bound=PersonLike)


class ExclusionReason(StrEnum):
    """Why an entry was treated as the sender."""

    PERSON_ID = "person_id"
    #: IDが振り直されて一致しなくなっても、こちらで捕まえる。
    DISPLAY_NAME = "display_name"


class SenderIdentity(BaseModel):
    """The person actually sending the message.

    ``person_id`` と ``display_name`` の **両方** を必須にしている。片方だけを
    設定可能にすると、運用者は楽な方 (ID) だけを書き、マスタ再生成でIDが
    振り直された瞬間に除外が無効になる。
    """

    model_config = _STRICT

    person_id: str
    display_name: str
    #: 旧姓・英字表記など、実際に観測した別表記だけを入れる。推測で足さないこと
    #: (別人を誤って除外すると、その人物は永久に紹介されなくなる)。
    name_aliases: tuple[str, ...] = ()

    def id_key(self) -> str:
        return normalize_identifier(self.person_id)

    def name_keys(self) -> frozenset[str]:
        keys = {normalize_name(name) for name in (self.display_name, *self.name_aliases)}
        # 空の氏名が鍵に混ざると、氏名未設定のマスタ行が全部「本人」になる。
        keys.discard("")
        return frozenset(keys)


class ExcludedPerson(BaseModel):
    """One entry that was removed, and why. ログ/レポート用。"""

    model_config = _STRICT

    person_id: str
    display_name: str
    reason: ExclusionReason


class SelfExclusionResult(BaseModel):
    """The outcome of one exclusion pass.

    件数と理由を返すのは、除外が **効いていること** を実行レポートで確認できる
    ようにするため (12.6: 「安全弁を作った」と「安全弁が効いている」は別物)。
    """

    model_config = _STRICT

    kept_count: int
    excluded: tuple[ExcludedPerson, ...]

    @property
    def excluded_any(self) -> bool:
        return bool(self.excluded)


def _reason_for(person: PersonLike, sender: SenderIdentity) -> ExclusionReason | None:
    """Whether ``person`` is the sender, and on which key it matched."""
    person_id = normalize_identifier(person.person_id)
    if person_id and person_id == sender.id_key():
        return ExclusionReason.PERSON_ID
    person_name = normalize_name(person.display_name)
    # 空文字を一致させない。マスタ側の氏名欄が取れていない行を全部「本人」と
    # 見なしてしまい、紹介できる人が居なくなる。
    if person_name and person_name in sender.name_keys():
        return ExclusionReason.DISPLAY_NAME
    return None


def partition_self(
    items: Iterable[PersonT], sender: SenderIdentity
) -> tuple[tuple[PersonT, ...], SelfExclusionResult]:
    """Split ``items`` into the ones to keep and a report of what was removed."""
    kept: list[PersonT] = []
    excluded: list[ExcludedPerson] = []
    for item in items:
        reason = _reason_for(item, sender)
        if reason is None:
            kept.append(item)
            continue
        excluded.append(
            ExcludedPerson(
                person_id=item.person_id,
                display_name=item.display_name,
                reason=reason,
            )
        )
    result = SelfExclusionResult(kept_count=len(kept), excluded=tuple(excluded))
    return tuple(kept), result


def exclude_self(items: Iterable[PersonT], sender: SenderIdentity) -> tuple[PersonT, ...]:
    """Remove the sender from ``items``.

    **マッチング経路と穴埋め経路の両方がこの関数を呼ぶこと。** 片方だけだと
    必ず漏れる (8.4)。理由付きの結果が要る場合も、別実装を書かずに
    :func:`partition_self` を使うこと -- どちらも同じ判定を通る。
    """
    kept, _ = partition_self(items, sender)
    return kept
