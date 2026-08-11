"""Randomized waits.

5.2: **操作の速さそのものがボット判定のシグナルになる。**

送信間隔と1実行あたりの送信上限は、ボット検知が疑われたときに調整する運用ノブでも
あるため、すべて設定値として外に出してある (:class:`config.schema.WaitsConfig`,
``send.interval``)。

**送信間隔は dry_run 時にも適用する** -- 実ブラウザ操作は発生しているため。

区間の抽選 (:func:`sample`) は純粋関数として分離してあり、テストできる。実際に
眠る :func:`pause` だけがこのモジュールに閉じている
(``tests/guardrails/test_no_raw_sleep.py`` が ``time.sleep`` の使用箇所をここに
限定している)。
"""

from __future__ import annotations

import random
import time

from jobmedley_scout.config.schema import WaitRange


def sample(interval: WaitRange, rng: random.Random | None = None) -> float:
    """Pick a wait duration from the range. Pure given an injected ``rng``."""
    source = rng or random
    return source.uniform(interval.min_seconds, interval.max_seconds)


def pause(interval: WaitRange, rng: random.Random | None = None) -> float:
    """Sleep for a sampled duration and return how long it slept.

    実際に眠る唯一の場所。ここ以外で ``time.sleep`` を呼ぶと、ガードレール
    テストが落ちる。
    """
    seconds = sample(interval, rng)
    time.sleep(seconds)
    return seconds


def total_expected_seconds(interval: WaitRange, count: int) -> float:
    """Expected total wait for ``count`` intervals.

    12.2 の実行時間バジェット計算式に使う:

        所要時間 = 起動時間 + 送信件数 × 平均送信間隔 + 返信スキャン + 分析同期

    **送信上限と送信間隔の期待値を先に確定してから、上限件数を決めること**
    (逆順にしない)。この関数はその見積もりを機械的に出すためにある。
    """
    if count <= 0:
        return 0.0
    return count * (interval.min_seconds + interval.max_seconds) / 2.0
