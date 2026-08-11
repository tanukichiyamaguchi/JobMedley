"""8.4: 送信者本人が自分を三人称で紹介する事故を、両方の経路で防ぐ。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from jobmedley_scout.generation.matching import SenderProfile
from jobmedley_scout.generation.self_exclusion import (
    ExclusionReason,
    SenderIdentity,
    exclude_self,
    partition_self,
)

SENDER = SenderIdentity(person_id="p-001", display_name="佐藤花子")


@dataclass(frozen=True)
class TopUpEntry:
    """穴埋め経路が扱う形。マッチング経路とは別の型でも同じ関数を通る。"""

    person_id: str
    display_name: str
    reason_to_add: str = "件数の穴埋め"


def test_excluded_by_name_after_the_master_was_renumbered() -> None:
    """マスタを作り直すと連番IDが振り直され、設定のIDだけが古いまま残る。
    IDのみの照合だと、その瞬間に除外が黙って無効になる。"""
    master = (
        SenderProfile(person_id="p-042", display_name="佐藤花子"),
        SenderProfile(person_id="p-043", display_name="鈴木一郎"),
    )

    kept = exclude_self(master, SENDER)

    assert [person.person_id for person in kept] == ["p-043"]


def test_excluded_by_id_when_the_name_changed() -> None:
    """改姓などで氏名が変わってもIDで捕まえる。片方だけでは足りない。"""
    master = (SenderProfile(person_id="p-001", display_name="山田花子"),)

    assert exclude_self(master, SENDER) == ()


def test_name_matching_ignores_spacing_and_width() -> None:
    master = (SenderProfile(person_id="p-900", display_name="佐藤　花子"),)

    assert exclude_self(master, SENDER) == ()


def test_name_aliases_are_honoured() -> None:
    sender = SenderIdentity(person_id="p-001", display_name="佐藤花子", name_aliases=("山田花子",))
    master = (SenderProfile(person_id="p-777", display_name="山田花子"),)

    assert exclude_self(master, sender) == ()


def test_the_same_function_serves_the_matching_and_the_top_up_path() -> None:
    """片方の経路にしか除外を書かないと必ず漏れる (8.4)。両方の形が同じ
    関数を通ることを、実際に両方の型で確かめる。"""
    matching_path = (
        SenderProfile(person_id="p-042", display_name="佐藤花子"),
        SenderProfile(person_id="p-043", display_name="鈴木一郎"),
    )
    top_up_path = (
        TopUpEntry(person_id="p-042", display_name="佐藤花子"),
        TopUpEntry(person_id="p-043", display_name="鈴木一郎"),
    )

    kept_matching = exclude_self(matching_path, SENDER)
    kept_top_up = exclude_self(top_up_path, SENDER)

    assert [person.person_id for person in kept_matching] == ["p-043"]
    assert [person.person_id for person in kept_top_up] == ["p-043"]


def test_blank_names_never_match_the_sender() -> None:
    """氏名欄が取れていないマスタ行を全部「本人」にしない。"""
    master = (SenderProfile(person_id="p-500", display_name="   "),)

    assert len(exclude_self(master, SENDER)) == 1


def test_sender_without_a_name_still_excludes_by_id() -> None:
    sender = SenderIdentity(person_id="p-001", display_name="")
    master = (
        SenderProfile(person_id="p-001", display_name="佐藤花子"),
        SenderProfile(person_id="p-002", display_name=""),
    )

    kept = exclude_self(master, sender)

    assert [person.person_id for person in kept] == ["p-002"]


def test_partition_reports_why_each_entry_was_removed() -> None:
    master = (
        SenderProfile(person_id="p-001", display_name="別名義"),
        SenderProfile(person_id="p-042", display_name="佐藤花子"),
        SenderProfile(person_id="p-043", display_name="鈴木一郎"),
    )

    kept, result = partition_self(master, SENDER)

    assert len(kept) == 1
    assert result.kept_count == 1
    assert result.excluded_any is True
    assert [item.reason for item in result.excluded] == [
        ExclusionReason.PERSON_ID,
        ExclusionReason.DISPLAY_NAME,
    ]


def test_no_other_generation_module_reimplements_self_exclusion() -> None:
    """除外の実装が2つに増えた時点で、片方が古くなり漏れが復活する (8.4)。
    自己除外を名乗る関数は self_exclusion.py 以外に定義させない。"""
    package = Path(__file__).resolve().parents[2] / "src" / "jobmedley_scout" / "generation"
    pattern = re.compile(r"def\s+\w*(?:exclude\w*self|self\w*exclud)\w*\s*\(", re.IGNORECASE)

    offenders = [
        path.name
        for path in sorted(package.glob("*.py"))
        if path.name != "self_exclusion.py" and pattern.search(path.read_text(encoding="utf-8"))
    ]

    assert offenders == [], (
        f"{offenders} が自己除外を再実装しています。"
        "generation.self_exclusion.exclude_self を呼ぶこと (8.4)。"
    )
