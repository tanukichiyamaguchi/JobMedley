"""Exception hierarchy.

The distinction that actually matters operationally is **permanent vs transient**.
A permanent error must abort the run and force a non-zero exit code; a transient
one may be warned about and skipped (but the skip must still be counted and
reported -- see ``runtime.report``).

参照実装の事故 (6.6):
パスワード期限切れで全 API がエラーを返していたにもかかわらず、各メソッドが
警告ログを出して空の値を返すだけだったため、CI は成功 (緑) のまま送信0件が
続いた。`PermanentAuthError` は「例外を握りつぶすすべての except 節で再送出
する」対象であり、これを守るためのテストが tests/api/test_auth_error_propagates.py。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jobmedley_scout.config.placeholders import Unresolved


class ScoutError(Exception):
    """Base class for every error this system raises deliberately."""


class PermanentError(ScoutError):
    """An error that cannot be fixed by retrying. The run must abort non-zero.

    恒久エラーを一時エラーと同じ扱いにすると、原則2の「静かなゼロ件」になる。
    """


class TransientError(ScoutError):
    """An error that may reasonably succeed on a later run.

    握りつぶしてよいが、件数は必ずレポートに出すこと (12.5)。
    """


class PermanentAuthError(PermanentError):
    """The platform session is no longer valid; every subsequent call will fail.

    判定は保守的に行う (6.6): 401、または 403 かつエラーコードが認証系のとき
    だけ。単発の権限エラーで実行全体を落とさないため。
    """

    def __init__(self, detail: str, *, status: int | None = None, code: str | None = None):
        self.detail = detail
        self.status = status
        self.code = code
        parts = [detail]
        if status is not None:
            parts.append(f"status={status}")
        if code is not None:
            parts.append(f"code={code}")
        super().__init__(" ".join(parts))


class UnresolvedCoordinateError(PermanentError):
    """A site coordinate was used before it had been confirmed against real data.

    「推測で埋めないこと」の実行時最終防壁。型検査 (Coord[T]) と設定読込時の
    検証をすり抜けた場合にのみここへ到達する。
    """

    def __init__(self, coordinate: Unresolved, *, used_by: str) -> None:
        self.coordinate = coordinate
        self.used_by = used_by
        super().__init__(
            f"未確定の媒体座標 '{coordinate.key}' を {used_by} が使用しようとしました。\n"
            f"  ラダー段階: {coordinate.stage}\n"
            f"  取得方法  : {coordinate.how_to_obtain}\n"
            f"  `scout coordinates` で未確定の座標一覧を確認し、"
            f"config/site_coordinates.yaml に記入してください。"
        )


class ConfigError(PermanentError):
    """The configuration file is invalid: unknown key, wrong type, missing key.

    寛容な読み込み (キーが無ければ既定値) は事故装置 (7.6)。ここで必ず落とす。
    """


class StateIntegrityError(PermanentError):
    """The state database regressed or is internally inconsistent.

    12.1 の巻き戻り事故 (送信記録56件消失) を検知するための例外。
    """


class WipeoutDetected(PermanentError):
    """There was at least one target, nothing was sent, and something failed.

    原則2の「静かなゼロ件」を能動的に異常として扱うための例外。
    """


class KillSwitchEngaged(ScoutError):
    """The kill-switch file is present. This is a clean, intentional stop.

    エラーではないので終了コードは 0 系。異常終了と混同しないこと。
    """


class GenerationError(TransientError):
    """Message generation failed for one candidate.

    集計値として出力する (8.2)。ログだけに出すと、LLM API の仕様変更で
    本番が全件失敗しても気づけない。
    """


class SendFailed(TransientError):
    """A send attempt failed in a way that leaves the outcome knowable as failed.

    送信APIには自動リトライを掛けない (12.5)。次回実行に委ねる。
    """
