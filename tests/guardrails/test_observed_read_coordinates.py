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

import json
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


# ---------------------------------------------------------------------------
# 要求本文の雛形
#
# **ここに在った3つの試験が、欠陥を固定していた** (2026-08-30)。
#
# 旧 test_the_template_does_not_ship_real_search_values は、雛形の ``age`` が
# ``{"from": "<string>", "to": "<string>"}`` **であること** を要求していた。
# 理由として 13.2 (個人データをログや設定に残さない) を挙げていたが、これは
# 取り違えである -- 13.2 が守るのは **候補者** の氏名・会員番号・年齢・居住地で
# あり、この本文は **運用者自身が保存した検索条件** である。候補者は返ってくる
# *応答* の側にある。
#
# 結果、実測35回目 (scout preview 1回目) で ``"<bool>"`` や ``"<number>"`` と
# いう文字列が40キーぶん媒体へ飛び、HTTP 500 が返った。報告は正しく「0件では
# なく取りに行けていません」と言ったが、**試験は最後まで緑だった**。
#
# 教訓は「絞り込みの値を伏せるな」ではない。**伏せた値を、確定した値のふりを
# して置くな** である。形しか観測していないものは UNRESOLVED である。
# ---------------------------------------------------------------------------


def _template() -> str:
    """The coordinate, or a **visible skip** while it is still UNRESOLVED.

    黙って通さない。``pytest.skip`` は報告に残るので、「未確定なので見ていない」
    ことが数として見える -- 旧実装のように「緑だから正しい」とは読めなくなる。
    """
    value = _coordinates()["api.candidate_list.payload_template"]
    if not isinstance(value, str) or value.strip() == "UNRESOLVED":
        pytest.skip(
            "api.candidate_list.payload_template は UNRESOLVED。"
            "`scout recon observe-search` で観測して記入するまで検査できません。"
        )
    return value


def test_the_template_is_either_unresolved_or_actually_callable() -> None:
    """**中間の状態を許さない。** 形だけ観測したものを確定扱いしない。

    ここが旧実装の欠陥そのものである。旧実装は ``age`` が ``"<string>"``
    **であること** を要求していた。つまり「呼べない本文」を正しい状態として
    固定していた。いまは逆で、**差し込みを済ませたら呼べる本文になる** ことを
    要求する -- 偵察の印が1つでも残っていたら落ちる。
    """
    from jobmedley_scout.api.payloads import assert_fully_filled
    from jobmedley_scout.runtime.commands.ingest import (
        PAGE_SIZE_SLOT,
        PAGE_SLOT,
        SEARCH_CONDITION_SLOT,
        _substitute_slots,
    )

    body = _substitute_slots(
        json.loads(_template()),
        {SEARCH_CONDITION_SLOT: "739599", PAGE_SLOT: 1, PAGE_SIZE_SLOT: 25},
    )
    assert isinstance(body, dict)
    assert_fully_filled(
        body,
        used_by="tests.guardrails.test_observed_read_coordinates",
        coordinate="api.candidate_list.payload_template",
        consequence="**このまま呼ぶと媒体はエラーを返し、それが「0件」として現れます。**",
    )


def test_a_filled_template_carries_the_three_runtime_slots() -> None:
    """**この媒体の一覧は「保存した検索条件 + ページ」で引く。**

    3つのうち1つでも欠けると静かに間違う。``{{PAGE}}`` が無ければ毎回1ページ目を
    引きながら報告だけがページを進み、``{{SEARCH_CONDITION_ID}}`` が無ければ
    別の保存条件の母集団を引く (原則2)。
    """
    from jobmedley_scout.runtime.commands.ingest import LIST_SLOT_PATTERN

    body = json.loads(_template())
    slots = [
        body["customer_search_condition_id"],
        body["pagination"]["limit"],
        body["pagination"]["page"],
    ]
    for slot in slots:
        assert isinstance(slot, str) and LIST_SLOT_PATTERN.match(slot), (
            f"差し込み欄が記法になっていません: {slot!r}。"
            f"`scout recon observe-search` の出力をそのまま貼ってください。"
        )


def test_a_filled_template_keeps_the_filter_keys_that_were_observed() -> None:
    """絞り込みの欄を落とすと、**別の母集団を引いたまま気付かない** (原則2)。

    観測した欄の一覧は :data:`recon.search_payload.OBSERVED_FILTER_KEYS` に在り、
    ``observe-search`` は観測のたびに差分を報告する。ここではその一覧が座標と
    食い違っていないことだけを固定する。
    """
    from jobmedley_scout.recon.search_payload import OBSERVED_FILTER_KEYS

    body = json.loads(_template())
    missing = [key for key in OBSERVED_FILTER_KEYS if key not in body]
    assert not missing, f"実測した要求本文の {missing} が雛形から消えています"


def test_the_saved_condition_id_is_a_slot_rather_than_a_literal() -> None:
    """**保存条件のIDは実行時に config.yaml から入る。**

    座標に直書きすると、config.yaml の ingest.search_condition_id を変えても
    引く母集団が変わらない -- 設定が効かないことに誰も気付かない (原則2)。
    """
    assert "739599" not in _template(), "保存条件の実IDが雛形に直書きされています"


def test_a_filled_template_never_names_a_candidate() -> None:
    """**唯一伏せる欄。** ``member_id`` が埋まっていれば候補者を名指しする。

    :data:`recon.search_payload.WITHHELD_KEYS` がここを伏せ、伏せた印は
    ``<withheld>`` なので、貼ったまま呼べば門が止める。
    """
    from jobmedley_scout.recon.search_payload import WITHHELD_MARKER

    member_id = json.loads(_template())["member_id"]
    assert member_id in ([], WITHHELD_MARKER), (
        f"絞り込みの member_id に候補者が入っています: {type(member_id).__name__}。"
        f"これは13.2 が守る対象そのものです。"
    )


def test_the_member_id_filter_is_not_mistaken_for_the_send_payload_field() -> None:
    """**同名の別物である。**

    要求本文の ``member_id`` は絞り込み条件 (配列) で、送信payloadの
    ``memberId`` (単数) とは別物。6.4 の「業界/職種の取り違え」と同じ形なので、
    座標ファイルに注記が在ることを固定する。
    """
    text = COORDINATES.read_text(encoding="utf-8")
    assert (
        "送信payloadの memberId" in text
    ), "member_id が送信payloadの memberId ではないことの注記が消えています"


def test_the_candidate_list_endpoint_needs_its_payload_template_before_ingest() -> None:
    """雛形が無いまま ingest を通してはいけない。

    通せば 400 か「絞り込み無しの全件」が返る。**どちらも静かに間違う** --
    前者は0件、後者は対象外の候補者へ送る材料になる (原則2 / 13.6)。
    """
    from jobmedley_scout.config.coordinates import REQUIRED_BY_COMMAND

    assert "api.candidate_list.payload_template" in REQUIRED_BY_COMMAND["ingest"]


# ===========================================================================
# 氏名 -- **この媒体の一覧には無い** (実測24回目)
# ===========================================================================


def test_a_candidate_can_exist_without_a_name() -> None:
    """**``code`` を氏名の欄へ入れさせない。**

    参照実装は氏名がある前提で ``display_name`` を必須にしていた。実測した
    一覧の応答に氏名の欄は無く、在るのは ``code`` (会員番号) である。
    必須のままなら取り込みは「何かを入れる」しかなくなり、入るのは ``code``
    になる。それは「氏名: 3323741」としてモデルに渡り、モデルはそれを名前と
    して文面に書く -- 6.4 の取り違えと同じ事故である。
    """
    from jobmedley_scout.models.candidate import Candidate

    candidate = Candidate(candidate_id="3323741", raw_id_observed="3323741")
    assert candidate.display_name is None


def test_a_nameless_candidate_reaches_the_model_as_undisclosed() -> None:
    """空欄は **省略ではなく「非公開」** として渡る (8.3 対策1)。

    省略するとモデルは「渡し忘れ」と区別できず、補って書く余地が残る。
    """
    from jobmedley_scout.generation.facts import UNDISCLOSED, build_facts
    from jobmedley_scout.models.candidate import Candidate

    facts = build_facts(Candidate(candidate_id="3323741", raw_id_observed="3323741"))
    assert facts.display_name.label == "氏名"
    assert facts.display_name.rendered == UNDISCLOSED


def test_a_nameless_candidate_can_be_stored() -> None:
    """列は ``NOT NULL`` なので、保存の境界で空文字にする。

    **保存の境界であってプロンプトの境界ではない。** モデルへ渡るのは
    ``Candidate`` 側で、そちらは ``None`` のまま「非公開」として渡る。
    """
    from jobmedley_scout.models.candidate import Candidate
    from jobmedley_scout.state import candidate_repo
    from jobmedley_scout.state.db import connect, migrate
    from tests.generation.helpers import make_clock

    clock = make_clock()
    connection = connect(Path(":memory:"))
    migrate(connection, clock)
    candidate_repo.upsert_candidate(
        connection,
        Candidate(candidate_id="3323741", raw_id_observed="3323741"),
        source="members/search",
        clock=clock,
    )
    assert candidate_repo.display_name_of(connection, "3323741") == ""


def test_a_subject_refuses_to_be_built_without_a_name() -> None:
    """**「様」だけの件名を送らない。**

    氏名が無い媒体では件名の組み立ては成立しない。ここで例外になるのが正しい
    -- 黙って「様 | ... 」を作れば、それがそのまま候補者に届く (13.6)。

    なお **この媒体には件名の欄そのものが無い** (送信payloadに subject が無い)。
    件名を要求する経路をこの媒体で使うこと自体が誤りである。
    """
    from jobmedley_scout.errors import GenerationError
    from jobmedley_scout.generation.subject import build_subject
    from jobmedley_scout.models.candidate import Candidate
    from jobmedley_scout.models.message import GeneratedCore
    from tests.generation.helpers import PREFIX_LEN, make_clock

    core = GeneratedCore(
        subject="ご案内",
        opening="はじめまして。",
        motivation="拝見しました。",
        introductions=(),
        closing="よろしくお願いいたします。",
    )
    with pytest.raises(GenerationError, match="氏名"):
        build_subject(
            Candidate(candidate_id="3323741", raw_id_observed="3323741"),
            core,
            make_clock(),
            already_used_subjects=(),
            prefix_len=PREFIX_LEN,
        )
