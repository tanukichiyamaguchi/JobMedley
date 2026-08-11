"""Silent-zero detection (原則2).

> **最も危険な失敗は例外ではなく「静かなゼロ件」。**

参照実装では、パスワード期限切れで全 API がエラーを返していたにもかかわらず、
各メソッドが警告を出して空の値を返すだけだったため、**CIは緑のまま送信0件が
数日続いた** (6.6)。例外が飛ばないので、監視も気づかない。

そこで「対象はあった・1件も送れていない・失敗は起きている」という組み合わせを
**能動的に異常と判定する**。判定材料はすべて呼び出し側が数えた件数なので、
このモジュールは純粋関数のままでよい (api/ から import される)。

判定表:

===========  ======  ========  ==============================================
targets      sent    failures  判定
===========  ======  ========  ==============================================
>= 1         0       >= 1      **全滅** -- 例外にする
>= 1         0       0         全滅ではない (全件が正当に除外された可能性)
>= 1         >= 1    任意      送信は発生している
0            0       任意      そもそも対象が無い
===========  ======  ========  ==============================================

``(targets >= 1, sent == 0, failures == 0)`` を全滅に含めないのは、除外条件を
満たして1件も送らなかった正常な日と区別できないため。ただし「対象があるのに
0件送信」という事実は運用上の注意対象なので、:attr:`WipeoutVerdict.noteworthy`
として別に立てて、レポートには必ず出す (12.5: 件数は必ず報告する)。
"""

from __future__ import annotations

from dataclasses import dataclass

from jobmedley_scout.errors import WipeoutDetected


@dataclass(frozen=True)
class WipeoutVerdict:
    """Whether this run was a silent wipeout, and the reason either way.

    真偽値だけを返さないのは、呼び出し側が「なぜ全滅と判定した/しなかったか」を
    そのままレポートに載せられるようにするため (8.5/12.8)。
    """

    detected: bool
    reason: str
    targets: int
    sent: int
    failures: int

    @property
    def noteworthy(self) -> bool:
        """Targets existed but nothing was sent -- worth printing even if not a wipeout."""
        return self.targets >= 1 and self.sent == 0

    def describe(self) -> str:
        return f"対象{self.targets}件・送信{self.sent}件・失敗{self.failures}件: {self.reason}"


def detect_wipeout(targets: int, sent: int, failures: int) -> WipeoutVerdict:
    """Detect the silent zero: there were targets, nothing was sent, something failed.

    原則2。例外が飛ばない失敗を、件数の形から異常として拾い上げる唯一の場所。
    """
    for name, value in (("targets", targets), ("sent", sent), ("failures", failures)):
        if value < 0:
            raise ValueError(f"{name} が負の値です: {value}")

    if targets == 0:
        return WipeoutVerdict(
            detected=False,
            reason="対象が0件なので全滅ではない (対象抽出そのものは別途 9.6 で監視する)",
            targets=targets,
            sent=sent,
            failures=failures,
        )
    if sent >= 1:
        return WipeoutVerdict(
            detected=False,
            reason="送信が1件以上発生している",
            targets=targets,
            sent=sent,
            failures=failures,
        )
    if failures == 0:
        # 全件が除外条件に当たった正常な日と区別できないので、ここは異常にしない。
        # ただし noteworthy=True なので、レポートには必ず出る (12.5)。
        return WipeoutVerdict(
            detected=False,
            reason=(
                "対象はあるが失敗は0件。全件が除外条件に当たった可能性があり、"
                "全滅とは判定しない (ただし要注意として報告する)"
            ),
            targets=targets,
            sent=sent,
            failures=failures,
        )
    return WipeoutVerdict(
        detected=True,
        reason=(
            "対象があり、1件も送信できず、失敗が発生している。"
            "原則2の「静かなゼロ件」なので異常として扱う"
        ),
        targets=targets,
        sent=sent,
        failures=failures,
    )


def raise_if_wipeout(verdict: WipeoutVerdict) -> None:
    """Turn a detected wipeout into the exception that fails the run.

    判定と送出を分けているのは、レポートに件数と理由を出してから落としたいため
    (12.5: 握りつぶしてよい失敗でも件数は必ず出す)。
    """
    if verdict.detected:
        raise WipeoutDetected(verdict.describe())
