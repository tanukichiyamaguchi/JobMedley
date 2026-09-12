"""The platform's remaining send quota. **毎回読む。**

8章の要求は「残数は毎回読む」である。引き算で持つと必ずずれる -- 媒体側で人が
送るし、月次でリセットされる。座標ファイルの注記にはこうある::

    2026-08-21 observe-api 3回目で **観測した**。一覧を開くと必ず飛んでいる::

        GET /api/customers/messages/scout_count/
          remaining_count   <number>
          total_count       <number>

    **これは 8章の「残数は毎回読む」を満たす**。

なぜこのモジュールが今まで無かったのか
--------------------------------------

**座標は満たしていたが、コードは一度も読んでいなかった。** 2026-09-12 に本番の
送信が ``HTTP 200`` かつ ``data.result.errorMessage`` 有りで失敗し、原因を追う
途中で ``api.quota.url_pattern`` の呼び出し元が **0件** であることが分かった。

座標を観測し、``Endpoint`` を定義し、「8章を満たす」と書いて、そこで止まって
いた。**部品ごとには正しく、部品のあいだが繋がっていない** -- このプロジェクトが
繰り返している形である (実測48・49・55回目、そして辞退の判定も同じだった)。

読まないあいだ、枠を使い切っていても **理由の分からない失敗** として現れる。
媒体のエラー文言は 13.2 のため記録していないので、画面を見るまで誰にも分からない。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from jobmedley_scout.api.client import JobMedleyApiClient
from jobmedley_scout.api.endpoints import QUOTA, Endpoint
from jobmedley_scout.config.placeholders import Unresolved

#: 観測したキー名。**どちらが「残り」かは名前が述べている。**
REMAINING_KEY: Final[str] = "remaining_count"
TOTAL_KEY: Final[str] = "total_count"


class QuotaReading:
    """What the platform said about the remaining quota. **三値である。**

    ``remaining`` が ``None`` は **「読めなかった」** であって「0」ではない。
    畳むと、応答の形が変わった日に「枠切れ」と報告して静かに送信を止める
    (原則2) か、逆に枠が無いのに送り始める。どちらも事故である。
    """

    __slots__ = ("remaining", "total", "evidence")

    def __init__(self, *, remaining: int | None, total: int | None, evidence: str) -> None:
        self.remaining = remaining
        self.total = total
        self.evidence = evidence

    def exhausted(self) -> bool:
        """Whether the quota is known to be used up. **読めなければ False。**

        読めないことを「枠切れ」に倒すと、応答の形が変わった日に送信が全部
        止まる。止めるかどうかは :func:`should_stop` が決める。
        """
        return self.remaining is not None and self.remaining <= 0

    def readable(self) -> bool:
        return self.remaining is not None

    def describe(self) -> str:
        """A line for the report. **値を出してよい** -- 個人データではない。"""
        if self.remaining is None:
            return f"送信枠の残数: 読めませんでした ({self.evidence})"
        if self.total is None:
            return f"送信枠の残数: {self.remaining}"
        return f"送信枠の残数: {self.remaining} / {self.total}"


def _as_int(value: object) -> int | None:
    """Read a count. **文字列で来ても読む** (7.7 の寛容さ)。真偽値は数ではない。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text)
        except ValueError:
            return None
    return None


def reading_from_response(body: Mapping[str, object] | None) -> QuotaReading:
    """Parse the quota response. **推測で埋めない** (原則3)。"""
    if body is None:
        return QuotaReading(remaining=None, total=None, evidence="応答がJSONではありません")
    remaining = _as_int(body.get(REMAINING_KEY))
    total = _as_int(body.get(TOTAL_KEY))
    if remaining is None:
        present = ", ".join(sorted(str(k) for k in body)) or "(空)"
        return QuotaReading(
            remaining=None,
            total=total,
            # **キー名は出すが値は出さない。** 残数は個人データではないが、
            # 読めなかった応答に何が入っているかは分からないので値は伏せる。
            evidence=f"{REMAINING_KEY} が数として読めません。在ったキー: {present}",
        )
    return QuotaReading(remaining=remaining, total=total, evidence="観測しました")


def read_quota(client: JobMedleyApiClient, endpoints: Mapping[str, Endpoint]) -> QuotaReading:
    """Ask the platform how many sends are left. **副作用は無い** (GET)。

    座標が未確定なら「読めなかった」を返す。**送信を止めるかどうかはここでは
    決めない** -- 呼び出し側が :func:`should_stop` で決める。
    """
    endpoint = endpoints.get(QUOTA)
    if endpoint is None:
        return QuotaReading(
            remaining=None, total=None, evidence="残数照会のendpointが組まれていません"
        )
    url = endpoint.url_pattern
    # **ここで require() を使わない。** 未確定なら例外を投げるのが require の
    # 仕事だが、残数が読めないことは送信を止める理由ではない (:func:`should_stop`)。
    # 投げると、座標が1つ埋まっていないだけで送信路全体が死ぬ。
    if url is None or isinstance(url, Unresolved):
        return QuotaReading(
            remaining=None, total=None, evidence="api.quota.url_pattern が未確定です"
        )
    outcome = client.call(endpoint, url=url)
    if not outcome.succeeded:
        return QuotaReading(
            remaining=None, total=None, evidence=f"照会が失敗しました (HTTP {outcome.status})"
        )
    return reading_from_response(outcome.json_body())


def should_stop(reading: QuotaReading) -> bool:
    """Whether to refuse to send. **読めた上で0以下のときだけ止める。**

    読めなかったときに止めると、応答の形が変わった日に送信が全部止まる
    (「静かなゼロ件」の別の顔)。読めないことは報告に出し、送信自体は他の関門に
    委ねる -- 枠が本当に無ければ媒体が断るので、取り消せない事故にはならない。
    """
    return reading.exhausted()
