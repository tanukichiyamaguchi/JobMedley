"""レジュメのキー写像を、**取り違えられない形で固定する**。

6.4 はこのリポジトリで最も繰り返し引かれている事故である。

> 参照実装の最大の罠は、レジュメのトップレベルの「業界」「職種」が *経験してきた*
> もので、希望条件オブジェクト配下の同名キーが *希望する* ものだったこと。これを
> 取り違えて「ご希望の◯◯業界」と書き、運用者から「嘘が多い」と指摘された。

2026-08-22 observe-resume 2回目で、この媒体の実際の形が分かった。**同じ罠が同じ形で
在る** -- 経験は ``appeal`` 配下、希望は ``desiredCondition`` 配下で、どちらも
``jobCategories`` 系の名前を持つ。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

COORDINATES = Path("config/site_coordinates.yaml")

RESUME_URL = (
    "https://customers.job-medley.com/api/customers/graphql/MemberOnScoutProfileModalOfDesktop"
)


def _coordinates() -> dict[str, object]:
    loaded = yaml.safe_load(COORDINATES.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_the_resume_url_is_the_profile_modal_query() -> None:
    assert _coordinates()["api.resume.url_pattern"] == RESUME_URL


def test_the_resume_cannot_be_called_until_the_query_document_is_known() -> None:
    """**URLだけでは呼べない。** GraphQL は query の無いリクエストを受け付けない。

    送信 payload で一度同じ穴を開けている。埋まったらこの試験を消すこと --
    消すのは「読めそう」と思ったときではなく、**問い合わせ文を実測したとき**である。
    """
    from jobmedley_scout.config.coordinates import REQUIRED_BY_COMMAND

    assert _coordinates()["api.resume.payload_template"] == "UNRESOLVED"
    assert "api.resume.payload_template" in REQUIRED_BY_COMMAND["ingest"]


# ---------------------------------------------------------------------------
# 6.4: 経験と希望を取り違えない
# ---------------------------------------------------------------------------

#: 経験側の包み。**希望側と共有していない。**
EXPERIENCED_PREFIX = "data.memberGet.member.appeal."
#: 希望側の包み。
DESIRED_PREFIX = "data.memberGet.member.desiredCondition."


def test_experienced_occupations_come_from_the_appeal_branch() -> None:
    value = str(_coordinates()["resume.fields.experienced_occupations"])
    assert value.startswith(EXPERIENCED_PREFIX), value
    assert "desiredCondition" not in value, "希望側の経路が経験の欄に入っています (6.4)"


def test_desired_occupations_come_from_the_desired_branch() -> None:
    value = str(_coordinates()["resume.fields.desired_occupations"])
    assert value.startswith(DESIRED_PREFIX), value
    assert ".appeal." not in value, "経験側の経路が希望の欄に入っています (6.4)"


def test_the_two_occupation_paths_are_never_the_same() -> None:
    """**同じ値なら、どちらかが必ず嘘になる。**"""
    coordinates = _coordinates()
    assert (
        coordinates["resume.fields.experienced_occupations"]
        != coordinates["resume.fields.desired_occupations"]
    )


# ---------------------------------------------------------------------------
# 「無い」と「まだ知らない」を混ぜない
# ---------------------------------------------------------------------------

#: この媒体に **存在しないと確認できた** 軸。``null`` であって UNRESOLVED ではない。
#:
#: 101キーを値抜きで読んだ結果である。業界・語学・会員ステータス・専門領域・
#: 職務要約に相当するキーは1つも無かった。
CONFIRMED_ABSENT: tuple[str, ...] = (
    "resume.fields.experienced_industries",
    "resume.fields.desired_industries",
    "resume.fields.language_text",
    "resume.fields.membership_status",
    "resume.fields.specialty",
    "resume.fields.summary",
)


@pytest.mark.parametrize("key", CONFIRMED_ABSENT)
def test_an_absent_axis_is_null_not_unresolved(key: str) -> None:
    """``null`` は確定した答えであり、``UNRESOLVED`` は未確定である。

    混ぜると「まだ調べていない」と「調べた結果無かった」が報告から区別できなく
    なり、同じ調査を何度も繰り返すことになる。
    """
    assert _coordinates()[key] is None, f"{key} が null ではありません"


def test_employment_status_is_not_filed_as_membership_status() -> None:
    """**就業状況と会員ステータスは別概念である。**

    ``personalInformation.employmentStatus`` (就業中/離職中) をここへ入れると、
    モデルには「会員ステータス: 就業中」として渡る -- 6.4 の取り違えと同じ形。
    """
    assert _coordinates()["resume.fields.membership_status"] is None
    text = COORDINATES.read_text(encoding="utf-8")
    assert "employmentStatus" in text, "近いキーが在ることの注記が消えています"
    assert "別概念" in text


def test_self_pr_is_not_filed_as_the_work_summary() -> None:
    """**自己PRと職務要約は別物である。**

    職務要約は職歴の要約、自己PRは自己売り込み。``appeal.selfPr`` をここへ入れると
    モデルには「職務要約」として渡り、モデルはそう扱う。
    """
    assert _coordinates()["resume.fields.summary"] is None
    text = COORDINATES.read_text(encoding="utf-8")
    assert "selfPr" in text, "自己PRが別の欄に在ることの注記が消えています"


# ---------------------------------------------------------------------------
# 応答で新たに分かったこと
# ---------------------------------------------------------------------------


def test_the_refusal_signal_is_recorded_as_something_to_honour() -> None:
    """**候補者はスカウトを辞退できる。**

    辞退された相手へ送り続けるのは最悪の失敗である (13.6)。``latestRefusedAt`` は
    画面には出ていなかった -- 応答を読まなければ気付けなかった。
    """
    text = COORDINATES.read_text(encoding="utf-8")
    assert "latestRefusedAt" in text
    assert "送信前判定に必ず入れる" in text


def test_the_scheduled_qualification_flag_is_recorded() -> None:
    """取得済みと取得予定を混ぜて「◯◯をお持ちの方へ」と書くと嘘になる。"""
    text = COORDINATES.read_text(encoding="utf-8")
    assert "isScheduled" in text
    assert "取得予定" in text


def test_the_api_confirms_there_is_no_name_anywhere() -> None:
    """運用者の「取得できるのは会員番号のみ」が API 側でも裏付けられた。"""
    text = COORDINATES.read_text(encoding="utf-8")
    assert "名前の欄は101キーのどこにも" in text
