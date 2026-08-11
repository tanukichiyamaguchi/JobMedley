"""Run reporting.

12.8: 「送信件数・失敗件数・スキップ理由の内訳を、**実行のたびに1行のサマリで出す**」。

なぜ1行のサマリが要るか -- 9.6 の事故がその答えである:

> 上限つきバッチが同じ最古N件を再訪していた。**この種のバグはエラーを出さないため、
> 件数のログ (対象N件・処理M件) を出さないと発見できない。**

同じ理由で、**読み取り失敗によるスキップ件数を必ず出す** (12.5)。黙って対象が減るのを
防ぐため。生成失敗も集計値として出す (8.2) -- ログの奥に埋めると、LLM APIの仕様変更で
全件失敗しても気づけない。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from jobmedley_scout.models.send_record import SendSlot


@dataclass
class RunReport:
    """Counts for one run, plus the derived anomaly verdicts."""

    run_id: str
    command: str
    dry_run: bool

    # --- 対象と処理 (9.6: 両方出さないと再訪バグが見えない) -----------------
    targets_total: int = 0
    targets_processed: int = 0

    # --- 生成 (8.2: 集計値として出す) ---------------------------------------
    generated_ok: int = 0
    generation_failed: int = 0

    # --- 送信 (9.4: 枠ごと。UNKNOWN も一級市民) -----------------------------
    sent_by_slot: dict[SendSlot, int] = field(
        default_factory=lambda: {slot: 0 for slot in SendSlot}
    )
    send_failed: int = 0

    # --- スキップ -----------------------------------------------------------
    skipped_by_reason: dict[str, int] = field(default_factory=dict)
    #: 12.5: 媒体の読み取りAPIの失敗は警告してスキップしてよいが、**件数を出す**。
    read_errors_skipped: int = 0

    #: 9.7: 上限で切り捨てが起きたら、必ず件数と対処法をログに出す。
    truncated_by_cap: dict[str, int] = field(default_factory=dict)

    #: 6.5: マッピング追加のサインとなる、未知のenum生値。
    unknown_enum_values: tuple[str, ...] = ()

    @property
    def sent_total(self) -> int:
        return sum(self.sent_by_slot.values())

    @property
    def skipped_total(self) -> int:
        return sum(self.skipped_by_reason.values())

    def record_skip(self, reason: str) -> None:
        self.skipped_by_reason[reason] = self.skipped_by_reason.get(reason, 0) + 1

    def record_sent(self, slot: SendSlot) -> None:
        self.sent_by_slot[slot] = self.sent_by_slot.get(slot, 0) + 1

    def record_truncation(self, scope: str, dropped: int) -> None:
        """9.7 / 12.8: 切り捨ては黙って起こさない。

        「上限つきクエリすべてについて、対象数が上限を超えたら何が起きるか」を
        運用者が見られるようにするため。
        """
        if dropped > 0:
            self.truncated_by_cap[scope] = self.truncated_by_cap.get(scope, 0) + dropped

    # --- 異常検知 -----------------------------------------------------------
    @property
    def is_wipeout(self) -> bool:
        """原則2: 最も危険な失敗は例外ではなく「静かなゼロ件」。

        処理対象が1件以上あるのに、送信0件かつ失敗あり。
        """
        return self.targets_total >= 1 and self.sent_total == 0 and self.send_failed >= 1

    @property
    def is_silent_zero(self) -> bool:
        """対象があったのに何も送らず、失敗も無い。

        全員が正当にスキップされた可能性もあるが、条件が壊れている可能性もある。
        警告として出し、人間が判断する。
        """
        return (
            self.targets_total >= 1
            and self.sent_total == 0
            and self.send_failed == 0
            and not self.dry_run
        )

    @property
    def generation_all_failed(self) -> bool:
        """8.2: LLM APIの仕様変更で本番が静かに全滅しうる。"""
        attempted = self.generated_ok + self.generation_failed
        return attempted >= 1 and self.generated_ok == 0

    def anomalies(self) -> tuple[str, ...]:
        """能動通知すべき事象 (12.8)。ジョブの実行結果画面だけでは気づけない。"""
        found: list[str] = []
        if self.is_wipeout:
            found.append(
                f"全滅検知: 対象 {self.targets_total} 件に対し送信0件・失敗 {self.send_failed} 件"
            )
        if self.is_silent_zero:
            found.append(
                f"静かなゼロ件: 対象 {self.targets_total} 件だが送信も失敗も0件。"
                f"対象条件かスキップ判定が壊れている可能性があります"
            )
        if self.generation_all_failed:
            found.append(
                f"生成が全件失敗 ({self.generation_failed} 件)。"
                f"LLM APIの仕様変更の可能性があります (8.2)"
            )
        if self.read_errors_skipped > 0:
            found.append(f"読み取り失敗によるスキップ {self.read_errors_skipped} 件")
        if self.truncated_by_cap:
            detail = ", ".join(f"{k}:{v}件" for k, v in sorted(self.truncated_by_cap.items()))
            found.append(f"上限による切り捨て: {detail} -- 上限の引き上げを検討してください")
        return tuple(found)

    # --- 出力 ---------------------------------------------------------------
    def one_line(self) -> str:
        """The single-line summary 12.8 asks for."""
        mode = "DRY-RUN" if self.dry_run else "LIVE"
        slots = "/".join(f"{slot}={self.sent_by_slot.get(slot, 0)}" for slot in SendSlot)
        return (
            f"[{mode}] {self.command} run={self.run_id} "
            f"対象={self.targets_total} 処理={self.targets_processed} "
            f"生成OK={self.generated_ok} 生成NG={self.generation_failed} "
            f"送信={self.sent_total} ({slots}) 失敗={self.send_failed} "
            f"スキップ={self.skipped_total} 読取失敗={self.read_errors_skipped}"
        )

    def render(self) -> str:
        lines = [self.one_line(), ""]

        if self.skipped_by_reason:
            lines.append("スキップ理由の内訳:")
            for reason, count in sorted(self.skipped_by_reason.items(), key=lambda kv: -kv[1]):
                lines.append(f"  {count:>4} 件  {reason}")
            lines.append("")

        # 9.4 の恒等式をレポート上でも見えるようにする。
        lines.append(
            f"送信枠の内訳 (合計 == 有料 + 無料 + 不明): {self.sent_total} == "
            + " + ".join(str(self.sent_by_slot.get(slot, 0)) for slot in SendSlot)
        )

        if self.unknown_enum_values:
            lines.append("")
            lines.append("未知のenum値 (マッピング追加のサイン, 6.5):")
            lines.extend(f"  {value}" for value in self.unknown_enum_values)

        anomalies = self.anomalies()
        if anomalies:
            lines.append("")
            lines.append("**要確認**")
            lines.extend(f"  - {item}" for item in anomalies)
        return "\n".join(lines)


def assert_identity(report: RunReport) -> None:
    """9.4 の恒等式が成り立つことを表明する。

    「合計イコールAプラスBプラス不明」が崩れているなら、どこかで枠を落としている。
    """
    total = sum(report.sent_by_slot.values())
    if total != report.sent_total:  # pragma: no cover - defensive
        raise AssertionError(
            f"送信枠の恒等式が壊れています: {report.sent_total} != {total}。"
            f"9.4: 「不明」を独立したカテゴリとして残すこと。"
        )
