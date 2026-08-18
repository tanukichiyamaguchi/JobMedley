"""配信されている JavaScript から、GraphQL の操作の形を読む。純粋。

**なぜこの道に切り替えたのか (2026-08-18)。**

段階3はここまで ``capture-open`` -- 遮断を武装したまま送信ボタンを押して、
中断された通信から送信APIを観測する -- で進めてきた。仕様どおりの方法であり、
実際に前進もした (実測6回目で送信フォームまで開いた)。しかしこの道は
**生きたSPAと格闘する道** である。ツアー案内、読み込み表示、画面下部の固定バー、
サイドカバー、無効化されたボタン。UIの癖が1つ現れるたびに実行を1回消費する。
6回目の観測には ``button.c-button--disabled`` が出ていた -- 送信ボタンは求人を
選ぶまで押せない作りの可能性が高く、この道はまだ数往復を要する。

**そして、その往復は必要ではなかった。**

5回目・6回目で送信APIのURLの形はすでに分かっている。

    https://customers.job-medley.com/api/customers/graphql/<操作名>

足りないのは **送信 mutation の名前と変数の形だけ** である。そしてそれは、
媒体が配信している JavaScript の中に **文字列として入っている**。GraphQL の
クライアントは操作をどこかに持っていなければ送れないからである。

配信ファイルの取得は **GET** である。GET には副作用が無い (:mod:`recon.gate` の
``SAFE_METHODS`` がそう定めている)。つまりこの道では、**送信は原理的に起こりえない** --
ボタンを押さないからではなく、押す操作そのものが存在しないからである。

本モジュールはその解析だけを持つ。ブラウザには一切触れない (13.4)。

**印字してよいもの。** ここが扱うのは媒体のコードであってページの文言ではないが、
だからといって中身を実行ログへ流してよいことにはならない。取り出して報告するのは
**操作の種別・名前・変数の型** だけ、つまり段階3の成果物そのものに限る (13.2)。
原文は決して印字しない。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

#: ``<script src="...">`` の src。属性の順序も引用符も媒体依存なので緩く採る。
_SCRIPT_SRC = re.compile(
    r"""<script[^>]*\ssrc\s*=\s*(?P<q>["'])(?P<src>[^"']+)(?P=q)""",
    re.IGNORECASE,
)

#: GraphQL の操作定義。素の SDL がバンドルに文字列として残っている形を拾う。
#:
#: 変数宣言 ``($a: T!, $b: U)`` は **括弧の中に括弧が来ない** 前提で採る。
#: GraphQL の変数宣言に入れ子の括弧が現れるのは既定値がオブジェクトのときだけで、
#: そのときは採り損ねる -- **採り損ねる側に倒す** (推測で埋めないため)。
_OPERATION = re.compile(
    r"\b(?P<kind>query|mutation|subscription)\s+"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*"
    r"(?:\((?P<vars>[^()]*)\))?\s*[{@]"
)

#: 変数1つ分の宣言 ``$name: Type!``。
_VARIABLE = re.compile(r"\$(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*(?P<type>[A-Za-z0-9_!\[\]]+)")

#: 操作名が送信を名乗っている手掛かり。**順位付けにのみ使う。除外には使わない。**
#: ``MemberOnScoutProfileModalOfDesktop`` のように、読み取りの名前にも scout は
#: 入る。だから名前だけで送信と断ずることはできず、``mutation`` であることと
#: 併せて初めて意味を持つ。
SEND_NAME_HINTS: tuple[str, ...] = ("scout", "send", "offer", "message", "create", "submit")


@dataclass(frozen=True)
class GraphQLOperation:
    """One operation definition found in the served JavaScript.

    保持するのは **形だけ** である。本文 (選択セット) は持たない -- 段階3が要る
    のは「どの操作名でどんな変数を送るか」であって、本文はサーバが持っている。
    """

    kind: str
    name: str
    #: ``(変数名, 型)`` の並び。宣言が無ければ空。
    variables: tuple[tuple[str, str], ...] = ()

    @property
    def is_mutation(self) -> bool:
        return self.kind == "mutation"

    def looks_like_send(self) -> bool:
        """**名前が送信を名乗り、かつ状態変更であること。**

        名前だけでは足りない (読み取りの名前にも ``Scout`` は入る)。
        ``mutation`` だけでも足りない (既読にする等の無関係な更新がある)。
        """
        lowered = self.name.lower()
        return self.is_mutation and any(hint in lowered for hint in SEND_NAME_HINTS)

    def signature(self) -> str:
        """``mutation Name($a: T!, $b: U)`` -- 報告に出してよい形だけ。"""
        if not self.variables:
            return f"{self.kind} {self.name}"
        inside = ", ".join(f"${name}: {type_}" for name, type_ in self.variables)
        return f"{self.kind} {self.name}({inside})"


def script_urls(html: str, base_url: str) -> tuple[str, ...]:
    """``<script src>`` を絶対URLにして、文書順・重複なしで返す。**Pure.**

    相対参照の解決はここで行う。``//`` 始まりはスキーム相対、``/`` 始まりは
    オリジン相対、それ以外はディレクトリ相対である。
    """
    scheme, origin, directory = _split(base_url)
    found: list[str] = []
    for match in _SCRIPT_SRC.finditer(html):
        src = match.group("src").strip()
        if not src or src.startswith("data:"):
            continue
        if src.startswith("//"):
            resolved = f"{scheme}:{src}"
        elif src.startswith(("http://", "https://")):
            resolved = src
        elif src.startswith("/"):
            resolved = origin + src
        else:
            resolved = directory + src
        if resolved not in found:
            found.append(resolved)
    return tuple(found)


def _split(url: str) -> tuple[str, str, str]:
    """``(scheme, origin, directory)``。**Pure.** 標準ライブラリを使わない理由は無いが、
    ここは1行の話なので依存を増やさない。"""
    scheme, _, rest = url.partition("://")
    if not rest:
        return "https", url, url
    host, _, path = rest.partition("/")
    origin = f"{scheme}://{host}"
    directory = origin + "/" + path.rsplit("/", 1)[0] + "/" if "/" in path else origin + "/"
    return scheme, origin, directory


def operations_in(source: str) -> tuple[GraphQLOperation, ...]:
    """Operation definitions found in one file. **Pure. 原文は返さない。**

    同じ操作が複数回現れる (分割・再出力) ので重複は畳む。畳む鍵は
    ``(種別, 名前, 変数)`` -- 名前だけで畳むと、同名で変数の違う定義が消える。
    """
    found: list[GraphQLOperation] = []
    seen: set[tuple[str, str, tuple[tuple[str, str], ...]]] = set()
    for match in _OPERATION.finditer(source):
        variables = tuple(
            (var.group("name"), var.group("type"))
            for var in _VARIABLE.finditer(match.group("vars") or "")
        )
        key = (match.group("kind"), match.group("name"), variables)
        if key in seen:
            continue
        seen.add(key)
        found.append(GraphQLOperation(kind=key[0], name=key[1], variables=variables))
    return tuple(found)


def rank_send_operations(
    operations: Iterable[GraphQLOperation],
) -> tuple[GraphQLOperation, ...]:
    """Operations most likely to be the scout send, best first. **Pure.**

    並びは (送信を名乗る mutation, その他の mutation, 読み取り) の順、同順位内は
    名前順。**落とさない** -- 段階3では送信操作の名前が未知なので、絞り込みは
    順位付けまでにとどめる (:mod:`recon.gate` の docstring と同じ理由)。
    """

    def key(operation: GraphQLOperation) -> tuple[int, int, str]:
        return (not operation.looks_like_send(), not operation.is_mutation, operation.name)

    return tuple(sorted(operations, key=key))


def merge_operations(
    per_file: Sequence[Sequence[GraphQLOperation]],
) -> tuple[GraphQLOperation, ...]:
    """Fold several files' findings into one list, keeping document order. **Pure.**"""
    found: list[GraphQLOperation] = []
    seen: set[tuple[str, str, tuple[tuple[str, str], ...]]] = set()
    for operations in per_file:
        for operation in operations:
            key = (operation.kind, operation.name, operation.variables)
            if key not in seen:
                seen.add(key)
                found.append(operation)
    return tuple(found)


def send_url_pattern(origin: str, operation: GraphQLOperation) -> str:
    """The send endpoint for one operation. **Pure.**

    形は実測5回目・6回目で確定している (``/api/customers/graphql/<操作名>``)。
    **観測した形に観測した名前を入れるだけ** で、推測は含まない。
    """
    return f"{origin.rstrip('/')}/api/customers/graphql/{operation.name}"
