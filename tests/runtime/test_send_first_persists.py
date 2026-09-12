"""**送信路を、本物のスキーマに対して通す。**

2026-09-11、実送信の1通目が ``sqlite3.IntegrityError: FOREIGN KEY constraint
failed`` で落ちた。``send_records.candidate_id`` は ``candidates(candidate_id)``
への外部キーなのに、``send_first`` が **送る相手を保存せずに** 冪等キーを
予約していたためである。

原因は共用の仕方にあった。``collect_candidates`` は下見と送信で共用しており、
**下見は保存しないことが要点** である（送っていないのに「取り込み済み」になると、
後から送信対象だったのかが分からなくなる）。その関数をそのまま送信路で使ったので、
保存だけが抜けた。**下見と送信で、保存の要否が正反対だった。**

既存の検査が通り抜けた理由は2つある。

* ``test_send_first_gates.py`` は門が **閉じている** ことだけを見る。門の先は
  共同作業者を全部罠にしてあるので、そもそも到達しない
* ``test_send_repo_integration.py`` は本物のDBを使うが、**候補者を手で
  INSERT してから** 予約する。外部キーは最初から満たされている

つまり「本物のスキーマ」と「本物の呼び出し順」が、一度も同じ場所に無かった。
このファイルがその場所である。門の **先** を、本物のDBに対して通す。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from jobmedley_scout.api.client import ApiOutcome
from jobmedley_scout.api.endpoints import QUOTA, SEND_PAID, Endpoint
from jobmedley_scout.api.transport import HttpResponse
from jobmedley_scout.clock import FixedClock
from jobmedley_scout.config.schema import IngestConfig, LlmConfig, SafetyConfig
from jobmedley_scout.generation.scout_message import GeneratedMessage, GenerationOutcome
from jobmedley_scout.models.candidate import (
    Candidate,
    ScoutHistoryEntry,
    ScoutHistorySummary,
)
from jobmedley_scout.models.send_record import SendSlot
from jobmedley_scout.runtime.commands import send_first as send_first_module
from jobmedley_scout.runtime.commands.ingest import IngestReport
from jobmedley_scout.runtime.commands.send_first import (  # noqa: F401
    FirstSendReport,
    FirstSendStage,
    send_first,
)
from jobmedley_scout.state.db import connect, migrate

START = datetime(2026, 9, 11, 9, 0, tzinfo=UTC)
CANDIDATE_ID = "3323741"
SEARCH_UUID = "uuid-from-the-list-response"

#: 観測できて、履歴が空だった状態。**未観測 (None) とは別物である。**
_OBSERVED_NONE = ScoutHistorySummary()

#: 実測した送信 payload の形。記法はすべて差し込みで埋まる。
#:
#: ``query`` を省くと :func:`api.payloads.build_send_payload` が撥ねる -- 封筒ごと
#: 記録していない雛形では送れないためである。**この検査を書いたときに実際に
#: 撥ねられた。** 門が効いている証拠なので、雛形の側を実測の形に合わせる。
PAYLOAD_TEMPLATE = (
    '{"operationName": "SendSingleScout",'
    ' "query": "mutation SendSingleScout($input: SendSingleScoutInput!)'
    ' { sendSingleScout(input: $input) { scoutedMemberId errorMessage __typename } }",'
    ' "variables": {"input": {'
    '"jobOfferId": 1, "jobOfferSalaryId": 2,'
    ' "memberId": "{{CANDIDATE_ID}}",'
    ' "scoutMessage": "{{BODY}}",'
    ' "searchUuid": "{{SEARCH_UUID}}"}}}'
)


class _Coordinates:
    """Only what the send path actually asks for."""

    def json_path(self, key: str) -> str:
        assert key == "api.send.paid.payload_template"
        return PAYLOAD_TEMPLATE


class _Client:
    """Records the one call the send path makes, and answers 200."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def call(
        self,
        endpoint: Endpoint,
        *,
        url: str,
        json_body: object = None,
        idempotency_key: str | None = None,
    ) -> ApiOutcome:
        self.calls.append({"url": url, "body": json_body, "key": idempotency_key})
        return ApiOutcome(
            endpoint_id=endpoint.id,
            status=200,
            succeeded=True,
            response=HttpResponse(
                status=200,
                body_text='{"data": {"sendSingleScout": {"scoutedMemberId": "m1",'
                ' "errorMessage": null, "__typename": "SendSingleScoutPayload"}}}',
                headers={"Content-Type": "application/json"},
            ),
        )


def _candidate(history: ScoutHistorySummary | None = _OBSERVED_NONE) -> Candidate:
    """A candidate. **既定は「観測して履歴なし」** -- 新規候補者の通常形である。

    既定を ``None`` (未観測) にしてはいけない。未観測は安全側に倒れて除外される
    ので、この検査ファイルの主題である「送信まで到達する」が永久に成立しなく
    なる。実際、直近送信の関門を入れた直後にこの7件が全部落ちた -- 関門が
    効いている証拠であり、検査側を実態へ合わせるのが正しい。
    """
    return Candidate(
        candidate_id=CANDIDATE_ID,
        raw_id_observed=CANDIDATE_ID,
        member_code="00831678",
        residence="東京都渋谷区",
        scout_history=history,
    )


def _message() -> GeneratedMessage:
    return GeneratedMessage(
        candidate_id=CANDIDATE_ID,
        outcome=GenerationOutcome.GENERATED,
        body="本文" * 200,
        attempts=1,
        requests=1,
    )


@pytest.fixture()
def clock() -> FixedClock:
    return FixedClock(START)


@pytest.fixture()
def db(tmp_path: Path, clock: FixedClock) -> sqlite3.Connection:
    """**本物のスキーマ。** 外部キーもそのまま効いている。"""
    connection = connect(tmp_path / "scout.db")
    migrate(connection, clock)
    return connection


@pytest.fixture()
def run(
    monkeypatch: pytest.MonkeyPatch,
    db: sqlite3.Connection,
    clock: FixedClock,
    tmp_path: Path,
) -> Any:
    """送信路を、取り込みと生成だけ差し替えて通す。

    差し替えるのは **外部に出る2つ** だけである。保存も予約も送信も、本物の
    コードが本物のDBに対して走る -- そうでなければ、今回の欠陥はまた通り抜ける。
    """
    client = _Client()

    def _collect(*_args: Any, **_kwargs: Any) -> tuple[IngestReport, list[Candidate]]:
        report = IngestReport()
        report.search_uuid = SEARCH_UUID
        return report, [_candidate()]

    monkeypatch.setattr(send_first_module, "collect_candidates", _collect)
    monkeypatch.setattr(send_first_module, "generate_scout_body", lambda *a, **k: _message())

    def _go() -> FirstSendReport:
        return send_first(
            client,  # type: ignore[arg-type]
            {
                SEND_PAID: Endpoint(
                    id=SEND_PAID,
                    method="POST",
                    url_pattern="https://example.invalid/graphql/SendSingleScout",
                    success_statuses=None,
                    slot=SendSlot.PAID,
                    side_effectful=True,
                )
            },
            _Coordinates(),  # type: ignore[arg-type]
            IngestConfig(search_condition_id="1", page_size=25, max_pages=1, fetch_resumes=True),
            SafetyConfig(
                dry_run=False,
                state_loss_guard=True,
                kill_switch_path=tmp_path / "kill",
                ingest_cap=200,
                max_llm_requests_per_message=6,
            ),
            db,
            clock,
            llm=object(),  # type: ignore[arg-type]
            llm_config=LlmConfig(
                model="claude-sonnet-5",
                max_tokens=16000,
                thinking_enabled=True,
                effort="medium",
                max_retries=3,
            ),
            prompt_template="",
            clinic={},
            clinic_address="",
            max_requests=6,
            acknowledged=True,
            run_id="test-run",
            destination=tmp_path / "sent.md",
            skip_if_scouted_within_days=3,
        )

    _go.client = client  # type: ignore[attr-defined]
    return _go


def test_the_send_path_reaches_the_send(run: Any) -> None:
    """**これが欠けていた検査である。**

    門のテストは門が閉じていることしか見ず、DBのテストは候補者を手で入れていた。
    どちらも緑のまま、実送信は外部キー違反で落ちた。
    """
    report = run()
    assert report.reached() is FirstSendStage.SENT
    assert len(run.client.calls) == 1, "送信APIがちょうど1回呼ばれること"


def test_the_recipient_is_persisted_before_the_key_is_reserved(
    run: Any, db: sqlite3.Connection
) -> None:
    """送る相手が ``candidates`` に載ること。

    載っていなければ冪等キーを予約できない（外部キー）。**そして載せる理由は
    外部キーだけではない** -- 誰に送ったか分からない送信を 9.4 が禁じているし、
    次回の重複判定もここを見る (9.3)。
    """
    report = run()
    assert report.stored is True
    rows = db.execute(
        "SELECT candidate_id FROM candidates WHERE candidate_id = ?", (CANDIDATE_ID,)
    ).fetchall()
    assert len(rows) == 1


def test_the_send_record_lands_as_sent(run: Any, db: sqlite3.Connection) -> None:
    """送信記録が ``sent`` で残ること。**枠は後から復元できない** (9.4)。"""
    run()
    row = db.execute(
        "SELECT status, send_slot, endpoint_id FROM send_records WHERE candidate_id = ?",
        (CANDIDATE_ID,),
    ).fetchone()
    assert row is not None, "送信記録が1行も無い"
    assert row[0] == "sent"
    assert row[1] == str(SendSlot.PAID)
    assert row[2] == SEND_PAID


def test_no_placeholder_survives_into_the_request(run: Any) -> None:
    """記法が媒体へそのまま渡らないこと (13.6)。

    ``{{SEARCH_UUID}}`` は実行時にしか分からない値なので、**送信路で初めて**
    埋まる。埋め忘れれば媒体に記法が届く。
    """
    run()
    sent = str(run.client.calls[0]["body"])
    assert "{{" not in sent, f"記法が残ったまま送っている: {sent}"
    assert SEARCH_UUID in sent


def test_the_idempotency_key_travels_with_the_request(run: Any) -> None:
    """予約したキーが、その要求に付いて出ること (9.2)。

    予約だけしてヘッダに乗せなければ、サーバ側の重複排除は効かない。
    """
    run()
    assert run.client.calls[0]["key"], "冪等キーが要求に乗っていない"


def test_the_saved_copy_does_not_claim_a_send_that_has_not_happened(
    run: Any, tmp_path: Path
) -> None:
    """成果物の見出しが、実際に起きたことと一致すること。

    以前は見出しが「1通目に送った文面」で決め打ちだった。この関数は **送信の
    前に** 呼ばれるので、送る前から「送った」と書いていた。``searchUuid`` が
    取れずに引き返せば、送っていないのに「送った文面」と題されたファイルだけが
    成果物に残る。
    """
    run()
    written = (tmp_path / "sent.md").read_text(encoding="utf-8")
    assert written.startswith("# 1通目に **送った** 文面")


def test_a_copy_survives_even_when_the_send_never_happens(
    monkeypatch: pytest.MonkeyPatch,
    run: Any,
    tmp_path: Path,
) -> None:
    """送らずに引き返しても文面は残り、**送ったとは書かれない** こと。

    残すこと自体は正しい（取り消せない操作の記録）。嘘をつかないことと両立する
    必要がある。
    """

    def _no_uuid(*_args: Any, **_kwargs: Any) -> tuple[IngestReport, list[Candidate]]:
        return IngestReport(), [_candidate()]  # search_uuid が空のまま

    monkeypatch.setattr(send_first_module, "collect_candidates", _no_uuid)
    report = run()

    assert report.reached() is FirstSendStage.NO_SEARCH_UUID
    assert not run.client.calls, "送信APIが呼ばれている"
    written = (tmp_path / "sent.md").read_text(encoding="utf-8")
    assert "まだ送っていません" in written
    assert "送った** 文面" not in written


# --------------------------------------------------------------------------
# 辞退した相手を外す -- **判定は書かれていたが、誰も呼んでいなかった**
# --------------------------------------------------------------------------


def test_a_candidate_who_refused_a_scout_is_never_sent_to(
    monkeypatch: pytest.MonkeyPatch, run: Any
) -> None:
    """**断った相手には送らない。**

    2026-09-12、本番の送信が ``HTTP 200`` かつ ``errorMessage`` 有りで失敗した。
    原因を追う途中で ``ScoutHistorySummary.refused()`` の呼び出し元が **0件**
    であることが分かった。媒体から ``latestRefusedAt`` を取り込み、モデルに
    ``refused()`` を書き、そこで止まっていた。

    **部品ごとには正しく、部品のあいだが繋がっていない** -- 実測48・49・55回目と
    同じ形である。だから検査も同じ形にする: **関門の先まで、本物のコードで通す。**

    ``refused()`` が真になる候補者を1人だけ返し、**送信要求が1件も飛ばない**
    ことを見る。単体で ``refused_scout()`` を呼ぶ検査では、今回の欠陥
    (誰も呼んでいない) は捕まらない。

    **送信日時を古い実日時で埋めてあるのが要点である。** 最初に書いたときは
    ``latest_sent_at`` を空にしていて、候補者は確かに外れたが **外したのは直近
    送信の関門だった** (日時が読めない → 判定不能 → 外す)。辞退の関門を消しても
    この検査は緑になってしまう。**別の理由で緑になる検査は、何も守っていない。**
    そこで直近送信の関門は素通りさせ、辞退だけで外れる形にしてある。
    """
    refused = ScoutHistorySummary(
        entries=(
            ScoutHistoryEntry(
                # 1年以上前。直近送信の関門は NO_MATCH を返して通す。
                latest_sent_at="2025/05/28 10:00:00",
                sent_count=1,
                latest_refused_at="2025/06/01 10:00:00",
            ),
        )
    )

    def _collect(*_args: Any, **_kwargs: Any) -> tuple[IngestReport, list[Candidate]]:
        report = IngestReport()
        report.search_uuid = SEARCH_UUID
        return report, [_candidate(refused)]

    monkeypatch.setattr(send_first_module, "collect_candidates", _collect)

    report = run()

    assert run.client.calls == [], "辞退した相手へ送信要求が飛んでいる"
    assert report.chosen is False
    assert report.stored is False
    assert report.skipped, "外した理由が報告に残っていない (原則2)"
    assert any("辞退" in outcome.evidence for outcome in report.skipped)


def test_a_candidate_who_did_not_refuse_still_gets_the_message(run: Any) -> None:
    """**倒しすぎないこと。** 断っていない相手は通ること。

    安全側へ倒す関門は、倒しすぎれば「静かなゼロ件」になる (原則2)。既定の
    候補者 (観測済み・履歴なし・辞退なし) は送信まで到達しなければならない。
    """
    report = run()
    assert report.chosen is True
    assert len(run.client.calls) == 1


# --------------------------------------------------------------------------
# 送信枠の残数 -- **座標は満たしていたが、コードは読んでいなかった**
# --------------------------------------------------------------------------


def _with_quota(body: dict[str, object]) -> Any:
    """A client that answers the quota query, then the send."""

    class _QuotaClient(_Client):
        def call(
            self,
            endpoint: Endpoint,
            *,
            url: str,
            json_body: Any = None,
            idempotency_key: str | None = None,
        ) -> ApiOutcome:
            if endpoint.id == QUOTA:
                return ApiOutcome(
                    endpoint_id=endpoint.id,
                    status=200,
                    succeeded=True,
                    response=HttpResponse(
                        status=200,
                        body_text=json.dumps(body),
                        headers={"Content-Type": "application/json"},
                    ),
                )
            return super().call(
                endpoint, url=url, json_body=json_body, idempotency_key=idempotency_key
            )

    return _QuotaClient()


_QUOTA_ENDPOINT = Endpoint(
    id=QUOTA,
    method="GET",
    url_pattern="https://example.invalid/api/customers/messages/scout_count/",
    success_statuses=frozenset(range(200, 300)),
    slot=SendSlot.UNKNOWN,
    side_effectful=False,
)


def _run_with(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
    db: sqlite3.Connection,
    clock: FixedClock,
    tmp_path: Path,
    candidate: Candidate | None = None,
) -> FirstSendReport:
    """送信路を、渡した client で通す。**残数照会の endpoint を組んである。**

    :func:`run` フィクスチャは ``SEND_PAID`` だけを渡すので、残数照会は
    「endpointが組まれていません」で読めないまま通る。それでは今回足した配線を
    一度も踏まないので、こちらを使う。
    """
    row = candidate if candidate is not None else _candidate()

    def _collect(*_args: Any, **_kwargs: Any) -> tuple[IngestReport, list[Candidate]]:
        report = IngestReport()
        report.search_uuid = SEARCH_UUID
        return report, [row]

    monkeypatch.setattr(send_first_module, "collect_candidates", _collect)
    monkeypatch.setattr(send_first_module, "generate_scout_body", lambda *a, **k: _message())

    return send_first(
        client,
        {
            SEND_PAID: Endpoint(
                id=SEND_PAID,
                method="POST",
                url_pattern="https://example.invalid/graphql/SendSingleScout",
                success_statuses=None,
                slot=SendSlot.PAID,
                side_effectful=True,
            ),
            QUOTA: _QUOTA_ENDPOINT,
        },
        _Coordinates(),  # type: ignore[arg-type]
        IngestConfig(search_condition_id="1", page_size=25, max_pages=1, fetch_resumes=True),
        SafetyConfig(
            dry_run=False,
            state_loss_guard=True,
            kill_switch_path=tmp_path / "kill",
            ingest_cap=200,
            max_llm_requests_per_message=6,
        ),
        db,
        clock,
        llm=object(),  # type: ignore[arg-type]
        llm_config=LlmConfig(
            model="claude-sonnet-5",
            max_tokens=16000,
            thinking_enabled=True,
            effort="medium",
            max_retries=3,
        ),
        prompt_template="",
        clinic={},
        clinic_address="",
        max_requests=6,
        acknowledged=True,
        run_id="test-run",
        destination=tmp_path / "sent.md",
        skip_if_scouted_within_days=3,
    )


def test_a_used_up_quota_stops_before_the_send(
    monkeypatch: pytest.MonkeyPatch,
    db: sqlite3.Connection,
    clock: FixedClock,
    tmp_path: Path,
) -> None:
    """**枠を使い切っていたら送らない。**

    2026-09-12 まで ``api.quota.url_pattern`` は **一度も呼ばれていなかった。**
    座標は 2026-08-21 に観測済みで、注記には「これは8章の『残数は毎回読む』を
    満たす」と書いてあった。満たしていたのは座標だけである。

    読まないあいだ、枠切れは **理由の分からない失敗** として現れる -- 媒体の
    エラー文言は 13.2 のため記録していないので、画面を見るまで誰にも分からない。
    """
    client = _with_quota({"remaining_count": 0, "total_count": 62})
    report = _run_with(client, monkeypatch, db, clock, tmp_path)

    assert report.reached() is FirstSendStage.QUOTA_EXHAUSTED
    assert report.quota is not None
    assert report.quota.remaining == 0
    # 送信要求が飛んでいないこと。照会 (GET) は飛んでよい。
    assert [c for c in client.calls if c[0].id != QUOTA] == []
    assert "送っていません" in report.render()
    assert "使い切って" in report.render()


def test_room_in_the_quota_lets_the_send_through(
    monkeypatch: pytest.MonkeyPatch,
    db: sqlite3.Connection,
    clock: FixedClock,
    tmp_path: Path,
) -> None:
    """**倒しすぎないこと。** 枠が在れば送る。残数は報告に出る (8章)。"""
    client = _with_quota({"remaining_count": 51, "total_count": 62})
    report = _run_with(client, monkeypatch, db, clock, tmp_path)

    assert report.reached() is FirstSendStage.SENT
    assert "51" in report.render()


def test_an_unreadable_quota_does_not_stop_the_send(
    monkeypatch: pytest.MonkeyPatch,
    db: sqlite3.Connection,
    clock: FixedClock,
    tmp_path: Path,
) -> None:
    """**読めないことを「枠切れ」に倒さない。**

    倒すと、応答の形が変わった日に送信が全部止まる -- 「静かなゼロ件」の別の顔
    である。読めないことは報告に出し、送信自体は他の関門に委ねる。枠が本当に
    無ければ媒体が断るので、取り消せない事故にはならない。
    """
    client = _with_quota({"remaining": 51})
    report = _run_with(client, monkeypatch, db, clock, tmp_path)

    assert report.reached() is FirstSendStage.SENT
    assert report.quota is not None
    assert report.quota.readable() is False
    assert "読めません" in report.render()
