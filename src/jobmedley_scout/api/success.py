"""Success determination -- the ONE place an HTTP status is judged.

6.2 の事故:

> **成功ステータスはエンドポイントごとに違います。** 参照実装では通常送信が200、
> プラチナ送信とピックアップ送信が201でした。200のみを成功とみなす実装では、
> 成功しているのに失敗扱いになります。**成功判定は1箇所に集約してください。**

「1箇所に集約」を人間の規律ではなくテストで守るため、
``tests/guardrails/test_source_conventions.py`` が、本モジュール以外での
ステータスコードの数値比較 (``status == 200`` / ``status_code in (...)`` など) を
ソース走査で禁止している。

新しいエンドポイントを足すときは、成功ステータスを **実測してから** 座標に書くこと。
推測で 200 を書くと、この事故をそのまま再現する。

**失敗を運んでくる経路は3本ある。** 実測31回目 (follow-send) で送信の mutation 文書
そのものを観測して、3本目が在ることが分かった::

    mutation SendSingleScout($input: MessageScoutSendInput!) {
      result: messageScoutSend(input: $input) {
        scoutedMemberId
        errorMessage        <- **これ**
        __typename
      }
    }

1. HTTPステータス          -- 集合に入っているか (6.2)
2. 本文の ``errors`` 配列   -- GraphQL の伝送・実行時エラー (:func:`graphql_errors`)
3. **本文の ``errorMessage`` 欄** -- 媒体の業務エラー (:func:`payload_error_paths`)

3本目は 1 も 2 も通り抜ける。**HTTP 200・errors 無し・それでも送れていない** 形が
ありうる。媒体がわざわざ選択集合に入れている以上、埋まる場面が在るということである。
見落とすと送信済みとして記録され、その候補者は二度と対象にならない -- 原則2 の
「静かなゼロ件」が **恒久化** する。だから3本とも見る。

**まだ観測していないのは「どんなときに埋まるか」である。** 欄の存在は確定だが、
失敗応答そのものは1度も見ていない (実送信をしていないから)。よって判定は保守的に
倒す -- **空でない値が入っていたら失敗**。逆向き (埋まっているのに成功とみなす) の
間違いだけは犯さない。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Final

from jobmedley_scout.api.endpoints import Endpoint
from jobmedley_scout.config.placeholders import require
from jobmedley_scout.errors import ConfigError, UnresolvedCoordinateError


def graphql_errors(body: Mapping[str, object] | None) -> tuple[str, ...]:
    """Error *codes* carried in a GraphQL response body. **文言は返さない。**

    **GraphQL は失敗しても HTTP 200 を返す。** 成否は本文の ``errors`` 配列に
    書かれている。媒体のスカウト送信は GraphQL の mutation なので (実測20回目)、
    ステータスコードだけを見る判定は **失敗を成功として数える**。

    数えた瞬間に何が起きるか。送信は「済んだ」ものとして状態に記録され、その
    候補者は二度と対象にならない (重複送信の防止が効いてしまう)。**送っていない
    のに、送ったことになる。** 原則2 の「静かなゼロ件」そのものである。

    返すのは ``extensions.code`` などの **コード** だけで、``message`` は返さない
    -- 媒体のエラー文言には候補者名が混ざりうる (13.2)。コードが無いエラーは
    ``"(コード無し)"`` として数える。**数を落とさない** ことのほうが大事である。
    """
    if not isinstance(body, Mapping):
        return ()
    errors = body.get("errors")
    if not isinstance(errors, Sequence) or isinstance(errors, str | bytes):
        return ()
    found: list[str] = []
    for entry in errors:
        code = ""
        if isinstance(entry, Mapping):
            extensions = entry.get("extensions")
            if isinstance(extensions, Mapping):
                code = str(extensions.get("code") or "")
            if not code:
                code = str(entry.get("code") or "")
        found.append(code or "(コード無し)")
    return tuple(found)


#: 送信 mutation の選択集合に在る、**操作ごとのエラー欄** (2026-08-23 実測31回目)。
#: 名前は観測そのものである。推測で別名 (``error`` / ``message`` 等) を足さない --
#: 足すと、たまたま当たったキーを「正しい」と誤認したまま運用に入る (原則3)。
PAYLOAD_ERROR_FIELD = "errorMessage"

#: ``data`` の下を潜る深さの上限。無限に深い応答で暴走しないための安全弁であって、
#: 媒体の事実ではない。観測した送信の応答は ``data.result.errorMessage`` の深さ2。
MAX_PAYLOAD_DEPTH = 6


def _is_filled(value: object) -> bool:
    """Whether this ``errorMessage`` slot actually carries a complaint.

    ``null`` と空文字は「エラー無し」。それ以外は **中身を見ずに** エラーとみなす。
    形 (文字列か、配列か、オブジェクトか) はまだ観測していないので、形で分岐する
    判定を書かない -- 想定外の形が来たときに黙って成功へ倒れる。
    """
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping | Sequence | bytes):
        return bool(value)
    return True


def _walk_payload(node: object, path: str, found: list[str], depth: int) -> None:
    if depth <= 0:
        return
    if isinstance(node, Mapping):
        for key, value in node.items():
            child = f"{path}.{key}"
            if key == PAYLOAD_ERROR_FIELD:
                if _is_filled(value):
                    found.append(child)
                continue
            _walk_payload(value, child, found, depth - 1)
        return
    if isinstance(node, Sequence) and not isinstance(node, str | bytes):
        for index, item in enumerate(node):
            _walk_payload(item, f"{path}[{index}]", found, depth - 1)


def payload_error_paths(body: Mapping[str, object] | None) -> tuple[str, ...]:
    """Key paths under ``data`` where a non-empty ``errorMessage`` sits.

    **返すのはキーパスだけで、文言は返さない** (13.2)。媒体のエラー文言には
    候補者名や会員番号が混ざりうるので、Actions のログへ出せない。
    ``graphql_errors`` が ``message`` を捨ててコードだけ返すのと同じ理由である。

    ``errors`` 配列は見ない -- あちらは :func:`graphql_errors` の担当で、二重に
    数えると「失敗の内訳」が壊れる。ここが見るのは ``data`` の下だけである。
    """
    if not isinstance(body, Mapping):
        return ()
    found: list[str] = []
    _walk_payload(body.get("data"), "data", found, MAX_PAYLOAD_DEPTH)
    return tuple(found)


#: 「媒体が受け付けた」とだけ言える範囲。**エンドポイント固有の成功判定ではない。**
ACCEPTED_STATUSES: Final[frozenset[int]] = frozenset(range(200, 300))


def was_accepted(status: int) -> bool:
    """Whether the platform accepted the request at all. **送信の可否には使わない。**

    6.2 の規律は「成功ステータスはエンドポイントごとに違うので判定を1箇所に
    集める」ことである。集める先がここなので、**エンドポイントの手が届かない
    ところで 2xx を数値で書かない** ために、この名前を置いてある。

    使ってよいのは偵察だけである。偵察には ``Endpoint`` が無い -- 画面自身が
    飛ばした通信を横から聴いているだけで、座標の成功集合を持っていない。そこで
    知りたいのも「この本文は通る本文か」であって「送信は成功したか」ではない。

    **送信の判定に使ってはならない。** 送信は3本立て (ステータス・``errors``・
    ``errorMessage``) で見る必要があり、ここはその1本目しか見ていない。
    送信路は必ず :func:`is_success` を通すこと。
    """
    return status in ACCEPTED_STATUSES


def is_success(endpoint: Endpoint, status: int, body: Mapping[str, object] | None = None) -> bool:
    """Whether this response means success **for this endpoint**.

    3本の経路すべてを通ったものだけが成功である。ステータスが集合に入っていること、
    ``errors`` 配列が空であること、そして **``errorMessage`` 欄が埋まっていない**
    こと。1本でも欠かすと、失敗が成功として記録される (モジュール冒頭の3本立て)。
    """
    statuses = require(endpoint.success_statuses, used_by=f"api.success.is_success({endpoint.id})")
    if statuses is None:
        raise ConfigError(
            f"エンドポイント '{endpoint.id}' の成功ステータスが null です。"
            f"この枠が存在しないなら、そもそも呼び出さないでください。"
        )
    if status not in statuses:
        return False
    # **本文が読めなかったことを「エラー無し」と読み替えない…わけではない。**
    #
    # ここは意図的に「本文が無ければステータスの判定に従う」にしてある。
    # GraphQL 以外のエンドポイント (本文が空の 204 等) が同じ関数を通るからで、
    # そこで「本文が読めない = 失敗」にすると正常な応答を全部落とす。
    # GraphQL の失敗は **errors が在る** ことで示されるので、在るときだけ見る。
    # errorMessage 欄も同じ扱い -- 欄が無い応答は無言、在って埋まっていれば失敗。
    return not graphql_errors(body) and not payload_error_paths(body)


def describe_status(
    endpoint: Endpoint, status: int, body: Mapping[str, object] | None = None
) -> str:
    """A human-readable verdict, for logs and the run report."""
    codes = graphql_errors(body)
    paths = payload_error_paths(body)
    try:
        verdict = "成功" if is_success(endpoint, status, body) else "失敗"
    except (ConfigError, UnresolvedCoordinateError):
        # 診断文字列を作るだけの関数なので、座標未確定でも例外にせず事実を書く。
        # 判定そのものが必要な経路は is_success を直接呼び、そこでは止まる。
        verdict = "判定不能(成功ステータスが未確定)"
    # **200 なのに失敗、を必ず言葉にする。** ここを黙ると、ログを読んだ人間が
    # 「200 が並んでいるから送れている」と誤読する。
    details: list[str] = []
    if codes:
        details.append(f"応答本文のエラー: {', '.join(codes)}")
    if paths:
        details.append(f"エラー欄に文言あり: {', '.join(paths)}")
    detail = f" ({' / '.join(details)})" if details else ""
    return f"{endpoint.id}: HTTP {status} -> {verdict}{detail}"


#: 本文の種別を言うときに読む見出し。**値は読まない。**
CONTENT_TYPE_HEADER = "content-type"

#: 本文の頭を見て「HTMLらしい」と言うための印。**中身は読まない。**
HTML_PREFIXES: Final[tuple[str, ...]] = ("<!doctype", "<html", "<?xml")


def describe_body_shape(body_text: str, headers: Mapping[str, str] | None = None) -> str:
    """What kind of thing the body is, **without quoting any of it** (13.2).

    実測39回目、レジュメが読めない理由が「HTTP 200 / 応答本文がオブジェクトでは
    ありません」までは絞れたが、**そこで止まった**。オブジェクトでないことは
    分かっても、HTMLなのか、空なのか、JSONの配列なのかが分からない。次の手が
    決まらない報告は、原則2 の言う「静かなゼロ」の一段浅い版である。

    ここが返すのは種別と長さと ``content-type`` だけで、本文は1文字も出さない。
    ログイン画面のHTMLが返っていれば「HTMLらしい」と分かり、それだけで
    セッションの問題だと決まる -- 中身を読む必要は無い。
    """
    parts: list[str] = []
    kind = (headers or {}).get(CONTENT_TYPE_HEADER) or (headers or {}).get("Content-Type") or ""
    if kind:
        parts.append(f"content-type: {kind.split(';', 1)[0].strip()}")
    parts.append(f"長さ {len(body_text)} 字")

    stripped = body_text.strip()
    if not stripped:
        parts.append("本文が空です")
        return " / ".join(parts)
    lowered = stripped[:16].lower()
    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        if lowered.startswith(HTML_PREFIXES):
            # **これが分かれば十分。** 画面が返っているならセッションか経路の問題。
            parts.append("JSONではなくHTMLらしい (ログイン画面等の可能性)")
        else:
            parts.append("JSONとして読めません")
        return " / ".join(parts)

    if isinstance(parsed, list):
        parts.append(f"JSONの配列 ({len(parsed)} 要素)")
    elif parsed is None:
        parts.append("JSONの null")
    else:
        parts.append(f"JSONだがオブジェクトではない ({type(parsed).__name__})")
    return " / ".join(parts)


def describe_failure(
    endpoint: Endpoint, status: int, body: Mapping[str, object] | None = None
) -> str | None:
    """Why this response is *not* a success. ``None`` when it is one.

    送信記録の ``failure_reason`` はここから作る。以前は ``f"HTTP {status}"`` を
    そのまま入れていたが、**GraphQL の失敗はステータスが 200 なので「HTTP 200」と
    しか書かれない記録が残る**。読んだ人間には成功と区別がつかず、原因も分からない。
    レポートが起きたことと食い違うのは、それ自体が事故である。

    ここでも **値は1つも含めない** (13.2)。出すのはエラーコードとキーパスだけで、
    媒体の文言は出さない -- 候補者名が混ざりうるため。文言そのものは媒体の画面で
    確認すること。
    """
    if is_success(endpoint, status, body):
        return None
    parts: list[str] = []
    codes = graphql_errors(body)
    if codes:
        parts.append(f"応答本文の errors: {', '.join(codes)}")
    paths = payload_error_paths(body)
    if paths:
        parts.append(f"エラー欄に文言あり (文言は伏せています): {', '.join(paths)}")
    if not parts:
        parts.append("成功とみなすステータスの集合に入っていません")
    return f"HTTP {status} / " + " / ".join(parts)
