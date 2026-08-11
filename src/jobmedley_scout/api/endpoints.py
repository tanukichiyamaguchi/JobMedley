"""The endpoint registry.

6.1 で特定すべきエンドポイントの種類:
保存検索の条件取得 / 候補者一覧 (ページング付き) / レジュメ / 送信前チェック /
送信 (**枠ごとに別エンドポイントの可能性が高い**) / 送信枠の残数照会。

URLも成功ステータスも payload 形状も、すべて段階3の偵察で確定する **座標** である。
確定するまでは :class:`Unresolved` のままなので、送信経路は型検査と
``assert_ready_for`` の両方で止まる。
"""

from __future__ import annotations

from dataclasses import dataclass

from jobmedley_scout.config.placeholders import Coord
from jobmedley_scout.config.site_coordinates import SiteCoordinates
from jobmedley_scout.models.send_record import SendSlot

# エンドポイントID。送信結果に必ず載せる (9.4) ので、安定した文字列であること。
SEND_PAID = "send.paid"
SEND_FREE = "send.free"
CANDIDATE_LIST = "candidate_list"
RESUME = "resume"
PRECHECK = "precheck"
QUOTA = "quota"

#: 送信枠が確定していないときに使う値。**一級市民として扱う** (9.4)。
#: 「不明」を有料/無料のどちらかへ既定で寄せると、恒等式が壊れて嘘の内訳が出る。
UNKNOWN_ENDPOINT = "unknown"


@dataclass(frozen=True)
class Endpoint:
    """One platform endpoint, with its coordinates still possibly unconfirmed."""

    id: str
    method: str
    url_pattern: Coord[str | None]
    #: **成功とみなすステータスはエンドポイントごとに違う** (6.2)。参照実装では
    #: 通常送信が200、プラチナ送信とピックアップ送信が201だった。200のみを成功と
    #: みなす実装では、成功しているのに失敗扱いになる。
    success_statuses: Coord[frozenset[int] | None]
    slot: SendSlot
    side_effectful: bool


def build_endpoints(coordinates: SiteCoordinates) -> dict[str, Endpoint]:
    """Assemble the registry from the coordinate file."""
    return {
        SEND_PAID: Endpoint(
            id=SEND_PAID,
            method="POST",
            url_pattern=coordinates.url("api.send.paid.url_pattern"),
            success_statuses=coordinates.status_set("api.send.paid.success_statuses"),
            slot=SendSlot.PAID,
            side_effectful=True,
        ),
        SEND_FREE: Endpoint(
            id=SEND_FREE,
            method="POST",
            url_pattern=coordinates.optional_url("api.send.free.url_pattern"),
            success_statuses=coordinates.optional_status_set("api.send.free.success_statuses"),
            slot=SendSlot.FREE,
            side_effectful=True,
        ),
        CANDIDATE_LIST: Endpoint(
            id=CANDIDATE_LIST,
            method="GET",
            url_pattern=coordinates.url("api.candidate_list.url_pattern"),
            # 読み取り系の成功判定は座標化していない。2xx 全般でよく、枠ごとの
            # 差異が問題になるのは副作用のある送信だけだから (6.2)。
            success_statuses=frozenset(range(200, 300)),
            slot=SendSlot.UNKNOWN,
            side_effectful=False,
        ),
        RESUME: Endpoint(
            id=RESUME,
            method="GET",
            url_pattern=coordinates.url("api.resume.url_pattern"),
            success_statuses=frozenset(range(200, 300)),
            slot=SendSlot.UNKNOWN,
            side_effectful=False,
        ),
        PRECHECK: Endpoint(
            id=PRECHECK,
            method="GET",
            url_pattern=coordinates.optional_url("api.precheck.url_pattern"),
            success_statuses=frozenset(range(200, 300)),
            slot=SendSlot.UNKNOWN,
            side_effectful=False,
        ),
        QUOTA: Endpoint(
            id=QUOTA,
            method="GET",
            url_pattern=coordinates.optional_url("api.quota.url_pattern"),
            success_statuses=frozenset(range(200, 300)),
            slot=SendSlot.UNKNOWN,
            side_effectful=False,
        ),
    }


def send_endpoint_for(endpoints: dict[str, Endpoint], slot: SendSlot) -> Endpoint:
    """The send endpoint for a slot.

    6.3: 全経路を解明する必要はない。参照実装では通常送信のワンタイムトークン生成
    エンドポイントが特定できなかったが、**別の送信枠はトークン不要で全会員種別に
    送れる** と分かったため、全送信をその枠に流して未解明の経路を捨てた。
    「目的を満たす最小の経路」が1本見つかれば十分である。
    """
    if slot is SendSlot.PAID:
        return endpoints[SEND_PAID]
    if slot is SendSlot.FREE:
        return endpoints[SEND_FREE]
    raise ValueError(
        f"送信枠 {slot} に対応する送信エンドポイントがありません。"
        f"UNKNOWN 枠から送信してはいけません (9.4: 枠は後から復元できない)。"
    )
