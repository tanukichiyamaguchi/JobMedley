"""段階4-1b: **スキーマに直接尋ねる。** 送信は起こらない。

**なぜ要るのか。** 段階4-1 は「配信ファイルを読めば dryRun の有無が分かる」と
考えていた。実測27回目でその前提が誤りだと分かった。配信ファイルに入っているのは
**操作の定義 (クエリ文書) であって、スキーマではない**::

    mutation SendSingleScout($input: MessageScoutSendInput!)

文書に出てくるのは変数の *型名* だけで、その型が **どんなフィールドを持つか** は
書かれていない。読めるのは「何という型か」までである。

GraphQL には、それを尋ねる **標準の問い合わせ** がある。``__type(name:)`` は
``query`` であって ``mutation`` ではないので、**送信は起こらない**。

**このコマンドが出す通信は、ここに書いてある1本の問い合わせだけである。**
本文はモジュール定数で、呼び出し側から差し替えられない -- 差し替えられる形に
すると、``mutation`` を1行入れるだけで送信になる (13.6)。
:mod:`tests.guardrails` がソース走査でそれを固定している。

**返るのは型の名前と、そのフィールドの名前・型だけである。** 個人データは
原理的に入らない -- スキーマは誰の値でもない (13.2)。

introspection が無効な媒体は多い。**無効だったことも確定した答えである** --
「まだ分からない」ではなく「この経路では分からない」なので、報告はそう述べる。
"""

from __future__ import annotations

import json
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from jobmedley_scout.browser import session_store
from jobmedley_scout.browser.context import browser_context
from jobmedley_scout.config.placeholders import Coord, require
from jobmedley_scout.config.schema import BrowserConfig
from jobmedley_scout.recon.open_structure import redact_url

#: 尋ねる型。**送信の入力型である。** 実測27回目に配信ファイルから読んだ名前。
TARGET_TYPE = "MessageScoutSendInput"

#: 一括送信の入力型。ついでに尋ねる -- 同じ1本では尋ねられないので2本目になる。
BULK_TYPE = "MessageScoutBulkSendInput"

#: **このコマンドが出す唯一の問い合わせ。**
#:
#: ``query`` である。``mutation`` の語はどこにも無く、副作用は起こらない。
#: ``$name`` 以外に変数は無いので、呼び出し側が中身を差し替える余地も無い。
INTROSPECTION_QUERY = """
query ReconInputTypeFields($name: String!) {
  __type(name: $name) {
    name
    kind
    inputFields {
      name
      type { name kind ofType { name kind } }
    }
  }
}
""".strip()

#: dryRun 相当を名乗るフィールド名の断片。**名前で当たりを付けるだけ。**
DRY_RUN_HINTS: tuple[str, ...] = ("dryrun", "dry_run", "preview", "validateonly", "test")


class IntrospectStage(StrEnum):
    """辿り着いた段。**時系列の順に並んでいる。**"""

    NO_SESSION = "no_session"
    NOT_ANSWERED = "not_answered"
    DISABLED = "disabled"
    ANSWERED = "answered"


@dataclass(frozen=True)
class TypeFields:
    """One input type's field names and types. **値は無い。スキーマである。**"""

    type_name: str
    fields: tuple[tuple[str, str], ...] = ()
    #: 尋ねたが型が返らなかった理由 (定型句のみ)。
    reason: str = ""

    def dry_run_candidates(self) -> tuple[str, ...]:
        """Field names that look like a dry-run switch. **Pure.**"""
        return tuple(
            name
            for name, _type in self.fields
            if any(hint in name.lower().replace("_", "") for hint in DRY_RUN_HINTS)
        )

    def render(self) -> str:
        lines = [f"  型: {self.type_name}"]
        if self.reason:
            lines.append(f"    尋ねましたが型は返りませんでした: {self.reason}")
            return "\n".join(lines)
        if not self.fields:
            lines.append("    フィールドが1つもありませんでした。")
            return "\n".join(lines)
        lines.append(f"    フィールド ({len(self.fields)} 個):")
        lines.extend(f"      {name}: {type_name}" for name, type_name in self.fields)
        if found := self.dry_run_candidates():
            lines.append(f"    **dryRun 相当らしいフィールド**: {', '.join(found)}")
        else:
            lines.append("    dryRun 相当らしいフィールドはありません。")
        return "\n".join(lines)


@dataclass(frozen=True)
class IntrospectObservation:
    """The whole run, in the shape the report needs."""

    endpoint: str
    session_present: bool = True
    #: サーバが答えたか。HTTP が通っても introspection が無効なら False。
    answered: bool = False
    #: introspection が **無効だと分かった** か。「答えなかった」とは違う。
    disabled: bool = False
    types: tuple[TypeFields, ...] = ()
    note: str = ""

    def reached(self) -> IntrospectStage:
        """The single stage the run actually reached. **報告はこれだけを見る。**"""
        # **後の工程の条件は、前の工程の条件を含んでいなければならない。**
        #
        # 最初こう書いて即座に落ちた::
        #
        #     (IntrospectStage.DISABLED, not self.disabled)
        #
        # 「無効ではない」は「答えた」より弱い。何も答えず無効でもない状態では
        # NOT_ANSWERED で止まったのに DISABLED が素通りし、単調性の番人が
        # 正常な実行を嘘として例外にした。**鎖に載せてよいのは、前を通らないと
        # 後が始まらない条件だけである** (observe_api.reached の注記と同じ)。
        chain: tuple[tuple[IntrospectStage, bool], ...] = (
            (IntrospectStage.NO_SESSION, self.session_present),
            # 「答えた」か「無効だと分かった」か -- どちらかなら確定している。
            (IntrospectStage.NOT_ANSWERED, self.answered or self.disabled),
            # そのうえで「フィールドが返った」か。
            (IntrospectStage.DISABLED, self.answered),
        )
        stopped: IntrospectStage | None = None
        for stage, passed in chain:
            if not passed and stopped is None:
                stopped = stage
            elif passed and stopped is not None:
                raise ValueError(
                    f"IntrospectObservation の状態が時系列と矛盾しています:"
                    f" {stopped.value} で止まったのに {stage.value} を通過した証拠がある。"
                    " (報告を嘘にしないため停止)"
                )
        return stopped or IntrospectStage.ANSWERED

    def render(self) -> str:
        lines = ["段階4: 送信の入力型をスキーマに尋ねた (**送信は起こしていません**)", ""]
        stage = self.reached()

        if stage is IntrospectStage.NO_SESSION:
            lines.append("  保存セッションがありません。段階1からやり直してください。")
            return "\n".join(lines)
        if stage is IntrospectStage.NOT_ANSWERED:
            lines.append("  **サーバが答えませんでした。**")
            lines.append(f"  {self.note}")
            lines.append("  これは「introspection が無効」とは違います --")
            lines.append("  無効だと分かったわけではないので、まだ確定していません。")
            return "\n".join(lines)
        if stage is IntrospectStage.DISABLED:
            lines.append("  **introspection は無効でした。**")
            lines.append(f"  {self.note}")
            lines.append("")
            lines.append("  これは確定した答えです。「まだ分からない」ではなく")
            lines.append("  **「この経路では分からない」** なので、段階4-2 (dryRun での")
            lines.append("  検証) は使えません。docs/ladder.md の 4-3 (少件数の実送信) へ")
            lines.append("  進んでください。")
            return "\n".join(lines)

        lines.append(f"  尋ねた先: {self.endpoint}")
        lines.append("")
        for entry in self.types:
            lines.append(entry.render())
            lines.append("")
        lines.extend(self._verdict_lines())
        lines.append("")
        lines.append("**このコマンドが出した通信は、query が2本だけです。**")
        lines.append("mutation は1つも出していません -- 送信は起こす操作そのものが")
        lines.append("存在しません。返ったのは型とフィールドの名前だけで、")
        lines.append("誰かの値ではありません (13.2)。")
        return "\n".join(lines)

    def _verdict_lines(self) -> list[str]:
        """**判定を1行で述べる。** 読む人が探さなくてよいように。"""
        answered = [entry for entry in self.types if not entry.reason]
        if not answered:
            return ["  どの型も返りませんでした。"]
        found = [
            f"{entry.type_name}.{name}" for entry in answered for name in entry.dry_run_candidates()
        ]
        if found:
            return [
                f"  **dryRun 相当が在ります**: {', '.join(found)}",
                "  段階4-2 が使えます -- 送らずに応答の形が分かります。",
            ]
        return [
            "  **dryRun 相当はありません。**",
            "  スキーマが答えた以上、これは推測ではなく確定です。",
            "  段階4-2 は使えないので、docs/ladder.md の 4-3 (少件数の実送信) へ",
            "  進んでください。",
        ]


def introspect_send_input(
    config: BrowserConfig,
    credentials_dir: Path,
    api_base_url: Coord[str],
) -> IntrospectObservation:
    """Ask the GraphQL endpoint what the send input type accepts.

    **押さない。開かない。** 認証済みのコンテキストから問い合わせを1本ずつ送る
    だけである。本文は :data:`INTROSPECTION_QUERY` 固定で、``mutation`` の語は
    どこにも無い。
    """
    base = require(api_base_url, used_by="recon.introspect.introspect_send_input")
    endpoint = f"{base.rstrip('/')}/api/customers/graphql/ReconInputTypeFields"

    session = session_store.session_path(credentials_dir)
    if not session.exists():
        return IntrospectObservation(endpoint=endpoint, session_present=False)

    with browser_context(config, storage_state=session) as (context, _page):
        results: list[TypeFields] = []
        disabled = False
        note = ""
        for type_name in (TARGET_TYPE, BULK_TYPE):
            fields, reason, was_disabled = _ask(context, endpoint, type_name)
            results.append(TypeFields(type_name=type_name, fields=fields, reason=reason))
            if was_disabled:
                disabled = True
                note = reason
        answered = any(not entry.reason for entry in results)
        if not answered and not disabled:
            note = results[0].reason if results else "応答を読めませんでした"
        return IntrospectObservation(
            endpoint=redact_url(endpoint),
            answered=answered,
            disabled=disabled and not answered,
            types=tuple(results),
            note=note,
        )


def _ask(
    context: Any, endpoint: str, type_name: str
) -> tuple[tuple[tuple[str, str], ...], str, bool]:
    """Send one introspection query. Returns ``(fields, reason, disabled)``."""
    body: str | None = None
    try:
        response = context.request.post(
            endpoint,
            data={
                "operationName": "ReconInputTypeFields",
                "query": INTROSPECTION_QUERY,
                "variables": {"name": type_name},
            },
        )
        body = response.text()
    except Exception:  # noqa: BLE001 -- 生のメッセージは出さない (13.2)
        return (), "問い合わせを送れませんでした", False
    return parse_type_fields(body, type_name)


def parse_type_fields(
    body: str | None, type_name: str
) -> tuple[tuple[tuple[str, str], ...], str, bool]:
    """Read one introspection response. **Pure.** ``(fields, reason, disabled)``.

    **判定できないものは全て「分からない」側へ倒す。** 「たぶん無効だろう」を
    True にすると、まだ確定していないことを確定として報告することになる。
    """
    if not body:
        return (), "応答が空でした", False
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return (), "JSONとして読めませんでした", False
    if not isinstance(payload, dict):
        return (), "JSONとして読めませんでした", False

    if errors := payload.get("errors"):
        # **文言は出さない。** サーバのメッセージに値が混ざりうる (13.2)。
        # introspection の無効化は決まった言い方が無いので、**分類だけを見る**。
        codes = _error_codes(errors)
        disabled = any("introspect" in code.lower() for code in codes)
        label = ", ".join(codes) if codes else "コードなし"
        return (), f"errors が返りました ({label})", disabled

    data = payload.get("data")
    if not isinstance(data, dict):
        return (), "data がありませんでした", False
    node = data.get("__type")
    if node is None:
        # **型が null で返った。** introspection は動いているが、その名前の型は
        # 無い。無効とは違うので disabled にしない。
        return (), f"{type_name} という型はありませんでした", False
    if not isinstance(node, dict):
        return (), "__type を読めませんでした", False

    raw = node.get("inputFields")
    if not isinstance(raw, list):
        return (), "inputFields がありませんでした", False
    fields: list[tuple[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if name:
            fields.append((name, _type_name(item.get("type"))))
    return tuple(fields), "", False


def _error_codes(errors: object) -> tuple[str, ...]:
    """``extensions.code`` only. **文言は返さない** (13.2)。"""
    if not isinstance(errors, list):
        return ()
    codes: list[str] = []
    for entry in errors:
        if not isinstance(entry, dict):
            continue
        extensions = entry.get("extensions")
        if isinstance(extensions, dict):
            with suppress(Exception):
                if code := extensions.get("code"):
                    codes.append(str(code))
    return tuple(codes)


def _type_name(node: object) -> str:
    """A readable type name from an introspection type node. **Pure.**"""
    if not isinstance(node, dict):
        return "?"
    if name := node.get("name"):
        return str(name)
    kind = str(node.get("kind") or "")
    inner = _type_name(node.get("ofType"))
    if kind == "NON_NULL":
        return f"{inner}!"
    if kind == "LIST":
        return f"[{inner}]"
    return inner


__all__ = [
    "BULK_TYPE",
    "DRY_RUN_HINTS",
    "INTROSPECTION_QUERY",
    "TARGET_TYPE",
    "IntrospectObservation",
    "IntrospectStage",
    "TypeFields",
    "introspect_send_input",
    "parse_type_fields",
]
