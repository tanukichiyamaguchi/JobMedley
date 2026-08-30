"""**届いた先が頼んだ先か。** 実測44〜45回目で測っていなかったもの。

レジュメAPIが3回とも **1バイト違わない** 5万字のHTMLを返した。

    1回目: content-type: text/html / 長さ 51976 字
    2回目: 同じ (CSRFトークンを足した)
    3回目: 同じ (Accept / Accept-Language / Origin も足した)

**ヘッダを足しても応答が1バイトも動かない。** 要求がハンドラに届いていない形である。
転送されていれば ``HTTP 200 + HTML`` はそのまま説明が付くが、**測っていなかった
ので分からなかった**。Playwright は既定で転送を辿るので、頼んだ先とは別のページの
中身が 200 で返る。
"""

from __future__ import annotations

from jobmedley_scout.api.transport import HttpResponse

ASKED = "https://customers.example.test/api/customers/graphql/MemberGet"


def test_an_answer_from_the_same_url_is_not_a_redirect() -> None:
    response = HttpResponse(status=200, body_text="{}", final_url=ASKED)
    assert response.was_redirected(ASKED) is False


def test_an_answer_from_a_login_page_is_a_redirect() -> None:
    """**これが見分けたかったもの。** HTTP 200 でも、答えたのは別のページである。"""
    response = HttpResponse(
        status=200, body_text="<html>", final_url="https://customers.example.test/customers/sign_in"
    )
    assert response.was_redirected(ASKED) is True


def test_not_having_measured_is_not_the_same_as_not_redirected() -> None:
    """**3値を潰さない** (原則2)。

    ``final_url`` が空なのは「転送されていない」ではなく「分からない」である。
    同じ言葉にすると、測れていないことが「問題なし」として現れる。
    """
    assert HttpResponse(status=200, body_text="{}").was_redirected(ASKED) is None


def test_a_query_string_difference_is_not_a_redirect() -> None:
    """問い合わせ文字列が足されただけなら、答えたのは同じ経路である。

    ここを厳密一致にすると、正常な応答が全部「転送された」になり、報告が狼少年になる。
    """
    response = HttpResponse(status=200, body_text="{}", final_url=f"{ASKED}?trace=1")
    assert response.was_redirected(ASKED) is False
