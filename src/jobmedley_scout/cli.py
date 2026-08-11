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
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from jobmedley_scout.config.audit import assert_ready_for, render_audit
from jobmedley_scout.config.loader import load_all
from jobmedley_scout.config.secrets import load_secrets
from jobmedley_scout.errors import KillSwitchEngaged, ScoutError
from jobmedley_scout.runtime.exit_codes import ExitCode, exit_code_for

if TYPE_CHECKING:  # pragma: no cover - typing only
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
    sub.add_parser("followup", help="追客を実行する")
    sub.add_parser("sync-replies", help="返信を検知する")
    sub.add_parser("analytics", help="週次・月次の分析を出力する")

    dryrun = sub.add_parser("dryrun", help="送信直前まで通す (段階5)")
    dryrun.add_argument("--limit", type=int, default=5)

    recon = sub.add_parser("recon", help="偵察コマンド (常設)")
    recon_sub = recon.add_subparsers(dest="recon_command", required=True)
    # ``login`` に --headful は無い。**常にヘッドフルで開く。** 人間が操作する
    # コマンドなので選択肢にする意味がなく、選べる形にすると headless=true の
    # 既定のまま実行して「何も起きない」事故が起きる。
    recon_sub.add_parser("login", help="手動ログインしセッションを保存 (常にヘッドフル)")
    verify = recon_sub.add_parser("verify-session", help="保存セッションで入り直して確認")
    # こちらは機械判定なので既定はヘッドレスでよい。目で見たいときだけ開く。
    verify.add_argument("--headful", action="store_true", help="画面を開いて目視でも確認する")
    recon_sub.add_parser("capture-send", help="送信を中断しつつ内部APIを特定")
    recon_sub.add_parser("resume-keys", help="レジュメのキーパスを出力 (値は出さない)")
    recon_sub.add_parser("inbox", help="受信箱の構造を観測")

    session = sub.add_parser("session", help="セッション管理")
    session_sub = session.add_subparsers(dest="session_command", required=True)
    session_sub.add_parser("export", help="保存セッションをbase64で出力 (CIシークレット用)")

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
        from jobmedley_scout.browser.session_store import to_base64

        print(to_base64(config.paths.credentials_dir))
        return int(ExitCode.OK)

    # ここから先は媒体座標を要求する。足りなければ **明示的に停止する** --
    # 黙って0件で成功しないことが要件 (原則2)。
    command_key = {
        "ingest": "ingest",
        "generate": "generate",
        "send": "send",
        "followup": "followup",
        "sync-replies": "sync-replies",
        "analytics": "analytics",
        "dryrun": "send",
    }.get(args.command)

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


#: 偵察サブコマンドから必須座標集合への写像。``login`` と ``verify-session`` が
#: ``recon-login`` (空集合) を指すのが要点である。**段階1は発見の工程なので、
#: 段階1の座標を要求しない。** 要求すると1歩目が自分の成果物待ちで始められない。
_RECON_COORDINATE_KEYS: dict[str, str] = {
    "login": "recon-login",
    "verify-session": "recon-login",
    "capture-send": "recon-capture-send",
    "resume-keys": "recon-resume-keys",
    "inbox": "recon-capture-send",
}


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

    if args.recon_command == "verify-session":
        from jobmedley_scout.recon.verify_session import Verdict, verify_saved_session

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


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
