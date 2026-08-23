"""応答が届くたびに **形だけ** を取り、本文は捨てる聴き手。

:mod:`recon.observe_api` のために書いたが、``observe-resume`` も同じものを要る
ので切り出した。**「値を出さずに形だけ出す」という問題は同じ** なので、
新しく書き起こさない (:mod:`recon.api_shape` と同じ判断)。

**値は1文字も残らない。** 応答が来たその場でキーパスに落として捨てる。生の本文を
持ち回ると、例外のメッセージや保存経路から個人データが漏れる余地が増える (13.2)。
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from jobmedley_scout.recon.api_shape import (
    ObservedCall,
    describe_response,
    operation_name,
    scan_text,
)
from jobmedley_scout.recon.gate import is_own_origin
from jobmedley_scout.recon.open_structure import redact_url
from jobmedley_scout.recon.payload_shape import shape_of
from jobmedley_scout.recon.resume_keys import KeyPath

#: 聴く対象。**媒体自身のオリジンなら全部。**
#:
#: 判定はホスト名で行うこと。実測23回目、``_MEDIA_HOST not in url`` で見ていた
#: せいで計測ビーコンが報告に紛れ込んだ -- ビーコンは送信元ページのURLを ``dl=``
#: に載せるので、URLの文字列の中には媒体のホスト名がそのまま入る。
MEDIA_HOST = "job-medley.com"

#: 本文を読んでよい応答の種別。画像や動画は読まない (読む意味が無く、重い)。
READABLE_TYPES: tuple[str, ...] = ("json", "html", "javascript", "text/plain", "xml")


@dataclass
class ResponseShapeListener:
    """Collects response shapes as they arrive. **本文は溜めない。**

    応答が来たその場でキーの形に落として捨てる。生の本文を持ち回ると、例外の
    メッセージや保存経路から個人データが漏れる余地が増える (13.2)。

    **``browser.capture.ResponseBuffer`` を使わない理由。** あちらも
    ``page.on("response")`` を張る器で、返信検知 (10.4) のために書かれている。
    使わないのは2点で違うからである:

    * あちらは本文を **溜める** (40万字で切り詰めて窓が閉じるまで保持する)。
      返信の照合には本文が要るが、こちらは形しか要らない。**要らないものを
      持たない** ほうが、漏れる経路が少ない
    * あちらの ``measurement_window()`` は入場時と退場時に全部捨てる。
      10.4 の事故 (ブートストラップの応答が本番の照合に混ざった) を構文で
      塞ぐ設計で、正しいが、こちらの用途 (遷移中の全応答を残らず聴く) とは
      窓の切り方が合わない
    """

    calls: list[ObservedCall] = field(default_factory=list)
    #: 媒体のオリジンではなかったので聴かなかった応答の数。
    #:
    #: **必ず報告に出す。** 実測21回目、聴けた応答は0件だったが「いくつ無視したか」
    #: を出していなかったので、「本当に応答が無かった」のか「絞り込みが狭すぎた」
    #: のかが報告から決められなかった。数を出していれば即座に分かった --
    #: 自分で作った静かなゼロ件である。
    ignored: int = 0
    #: 読める種別ではなかったので本文を見なかった応答の数 (画像等)。
    skipped_binary: int = 0

    def hear(self, response: Any) -> None:
        url = ""
        with suppress(Exception):
            url = str(response.url)
        if not url:
            return
        if not is_own_origin(url, MEDIA_HOST):
            # **ホスト名で判定する。URL全体の部分一致では駄目。** 実測23回目、
            # 計測ビーコンが報告に紛れ込んだ -- ビーコンは「どのページから
            # 送ったか」を ``dl=`` に載せるので、URLの文字列の中には媒体の
            # ホスト名がそのまま入っている (:func:`recon.gate.is_own_origin`)。
            self.ignored += 1
            return

        request_body: str | None = None
        method = "GET"
        with suppress(Exception):
            request_body = response.request.post_data
            method = str(response.request.method)

        # **要求の形を、応答より先に取る。** 呼ぶために要るのはこちらである。
        # 値は出さない -- 応答と同じ ``describe_response`` を通す (13.2)。
        request_keys: tuple[KeyPath, ...] = ()
        request_reason = ""
        request_dropped = 0
        request_template = ""
        if request_body and method not in ("GET", "HEAD"):
            request_keys, request_reason, request_dropped = describe_response(request_body)
            # **GraphQL は封筒ごと残す。** キーパスだけでは呼べない -- 問い合わせ文
            # (``query``) の *中身* が要る。GraphQL は query の無いリクエストを
            # 受け付けないからである。
            #
            # 送信payloadで一度この穴を開けている。あのときは「長いから」という
            # 理由で query を落としており、貼っても送れない雛形になっていた
            # (:mod:`recon.payload_shape` の冒頭)。
            #
            # ``sentinel=""`` で呼ぶと、目印の名指しをせずに **variables の値だけ**
            # を種別へ伏せる。``query`` と ``operationName`` はスキーマの語彙で
            # あって個人データではない (13.2)。
            if "graphql" in url.lower():
                shape = shape_of(request_body, None, "")
                request_template = shape.template if shape else ""

        content_type = ""
        with suppress(Exception):
            content_type = str(response.headers.get("content-type", ""))
        short_type = content_type.split(";", 1)[0].strip()

        if content_type and not any(kind in content_type.lower() for kind in READABLE_TYPES):
            # 画像・動画・フォント。読む意味が無く、重い。**数だけ残す。**
            self.skipped_binary += 1
            return

        body: str | None = None
        reason = ""
        try:
            body = response.text()
        except Exception:  # noqa: BLE001 -- 生のメッセージは出さない (13.2)
            reason = "応答本文を取り出せませんでした"

        keys: tuple[Any, ...] = ()
        dropped = 0
        uuid_like = 0
        mentions = 0
        if not reason:
            keys, reason, dropped = describe_response(body)
            if reason:
                # JSON では無かった。**値は見ずに、形の数だけ数える。**
                uuid_like, mentions = scan_text(body)
        # **ここで本文を捨てる。** 以降どこにも残らない。
        body = None

        redacted = redact_url(url)
        self.calls.append(
            ObservedCall(
                # **URLからも名前を付ける。** この媒体の読み取りは REST なので、
                # GraphQL の封筒を探すだけでは19本すべてが無名になる (実測23回目)。
                operation=operation_name(request_body, redacted),
                redacted_url=redacted,
                method=method,
                keys=keys,
                unread_reason=reason,
                dropped_keys=dropped,
                content_type=short_type,
                uuid_like=uuid_like,
                send_key_mentions=mentions,
                request_keys=request_keys,
                request_unread_reason=request_reason,
                request_dropped_keys=request_dropped,
                request_template=request_template,
            )
        )


__all__ = ["MEDIA_HOST", "READABLE_TYPES", "ResponseShapeListener"]
