"""The send call.

9.4 の不変条件をここで作る:

> 参照実装では分析基盤を後から入れたため、それ以前の送信は送信枠が記録されて
> おらず「内訳不明」が積み上がりました。**送信枠は後から絶対に復元できません。**

したがって :class:`SendResult` は ``endpoint_id`` と ``slot`` を必ず持って返る。
失敗時も持って返る -- 失敗の内訳も枠ごとに見たいから。

12.5: **この関数はリトライしない。** 意図的である。送信APIへ「親切にリトライを
足す」と二重送信事故に直結する。冪等キーは呼び出し前に永続化済みで (9.2)、
失敗は次回実行に委ねる。
"""

from __future__ import annotations

from jobmedley_scout.api.client import JobMedleyApiClient
from jobmedley_scout.api.endpoints import Endpoint
from jobmedley_scout.api.payloads import build_send_payload
from jobmedley_scout.config.placeholders import require
from jobmedley_scout.errors import ConfigError
from jobmedley_scout.models.message import AssembledMessage
from jobmedley_scout.models.send_record import ReservedSend, SendResult

URL_PLACEHOLDER_CANDIDATE_ID = "{{CANDIDATE_ID}}"


def build_url(pattern: str, candidate_id: str) -> str:
    """Substitute the candidate into a URL pattern.

    偵察が記録した実URLの、候補者IDに当たる部分を ``{{CANDIDATE_ID}}`` に
    書き換えて座標に入れる運用を想定している。
    """
    return pattern.replace(URL_PLACEHOLDER_CANDIDATE_ID, candidate_id)


def send_message(
    client: JobMedleyApiClient,
    endpoint: Endpoint,
    reserved: ReservedSend,
    message: AssembledMessage,
    *,
    payload_template: object,
    platform_candidate_id: str | None = None,
    followup_days: int | None = None,
) -> SendResult:
    """Send one message. Never retries. Always reports slot and endpoint.

    ``platform_candidate_id`` は、画面に出ているIDと送信APIが要求するIDが違う
    場合 (6.8 の新旧サブシステム分断) に、橋渡しで得た媒体側IDを渡すための引数。
    同じなら ``None`` でよく、予約済みレコードのIDが使われる。
    """
    url_pattern = require(endpoint.url_pattern, used_by=f"api.send.send_message({endpoint.id})")
    if url_pattern is None:
        raise ConfigError(
            f"送信エンドポイント '{endpoint.id}' のURLが null です。"
            f"この枠は存在しないので、送信枠の選択が誤っています。"
        )

    target_id = platform_candidate_id or reserved.candidate_id
    url = build_url(url_pattern, target_id)

    payload = build_send_payload(
        payload_template,  # type: ignore[arg-type]  # Coord[str|None]; require は内部で行う
        candidate_id=target_id,
        subject=message.subject,
        body=message.body,
        followup_days=followup_days,
        used_by=f"api.send.send_message({endpoint.id})",
    )

    # 例外はあえて捕まえない。
    # - PermanentAuthError は上位へ伝播させる必要がある (6.6: 握りつぶすと
    #   CIが緑のまま送信0件になる)。
    # - それ以外の伝送エラーも「確定失敗」とはみなせない。送信が届いたかどうかが
    #   不明なので、呼び出し側は状態を SENDING のまま残し、次回 **同じ冪等キー** で
    #   再送する (9.2)。ここで failed に落とすと新しいキーが発行され、前回が実は
    #   成功していた場合に二重送信になる。
    outcome = client.call(
        endpoint, url=url, json_body=payload, idempotency_key=reserved.idempotency_key
    )
    return SendResult(
        candidate_id=reserved.candidate_id,
        endpoint_id=endpoint.id,
        slot=endpoint.slot,
        succeeded=outcome.succeeded,
        http_status=outcome.status,
        platform_message_id=_extract_message_id(outcome.json_body()),
        failure_reason=None if outcome.succeeded else f"HTTP {outcome.status}",
        idempotency_key=reserved.idempotency_key,
    )


def _extract_message_id(body: object) -> str | None:
    """Best-effort extraction of the platform's message id.

    キー名は未確定 (段階3の偵察で分かる)。**推測で複数のキーを試して当たった
    ものを採用する** ような書き方はしない -- 当たってしまうと、それが正しいと
    誤認したまま運用に入る。素直に取れなければ None を返し、レポートには
    「媒体メッセージID未取得」と出す。
    """
    if not isinstance(body, dict):
        return None
    value = body.get("id")
    return str(value) if isinstance(value, str | int) else None
