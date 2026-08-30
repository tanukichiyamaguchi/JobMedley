"""候補者を取り込む。**一覧とレジュメを読み、DBへ入れる。送信は一切しない。**

段階3で読み取りの座標が揃ったので、初めて実際に取りに行ける。

    POST  members/search/                                一覧 (ページ繰り)
    POST  graphql/MemberOnScoutProfileModalOfDesktop     レジュメ (1人ずつ)

**このコマンドの一番の仕事は、0件を0件として報告しないことである** (原則2)。

参照実装の最悪の失敗は「静かなゼロ件」だった -- 取れなかったのに成功として
終わり、翌朝まで誰も気付かない。だから ``IngestReport`` は **なぜ0件なのかを
必ず1つ選ぶ**。選べない状態は矛盾なので例外にする。

**判断はここに置かない。** 応答をモデルへ写すのは :mod:`api.candidates` (純粋)
で、ここがやるのは順番・ページ繰り・上限・保存だけである (13.4)。
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from jobmedley_scout.api.candidates import (
    candidate_from_row,
    describe_row_shapes,
    resume_from_response,
    resume_keypaths,
    rows_in,
    search_uuid_in,
    unresolved_resume_fields,
)
from jobmedley_scout.api.client import ApiOutcome, JobMedleyApiClient
from jobmedley_scout.api.endpoints import CANDIDATE_LIST, RESUME, Endpoint
from jobmedley_scout.api.payloads import assert_fully_filled, parse_payload_template
from jobmedley_scout.api.success import describe_body_shape, describe_failure
from jobmedley_scout.clock import Clock
from jobmedley_scout.config.placeholders import require
from jobmedley_scout.config.schema import IngestConfig, SafetyConfig
from jobmedley_scout.config.site_coordinates import SiteCoordinates
from jobmedley_scout.errors import ConfigError
from jobmedley_scout.models.candidate import Candidate
from jobmedley_scout.state import candidate_repo

#: 一覧の要求本文で差し替える欄の名前。**座標側と揃っていること**
#: (:data:`recon.search_payload.RUNTIME_SLOTS`)。
SEARCH_CONDITION_SLOT = "SEARCH_CONDITION_ID"
PAGE_SLOT = "PAGE"
PAGE_SIZE_SLOT = "PAGE_SIZE"

#: 一覧の差し込み記法。**名前と、観測した型を一緒に運ぶ。**
#:
#: この媒体は型が一貫していない (実測36回目)。同じ職種IDが
#: ``desired_job_category_ids: ["10"]`` では文字列、
#: ``career_job_categories[].job_category_id: 10`` では数値である。だから型を
#: こちらで決められない -- 決めれば推測になる (原則3)。
#:
#: 一番危ないのは ``pagination.page`` である。型違いで媒体が無視した場合、毎回
#: 1ページ目が返り、**報告だけがページを進む**。エラーは出ないので、重複が
#: 静かに育つ (原則2)。
LIST_SLOT_PATTERN = re.compile(r"^\{\{([A-Z_]+):(string|number)\}\}$")

#: レジュメの要求本文で差し替える目印。**こちらは記法のまま。**
#:
#: レジュメは GraphQL で、変数の型がスキーマ側で決まっている
#: (``memberId`` を実測20回目に観測済み)。型を観測から運ぶ必要が無い。
MEMBER_ID_SLOT = "{{MEMBER_ID}}"

#: 取り込み元の名前。``candidates.source`` に残る。
SOURCE = "members/search"


class IngestStage(StrEnum):
    """辿り着いた段。**時系列の順に並んでいる。**"""

    LIST_NOT_CALLED = "list_not_called"
    LIST_FAILED = "list_failed"
    NO_ROWS = "no_rows"
    STORED = "stored"


@dataclass(frozen=True)
class PageResult:
    """One page of the list. **失敗も1件として残す。**"""

    page: int
    status: int
    succeeded: bool
    rows: int


@dataclass
class IngestReport:
    """What actually happened. **0件の理由を必ず1つ選ぶ。**"""

    pages: list[PageResult] = field(default_factory=list)
    stored: int = 0
    #: 一覧の応答から取れた検索識別子。送信payloadの ``searchUuid`` に載る。
    search_uuid: str | None = None
    #: レジュメを取りに行った件数と、取れた件数。
    resumes_requested: int = 0
    resumes_read: int = 0
    #: 上限で打ち切ったか。**黙って切らない。**
    capped_by: str = ""
    #: 未確定のままだったレジュメの軸。空でないなら、その項目は永久に「非公開」。
    unresolved_fields: tuple[str, ...] = ()
    #: 一覧の行の、材料になる欄がどんな形だったか。**キー名と件数だけ** (13.2)。
    #:
    #: 実測40回目、レジュメが読めずモデルへ渡った人物の事実は2つだけだったが、
    #: 一覧の行には年齢も資格も載っていた -- 読んでいなかっただけである。読む
    #: ようにしたが、**値の形は観測していない**。外したら黙って「非公開」に
    #: なるので、形を報告して次の実行で分かるようにする (原則2/原則3)。
    row_shapes: tuple[str, ...] = ()
    #: レジュメが読めなかった理由と件数。**理由だけで、本文は持たない** (13.2)。
    #:
    #: 実測38回目、報告は「レジュメ: 0 / 1 件 読めました」としか言えなかった。
    #: 0件であることは分かるが **なぜか** が分からない -- 一覧路で直したのと
    #: 同じ病気が、レジュメ路に残っていた (原則2)。
    resume_failures: dict[str, int] = field(default_factory=dict)

    def rows_seen(self) -> int:
        return sum(page.rows for page in self.pages)

    def reached(self) -> IngestStage:
        """The single stage the run actually reached.

        単調性が破れる状態は嘘なので、報告せず例外にする
        (:meth:`recon.observe_api.ApiObservation.reached` と同じ規律)。
        """
        chain: tuple[tuple[IngestStage, bool], ...] = (
            (IngestStage.LIST_NOT_CALLED, bool(self.pages)),
            (IngestStage.LIST_FAILED, any(page.succeeded for page in self.pages)),
            (IngestStage.NO_ROWS, self.rows_seen() > 0),
        )
        stopped: IngestStage | None = None
        for stage, passed in chain:
            if not passed and stopped is None:
                stopped = stage
            elif passed and stopped is not None:
                raise ValueError(
                    f"IngestReport の状態が時系列と矛盾しています: {stopped.value} で"
                    f"止まったのに {stage.value} を通過した証拠がある。"
                    " (報告を嘘にしないため停止)"
                )
        return stopped or IngestStage.STORED

    def render(self) -> str:
        lines = ["候補者の取り込み (**送信はしていません**)", ""]
        stage = self.reached()

        if stage is IngestStage.LIST_NOT_CALLED:
            lines.append("  **一覧APIを1回も呼んでいません。** 上限の設定を確認してください。")
            return "\n".join(lines)
        lines.extend(self._page_lines())

        if stage is IngestStage.LIST_FAILED:
            lines.append("")
            lines.append("  **一覧APIが1ページも成功しませんでした。**")
            lines.append("  0件ではありません -- **取りに行けていません**。")
            lines.append("  上のステータスを見てください。401/403 ならセッション切れです。")
            return "\n".join(lines)
        if stage is IngestStage.NO_ROWS:
            lines.append("")
            lines.append("  **応答は成功しましたが、候補者が0件でした。**")
            lines.append("  取りに行けてはいます -- 検索条件に合う候補者が居ないか、")
            lines.append("  条件のIDが違います (config.yaml の ingest.search_condition_id)。")
            return "\n".join(lines)

        lines.append("")
        lines.append(f"  見えた候補者: {self.rows_seen()} 件 / 保存した: {self.stored} 件")
        if self.row_shapes:
            lines.append("  一覧の行から読めた材料 (1件目の形):")
            lines.extend(f"    {note}" for note in self.row_shapes)
        lines.append(
            f"  検索識別子: {'取れました' if self.search_uuid else '**取れませんでした**'}"
            f" (送信payloadの searchUuid に載る)"
        )
        if self.resumes_requested:
            lines.append(
                f"  レジュメ: {self.resumes_read} / {self.resumes_requested} 件 読めました"
            )
            # **読めなかった理由を必ず言う。** 「0件」だけでは次の手が決まらない。
            for reason, count in sorted(self.resume_failures.items()):
                lines.append(f"    **読めなかった**: {reason} ({count} 件)")
        else:
            lines.append("  レジュメ: 取りに行っていません (ingest.fetch_resumes)")
        if self.capped_by:
            # **黙って切らない。** 切った理由を述べないと「それで全部」に見える。
            lines.append(f"  **上限で打ち切りました**: {self.capped_by}")
        if self.unresolved_fields:
            lines.append(f"  **未確定のままのレジュメ項目**: {', '.join(self.unresolved_fields)}")
            lines.append("  これらは永久に「非公開」としてモデルへ渡ります。")
        return "\n".join(lines)

    def _page_lines(self) -> list[str]:
        out = [f"  一覧APIの呼び出し: {len(self.pages)} 回"]
        for page in self.pages:
            mark = "成功" if page.succeeded else "**失敗**"
            out.append(f"    {page.page}ページ目: {mark} (HTTP {page.status}) 行 {page.rows}")
        return out


def ingest(
    client: JobMedleyApiClient,
    endpoints: Mapping[str, Endpoint],
    coordinates: SiteCoordinates,
    config: IngestConfig,
    safety: SafetyConfig,
    connection: sqlite3.Connection,
    clock: Clock,
) -> IngestReport:
    """Fetch candidates page by page, enrich with resumes, and store them."""
    report, collected = collect_candidates(client, endpoints, coordinates, config, safety)
    for candidate in collected:
        candidate_repo.upsert_candidate(connection, candidate, source=SOURCE, clock=clock)
    report.stored = len(collected)
    return report


def collect_candidates(
    client: JobMedleyApiClient,
    endpoints: Mapping[str, Endpoint],
    coordinates: SiteCoordinates,
    config: IngestConfig,
    safety: SafetyConfig,
    *,
    cap: int | None = None,
) -> tuple[IngestReport, list[Candidate]]:
    """Fetch and enrich, **without storing**. Returns the report and the rows.

    :func:`ingest` は取り込んで保存する。文面の下見 (``scout preview``) は
    保存せずに候補者そのものを要るので、取ってくる部分だけを切り出してある。

    **保存しないことが下見の要点である。** 下見で保存すると、送っていないのに
    「取り込み済み」になり、後から見たときに送信対象だったのかどうかが
    分からなくなる。

    ``cap`` は ``safety.ingest_cap`` より **小さい側だけ** を採る。下見で1件だけ
    欲しいときに使う -- 大きい側を採れる形にすると、上限の意味が消える (9.7)。
    """
    report = IngestReport(unresolved_fields=unresolved_resume_fields(coordinates))
    list_endpoint = endpoints[CANDIDATE_LIST]
    list_url = _url_of(list_endpoint, used_by="runtime.commands.ingest")
    template = parse_payload_template(
        coordinates.json_path("api.candidate_list.payload_template"),
        used_by="runtime.commands.ingest",
    )

    collected: list[Candidate] = []
    for page in range(1, config.max_pages + 1):
        body = _fill(template, page=page, config=config)
        outcome = client.call(list_endpoint, url=list_url, json_body=body)
        payload = outcome.json_body() or {}
        rows = rows_in(payload) if outcome.succeeded else ()
        report.pages.append(
            PageResult(
                page=page, status=outcome.status, succeeded=outcome.succeeded, rows=len(rows)
            )
        )
        if not outcome.succeeded:
            # **失敗したページで止める。** 続けると「後半だけ取れた」結果になり、
            # 何件取れるはずだったのかが分からなくなる。
            break
        if report.search_uuid is None:
            report.search_uuid = search_uuid_in(payload)
        for row in rows:
            if not report.row_shapes:
                # **1件目だけ形を出す。** 全件出すと報告が読めなくなる。
                report.row_shapes = describe_row_shapes(row)
            # **小さい側だけを採る。** 大きい側を採れる形にすると上限の意味が消える。
            limit = safety.ingest_cap if cap is None else min(cap, safety.ingest_cap)
            if len(collected) >= limit:
                report.capped_by = (
                    f"safety.ingest_cap ({safety.ingest_cap} 件)"
                    if limit == safety.ingest_cap
                    else f"下見の上限 ({limit} 件)"
                )
                break
            if (candidate := candidate_from_row(row)) is not None:
                collected.append(candidate)
        if report.capped_by or not rows:
            break
    else:
        if report.rows_seen():
            report.capped_by = report.capped_by or f"ingest.max_pages ({config.max_pages} ページ)"

    if config.fetch_resumes and collected:
        collected = _with_resumes(client, endpoints, coordinates, collected, report)

    return report, collected


def _with_resumes(
    client: JobMedleyApiClient,
    endpoints: Mapping[str, Endpoint],
    coordinates: SiteCoordinates,
    candidates: Sequence[Candidate],
    report: IngestReport,
) -> list[Candidate]:
    """Fetch one resume per candidate. **読めなかった人も落とさない。**

    レジュメが読めないことは、その候補者が居ないことではない。落とすと件数が
    黙って減る (原則2)。読めた人だけ facts が付き、読めなかった人は空のまま
    進む -- 空なら「非公開」として渡るので、モデルは創作できない。
    """
    endpoint = endpoints[RESUME]
    url = _url_of(endpoint, used_by="runtime.commands.ingest._with_resumes")
    template = parse_payload_template(
        coordinates.json_path("api.resume.payload_template"),
        used_by="runtime.commands.ingest._with_resumes",
    )
    keypaths = resume_keypaths(coordinates)

    enriched: list[Candidate] = []
    for candidate in candidates:
        report.resumes_requested += 1
        body = _substitute(template, {MEMBER_ID_SLOT: candidate.raw_id_observed})
        outcome = client.call(endpoint, url=url, json_body=body)
        payload = _resume_payload(outcome)
        if payload is None:
            reason = _resume_failure_reason(endpoint, outcome)
            report.resume_failures[reason] = report.resume_failures.get(reason, 0) + 1
            enriched.append(candidate)
            continue
        report.resumes_read += 1
        enriched.append(
            candidate.model_copy(
                update={"resume": resume_from_response(payload, keypaths=keypaths)}
            )
        )
    return enriched


def _url_of(endpoint: Endpoint, *, used_by: str) -> str:
    """The endpoint's URL. **``null`` は「その経路が無い」であって空文字ではない。**

    取り込みに要る2つはどちらも必須座標なので、``null`` で来たら設定が壊れて
    いる。黙って空文字で呼びに行くと、届かなかったことが「0件」として現れる
    (原則2)。
    """
    url = require(endpoint.url_pattern, used_by=used_by)
    if url is None:
        raise ConfigError(
            f"{used_by}: エンドポイント {endpoint.id} のURLが null です。"
            f"取り込みにはこの経路が要ります。"
        )
    return url


def _resume_failure_reason(endpoint: Endpoint, outcome: ApiOutcome) -> str:
    """Why one resume could not be read. **値は1つも含めない** (13.2)。

    ステータス・GraphQL の errors コード・エラー欄のキーパスだけを出す。媒体の
    文言は出さない -- 候補者名が混ざりうる。理由の作り方は送信路と同じ関数に
    寄せてある (:func:`api.success.describe_failure`) ので、3本立ての判定
    (ステータス / errors / errorMessage) がそのまま効く。
    """
    body = outcome.json_body()
    mapping = body if isinstance(body, Mapping) else None
    if (described := describe_failure(endpoint, outcome.status, mapping)) is not None:
        return described
    # 成功しているのに読めなかった = 本文がオブジェクトでなかった。
    # **そこで止めない。** 何であるかまで言わないと次の手が決まらない (実測39回目)。
    shape = describe_body_shape(outcome.response.body_text, outcome.response.headers)
    return f"HTTP {outcome.status} / 応答本文がオブジェクトではありません ({shape})"


def _resume_payload(outcome: ApiOutcome) -> Mapping[str, object] | None:
    if not outcome.succeeded:
        return None
    body = outcome.json_body()
    return body if isinstance(body, Mapping) else None


def _fill(template: Mapping[str, Any], *, page: int, config: IngestConfig) -> dict[str, Any]:
    """Fill the list template's three slots. **数値は数値として入れる。**

    文字列で入れると媒体が「1」と 1 を違うものとして扱いうる。座標の雛形は
    値を伏せてあるので、型はこちらで決める必要がある。

    差し込んだあと、**残った ``<...>`` で止める** (実測35回目)。あのときの雛形は
    40キーのうち37キーが偵察の印 (``"<bool>"`` / ``"<number>"`` -- どちらも
    **文字列**) のままで、媒体は HTTP 500 を返した。報告は正しく「取りに行けて
    いません」と言ったが、**なぜ** かは言えなかった。門はここに要る。

    送信路には最初からこの門があった (:func:`api.payloads.assert_fully_filled`)。
    読み取り路に無かったのは非対称で、その非対称に理由は無い。
    """
    filled = _substitute_slots(
        template,
        {
            SEARCH_CONDITION_SLOT: config.search_condition_id,
            PAGE_SLOT: page,
            PAGE_SIZE_SLOT: config.page_size,
        },
    )
    if not isinstance(filled, dict):  # pragma: no cover - 雛形は必ずオブジェクト
        raise TypeError("一覧の要求本文がオブジェクトになりませんでした")
    assert_fully_filled(
        filled,
        used_by="runtime.commands.ingest",
        coordinate="api.candidate_list.payload_template",
        consequence=(
            "**このまま呼ぶと媒体はエラーを返し、それが「0件」として現れます。**"
            " 実際の値は `scout recon observe-search` で観測できます。"
        ),
    )
    return filled


def _substitute_slots(node: Any, values: Mapping[str, object]) -> Any:
    """Replace ``{{NAME:kind}}`` markers, **coercing to the observed kind**.

    型は観測が決める。ここは運ぶだけである (13.4)。記法に型が付いていなければ
    置き換えない -- 素通しさせて :func:`api.payloads.assert_fully_filled` に
    止めさせる。**「たぶん数値だろう」で入れないため。**
    """
    if isinstance(node, str):
        match = LIST_SLOT_PATTERN.match(node)
        if match is None:
            return node
        name, kind = match.group(1), match.group(2)
        if name not in values:
            return node
        return _as_kind(values[name], kind, name=name)
    if isinstance(node, Mapping):
        return {key: _substitute_slots(item, values) for key, item in node.items()}
    if isinstance(node, list):
        return [_substitute_slots(item, values) for item in node]
    return node


def _as_kind(value: object, kind: str, *, name: str) -> object:
    """Coerce a config value to the kind the platform was observed to use."""
    if kind == "string":
        return str(value)
    try:
        return int(str(value))
    except ValueError as exc:
        raise ConfigError(
            f"runtime.commands.ingest: 差し込み欄 {name} は媒体が数値で送る欄ですが、"
            f"設定の値を数値にできません: {value!r}。"
            f"config.yaml の ingest を確かめてください。"
        ) from exc


def _substitute(node: Any, values: Mapping[str, object]) -> Any:
    """Replace whole-string markers, keeping everything else as observed."""
    if isinstance(node, str):
        return values.get(node, node)
    if isinstance(node, Mapping):
        return {key: _substitute(item, values) for key, item in node.items()}
    if isinstance(node, list):
        return [_substitute(item, values) for item in node]
    return node


__all__ = [
    "LIST_SLOT_PATTERN",
    "MEMBER_ID_SLOT",
    "PAGE_SIZE_SLOT",
    "PAGE_SLOT",
    "SEARCH_CONDITION_SLOT",
    "SOURCE",
    "IngestReport",
    "IngestStage",
    "PageResult",
    "ingest",
]
