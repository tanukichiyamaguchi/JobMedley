"""実測で埋めた読み取り座標を、**別の似た通信で置き換えられないように固定する。**

2026-08-21 observe-api 3回目で、一覧を開いた瞬間に飛ぶ通信を全部聴いた。
候補者の並びを返していたのは1本だけだったが、**紛らわしいものが3本あった**::

    POST /api/customers/members/search/                    <- 候補者の並び
    POST /api/customers/received_favorites/search/         <- 「気になる」した人
    POST /api/customers/scouted_members/search/            <- 送信済みの人
    POST /api/customers/customer_search_conditions/search_manual/  <- 保存条件

**どれも名前が ``search`` で終わり、どれも POST で、どれも一覧を返す。**
取り違えても例外は出ない -- 出るのは「候補者0件」か「送信済みの人へ再送」で、
どちらも静かに間違う (原則2)。

だからここで名指しで固定する。座標を変えるときにこの試験が落ちる。
落ちたときに考えるべきは「期待値を書き換えるか」ではなく
「**その通信が候補者の並びを返すと観測したか**」である。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

COORDINATES = Path("config/site_coordinates.yaml")

#: 実測した候補者一覧。応答に ``members[]`` と ``search_uuid`` が載っていた。
OBSERVED_CANDIDATE_LIST = "https://customers.job-medley.com/api/customers/members/search/"

#: 実測した残数照会。応答は ``remaining_count`` / ``total_count`` の2キーだけ。
OBSERVED_QUOTA = "https://customers.job-medley.com/api/customers/messages/scout_count/"

#: **候補者の並びではないのに、名前が似ている通信。**
LOOKALIKES: tuple[str, ...] = (
    "received_favorites/search",
    "scouted_members/search",
    "customer_search_conditions/search_manual",
    "customer_search_conditions/search_recommend",
    "customer_search_conditions/label",
)


def _coordinates() -> dict[str, object]:
    loaded = yaml.safe_load(COORDINATES.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_the_candidate_list_is_the_endpoint_that_returned_members() -> None:
    assert _coordinates()["api.candidate_list.url_pattern"] == OBSERVED_CANDIDATE_LIST


@pytest.mark.parametrize("lookalike", LOOKALIKES)
def test_the_candidate_list_is_not_one_of_the_lookalikes(lookalike: str) -> None:
    """取り違えは例外にならず、**静かに間違う。**

    ``scouted_members/search`` を入れれば「送信済みの人だけ」が候補者になり、
    9章の重複判定を通り抜けて再送しうる。取り消せない (13.6)。
    """
    value = _coordinates()["api.candidate_list.url_pattern"]
    assert lookalike not in str(
        value
    ), f"候補者一覧に {lookalike} が入っています。これは候補者の並びではありません。"


def test_the_quota_is_read_rather_than_computed() -> None:
    """**残数は毎回読む** (8章)。

    引き算で持つと必ずずれる -- 媒体側で人が送るし、月次でリセットされる。
    実際に運用者の画面で2日のあいだに 62 -> 51 と動いている。
    """
    assert _coordinates()["api.quota.url_pattern"] == OBSERVED_QUOTA


def test_the_candidate_list_endpoint_is_a_post() -> None:
    """**この媒体は送信だけが GraphQL で、読み取りは REST の POST である。**

    ``endpoints.py`` は参照実装からの引き写しで長く ``GET`` と書いていた。
    GET のまま呼べば当たらず、404/405 は「候補者0件」と区別が付かない形で
    上流に伝わりうる (原則2)。
    """
    from jobmedley_scout.api.endpoints import CANDIDATE_LIST, build_endpoints
    from jobmedley_scout.config.loader import load_site_coordinates

    endpoints = build_endpoints(load_site_coordinates(COORDINATES))
    assert endpoints[CANDIDATE_LIST].method == "POST"
    assert endpoints[CANDIDATE_LIST].side_effectful is False


def test_the_request_body_of_the_candidate_list_is_still_unobserved() -> None:
    """**URLが決まっても、呼べるようにはなっていない。**

    3回目の observe-api は応答しか出していなかった。何を送れば同じ並びが
    返るのかはまだ観測していない。ここを黙って埋めると、推測した検索条件が
    そのまま送信対象になる (原則3)。

    観測できたらこの試験を消すこと -- 消すのは、``label.search_form`` に
    **似ている** と気付いたときではなく、**要求本文のキーを実測したとき** である。
    """
    text = COORDINATES.read_text(encoding="utf-8")
    assert "**要求本文の形はまだ観測していない。**" in text, (
        "要求本文が観測済みなら、この注記とこの試験を一緒に消してください。"
        "注記だけ消すと『呼べる』と読める座標ファイルになります。"
    )
