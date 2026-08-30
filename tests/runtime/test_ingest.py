"""取り込みの規律を固定する。**一番の仕事は、0件を0件として報告しないこと。**

参照実装の最悪の失敗は「静かなゼロ件」だった -- 取れなかったのに成功として
終わり、翌朝まで誰も気付かない。

だから ``IngestReport`` は **なぜ0件なのかを必ず1つ選ぶ**。3つは全く違う::

    LIST_NOT_CALLED  そもそも呼んでいない (上限の設定ミス)
    LIST_FAILED      呼んだが失敗した (セッション切れ等) -- **0件ではない**
    NO_ROWS          呼んで成功したが候補者が居なかった -- これだけが本当の0件
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from jobmedley_scout.api.client import JobMedleyApiClient
from jobmedley_scout.api.endpoints import build_endpoints
from jobmedley_scout.api.transport import HttpResponse, RecordedTransport
from jobmedley_scout.config.loader import load_site_coordinates
from jobmedley_scout.config.schema import IngestConfig, SafetyConfig
from jobmedley_scout.config.site_coordinates import SiteCoordinates
from jobmedley_scout.errors import ConfigError
from jobmedley_scout.runtime.commands.ingest import (
    MEMBER_ID_SLOT,
    PAGE_SLOT,
    IngestReport,
    IngestStage,
    PageResult,
    ingest,
)
from jobmedley_scout.state.db import connect, migrate
from tests.generation.helpers import make_clock

COORDINATES = load_site_coordinates(Path("config/site_coordinates.yaml"))

#: 検査用の要求本文。**本物の座標は使わない。**
#:
#: 2026-08-30 に ``api.candidate_list.payload_template`` を UNRESOLVED へ戻した
#: (形しか観測できていないものを確定扱いしていたため)。ここで見たいのはページ繰り
#: と報告の規律であって座標そのものではないので、呼べる形の代役を立てる。
#: 座標の中身は tests/guardrails/test_observed_read_coordinates.py が見る。
STAND_IN_TEMPLATE: dict[str, object] = {
    "age": {"from": "0", "to": "40"},
    "member_id": [],
    "customer_search_condition_id": "{{SEARCH_CONDITION_ID}}",
    "pagination": {"limit": "{{PAGE_SIZE}}", "page": "{{PAGE}}"},
}


def _coordinates(template: object = None) -> SiteCoordinates:
    """The real coordinates, with the list body swapped for a callable stand-in."""
    values = dict(COORDINATES.raw_items())
    values["api.candidate_list.payload_template"] = (
        json.dumps(STAND_IN_TEMPLATE, ensure_ascii=False) if template is None else template
    )
    return SiteCoordinates(values)


def _ingest_config(**overrides: object) -> IngestConfig:
    values: dict[str, object] = {
        "search_condition_id": "739599",
        "page_size": 25,
        "max_pages": 3,
        "fetch_resumes": False,
    }
    values.update(overrides)
    return IngestConfig(**values)  # type: ignore[arg-type]


def _safety(ingest_cap: int = 200) -> SafetyConfig:
    return SafetyConfig(
        dry_run=True,
        state_loss_guard=True,
        kill_switch_path=Path("/tmp/kill"),
        ingest_cap=ingest_cap,
        max_llm_requests_per_message=6,
    )


def _page(rows: int, *, uuid: str | None = "search-abc") -> HttpResponse:
    body: dict[str, object] = {
        "members": [{"id": 1000 + index} for index in range(rows)],
        "total": rows,
    }
    if uuid is not None:
        body["search_uuid"] = uuid
    return HttpResponse(status=200, body_text=json.dumps(body))


def _resume(self_pr: str = "訪問診療に関心があります。") -> HttpResponse:
    return HttpResponse(
        status=200,
        body_text=json.dumps(
            {
                "data": {
                    "memberGet": {
                        "member": {
                            "appeal": {
                                "selfPr": self_pr,
                                "qualifications": [{"name": "歯科衛生士", "isScheduled": False}],
                                "careerJobCategories": [],
                            },
                            "personalInformation": {"age": 24},
                            "desiredCondition": {
                                "jobCategories": [],
                                "workplaces": [],
                                "features": [{"id": 1, "name": "ネイルOK"}],
                            },
                        }
                    }
                }
            }
        ),
    )


def _run(
    responses: list[HttpResponse],
    *,
    config: IngestConfig | None = None,
    safety: SafetyConfig | None = None,
    template: object = None,
) -> tuple[IngestReport, RecordedTransport, sqlite3.Connection]:
    coordinates = _coordinates(template)
    transport = RecordedTransport(responses)
    client = JobMedleyApiClient(
        transport,
        auth_failure_codes=coordinates.string_list("api.auth_failure_codes"),
        idempotency_header=coordinates.optional_string("api.idempotency_header"),
    )
    clock = make_clock()
    connection = connect(Path(":memory:"))
    migrate(connection, clock)
    report = ingest(
        client,
        build_endpoints(coordinates),
        coordinates,
        config or _ingest_config(),
        safety or _safety(),
        connection,
        clock,
    )
    return report, transport, connection


# ---------------------------------------------------------------------------
# 0件の3つの意味を混ぜない
# ---------------------------------------------------------------------------


def test_a_failed_list_is_not_reported_as_zero_candidates() -> None:
    """**取れなかったことを「0件」にしない。** これが静かなゼロ件そのものである。"""
    report, _transport, _connection = _run([HttpResponse(status=500, body_text="{}")])
    assert report.reached() is IngestStage.LIST_FAILED
    text = report.render()
    assert "0件ではありません" in text
    assert "取りに行けていません" in text


def test_an_empty_but_successful_list_says_it_reached_the_platform() -> None:
    """**これだけが本当の0件である。** 届いてはいる。"""
    report, _transport, _connection = _run([_page(0)])
    assert report.reached() is IngestStage.NO_ROWS
    text = report.render()
    assert "応答は成功しましたが、候補者が0件" in text
    assert "search_condition_id" in text


def test_never_calling_the_list_is_its_own_stage() -> None:
    report = IngestReport()
    assert report.reached() is IngestStage.LIST_NOT_CALLED
    assert "1回も呼んでいません" in report.render()


def test_a_broken_chain_raises_rather_than_reporting_a_lie() -> None:
    """呼んでいないのに行が見えた、は矛盾である。**報告せず落とす。**"""
    report = IngestReport(pages=[PageResult(page=1, status=200, succeeded=False, rows=5)])
    with pytest.raises(ValueError, match="時系列と矛盾"):
        report.reached()


# ---------------------------------------------------------------------------
# ページ繰りと上限
# ---------------------------------------------------------------------------


def test_pages_are_walked_until_one_comes_back_empty() -> None:
    report, transport, _connection = _run([_page(25), _page(25), _page(0)])
    assert report.reached() is IngestStage.STORED
    assert report.rows_seen() == 50
    assert len(transport.requests) == 3
    assert [request.json_body["pagination"]["page"] for request in transport.requests] == [  # type: ignore[index]
        1,
        2,
        3,
    ]


def test_a_failed_page_stops_the_walk_rather_than_skipping_it() -> None:
    """**後半だけ取れた結果にしない。** 何件取れるはずだったのか分からなくなる。"""
    report, transport, _connection = _run(
        [_page(25), HttpResponse(status=500, body_text="{}"), _page(25)]
    )
    assert len(transport.requests) == 2
    assert report.rows_seen() == 25
    assert not report.pages[1].succeeded


def test_the_cap_is_reported_rather_than_silently_applied() -> None:
    """**黙って切らない。** 切った理由を述べないと「それで全部」に見える。"""
    report, _transport, _connection = _run([_page(25)], safety=_safety(ingest_cap=10))
    assert report.stored == 10
    assert "safety.ingest_cap" in report.capped_by
    assert "上限で打ち切りました" in report.render()


def test_running_out_of_pages_is_also_reported_as_a_cap() -> None:
    report, _transport, _connection = _run(
        [_page(25), _page(25)], config=_ingest_config(max_pages=2)
    )
    assert "ingest.max_pages" in report.capped_by


# ---------------------------------------------------------------------------
# 送信に要る値
# ---------------------------------------------------------------------------


def test_the_search_identifier_is_carried_out_of_the_same_response() -> None:
    """**1回の取得で候補者と検索識別子が揃う。**"""
    report, _transport, _connection = _run([_page(3), _page(0)])
    assert report.search_uuid == "search-abc"
    assert "検索識別子: 取れました" in report.render()


def test_a_missing_search_identifier_is_said_out_loud() -> None:
    report, _transport, _connection = _run([_page(3, uuid=None), _page(0, uuid=None)])
    assert report.search_uuid is None
    assert "**取れませんでした**" in report.render()


def test_the_saved_search_condition_reaches_the_request() -> None:
    _report, transport, _connection = _run([_page(0)], config=_ingest_config())
    body = transport.requests[0].json_body
    assert body is not None
    assert body["customer_search_condition_id"] == "739599"
    assert body["pagination"] == {"limit": 25, "page": 1}


def test_no_marker_survives_into_the_request() -> None:
    """**目印がそのまま媒体へ飛ばない。** 2つの家族があり、旧実装は片方しか見ていなかった。

    ``{{...}}`` は差し込むつもりだった箇所、``<...>`` は偵察が種別だけを記録した
    箇所である。**どちらも「まだ値が決まっていない」という同じ事実** なのに、
    ここは長く ``{{`` しか見ていなかった。だから ``"<bool>"`` が40キーぶん媒体へ
    飛んで HTTP 500 を返した実測35回目を、試験は緑のまま通した。
    """
    _report, transport, _connection = _run([_page(0)])
    sent = json.dumps(transport.requests[0].json_body, ensure_ascii=False)
    assert PAGE_SLOT not in sent
    assert "{{" not in sent
    assert "<" not in sent


def test_a_template_still_holding_a_recon_marker_refuses_to_call() -> None:
    """**呼ぶ前に止める。** 呼んでしまえば、失敗は「0件」として現れる (原則2)。

    実測35回目そのものである。送信路には最初からこの門があり
    (:func:`api.payloads.assert_fully_filled`)、読み取り路には無かった。
    その非対称に理由は無かった。
    """
    broken = json.dumps({**STAND_IN_TEMPLATE, "favorite": "<bool>"}, ensure_ascii=False)
    with pytest.raises(ConfigError) as caught:
        _run([_page(0)], template=broken)
    message = str(caught.value)
    assert "favorite" in message
    assert "api.candidate_list.payload_template" in message
    assert "observe-search" in message


def test_the_refusal_happens_before_any_request_reaches_the_platform() -> None:
    """止めるのは組み立ての時点であって、応答を見てからではない。"""
    transport = RecordedTransport([_page(0)])
    coordinates = _coordinates(json.dumps({**STAND_IN_TEMPLATE, "favorite": "<bool>"}))
    clock = make_clock()
    connection = connect(Path(":memory:"))
    migrate(connection, clock)
    with pytest.raises(ConfigError):
        ingest(
            JobMedleyApiClient(
                transport,
                auth_failure_codes=coordinates.string_list("api.auth_failure_codes"),
                idempotency_header=coordinates.optional_string("api.idempotency_header"),
            ),
            build_endpoints(coordinates),
            coordinates,
            _ingest_config(),
            _safety(),
            connection,
            clock,
        )
    assert transport.requests == [], "止めたと言いながら媒体へ呼びに行っています"


# ---------------------------------------------------------------------------
# レジュメ
# ---------------------------------------------------------------------------


def test_resumes_are_fetched_one_per_candidate_and_counted() -> None:
    report, transport, _connection = _run(
        [_page(2), _resume(), _resume()], config=_ingest_config(fetch_resumes=True, max_pages=1)
    )
    assert report.resumes_requested == 2
    assert report.resumes_read == 2
    assert MEMBER_ID_SLOT not in json.dumps(transport.requests[1].json_body, ensure_ascii=False)


def test_a_candidate_whose_resume_failed_is_kept_rather_than_dropped() -> None:
    """**レジュメが読めないことは、その候補者が居ないことではない。**

    落とすと件数が黙って減る。空のまま進めば「非公開」として渡るので、モデルは
    創作できない。
    """
    report, _transport, connection = _run(
        [_page(2), HttpResponse(status=500, body_text="{}"), _resume()],
        config=_ingest_config(fetch_resumes=True, max_pages=1),
    )
    assert report.resumes_requested == 2
    assert report.resumes_read == 1
    assert report.stored == 2


def test_the_resume_facts_reach_the_database_rows() -> None:
    _report, _transport, connection = _run(
        [_page(1), _resume()], config=_ingest_config(fetch_resumes=True, max_pages=1)
    )
    from jobmedley_scout.state import candidate_repo

    ids = candidate_repo.candidate_ids(connection)
    assert len(ids) == 1
    # **氏名は空のまま。** この媒体に氏名は無い。
    assert candidate_repo.display_name_of(connection, ids[0]) == ""


def test_skipping_resumes_says_so_rather_than_looking_like_zero() -> None:
    report, _transport, _connection = _run([_page(1), _page(0)])
    assert "取りに行っていません" in report.render()


def test_the_member_number_survives_into_the_database() -> None:
    """**取り込みと生成は別のプロセスである。**

    途中で持ち回せるのはDBだけなので、宛名に使う番号をここに残さなければ
    生成の時点で消えている。
    """
    from jobmedley_scout.state import candidate_repo

    page = HttpResponse(
        status=200,
        body_text=json.dumps(
            {"members": [{"id": 3323741, "code": "01613058"}], "search_uuid": "u", "total": 1}
        ),
    )
    _report, _transport, connection = _run([page], config=_ingest_config(max_pages=1))
    assert candidate_repo.member_code_of(connection, "3323741") == "01613058"
    # **氏名の欄は空のまま。**
    assert candidate_repo.display_name_of(connection, "3323741") == ""
