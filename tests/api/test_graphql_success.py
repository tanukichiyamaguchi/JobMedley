"""GraphQL を相手にしたときにだけ現れる事故を、テストで固定する。

実測20回目で、媒体のスカウト送信が **GraphQL の mutation** だと分かった。
GraphQL には REST と決定的に違う性質が1つある::

    失敗しても HTTP 200 を返し、成否は本文の errors 配列に書かれている。

この1点が、既存の判定の前提を2箇所で崩す。どちらも **失敗が成功に見える** 側の
崩れ方なので、原則2 の「静かなゼロ件」に直結する。
"""

from __future__ import annotations

import pytest

from jobmedley_scout.api.client import classify_auth_failure
from jobmedley_scout.api.endpoints import Endpoint
from jobmedley_scout.api.success import describe_status, graphql_errors, is_success
from jobmedley_scout.errors import ConfigError

SEND = Endpoint(
    id="send.paid",
    method="POST",
    slot="paid",
    url_pattern="https://example.invalid/api/customers/graphql/SendSingleScout",
    success_statuses=frozenset({200}),
    side_effectful=True,
)

#: 媒体が実際に返しうる形。失敗しているのに **HTTP は 200** である。
GRAPHQL_FAILURE = {
    "data": {"sendSingleScout": None},
    "errors": [
        {
            "message": "スカウト送信数の上限に達しています",
            "extensions": {"code": "SCOUT_LIMIT_EXCEEDED"},
        }
    ],
}

GRAPHQL_OK = {"data": {"sendSingleScout": {"id": "9999"}}}


def test_a_graphql_failure_at_http_200_is_not_counted_as_a_send() -> None:
    """**これを見落とすと、送っていないのに送ったことになる。**

    成功として記録された候補者は、重複送信の防止が効いて二度と対象にならない。
    つまり取りこぼしは静かに永続化する -- 原則2 の最悪の形である。
    """
    assert is_success(SEND, 200, GRAPHQL_OK) is True
    assert is_success(SEND, 200, GRAPHQL_FAILURE) is False


def test_the_verdict_says_out_loud_that_200_meant_failure() -> None:
    """**黙ると、ログを読んだ人間が「200 が並んでいるから送れている」と誤読する。**"""
    said = describe_status(SEND, 200, GRAPHQL_FAILURE)
    assert "失敗" in said
    assert "SCOUT_LIMIT_EXCEEDED" in said


def test_no_message_text_from_the_platform_ever_leaves_this_layer() -> None:
    """媒体のエラー文言には候補者名が混ざりうる (13.2)。**コードだけを持ち回る。**"""
    leaky = {
        "errors": [
            {
                "message": "山田太郎さん (会員番号 03323741) には送信できません",
                "extensions": {"code": "MEMBER_NOT_SCOUTABLE"},
            }
        ]
    }
    assert graphql_errors(leaky) == ("MEMBER_NOT_SCOUTABLE",)
    assert "山田" not in describe_status(SEND, 200, leaky)
    assert "03323741" not in describe_status(SEND, 200, leaky)


def test_an_error_without_a_code_is_still_counted() -> None:
    """**数を落とさないほうが、名前が付くことより大事である。**

    コード無しのエラーを 0 件と数えると、その応答は成功になる。
    """
    assert graphql_errors({"errors": [{"message": "何かが起きました"}]}) == ("(コード無し)",)
    assert is_success(SEND, 200, {"errors": [{"message": "何か"}]}) is False


def test_a_body_without_an_errors_array_falls_back_to_the_status() -> None:
    """GraphQL 以外のエンドポイントも同じ関数を通る。**そこを壊さない。**

    「本文が読めない = 失敗」にすると、本文の無い正常な応答 (204 等) を全部落とす。
    GraphQL の失敗は errors が **在る** ことで示されるので、在るときだけ見る。
    """
    assert is_success(SEND, 200, None) is True
    assert is_success(SEND, 200, {"data": {}}) is True
    assert is_success(SEND, 500, None) is False


def test_a_dead_session_arrives_as_http_200_in_graphql() -> None:
    """**6.6 の事故が、そのままの形では再現しない経路。**

    セッション失効は 401 でも 403 でも来ない。HTTP 200 の本文に
    ``UNAUTHENTICATED`` が入って来る。ステータスだけを見る判定はこれを1件も
    拾えず、全件が「失敗」として静かに積み上がる (CIは緑のまま送信0件)。
    """
    codes = frozenset({"unauthenticated", "unauthorized", "session_expired"})
    dead = {"errors": [{"extensions": {"code": "UNAUTHENTICATED"}}]}
    assert classify_auth_failure(200, dead, codes) == "UNAUTHENTICATED"


def test_an_ordinary_graphql_error_is_not_mistaken_for_a_dead_session() -> None:
    """**保守的に判定する** (6.6)。

    単発の権限エラーで実行全体を落とすと、送れるはずの候補者まで巻き添えになる。
    """
    codes = frozenset({"unauthenticated", "unauthorized", "session_expired"})
    assert classify_auth_failure(200, GRAPHQL_FAILURE, codes) is None


def test_the_status_only_paths_still_behave() -> None:
    """401 は無条件、403 はコードを伴うときだけ。**既存の規律を崩さない。**"""
    codes = frozenset({"session_expired"})
    assert classify_auth_failure(401, None, codes) == "http_401"
    assert classify_auth_failure(403, None, codes) is None
    assert classify_auth_failure(403, {"code": "session_expired"}, codes) == "session_expired"


def test_a_null_success_set_still_refuses_rather_than_guessing() -> None:
    """枠が存在しないなら呼ばれるべきではない。**黙って成功にしない。**"""
    absent = Endpoint(
        id="send.free",
        method="POST",
        slot="free",
        url_pattern=None,
        success_statuses=None,
        side_effectful=True,
    )
    with pytest.raises(ConfigError):
        is_success(absent, 200, GRAPHQL_OK)
