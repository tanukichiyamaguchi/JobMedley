"""``introspect`` -- **スキーマに尋ねる。送信は起こらない。**

段階4-1 は「配信ファイルを読めば dryRun の有無が分かる」と考えていた。実測27回目
でその前提が誤りだと分かった -- 配信ファイルに入っているのは **操作の定義であって
スキーマではない**。文書に出るのは変数の *型名* だけで、その型がどんなフィールドを
持つかは書かれていない。

GraphQL の ``__type(name:)`` は ``query`` である。送信は ``mutation`` なので、
このコマンドでは **起こす操作そのものが存在しない**。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobmedley_scout.recon.introspect import (
    BULK_TYPE,
    INTROSPECTION_QUERY,
    TARGET_TYPE,
    IntrospectObservation,
    IntrospectStage,
    TypeFields,
    parse_type_fields,
)


def _response(fields: list[dict[str, object]] | None, *, name: str = TARGET_TYPE) -> str:
    node = None if fields is None else {"name": name, "kind": "INPUT_OBJECT", "inputFields": fields}
    return json.dumps({"data": {"__type": node}})


def _field(name: str, type_name: str = "String") -> dict[str, object]:
    return {"name": name, "type": {"name": type_name, "kind": "SCALAR", "ofType": None}}


# ---------------------------------------------------------------------------
# 送信を起こさないこと -- ここが崩れたら全部が無意味になる
# ---------------------------------------------------------------------------


def test_the_only_query_this_command_sends_is_a_read() -> None:
    """**``mutation`` の語がどこにも無いこと。**"""
    assert "mutation" not in INTROSPECTION_QUERY.lower()
    assert INTROSPECTION_QUERY.lstrip().startswith("query ")
    assert "__type" in INTROSPECTION_QUERY


def test_the_query_body_cannot_be_supplied_by_a_caller() -> None:
    """**差し替えられる形にすると、mutation を1行入れるだけで送信になる** (13.6)。

    本文はモジュール定数で、``_ask`` はそれ以外を送らない。
    """
    source = Path("src/jobmedley_scout/recon/introspect.py").read_text(encoding="utf-8")
    assert '"query": INTROSPECTION_QUERY,' in source
    # 呼び出し側から本文を渡す口を作っていないこと。
    assert "def introspect_send_input(\n    config: BrowserConfig,\n" in source
    assert "query=" not in source, "問い合わせ文を引数で受け取る口ができています"


def test_the_variables_are_only_a_type_name() -> None:
    """変数が型名だけなら、呼び出し側が中身をすり替える余地が無い。"""
    source = Path("src/jobmedley_scout/recon/introspect.py").read_text(encoding="utf-8")
    assert '"variables": {"name": type_name},' in source


# ---------------------------------------------------------------------------
# 応答の読み方
# ---------------------------------------------------------------------------


def test_fields_are_read_with_their_types() -> None:
    fields, reason, disabled = parse_type_fields(
        _response([_field("memberId", "ID"), _field("scoutMessage", "String")]), TARGET_TYPE
    )
    assert not reason and not disabled
    assert fields == (("memberId", "ID"), ("scoutMessage", "String"))


def test_a_wrapped_type_is_rendered_readably() -> None:
    body = json.dumps(
        {
            "data": {
                "__type": {
                    "name": TARGET_TYPE,
                    "kind": "INPUT_OBJECT",
                    "inputFields": [
                        {
                            "name": "memberId",
                            "type": {
                                "name": None,
                                "kind": "NON_NULL",
                                "ofType": {"name": "ID", "kind": "SCALAR"},
                            },
                        }
                    ],
                }
            }
        }
    )
    fields, _reason, _disabled = parse_type_fields(body, TARGET_TYPE)
    assert fields == (("memberId", "ID!"),)


def test_a_dry_run_field_is_recognised() -> None:
    entry = TypeFields(type_name=TARGET_TYPE, fields=(("memberId", "ID"), ("dryRun", "Boolean")))
    assert entry.dry_run_candidates() == ("dryRun",)
    assert "**dryRun 相当らしいフィールド**" in entry.render()


def test_no_dry_run_field_is_said_plainly() -> None:
    entry = TypeFields(type_name=TARGET_TYPE, fields=(("memberId", "ID"),))
    assert entry.dry_run_candidates() == ()
    assert "dryRun 相当らしいフィールドはありません" in entry.render()


@pytest.mark.parametrize("name", ["dryRun", "dry_run", "previewOnly", "validateOnly", "testMode"])
def test_the_hints_catch_the_usual_spellings(name: str) -> None:
    entry = TypeFields(type_name=TARGET_TYPE, fields=((name, "Boolean"),))
    assert entry.dry_run_candidates() == (name,)


# ---------------------------------------------------------------------------
# 「答えなかった」と「無効だった」を混ぜない
# ---------------------------------------------------------------------------


def test_a_disabled_introspection_is_recognised_from_the_code_only() -> None:
    """**文言は見ない。** サーバのメッセージに値が混ざりうる (13.2)。"""
    body = json.dumps(
        {"errors": [{"message": "秘密の文言", "extensions": {"code": "INTROSPECTION_DISABLED"}}]}
    )
    fields, reason, disabled = parse_type_fields(body, TARGET_TYPE)
    assert fields == ()
    assert disabled
    assert "INTROSPECTION_DISABLED" in reason
    assert "秘密の文言" not in reason


def test_an_unrelated_error_is_not_called_disabled() -> None:
    """**判定できないものは「分からない」側へ倒す。**

    「たぶん無効だろう」を True にすると、まだ確定していないことを確定として
    報告することになる。
    """
    body = json.dumps({"errors": [{"extensions": {"code": "UNAUTHENTICATED"}}]})
    _fields, reason, disabled = parse_type_fields(body, TARGET_TYPE)
    assert not disabled
    assert "UNAUTHENTICATED" in reason


def test_a_missing_type_is_not_called_disabled() -> None:
    """introspection は動いているが、その名前の型が無い。**無効とは違う。**"""
    _fields, reason, disabled = parse_type_fields(_response(None), TARGET_TYPE)
    assert not disabled
    assert TARGET_TYPE in reason


@pytest.mark.parametrize("body", ["", None, "not json", "[]", '{"data": null}'])
def test_anything_unreadable_is_neither_answered_nor_disabled(body: str | None) -> None:
    fields, reason, disabled = parse_type_fields(body, TARGET_TYPE)
    assert fields == ()
    assert reason
    assert not disabled


# ---------------------------------------------------------------------------
# 報告
# ---------------------------------------------------------------------------


def test_no_session_stops_the_chain_first() -> None:
    observed = IntrospectObservation(endpoint="https://x/", session_present=False)
    assert observed.reached() is IntrospectStage.NO_SESSION
    assert "段階1" in observed.render()


def test_a_silent_server_is_not_reported_as_a_confirmed_answer() -> None:
    """**「答えなかった」を「無効だった」にしない** (原則2)。"""
    observed = IntrospectObservation(endpoint="https://x/", note="応答が空でした")
    assert observed.reached() is IntrospectStage.NOT_ANSWERED
    report = observed.render()
    assert "まだ確定していません" in report


def test_a_disabled_introspection_is_reported_as_a_settled_answer() -> None:
    """**無効だったことも確定した答えである。**"""
    observed = IntrospectObservation(
        endpoint="https://x/", disabled=True, note="errors が返りました (INTROSPECTION_DISABLED)"
    )
    assert observed.reached() is IntrospectStage.DISABLED
    report = observed.render()
    assert "確定した答え" in report
    assert "4-3" in report


def test_an_answered_run_states_the_verdict_in_one_line() -> None:
    observed = IntrospectObservation(
        endpoint="https://x/",
        answered=True,
        types=(
            TypeFields(type_name=TARGET_TYPE, fields=(("memberId", "ID"),)),
            TypeFields(type_name=BULK_TYPE, reason="型はありませんでした"),
        ),
    )
    assert observed.reached() is IntrospectStage.ANSWERED
    report = observed.render()
    assert "**dryRun 相当はありません。**" in report
    assert "確定です" in report


def test_a_run_that_found_dry_run_says_stage_two_is_usable() -> None:
    observed = IntrospectObservation(
        endpoint="https://x/",
        answered=True,
        types=(TypeFields(type_name=TARGET_TYPE, fields=(("dryRun", "Boolean"),)),),
    )
    report = observed.render()
    assert "**dryRun 相当が在ります**" in report
    assert "段階4-2 が使えます" in report


def test_a_broken_chain_raises_rather_than_reporting_a_lie() -> None:
    observed = IntrospectObservation(endpoint="https://x/", answered=False, disabled=False)
    assert observed.reached() is IntrospectStage.NOT_ANSWERED
    with pytest.raises(ValueError, match="時系列と矛盾"):
        IntrospectObservation(endpoint="https://x/", session_present=False, answered=True).reached()
