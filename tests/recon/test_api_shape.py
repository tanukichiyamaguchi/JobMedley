"""読み取りAPIの応答から **形だけ** を取り出す。値は1つも出さない。

一覧の応答には氏名・会員番号・年齢・性別・居住地・経歴が入っている。13.2 は
偵察の出力に個人データを残すことを禁じている。そして座標に要るのは値ではなく
**どのキーに何が入っているか** である。
"""

from __future__ import annotations

import json

from jobmedley_scout.recon.api_shape import (
    MAX_KEYS_REPORTED,
    ObservedCall,
    describe_response,
    looks_like_an_identifier,
    operation_name,
)

#: 媒体が返しうる形を模したもの。**実在しない値に差し替えてある。**
LIST_RESPONSE = json.dumps(
    {
        "data": {
            "searchMembers": {
                "searchUuid": "b1e2c3d4-0000-0000-0000-000000000000",
                "totalCount": 86,
                "edges": [
                    {
                        "node": {
                            "member": {
                                "id": "00000000",
                                "displayName": "山田太郎",
                                "age": 26,
                                "prefecture": "神奈川県",
                                "desiredOccupations": ["歯科衛生士"],
                                "licenses": ["歯科衛生士"],
                            },
                            "isScouted": False,
                        }
                    }
                ],
            }
        }
    },
    ensure_ascii=False,
)


def test_not_one_value_from_the_response_reaches_the_report() -> None:
    """**これがこのモジュールの唯一の存在理由である。**

    一覧の応答は個人データの塊である。キーの名前と値の種別だけを持ち出す。
    """
    keys, reason, _dropped = describe_response(LIST_RESPONSE)
    assert not reason
    rendered = ObservedCall(
        operation="SearchMembers", redacted_url="https://x/graphql/SearchMembers", method="POST"
    )
    rendered = ObservedCall(**{**rendered.__dict__, "keys": keys})
    text = rendered.render()
    for leaked in ("山田太郎", "神奈川県", "00000000", "b1e2c3d4"):
        assert leaked not in text, f"{leaked} が報告に漏れている"


def test_the_key_names_survive_because_that_is_the_whole_point() -> None:
    """伏せるのは値であって構造ではない。**キーの名前が無ければ座標は埋まらない。**"""
    keys, _reason, _dropped = describe_response(LIST_RESPONSE)
    paths = {path.path: path.value_kind for path in keys}
    assert paths["data.searchMembers.searchUuid"] == "string"
    assert paths["data.searchMembers.totalCount"] == "number"
    assert "data.searchMembers.edges[].node.member.displayName" in paths


def test_the_response_is_walked_deep_enough_to_reach_the_fields() -> None:
    """**浅く切ると「その項目は無い」と読み違える** (原則2 の静かなゼロ件)。

    GraphQL の応答は ``data.<op>.edges[].node.<entity>.<field>`` のように包みが
    深い。レジュメの既定 (4段) では中身に届く前に切れる。
    """
    keys, _reason, _dropped = describe_response(LIST_RESPONSE)
    deep = [p.path for p in keys if p.path.endswith("licenses")]
    assert deep, "6段目のフィールドまで届いていない"


def test_the_search_identifier_is_found_by_its_name_not_its_value() -> None:
    """**送信を止めている値の出所を、名前で当たりを付ける。**

    送信 payload には ``searchUuid`` が載る。その出所が一覧の応答のどこかに
    ある。値を見ずに探すには、キーの名前で探すしかない。

    名前が似ていることは同じ値である証明ではないので、報告は「候補」に留める。
    """
    keys, _reason, _dropped = describe_response(LIST_RESPONSE)
    call = ObservedCall(
        operation="SearchMembers",
        redacted_url="https://x/graphql/SearchMembers",
        method="POST",
        keys=keys,
    )
    assert call.search_id_candidates() == ("data.searchMembers.searchUuid",)
    assert "**検索の識別子らしいキー**" in call.render()


def test_a_map_keyed_by_member_id_does_not_leak_through_its_key_names() -> None:
    """**キーの名前が個人データになる場合がある。**

    応答が ``{"3323741": {...}}`` のように会員IDをキーにした地図だったら、
    キー名を出すことは会員番号を出すことである。落とす。
    """
    body = json.dumps({"data": {"members": {"3323741": {"age": 26}, "2973815": {"age": 31}}}})
    keys, _reason, dropped = describe_response(body)
    text = " ".join(path.path for path in keys)
    assert "3323741" not in text
    assert "2973815" not in text
    assert dropped > 0, "落とした事実が数に残っていない"


def test_dropping_is_reported_rather_than_hidden() -> None:
    """**落とした事実も観測である。** 黙ると「その応答は薄かった」と読み違える。"""
    body = json.dumps({"data": {"members": {"3323741": {"age": 26}}}})
    keys, _reason, dropped = describe_response(body)
    call = ObservedCall(
        operation="X", redacted_url="u", method="POST", keys=keys, dropped_keys=dropped
    )
    assert "落としたキー" in call.render()


def test_identifiers_are_recognised_by_shape() -> None:
    assert looks_like_an_identifier("3323741")
    assert looks_like_an_identifier("b1e2c3d4-0000-0000-0000-000000000000")
    assert looks_like_an_identifier("deadbeefdeadbeef")
    assert not looks_like_an_identifier("searchUuid")
    assert not looks_like_an_identifier("edges")


def test_an_unreadable_response_says_so_instead_of_looking_empty() -> None:
    """**「読めなかった」と「キーが無かった」は別の事実である** (原則2)。"""
    keys, reason, _dropped = describe_response("<html>error</html>")
    assert keys == ()
    assert "JSONとして読めませんでした" in reason
    call = ObservedCall(operation="", redacted_url="u", method="POST", unread_reason=reason)
    rendered = call.render()
    assert "キーパスは取れませんでした" in rendered
    assert "JSONとして読めませんでした" in rendered
    # 生の本文は報告に混ぜない。
    assert "<html>" not in rendered


def test_a_flood_of_keys_is_capped() -> None:
    """上限を超えたら切る。**切ったことは数で分かるようにする。**"""
    body = json.dumps({"data": {f"field{i}": i for i in range(MAX_KEYS_REPORTED + 50)}})
    keys, _reason, dropped = describe_response(body)
    assert len(keys) == MAX_KEYS_REPORTED
    assert dropped >= 50


def test_the_operation_name_comes_from_the_request_not_the_response() -> None:
    assert operation_name(json.dumps({"operationName": "SearchMembers"})) == "SearchMembers"
    assert operation_name("not json") == ""
    assert operation_name(None) == ""


# ===========================================================================
# 名前付け -- **RESTには操作名が無い** (実測23回目)
# ===========================================================================


def test_a_graphql_envelope_still_wins() -> None:
    from jobmedley_scout.recon.api_shape import operation_name

    body = '{"operationName": "SendSingleScout", "variables": {}}'
    assert operation_name(body, "https://customers.job-medley.com/api/x/y/") == "SendSingleScout"


def test_a_rest_call_is_named_from_its_path() -> None:
    """実測23回目の報告は、19本すべてが「(名前を読めませんでした)」だった。

    この媒体の読み取りは REST の POST なので、GraphQL の封筒を探しても
    名前は出てこない。**読めない報告は観測していないことに近づく。**
    """
    from jobmedley_scout.recon.api_shape import operation_name

    cases = {
        "https://customers.job-medley.com/api/customers/members/search/": "members/search",
        "https://customers.job-medley.com/api/customers/messages/scout_count/": (
            "messages/scout_count"
        ),
        "https://customers.job-medley.com/api/prefectures/": "prefectures",
        "https://customers.job-medley.com/api/customers/job_offers/published/?limit=100": (
            "job_offers/published"
        ),
    }
    for url, expected in cases.items():
        assert operation_name(None, url) == expected, url


def test_redacted_segments_never_become_the_name() -> None:
    """伏せ字は何も述べていないので、名前に混ぜない。

    ``customer_users/{id}/notifications/`` が ``{id}/notifications`` に
    なると、読む側は毎回同じ無意味な語を読まされる。
    """
    from jobmedley_scout.recon.api_shape import operation_name

    url = "https://customers.job-medley.com/api/customers/customer_users/{id}/notifications/"
    assert operation_name(None, url) == "customer_users/notifications"


def test_a_raw_identifier_in_the_path_never_becomes_the_name() -> None:
    """**伏せ損ねた識別子も落とす** (13.2)。

    名前付けは ``redact_url`` の後を前提にしているが、前提が崩れたときに
    会員番号が報告に出るのは事故なので、ここでも落とす。
    """
    from jobmedley_scout.recon.api_shape import operation_name

    url = "https://customers.job-medley.com/api/customers/members/3323741/resume/"
    name = operation_name(None, url)
    assert "3323741" not in name
    assert name == "members/resume"


def test_nothing_readable_stays_empty() -> None:
    from jobmedley_scout.recon.api_shape import operation_name

    assert operation_name(None, "") == ""
    assert operation_name("not json", "") == ""


# ===========================================================================
# 要求本文の形 -- **応答だけ分かっても呼べない** (実測23回目)
# ===========================================================================


def test_the_request_shape_is_reported_without_values() -> None:
    """一覧のURLが決まっても、**送る中身が分からなければ呼べない。**"""
    from jobmedley_scout.recon.api_shape import ObservedCall, describe_response

    keys, reason, dropped = describe_response(
        '{"pagination": {"page": 1, "limit": 25}, "age": {"from": 20, "to": 40}}'
    )
    assert not reason
    call = ObservedCall(
        operation="members/search",
        redacted_url="https://customers.job-medley.com/api/customers/members/search/",
        method="POST",
        request_keys=keys,
        request_dropped_keys=dropped,
    )
    rendered = "\n".join(call.request_lines())
    assert "pagination.page: <number>" in rendered
    assert "age.from: <number>" in rendered
    # **値は1つも出ない** (13.2)。検索条件から個人が絞り込まれうる。
    for value in ("25", "20", "40"):
        assert f": {value}" not in rendered


def test_a_get_reports_no_request_body() -> None:
    """GETに本文は無い。**「読めなかった」と書くと嘘になる。**"""
    from jobmedley_scout.recon.api_shape import ObservedCall

    call = ObservedCall(operation="prefectures", redacted_url="https://x/", method="GET")
    assert call.request_lines() == ()


def test_a_post_without_a_body_says_so_rather_than_going_silent() -> None:
    """0件を黙らない (原則2)。**本文が無いことと、読めなかったことは違う。**"""
    from jobmedley_scout.recon.api_shape import ObservedCall

    call = ObservedCall(operation="x/y", redacted_url="https://x/", method="POST")
    assert call.request_lines() == ("    要求本文: ありません (本文なしのPOST)",)


def test_an_unreadable_request_body_says_why() -> None:
    from jobmedley_scout.recon.api_shape import ObservedCall

    call = ObservedCall(
        operation="x/y",
        redacted_url="https://x/",
        method="POST",
        request_unread_reason="JSONとして読めませんでした",
    )
    assert call.request_lines() == ("    要求本文: 読めませんでした (JSONとして読めませんでした)",)
