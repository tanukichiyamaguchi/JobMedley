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
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

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


def is_success(endpoint: Endpoint, status: int, body: Mapping[str, object] | None = None) -> bool:
    """Whether this response means success **for this endpoint**.

    ステータスが成功の集合に入っていること **かつ** 本文にエラーが無いこと。
    片方だけでは足りない -- 詳細は :func:`graphql_errors` の docstring にある。
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
    return not graphql_errors(body)


def describe_status(
    endpoint: Endpoint, status: int, body: Mapping[str, object] | None = None
) -> str:
    """A human-readable verdict, for logs and the run report."""
    codes = graphql_errors(body)
    try:
        verdict = "成功" if is_success(endpoint, status, body) else "失敗"
    except (ConfigError, UnresolvedCoordinateError):
        # 診断文字列を作るだけの関数なので、座標未確定でも例外にせず事実を書く。
        # 判定そのものが必要な経路は is_success を直接呼び、そこでは止まる。
        verdict = "判定不能(成功ステータスが未確定)"
    # **200 なのに失敗、を必ず言葉にする。** ここを黙ると、ログを読んだ人間が
    # 「200 が並んでいるから送れている」と誤読する。
    detail = f" (応答本文のエラー: {', '.join(codes)})" if codes else ""
    return f"{endpoint.id}: HTTP {status} -> {verdict}{detail}"
