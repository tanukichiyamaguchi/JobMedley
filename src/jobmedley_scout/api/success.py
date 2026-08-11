"""Success determination -- the ONE place an HTTP status is judged.

6.2 の事故:

> **成功ステータスはエンドポイントごとに違います。** 参照実装では通常送信が200、
> プラチナ送信とピックアップ送信が201でした。200のみを成功とみなす実装では、
> 成功しているのに失敗扱いになります。**成功判定は1箇所に集約してください。**

「1箇所に集約」を人間の規律ではなくテストで守るため、
``tests/guardrails/test_status_compare_only_here.py`` が、本モジュール以外での
ステータスコードの数値比較 (``status == 200`` / ``status_code in (...)`` など) を
ソース走査で禁止している。

新しいエンドポイントを足すときは、成功ステータスを **実測してから** 座標に書くこと。
推測で 200 を書くと、この事故をそのまま再現する。
"""

from __future__ import annotations

from jobmedley_scout.api.endpoints import Endpoint
from jobmedley_scout.config.placeholders import require
from jobmedley_scout.errors import ConfigError, UnresolvedCoordinateError


def is_success(endpoint: Endpoint, status: int) -> bool:
    """Whether ``status`` means success **for this endpoint**."""
    statuses = require(endpoint.success_statuses, used_by=f"api.success.is_success({endpoint.id})")
    if statuses is None:
        raise ConfigError(
            f"エンドポイント '{endpoint.id}' の成功ステータスが null です。"
            f"この枠が存在しないなら、そもそも呼び出さないでください。"
        )
    return status in statuses


def describe_status(endpoint: Endpoint, status: int) -> str:
    """A human-readable verdict, for logs and the run report."""
    try:
        verdict = "成功" if is_success(endpoint, status) else "失敗"
    except (ConfigError, UnresolvedCoordinateError):
        # 診断文字列を作るだけの関数なので、座標未確定でも例外にせず事実を書く。
        # 判定そのものが必要な経路は is_success を直接呼び、そこでは止まる。
        verdict = "判定不能(成功ステータスが未確定)"
    return f"{endpoint.id}: HTTP {status} -> {verdict}"
