"""Detection sentinels for reconnaissance.

3章 段階3: 「件名・本文に検知用の目印文字列を入れ、**万一送信されても内容から
検知できるようにする**」。

センチネルは **解析時にのみ** 使う。武装中のブロック判定に使ってはいけない --
段階3では送信URLそのものが未知なので、センチネルやURLでの絞り込みは循環参照に
なるうえ、payload にセンチネルが載らない送信は素通ししてしまう
(:mod:`recon.gate` の fail-closed 方針を参照)。
"""

from __future__ import annotations

import hashlib

#: 目立ち、かつ通常の文面には絶対に現れない前置き。万一これが本番送信されて
#: しまった場合、媒体の送信済み画面を検索すれば即座に見つかる。
SENTINEL_PREFIX = "ZZRECON"


def make_sentinel(run_id: str) -> str:
    """A per-run sentinel string."""
    digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:10].upper()
    return f"{SENTINEL_PREFIX}-{digest}"


def sentinel_subject(sentinel: str) -> str:
    return f"{sentinel} 偵察テスト 送信されていはいけない"


def sentinel_body(sentinel: str) -> str:
    return (
        f"{sentinel}\n"
        "これは内部APIの形状を特定するための偵察用ダミー本文です。\n"
        "ネットワーク層で中断されるため送信されません。\n"
        "万一この文面を受信した方がいれば、システム上の不具合です。"
    )


def contains_sentinel(text: str, sentinel: str) -> bool:
    return sentinel in text


def find_sentinel_requests(
    recorded: tuple[tuple[str, str, str | None], ...], sentinel: str
) -> tuple[tuple[str, str, str | None], ...]:
    """Pick out the recorded requests that carried our sentinel.

    武装中は **全ての非GET** を記録しているので、この関数が「どれが送信APIだったか」を
    解析時に切り分ける。計測ビーコン等の無関係なPOSTも記録されているのが正常。
    """
    return tuple(entry for entry in recorded if entry[2] and sentinel in entry[2])
