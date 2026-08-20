"""段階3のもう1つの成果物: 送信 payload の **形**。

遮断して記録した本文には送信先の会員IDが載っている。13.2 は偵察の出力に個人
データを残すことを禁じている。そして **雛形に要るのは値ではなく形である** --
どのキーに何を入れるのかが分かれば、段階4はその形に自分の値を詰めて送る。
"""

from __future__ import annotations

import json

from jobmedley_scout.recon.payload_shape import (
    BODY_MARKER,
    idempotency_candidates,
    shape_of,
)

SENTINEL = "ZZRECON-DEADBEEF"

#: 実測19回目に観測した送信 (SendSingleScout) を模したもの。
#: **会員IDは実在しない値に差し替えてある** -- 試験の中でも実データは持たない。
REAL_SHAPE = json.dumps(
    {
        "operationName": "SendSingleScout",
        "query": "mutation SendSingleScout($input: SendSingleScoutInput!) { ... }",
        "variables": {
            "input": {
                "memberId": "00000000",
                "jobOfferId": "11111111",
                "subject": "件名です",
                "body": f"{SENTINEL}\nこれは偵察用のダミー本文です。",
                "isDraft": False,
                "attachmentIds": [],
            }
        },
    },
    ensure_ascii=False,
)


def test_no_value_from_the_captured_body_ever_reaches_the_template() -> None:
    """**値は1つも出さない。**

    出してよいのは形だけである。会員IDも、求人IDも、件名の文言も、記録した本文に
    載っていたというだけで偵察の成果ではない -- 成果は「どのキーに何を入れるか」
    である。
    """
    shape = shape_of(REAL_SHAPE, {}, SENTINEL)
    assert shape is not None
    for leaked in ("00000000", "11111111", "件名です", "ダミー本文"):
        assert leaked not in shape.template, f"{leaked} が雛形に漏れている"
        assert leaked not in shape.render(), f"{leaked} が報告に漏れている"


def test_the_sentinel_names_where_the_message_body_goes() -> None:
    """**目印だけは位置を名指しする。**

    これは値の漏洩ではない。目印はこちらが書いたものであり、「ここが本文の
    入り口だ」という観測そのものである。ここが分からなければ、段階4は生成した
    文面をどのキーに入れればよいのか決められない。
    """
    shape = shape_of(REAL_SHAPE, {}, SENTINEL)
    assert shape is not None
    assert shape.body_key == "variables.input.body"
    assert BODY_MARKER in shape.template
    # 目印そのものは雛形に残さない (実行ごとに変わる値であり、形ではない)。
    assert SENTINEL not in shape.template


def test_the_operation_name_and_the_variable_shape_survive() -> None:
    """形は残る。**伏せるのは値であって構造ではない。**"""
    shape = shape_of(REAL_SHAPE, {}, SENTINEL)
    assert shape is not None
    assert shape.operation == "SendSingleScout"
    paths = {path.path: path.value_kind for path in shape.keys}
    assert paths["input.memberId"] == "string"
    assert paths["input.isDraft"] == "bool"
    assert paths["input.attachmentIds"] == "array"


def test_the_graphql_envelope_survives_because_a_send_needs_it() -> None:
    """**問い合わせ文を落とした雛形は、貼っても送れない雛形である。**

    最初の実装はこれを落としていた。「長いし、雛形に要るのは変数の形だ」という
    理屈だったが、GraphQL は ``query`` の無いリクエストを受け付けない
    (persisted query なら ``extensions`` が代わりに要る)。

    問い合わせ文は媒体のAPIの定義そのもので、画面の文言でも個人データでもない
    ので 13.2 には触れない。伏せるべきは ``variables`` の **値** だけである。
    """
    shape = shape_of(REAL_SHAPE, {}, SENTINEL)
    assert shape is not None
    restored = json.loads(shape.template)
    assert restored["query"].startswith("mutation SendSingleScout")
    assert restored["operationName"] == "SendSingleScout"


def test_a_persisted_query_envelope_also_survives() -> None:
    """``query`` を送らない作り (APQ) でも、封筒はそのまま残す。"""
    body = json.dumps(
        {
            "operationName": "SendSingleScout",
            "extensions": {"persistedQuery": {"version": 1, "sha256Hash": "abc123"}},
            "variables": {"input": {"scoutMessage": SENTINEL}},
        }
    )
    shape = shape_of(body, {}, SENTINEL)
    assert shape is not None
    restored = json.loads(shape.template)
    assert restored["extensions"]["persistedQuery"]["sha256Hash"] == "abc123"


def test_the_body_placeholder_matches_what_the_sender_substitutes() -> None:
    """**出力と入力の語彙が揃っていないと、本文が差し替わらないまま送られる。**

    偵察が ``<本文>`` と書き、送信側が ``{{BODY}}`` を探していた時期があった。
    貼った雛形はそのまま通り、``<本文>`` という文字列がスカウトとして実在の
    候補者へ飛ぶ。送信は取り消せない (13.6)。
    """
    from jobmedley_scout.api.payloads import PLACEHOLDER_BODY

    assert BODY_MARKER == PLACEHOLDER_BODY

    shape = shape_of(REAL_SHAPE, {}, SENTINEL)
    assert shape is not None
    assert PLACEHOLDER_BODY in shape.template


def test_the_report_names_every_slot_the_operator_still_has_to_fill() -> None:
    """**貼っただけでは送れない、と先に言う。**

    種別の印が残ったまま送ると ``<string>`` がそのまま媒体へ渡る。
    本文は数えない -- あれは送信時にコードが差し込む。
    """
    shape = shape_of(REAL_SHAPE, {}, SENTINEL)
    assert shape is not None
    unfilled = shape.unfilled_keys()
    assert "variables.input.memberId" in unfilled
    assert "variables.input.jobOfferId" in unfilled
    assert "variables.input.scoutMessage" not in unfilled, "本文はコードが差し込む"
    assert "**まだ値が決まっていない欄**" in shape.render()


def test_an_unreadable_body_yields_nothing_rather_than_a_guess() -> None:
    """**読めなければ読めないと言う** (原則3)。

    「たぶん GraphQL だろう」と形を作れば、それは推測で座標を埋めることになる。
    """
    assert shape_of("これはJSONではありません", {}, SENTINEL) is None
    assert shape_of(None, {}, SENTINEL) is None
    assert shape_of("[1, 2, 3]", {}, SENTINEL) is None


def test_header_values_are_never_returned() -> None:
    """**ヘッダは名前だけ。** 値はセッションそのものである (12.7)。"""
    headers = {"Cookie": "session=SECRET", "X-CSRF-Token": "ALSO-SECRET"}
    shape = shape_of(REAL_SHAPE, headers, SENTINEL)
    assert shape is not None
    assert shape.header_names == ("Cookie", "X-CSRF-Token")
    assert "SECRET" not in shape.render()


def test_an_array_is_folded_to_one_element() -> None:
    """何件送ったかは **形ではなく、その回の都合** である。"""
    body = json.dumps({"operationName": "Bulk", "variables": {"ids": ["a", "b", "c", "d", "e"]}})
    shape = shape_of(body, {}, SENTINEL)
    assert shape is not None
    assert json.loads(shape.template)["variables"]["ids"] == ["<string>"]


def test_no_idempotency_header_is_not_the_same_as_there_being_none() -> None:
    """**この1回に載っていなかった**、以上のことは観測していない (原則3)。"""
    assert idempotency_candidates(("Cookie", "Content-Type")) == ()
    assert idempotency_candidates(("X-Idempotency-Key", "Cookie")) == ("X-Idempotency-Key",)
