"""Command-line entry point.

例外から終了コードへの写像は :mod:`runtime.exit_codes` の1箇所だけで行う。
恒久エラー (認証切れ・座標未確定・設定不正・状態破損・全滅) は必ず非0で終わる
(12.8)。キルスイッチは **異常ではなく意図的な停止** なので、それとは別の低い
コードを返す。

コマンドの設計方針: 座標が足りないコマンドは **黙って0件で成功せず、明示的に
停止する** (原則2)。何が足りないか・どう取得するかを ``assert_ready_for`` の
例外メッセージが述べる。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from jobmedley_scout.config.audit import assert_ready_for, render_audit
from jobmedley_scout.config.loader import load_all
from jobmedley_scout.config.secrets import load_secrets
from jobmedley_scout.errors import ConfigError, KillSwitchEngaged, ScoutError
from jobmedley_scout.runtime.exit_codes import ExitCode, exit_code_for

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jobmedley_scout.api.client import JobMedleyApiClient
    from jobmedley_scout.config.schema import Config
    from jobmedley_scout.config.site_coordinates import SiteCoordinates

DEFAULT_CONFIG = Path("config/config.yaml")
DEFAULT_COORDINATES = Path("config/site_coordinates.yaml")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scout",
        description="ジョブメドレー求人スカウト自動化システム",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--coordinates", type=Path, default=DEFAULT_COORDINATES)

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("coordinates", help="未確定の媒体座標を段階別に一覧する")
    sub.add_parser("preflight", help="環境を点検し、安全弁の実効値を印字する")
    sub.add_parser("should-run", help="営業日判定 (文字列プロトコルで結果を返す)")

    sub.add_parser("ingest", help="候補者を取り込む")
    sub.add_parser("generate", help="文面を生成する")
    sub.add_parser("send", help="送信する")
    first = sub.add_parser(
        "send-first",
        help="**1通だけ**送る (段階4-3)。dry_run 解除と取り消し不可の承知が要る",
    )
    # 13.6: **二重の明示操作。** dry_run の解除だけでは送らない。
    first.add_argument(
        "--i-understand-sends-are-irreversible",
        dest="acknowledged",
        action="store_true",
        help="送信は取り消せず、相手の受信箱に残り、月次の枠を1通消費することを承知する",
    )
    sub.add_parser("followup", help="追客を実行する")
    sub.add_parser("sync-replies", help="返信を検知する")
    sub.add_parser("analytics", help="週次・月次の分析を出力する")

    sub.add_parser("preview", help="1通目の文面を下見する (送信なし・本文はログに出さない)")

    dryrun = sub.add_parser("dryrun", help="送信直前まで通す (段階5)")
    dryrun.add_argument("--limit", type=int, default=5)

    recon = sub.add_parser("recon", help="偵察コマンド (常設)")
    recon_sub = recon.add_subparsers(dest="recon_command", required=True)
    # ``login`` に --headful は無い。**常にヘッドフルで開く。** 人間が操作する
    # コマンドなので選択肢にする意味がなく、選べる形にすると headless=true の
    # 既定のまま実行して「何も起きない」事故が起きる。
    recon_sub.add_parser("login", help="手動ログインしセッションを保存 (常にヘッドフル)")
    recon_sub.add_parser("observe-login", help="段階1の座標を観測して記入用の値を印字")
    recon_sub.add_parser("observe-list", help="段階2の残り座標を観測して記入用の値を印字")
    replay = recon_sub.add_parser(
        "replay-list", help="保存された構造スナップショットに対して同じ解析を再実行 (接続なし)"
    )
    replay.add_argument("snapshot", type=Path, help="observe-list が保存したJSON (またはそのログ)")
    verify = recon_sub.add_parser("verify-session", help="保存セッションで入り直して確認")
    # こちらは機械判定なので既定はヘッドレスでよい。目で見たいときだけ開く。
    verify.add_argument("--headful", action="store_true", help="画面を開いて目視でも確認する")
    recon_sub.add_parser("capture-send", help="送信を中断しつつ内部APIを特定")
    recon_sub.add_parser(
        "capture-open", help="送信遮断を武装した状態でカードのボタンを押し、ドロワーと送信路を観測"
    )
    recon_sub.add_parser(
        "follow-send", help="教わった導線 (求人選択→本文→確認→送信) をそのまま辿って送信路を観測"
    )
    recon_sub.add_parser(
        "observe-api", help="一覧を開いて読み取りAPIの応答の形を観測 (押下なし・値は出さない)"
    )
    recon_sub.add_parser(
        "introspect",
        help="送信の入力型をスキーマに尋ねる (query 2本のみ・押下なし・値は出さない)",
    )
    recon_sub.add_parser(
        "observe-resume",
        help="カードを押してレジュメAPIの応答の形を観測 (送信は遮断・値は出さない)",
    )
    recon_sub.add_parser(
        "read-bundle", help="配信JSから送信APIの操作名と変数の形を読む (GETのみ・押下なし)"
    )
    recon_sub.add_parser(
        "observe-job-offers",
        help="送信に要る求人ID (jobOfferId/jobOfferSalaryId) を観測 (押下なし・送信なし)",
    )
    recon_sub.add_parser(
        "observe-search",
        help="一覧の要求本文 (api.candidate_list.payload_template) を観測 (押下なし・送信なし)",
    )
    recon_sub.add_parser(
        "observe-headers",
        help="ブラウザが付ける要求ヘッダの出所を名前だけで探す (押下なし・値は出さない)",
    )
    recon_sub.add_parser("resume-keys", help="レジュメのキーパスを出力 (値は出さない)")
    recon_sub.add_parser("inbox", help="受信箱の構造を観測")

    session = sub.add_parser("session", help="セッション管理")
    session_sub = session.add_subparsers(dest="session_command", required=True)
    session_sub.add_parser("export", help="保存セッションをbase64で出力 (CIシークレット用)")
    session_sub.add_parser(
        "import", help="ブラウザの Copy as cURL を読み取ってセッションを作る (標準入力)"
    )
    session_sub.add_parser("check", help="シークレットからセッションを復元できるか確かめる")

    optout = sub.add_parser("optout", help="送信停止要求の管理")
    optout_sub = optout.add_subparsers(dest="optout_command", required=True)
    add_optout = optout_sub.add_parser("add", help="候補者を恒久的に除外する")
    add_optout.add_argument("candidate_id")

    purge = sub.add_parser("purge", help="個人データの削除 (13.2)")
    purge.add_argument("--candidate-id", help="この候補者の全データを削除する")
    purge.add_argument("--expired-dumps", action="store_true", help="期限切れの偵察ダンプを削除")

    report = sub.add_parser("report", help="実行サマリを出力する")
    report.add_argument("--last-run", action="store_true")

    notify = sub.add_parser("notify", help="異常を能動通知する")
    notify.add_argument("--reason", required=True)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        return _dispatch(args)
    except KillSwitchEngaged as exc:
        # 意図的な停止。異常終了と混同しない。
        print(f"停止: {exc}", file=sys.stderr)
        return int(ExitCode.KILL_SWITCH)
    except ScoutError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return exit_code_for(exc)
    except KeyboardInterrupt:  # pragma: no cover
        print("中断されました", file=sys.stderr)
        return int(ExitCode.INTERRUPTED)


def _dispatch(args: argparse.Namespace) -> int:
    # 座標を要求しないコマンドは、設定だけ読めば動く。
    config, coordinates = load_all(args.config, args.coordinates)

    if args.command == "coordinates":
        print(render_audit(coordinates))
        return int(ExitCode.OK)

    if args.command == "preflight":
        from jobmedley_scout.runtime.preflight import run_preflight

        report = run_preflight(config, coordinates, load_secrets())
        print(report.render())
        # 12.6: 失敗が1件でもあれば非0。CI側は continue-on-error を付けない。
        return int(ExitCode.OK if report.passed else ExitCode.PREFLIGHT_FAILED)

    if args.command == "should-run":
        from jobmedley_scout.clock import SystemClock
        from jobmedley_scout.runtime.calendar import render_protocol, should_run

        decision = should_run(SystemClock().now(), config.calendar)
        print(render_protocol(decision))
        return int(ExitCode.OK)

    if args.command == "session":
        return _dispatch_session(args, config)

    # ここから先は媒体座標を要求する。足りなければ **明示的に停止する** --
    # 黙って0件で成功しないことが要件 (原則2)。
    command_key = {
        "ingest": "ingest",
        "generate": "generate",
        "send": "send",
        "followup": "followup",
        "sync-replies": "sync-replies",
        "analytics": "analytics",
        # **``send`` に写像しない。** 一通も送らないコマンドが、送信の応答を
        # 解釈するための座標を要求すると、梯子が閉じる (coordinates.py の注記)。
        "dryrun": "dryrun",
    }.get(args.command)

    if args.command == "ingest":
        assert_ready_for(coordinates, "ingest")
        return _dispatch_ingest(config, coordinates)

    if args.command == "send-first":
        assert_ready_for(coordinates, "send-first")
        return _dispatch_send_first(args, config, coordinates)

    if args.command == "preview":
        # **送信の座標は要求しない。** 一通も送らないコマンドが送信の応答を
        # 解釈するための座標を要求すると、梯子が閉じる (coordinates.py の注記)。
        assert_ready_for(coordinates, "ingest")
        return _dispatch_preview(config, coordinates)

    if command_key is not None:
        assert_ready_for(coordinates, command_key)
        raise NotImplementedError(
            f"コマンド '{args.command}' の本体は、媒体座標が確定してから配線されます。"
            f"docs/ladder.md の手順に従ってください。"
        )

    if args.command == "recon":
        return _dispatch_recon(args, config, coordinates)

    parser_error = f"未実装のコマンド: {args.command}"
    print(parser_error, file=sys.stderr)
    return int(ExitCode.USAGE)


def _dispatch_preview(config: Config, coordinates: SiteCoordinates) -> int:
    """1通目の文面を下見する。**送信もDBへの保存も行わない。**

    本文はログに出さず、成果物へ書く。ログに出るのは数と種別だけである --
    本文には会員番号での宛名と居住地の市区町村が最初から入っており、それは
    13.2 が Actions のログに残すことを禁じているものそのものだからである。
    """
    from anthropic import Anthropic

    from jobmedley_scout.api.endpoints import build_endpoints
    from jobmedley_scout.browser import session_store
    from jobmedley_scout.browser.context import browser_context
    from jobmedley_scout.browser.navigation import goto
    from jobmedley_scout.config.placeholders import require
    from jobmedley_scout.config.secrets import load_secrets
    from jobmedley_scout.generation.clinic import load_clinic_facts
    from jobmedley_scout.runtime.commands.preview import preview

    secrets = load_secrets()
    api_key = secrets.require_anthropic_key()

    _restore_session_from_secrets(config)
    session = session_store.session_path(config.paths.credentials_dir)
    if not session.exists():
        print("保存セッションがありません。段階1からやり直してください。", file=sys.stderr)
        return int(ExitCode.AUTH_EXPIRED)

    clinic = load_clinic_facts(Path("config/clinic.yaml"))
    prompt_template = Path("config/prompts/scout_dental_hygienist.md").read_text(encoding="utf-8")
    destination = config.paths.recon_dump_dir / "preview" / "scout-preview.md"

    with browser_context(config.browser, storage_state=session) as (context, page):
        # **先に遷移する。** meta タグはページの一部なので、開く前は読めない。
        goto(
            page,
            require(coordinates.url("nav.candidate_list_url"), used_by="cli.preview"),
            config.browser,
        )
        client, csrf_note = _api_client_with_csrf(context, page, coordinates, used_by="cli.preview")
        print(f"CSRFトークン: {csrf_note}")
        report = preview(
            client,
            build_endpoints(coordinates),
            coordinates,
            config.ingest,
            config.safety,
            llm=Anthropic(api_key=api_key),
            llm_config=config.llm,
            prompt_template=prompt_template,
            clinic=clinic,
            clinic_address=clinic["CLINIC_ADDRESS"],
            max_requests=config.safety.max_llm_requests_per_message,
            destination=destination,
        )
    print(report.render())
    return int(ExitCode.OK)


def _dispatch_send_first(
    args: argparse.Namespace, config: Config, coordinates: SiteCoordinates
) -> int:
    """**1通だけ送る。** ここが取り返しのつかない唯一の場所である (13.6)。

    門は3つ。``safety.dry_run`` が明示的に false であること、取り消せないことの
    承知が渡されていること、そして上限が定数の1件であること。門は
    :func:`runtime.commands.send_first.send_first` の側にもあり、**どちらか
    片方だけでは送れない**。
    """
    from anthropic import Anthropic

    from jobmedley_scout.api.endpoints import build_endpoints
    from jobmedley_scout.browser import session_store
    from jobmedley_scout.browser.context import browser_context
    from jobmedley_scout.browser.navigation import goto
    from jobmedley_scout.clock import SystemClock
    from jobmedley_scout.config.placeholders import require
    from jobmedley_scout.config.secrets import load_secrets
    from jobmedley_scout.generation.clinic import load_clinic_facts
    from jobmedley_scout.runtime.commands.send_first import FirstSendStage, send_first
    from jobmedley_scout.state.db import open_state_db

    secrets = load_secrets()
    api_key = secrets.require_anthropic_key()

    _restore_session_from_secrets(config)
    session = session_store.session_path(config.paths.credentials_dir)
    if not session.exists():
        print("保存セッションがありません。段階1からやり直してください。", file=sys.stderr)
        return int(ExitCode.AUTH_EXPIRED)

    clinic = load_clinic_facts(Path("config/clinic.yaml"))
    prompt_template = Path("config/prompts/scout_dental_hygienist.md").read_text(encoding="utf-8")
    clock = SystemClock()
    connection = open_state_db(config.paths.state_dir / "state.sqlite3", clock)
    destination = config.paths.recon_dump_dir / "send-first" / "sent-message.md"

    with browser_context(config.browser, storage_state=session) as (context, page):
        list_url = require(coordinates.url("nav.candidate_list_url"), used_by="cli.send_first")
        goto(page, list_url, config.browser)
        client, csrf_note = _api_client_with_csrf(
            context, page, coordinates, used_by="cli.send_first"
        )
        print(f"CSRFトークン: {csrf_note}")
        report = send_first(
            client,
            build_endpoints(coordinates),
            coordinates,
            config.ingest,
            config.safety,
            connection,
            clock,
            llm=Anthropic(api_key=api_key),
            llm_config=config.llm,
            prompt_template=prompt_template,
            clinic=clinic,
            clinic_address=clinic["CLINIC_ADDRESS"],
            max_requests=config.safety.max_llm_requests_per_message,
            acknowledged=bool(getattr(args, "acknowledged", False)),
            run_id=f"send-first-{clock.now().isoformat()}",
            destination=destination,
        )
    print(report.render())
    # **送れなかったことを成功で終えない** (原則2)。
    return int(ExitCode.OK if report.reached() is FirstSendStage.SENT else ExitCode.UNKNOWN)


def _dispatch_ingest(config: Config, coordinates: SiteCoordinates) -> int:
    """候補者を取り込む。**認証済みブラウザの通信路をそのまま使う** (原則1)。

    ブラウザを開くのは Cookie を載せるためだけで、DOM は1つも触らない。
    ボタンも押さないので、送信は起こす操作そのものが存在しない。
    """
    from jobmedley_scout.api.endpoints import build_endpoints
    from jobmedley_scout.browser import session_store
    from jobmedley_scout.browser.context import browser_context
    from jobmedley_scout.browser.navigation import goto
    from jobmedley_scout.clock import SystemClock
    from jobmedley_scout.config.placeholders import require
    from jobmedley_scout.runtime.commands.ingest import ingest
    from jobmedley_scout.state.db import open_state_db

    # 12.7: **毎回シークレットから復元する。** 実行環境は使い捨てなので、
    # 前回の実行が残したファイルを当てにできない。
    _restore_session_from_secrets(config)
    session = session_store.session_path(config.paths.credentials_dir)
    if not session.exists():
        print(
            "保存セッションがありません。段階1からやり直してください。",
            file=sys.stderr,
        )
        return int(ExitCode.AUTH_EXPIRED)

    clock = SystemClock()
    connection = open_state_db(config.paths.state_dir / "state.sqlite3", clock)
    with browser_context(config.browser, storage_state=session) as (context, page):
        # **先に遷移する。** meta タグはページの一部なので、開く前は読めない。
        goto(
            page,
            require(coordinates.url("nav.candidate_list_url"), used_by="cli.ingest"),
            config.browser,
        )
        client, csrf_note = _api_client_with_csrf(context, page, coordinates, used_by="cli.ingest")
        print(f"CSRFトークン: {csrf_note}")
        report = ingest(
            client,
            build_endpoints(coordinates),
            coordinates,
            config.ingest,
            config.safety,
            connection,
            clock,
        )
    print(report.render())
    # **取れなかったことを成功で終えない** (原則2)。0件と失敗を終了コードで分ける。
    from jobmedley_scout.runtime.commands.ingest import IngestStage

    stage = report.reached()
    if stage is IngestStage.STORED:
        return int(ExitCode.OK)
    if stage is IngestStage.NO_ROWS:
        # 届いてはいる。条件に合う候補者が居ないだけなので、異常ではない。
        return int(ExitCode.OK)
    return int(ExitCode.UNKNOWN)


#: 偵察サブコマンドから必須座標集合への写像。``login`` と ``verify-session`` が
#: ``recon-login`` (空集合) を指すのが要点である。**段階1は発見の工程なので、
#: 段階1の座標を要求しない。** 要求すると1歩目が自分の成果物待ちで始められない。
_RECON_COORDINATE_KEYS: dict[str, str] = {
    "login": "recon-login",
    "observe-login": "recon-login",
    "verify-session": "recon-login",
    "observe-list": "recon-observe-list",
    "replay-list": "recon-replay",
    "capture-send": "recon-capture-send",
    "capture-open": "recon-capture-send",
    # 導線を辿るだけなので、必要な座標は capture-open と同じ (一覧URLと行の目印)。
    "follow-send": "recon-capture-send",
    # 一覧を開くだけ。必要な座標は capture-open と同じ (一覧URLと行の目印)。
    "observe-api": "recon-capture-send",
    # 一覧URLと行のセレクタさえ在れば押せる。送信は遮断で止めてある。
    "observe-resume": "recon-capture-send",
    # 尋ねるだけ。要るのは API のオリジンだけである。
    "introspect": "recon-capture-send",
    # 一覧URLさえ在れば読める (押下も送信も無い)。capture-open と同じ座標で足りる。
    "read-bundle": "recon-capture-send",
    # 一覧を開くだけ。押下も送信も無い。
    "observe-job-offers": "recon-capture-send",
    # 一覧を開いて要求本文を拾うだけ。聴く経路を座標から取るので専用の集合を使う。
    "observe-search": "recon-observe-search",
    # ページを開いて名前だけを読む。押下も送信も無い。
    "observe-headers": "recon-observe-headers",
    "resume-keys": "recon-resume-keys",
    "inbox": "recon-capture-send",
}


def _api_client_with_csrf(
    context: object, page: object, coordinates: SiteCoordinates, *, used_by: str
) -> tuple[JobMedleyApiClient, str]:
    """The API client, with the CSRF header the browser sends. **値は返さない。**

    実測41〜42回目。ブラウザは ``x-csrf-token`` を付けており、こちらは
    ``Content-Type`` しか付けていなかった。トークンの無い POST は弾かれ、
    ログイン画面へ転送される -- レジュメAPIが返していた5万字のHTMLの正体である。

    **トークンを読むにはページが開いていなければならない。** meta タグは
    ページの一部なので、遷移する前に読むと空になる。呼び出し側が先に遷移する。

    返す2つ目は **有無だけの報告文** で、トークンそのものは入らない (12.7)。
    """
    from jobmedley_scout.api.client import JobMedleyApiClient
    from jobmedley_scout.browser.csrf import csrf_headers
    from jobmedley_scout.browser.transport import PlaywrightTransport

    headers, note = csrf_headers(
        page,
        coordinates.optional_string("api.csrf_header_name"),
        coordinates.optional_string("api.csrf_meta_name"),
        used_by=used_by,
    )
    client = JobMedleyApiClient(
        PlaywrightTransport(context),
        auth_failure_codes=coordinates.string_list("api.auth_failure_codes"),
        idempotency_header=coordinates.optional_string("api.idempotency_header"),
        extra_headers=headers,
    )
    return client, note


def _dispatch_recon(
    args: argparse.Namespace,
    config: Config,
    coordinates: SiteCoordinates,
) -> int:
    assert_ready_for(coordinates, _RECON_COORDINATE_KEYS[args.recon_command])

    if args.recon_command == "login":
        from jobmedley_scout.recon.manual_login import run_manual_login

        observation = run_manual_login(config.browser, config.paths.credentials_dir)
        print(observation.render())
        return int(ExitCode.OK)

    if args.recon_command == "observe-login":
        from jobmedley_scout.recon.observe_login import observe_login

        # 認証済みの観測にはセッションが要る。12.7 のとおり毎回シークレットから復元する。
        _restore_session_from_secrets(config)
        print(observe_login(config.browser, config.paths.credentials_dir).render())
        return int(ExitCode.OK)

    if args.recon_command == "observe-list":
        from jobmedley_scout.recon.observe_list import observe_list
        from jobmedley_scout.recon.snapshot import render_snapshot_footer, save_capture

        # 認証済みの観測にはセッションが要る。12.7 のとおり毎回シークレットから復元する。
        _restore_session_from_secrets(config)
        observed, capture = observe_list(
            config.browser,
            config.paths.credentials_dir,
            coordinates.url("nav.candidate_list_url"),
        )
        print(observed.render())
        if capture is not None:
            # **観測の丸ごとを持ち帰る。** 値が出なかった実行も、この保存があれば
            # 手元で解析を直す材料になる (scout recon replay-list)。往復1回の価値を
            # 「値が出たか」から「構造を持ち帰れたか」に引き上げるのが狙い。
            path = save_capture(capture, config.paths.recon_dump_dir)
            print()
            print(render_snapshot_footer(path, capture))
        return int(ExitCode.OK)

    if args.recon_command == "replay-list":
        from jobmedley_scout.recon.observe_list import analyze_candidate_list
        from jobmedley_scout.recon.snapshot import load_capture

        # オフライン再生。媒体へは一切接続しない。実行時と **同一の解析関数** を
        # 保存された構造に対して走らせる -- 再生で直れば実行でも直る。
        text = Path(args.snapshot).read_text(encoding="utf-8")
        capture = load_capture(text)
        if capture is None:
            raise ConfigError(
                f"スナップショットを読めませんでした: {args.snapshot}\n"
                f"  observe-list が保存した JSON、またはそのログ出力"
                f" (BEGIN/END ブロックを含むテキスト) を指定してください。"
            )
        observed = analyze_candidate_list(
            capture,
            drawer_skip_reason=(
                "再生 (replay) ではクリックを行わないため、ドロワーは観測できません。"
                "この座標は実行時のみ観測されます。"
            ),
        )
        print("**再生 (replay)**: 保存された構造から再計算しました。媒体へは接続していません。")
        print()
        print(observed.render())
        return int(ExitCode.OK)

    if args.recon_command == "read-bundle":
        from jobmedley_scout.recon.read_bundle import read_bundle

        # 認証済みのHTMLが要る (ログイン後の画面が読み込む配信ファイルを知るため)。
        _restore_session_from_secrets(config)
        from_bundle = read_bundle(
            config.browser,
            config.paths.credentials_dir,
            coordinates.url("nav.candidate_list_url"),
        )
        print(from_bundle.render())
        return int(ExitCode.OK)

    if args.recon_command == "capture-open":
        from jobmedley_scout.recon.capture_open import capture_open
        from jobmedley_scout.recon.snapshot import encode_block

        # 認証済みの観測にはセッションが要る。12.7 のとおり毎回シークレットから復元する。
        _restore_session_from_secrets(config)
        opened, tree = capture_open(
            config.browser,
            config.paths.credentials_dir,
            coordinates.url("nav.candidate_list_url"),
            coordinates.selector("nav.list_ready_selector"),
        )
        print(opened.render())
        if tree is not None:
            from jobmedley_scout.recon.snapshot import ListCapture, save_capture

            capture = ListCapture(
                requested_url=opened.requested_url,
                landed_url=opened.landed_url,
                results=tree,
                zeros=(),
            )
            path = save_capture(capture, config.paths.recon_dump_dir)
            print()
            print(f"--- 構造スナップショット ---\n読んだDOM構造を保存しました: {path}")
            print("内容はタグ名・クラス名・親子関係のみです (13.2)。")
            _ = encode_block
        return int(ExitCode.OK)

    if args.recon_command == "follow-send":
        from jobmedley_scout.recon.follow_send import follow_send

        # 認証済みの観測にはセッションが要る。12.7 のとおり毎回シークレットから復元する。
        _restore_session_from_secrets(config)
        walk = follow_send(
            config.browser,
            config.paths.credentials_dir,
            coordinates.url("nav.candidate_list_url"),
            coordinates.selector("nav.list_ready_selector"),
        )
        print(walk.render())
        return int(ExitCode.OK)

    if args.recon_command == "observe-job-offers":
        from jobmedley_scout.recon.observe_job_offers import observe_job_offers

        _restore_session_from_secrets(config)
        offers_observed = observe_job_offers(
            config.browser,
            config.paths.credentials_dir,
            coordinates.url("nav.candidate_list_url"),
        )
        print(offers_observed.render())
        return int(ExitCode.OK)

    if args.recon_command == "observe-headers":
        from jobmedley_scout.recon.observe_headers import observe_headers

        # 認証済みの観測にはセッションが要る。12.7 のとおり毎回シークレットから復元する。
        _restore_session_from_secrets(config)
        headers_observed = observe_headers(
            config.browser,
            config.paths.credentials_dir,
            coordinates.url("nav.candidate_list_url"),
        )
        print(headers_observed.render())
        return int(ExitCode.OK)

    if args.recon_command == "observe-search":
        from jobmedley_scout.recon.observe_search import observe_search

        # 認証済みの観測にはセッションが要る。12.7 のとおり毎回シークレットから復元する。
        _restore_session_from_secrets(config)
        search_observed = observe_search(
            config.browser,
            config.paths.credentials_dir,
            coordinates.url("nav.candidate_list_url"),
            coordinates.url("api.candidate_list.url_pattern"),
        )
        print(search_observed.render())
        return int(ExitCode.OK)

    if args.recon_command == "observe-api":
        from jobmedley_scout.recon.observe_api import observe_api

        # 認証済みの観測にはセッションが要る。12.7 のとおり毎回シークレットから復元する。
        _restore_session_from_secrets(config)
        api_observed = observe_api(
            config.browser,
            config.paths.credentials_dir,
            coordinates.url("nav.candidate_list_url"),
            coordinates.selector("nav.list_ready_selector"),
        )
        print(api_observed.render())
        return int(ExitCode.OK)

    if args.recon_command == "observe-resume":
        from jobmedley_scout.recon.observe_resume import observe_resume

        # 認証済みの観測にはセッションが要る。12.7 のとおり毎回シークレットから復元する。
        _restore_session_from_secrets(config)
        resume_observed = observe_resume(
            config.browser,
            config.paths.credentials_dir,
            coordinates.url("nav.candidate_list_url"),
            coordinates.selector("nav.list_ready_selector"),
        )
        print(resume_observed.render())
        return int(ExitCode.OK)

    if args.recon_command == "introspect":
        from jobmedley_scout.recon.introspect import introspect_send_input

        # 認証済みの問い合わせにはセッションが要る。12.7 のとおり毎回復元する。
        _restore_session_from_secrets(config)
        schema = introspect_send_input(
            config.browser,
            config.paths.credentials_dir,
            coordinates.url("api.base_url"),
        )
        print(schema.render())
        return int(ExitCode.OK)

    if args.recon_command == "verify-session":
        from jobmedley_scout.recon.verify_session import Verdict, verify_saved_session

        # 12.7: **毎回シークレットから復元する。** 実行環境は使い捨てなので、
        # 前回の実行が残したファイルを当てにできない。ここで復元しておかないと、
        # クラウドでは常に「保存セッションがありません」で終わる。
        _restore_session_from_secrets(config)
        browser = (
            config.browser.model_copy(update={"headless": False})
            if args.headful
            else config.browser
        )
        result = verify_saved_session(
            browser,
            config.paths.credentials_dir,
            coordinates.selector("auth.success_marker_selector"),
        )
        print(result.render())
        if result.passed:
            return int(ExitCode.OK)
        if result.verdict is Verdict.NOT_RESTORED:
            # 復元できなかった = 保存セッションが無効。認証切れの帯で終える (12.8)。
            return int(ExitCode.AUTH_EXPIRED)
        # 判定不能・セッション未作成。**認証切れだと断定しない** ので別の番号。
        # 「点検で止めた」の帯が意味として一番近い。
        return int(ExitCode.PREFLIGHT_FAILED)

    raise NotImplementedError(
        f"偵察コマンド '{args.recon_command}' は段階3以降の配線です。\n"
        f"  先に段階1 (`scout recon login` → `scout recon verify-session`) と\n"
        f"  段階2 (`scout preflight`) を通してください。\n"
        f"  残りの座標の取得方法は `scout coordinates` と docs/ladder.md にあります。"
    )


def _restore_session_from_secrets(config: Config) -> Path | None:
    """Materialize the saved session from whichever secret is set.

    12.7 の要求どおり、置き先は ``credentials_dir`` のみ (状態ディレクトリではない)。
    """
    from jobmedley_scout.browser.session_store import session_path
    from jobmedley_scout.config.secrets import restore_storage_state

    return restore_storage_state(load_secrets(), session_path(config.paths.credentials_dir))


def _dispatch_session(args: argparse.Namespace, config: Config) -> int:
    from jobmedley_scout.browser.session_store import session_path, to_base64

    if args.session_command == "export":
        print(to_base64(config.paths.credentials_dir))
        return int(ExitCode.OK)

    if args.session_command == "import":
        from jobmedley_scout.handover.curl_session import (
            parse_curl,
            storage_state_from_curl,
            summarize,
            write_storage_state,
        )

        # **標準入力から読む。** 引数で受け取る形にすると、セッションクッキーが
        # シェル履歴とプロセス一覧に残る。ファイル経由にすると、リポジトリの中に
        # 置いてそのままコミットする事故が起きる。どちらも取り返しがつかない。
        pasted = sys.stdin.read()
        if not pasted.strip():
            pasted = load_secrets().session_curl or ""
        if not pasted.strip():
            raise ConfigError(
                "入力がありません。ブラウザの開発者ツール → Network で認証済みの\n"
                "  リクエストを右クリックし「Copy as cURL」でコピーしたものを、\n"
                "  標準入力から渡すか JOBMEDLEY_SESSION_CURL に設定してください。"
            )

        state = storage_state_from_curl(pasted)
        destination = session_path(config.paths.credentials_dir)
        write_storage_state(state, destination)
        print(summarize(state, parse_curl(pasted).user_agent))
        return int(ExitCode.OK)

    if args.session_command == "check":
        # シークレットが正しい形かどうかだけを、ブラウザも通信も使わずに確かめる。
        # 媒体へ到達できない環境 (この開発コンテナもそう) でも実行できる。
        restored = _restore_session_from_secrets(config)
        if restored is None:
            print(
                "シークレットにセッションがありません。\n"
                "  JOBMEDLEY_STORAGE_STATE_B64 または JOBMEDLEY_SESSION_CURL の\n"
                "  どちらかを設定してください。",
                file=sys.stderr,
            )
            return int(ExitCode.PREFLIGHT_FAILED)

        from jobmedley_scout.handover.curl_session import summarize

        state = json.loads(restored.read_text(encoding="utf-8"))
        print(summarize(state))
        # **これは「ログインできる」ことの確認ではない。** 形が正しいだけである。
        print("\n注意: 形式の確認だけです。実際に入れるかは verify-session が判定します。")
        return int(ExitCode.OK)

    parser_error = f"未実装のサブコマンド: session {args.session_command}"
    print(parser_error, file=sys.stderr)
    return int(ExitCode.USAGE)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
