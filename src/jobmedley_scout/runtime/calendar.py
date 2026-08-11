"""Business-calendar decision -- 「今日は送ってよい日か」.

12.4 の事故:

> 「土日祝は送らない」を **シェルスクリプトに書いた** ため、祝日を扱えず、
> テストもできませんでした。

したがって判定はアプリ側の純粋関数に置き、**休止の理由を文字列で返す**。実行基盤
(GitHub Actions のステップ) は「文字列プロトコルで結果を受け取る」だけにする --
判定ロジックをワークフローYAMLへ滲み出させない。滲み出した瞬間、テストできない
場所に業務ルールが増える。

フェイルセーフの向き (**明示的にこちらへ倒している**):

判定そのものが失敗したときは、``SKIP:`` 行が出ない。ワークフロー側は
``scout should-run || echo "RUN: ..."`` (:data:`FAILSAFE_RUN_LINE`) で受けており、
**休止指示が無ければ従来どおり送信へ進む**。設定エラーで業務を止めないためである。
その代わり、設定が壊れているなら次の起動前チェック (12.6) が同じ設定で落ちるので、
壊れた設定のまま送信が続くことにはならない。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Final

from jobmedley_scout.clock import to_jst
from jobmedley_scout.config.schema import CalendarConfig
from jobmedley_scout.errors import ConfigError

#: 実行基盤との唯一の接点。ワークフローは行頭の接頭辞だけを見る。
SKIP_PREFIX: Final[str] = "SKIP:"
RUN_PREFIX: Final[str] = "RUN:"

#: 判定に失敗したときにワークフローが代わりに出力する行。
#: ``.github/workflows/scout.yml`` の ``|| echo`` と **同じ文字列**であること。
#: ここに置いてあるのは、片方だけ書き換えられたときに気づけるようにするため。
FAILSAFE_RUN_LINE: Final[str] = f"{RUN_PREFIX} 判定に失敗したためフェイルセーフで続行"

#: ``datetime.weekday()`` の並び (月曜=0)。
_WEEKDAY_NAMES: Final[tuple[str, ...]] = ("月", "火", "水", "木", "金", "土", "日")
_SATURDAY: Final[int] = 5


@dataclass(frozen=True)
class RunDecision:
    """Whether this run may proceed, and **why** -- the reason gets logged."""

    should_run: bool
    #: 空文字は許さない。理由の無い休止はログを読んでも原因が分からず、
    #: 「なぜか送られていない」という最も厄介な状態になる。
    reason: str

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("RunDecision には必ず理由を付けること (12.4: 理由がログに残る)")


def should_run(now: datetime, cfg: CalendarConfig) -> RunDecision:
    """Decide whether the scout may run at ``now``.

    Evaluated in JST: 媒体も業務も日本時間で動く。UTC で曜日を切ると、日本の
    月曜早朝が日曜と判定されて丸1日止まる。
    """
    local = to_jst(now)
    today = local.date()

    # 判定順は「より恒久的な理由」を先に出す。土曜の朝に「時間外」とだけ
    # ログへ出ると、その日ずっと止まる理由が運用者に伝わらない。
    if cfg.skip_weekends and local.weekday() >= _SATURDAY:
        return RunDecision(
            should_run=False,
            reason=(
                f"{today.isoformat()} は{_WEEKDAY_NAMES[local.weekday()]}曜日 (JST) のため"
                f"送信しません"
            ),
        )

    if _is_holiday(today, cfg):
        return RunDecision(
            should_run=False,
            reason=f"{today.isoformat()} は休業日として設定されているため送信しません",
        )

    start, end = _validated_window(cfg)
    if not _in_window(local.hour, start, end):
        return RunDecision(
            should_run=False,
            reason=(
                f"現在 JST {local.hour}時は送信可能時間帯 "
                f"{start}時〜{end}時 の外のため送信しません"
            ),
        )

    return RunDecision(
        should_run=True,
        reason=(
            f"{today.isoformat()} ({_WEEKDAY_NAMES[local.weekday()]}) JST {local.hour}時は"
            f"送信可能時間帯 {start}時〜{end}時 の内側です"
        ),
    )


def render_protocol(decision: RunDecision) -> str:
    """Render the one line the CI step reads.

    実行基盤へ渡す情報はこの1行だけ。構造化データを渡さないのは、渡した分だけ
    ワークフロー側に判定が書かれるからである (12.4 の再発経路)。
    """
    prefix = RUN_PREFIX if decision.should_run else SKIP_PREFIX
    return f"{prefix} {decision.reason}"


def parse_protocol(line: str) -> RunDecision:
    """Inverse of :func:`render_protocol` -- the contract, executable.

    ワークフロー側の ``if [[ "$DECISION" == SKIP:* ]]`` と同じ判定をここに置き、
    往復をテストで固定する。**未知の接頭辞は休止扱いにしない**: 判定不能を休止に
    倒すと、出力形式が変わっただけで業務が止まる (フェイルセーフの向きが逆になる)。
    """
    stripped = line.strip()
    if stripped.startswith(SKIP_PREFIX):
        return RunDecision(should_run=False, reason=stripped[len(SKIP_PREFIX) :].strip())
    if stripped.startswith(RUN_PREFIX):
        return RunDecision(should_run=True, reason=stripped[len(RUN_PREFIX) :].strip())
    return RunDecision(
        should_run=True,
        reason=f"判定行を解釈できませんでした ({stripped!r})。フェイルセーフで続行します",
    )


def _is_holiday(today: date, cfg: CalendarConfig) -> bool:
    """Whether ``today`` is in the configured holiday list.

    不正な日付表記は **黙って無視しない**。無視すると「祝日を書いたのに送信された」
    という 12.4 そのものに戻る。ここで落ちても休止指示が出ないだけで業務は止まらず、
    起動前チェックが同じ設定で失敗する。
    """
    malformed: list[str] = []
    holidays: set[date] = set()
    for entry in cfg.holidays:
        try:
            holidays.add(date.fromisoformat(entry.strip()))
        except ValueError:
            malformed.append(entry)
    if malformed:
        raise ConfigError(
            f"calendar.holidays に日付として読めない項目があります: {malformed}。"
            f"YYYY-MM-DD 形式で記入してください。"
        )
    return today in holidays


def _validated_window(cfg: CalendarConfig) -> tuple[int, int]:
    """The send window as ``(start_hour, end_hour_exclusive)``.

    ``start == end`` を許さないのは、24時間なのか0時間なのかを **設定から読み取れる
    と言い切れない** ため。推測で片方に倒すと、24時間営業か全面停止かという最大級の
    差を無言で決めてしまう。
    """
    start = cfg.send_window_start_hour_jst
    end = cfg.send_window_end_hour_jst
    if not 0 <= start <= 23:
        raise ConfigError(f"calendar.send_window_start_hour_jst は 0〜23 の範囲です: {start}")
    if not 1 <= end <= 24:
        raise ConfigError(
            f"calendar.send_window_end_hour_jst は 1〜24 の範囲です (終端は排他): {end}"
        )
    if start == end:
        raise ConfigError(
            f"送信可能時間帯の開始と終了が同じ値です ({start})。"
            f"24時間なのか0時間なのか判別できません。終端は排他なので、"
            f"終日許可するなら 0〜24 と書いてください。"
        )
    return start, end


def _in_window(hour: int, start: int, end: int) -> bool:
    """Half-open ``[start, end)`` in JST hours, wrapping past midnight if needed."""
    if start < end:
        return start <= hour < end
    # start > end は日付をまたぐ窓 (例: 22時〜6時)。深夜帯に送る運用は推奨しないが、
    # 設定として書けてしまう以上、黙って「常に時間外」にしない。
    return hour >= start or hour < end
