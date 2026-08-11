"""普通のブラウザからのセッション持ち込みを固定する。

この変換が壊れると、**取り込みだけ成功してログインだけ復元されない** 状態になる。
段階1が合格したつもりで段階2へ進み、以降が静かに0件になる形なので、ここは
ブラウザ抜きで固定できる範囲を最大限に固定しておく。
"""

from __future__ import annotations

import pytest

from jobmedley_scout.errors import ConfigError
from jobmedley_scout.handover.curl_session import (
    cookies_from_header,
    parse_curl,
    storage_state_from_curl,
    summarize,
)
from jobmedley_scout.recon.known import is_platform_host

# Chrome の「Copy as cURL (bash)」が出す形。
CHROME_BASH = r"""curl 'https://customers.job-medley.com/api/v1/scouts' \
  -H 'accept: application/json' \
  -H 'accept-language: ja,en-US;q=0.9' \
  -H 'cookie: _jm_session=abc123; remember_token=zzz; _ga=GA1.2.3' \
  -H 'user-agent: Mozilla/5.0 (Macintosh) Chrome/140.0.0.0 Safari/537.36' \
  --compressed"""

# Windows の cmd 形式 (継続が ^、引用が ")。
WINDOWS_CMD = (
    'curl "https://customers.job-medley.com/api/v1/scouts" ^\n'
    '  -H "cookie: _jm_session=abc123" ^\n'
    '  -H "user-agent: Mozilla/5.0 (Windows NT 10.0)"'
)


def test_chrome_bash_paste_is_understood() -> None:
    observation = parse_curl(CHROME_BASH)

    assert observation.url == "https://customers.job-medley.com/api/v1/scouts"
    assert "_jm_session=abc123" in observation.cookie_header
    assert observation.user_agent is not None
    assert "Chrome/140" in observation.user_agent


def test_windows_cmd_paste_is_understood() -> None:
    """継続文字が ``^`` でも読めること。読めないと「Windowsだと動かない」になる。"""
    observation = parse_curl(WINDOWS_CMD)

    assert observation.url == "https://customers.job-medley.com/api/v1/scouts"
    assert observation.cookie_header == "_jm_session=abc123"


def test_curl_exe_prefix_is_accepted() -> None:
    observation = parse_curl('curl.exe "https://customers.job-medley.com/x" -H "cookie: a=1"')

    assert observation.cookie_header == "a=1"


def test_short_cookie_flag_is_accepted() -> None:
    """``-b`` 形式で出すブラウザ・拡張もある。"""
    observation = parse_curl("curl https://customers.job-medley.com/x -b 'a=1; b=2'")

    assert observation.cookie_header == "a=1; b=2"


def test_a_url_inside_a_data_body_is_not_mistaken_for_the_url() -> None:
    """``--data`` の中身をURLと取り違えると、クッキーが別ホストに紐づく。"""
    observation = parse_curl(
        "curl 'https://customers.job-medley.com/api/x' "
        '--data-raw \'{"next":"https://evil.invalid/"}\' '
        "-H 'cookie: s=1'"
    )

    assert observation.url == "https://customers.job-medley.com/api/x"


def test_missing_cookie_header_stops_instead_of_producing_an_empty_session() -> None:
    """**ここが要点。** クッキー無しのセッションを作ると静かな失敗になる。"""
    with pytest.raises(ConfigError, match="Cookie"):
        parse_curl("curl 'https://customers.job-medley.com/' -H 'accept: */*'")


def test_paste_that_is_not_curl_is_rejected_with_instructions() -> None:
    with pytest.raises(ConfigError, match="curl"):
        parse_curl("fetch('https://customers.job-medley.com/')")


def test_empty_input_is_rejected() -> None:
    with pytest.raises(ConfigError):
        parse_curl("   ")


def test_truncated_paste_is_rejected() -> None:
    """途中で切れた貼り付けを黙って通すと、欠けたセッションができる。"""
    with pytest.raises(ConfigError, match="値がありません"):
        parse_curl("curl 'https://customers.job-medley.com/' -H")


def test_cookies_are_scoped_to_the_observed_host_only() -> None:
    """親ドメインへ広げない。観測していないホストへセッションを送らない。"""
    cookies = cookies_from_header("a=1; b=2", url="https://customers.job-medley.com/api/x")

    assert [cookie.domain for cookie in cookies] == [
        "customers.job-medley.com",
        "customers.job-medley.com",
    ]
    assert all(cookie.secure for cookie in cookies)


def test_cookie_values_containing_equals_survive_intact() -> None:
    """JWT や base64 の値には ``=`` が入る。最初の ``=`` だけで割ること。"""
    cookies = cookies_from_header("t=eyJhbGc.ZXlKaGJHY2==", url="https://customers.job-medley.com/")

    assert cookies[0].value == "eyJhbGc.ZXlKaGJHY2=="


def test_duplicate_cookie_names_keep_the_first() -> None:
    """ブラウザは限定的なスコープのものを先に送る (RFC 6265)。"""
    cookies = cookies_from_header("s=narrow; s=broad", url="https://customers.job-medley.com/")

    assert [(c.name, c.value) for c in cookies] == [("s", "narrow")]


def test_storage_state_has_the_shape_playwright_expects() -> None:
    state = storage_state_from_curl(CHROME_BASH)

    assert {cookie["name"] for cookie in state["cookies"]} == {
        "_jm_session",
        "remember_token",
        "_ga",
    }
    first = state["cookies"][0]
    assert set(first) == {
        "name",
        "value",
        "domain",
        "path",
        "expires",
        "httpOnly",
        "secure",
        "sameSite",
    }
    # 有効期限は Cookie ヘッダに現れない。**知らないものを日付として書かない。**
    assert first["expires"] == -1


def test_local_storage_is_empty_unless_supplied() -> None:
    """取れていないものを、それらしい形で埋めない (原則3)。"""
    assert storage_state_from_curl(CHROME_BASH)["origins"] == []


def test_local_storage_is_attached_to_the_observed_origin_when_supplied() -> None:
    state = storage_state_from_curl(CHROME_BASH, local_storage={"token": "t"})

    assert state["origins"] == [
        {
            "origin": "https://customers.job-medley.com",
            "localStorage": [{"name": "token", "value": "t"}],
        }
    ]


def test_summary_never_prints_a_cookie_value() -> None:
    """13.2: セッションクッキーの値は、このシステムで最も危険な文字列である。"""
    report = summarize(storage_state_from_curl(CHROME_BASH))

    assert "abc123" not in report
    assert "zzz" not in report
    assert "_jm_session" in report  # 名前は出す。何が入ったか分からないと困る
    assert "3件" in report


def test_summary_warns_when_local_storage_is_absent() -> None:
    """クッキーだけでは復元できない媒体がありうる。黙って成功したことにしない。"""
    report = summarize(storage_state_from_curl(CHROME_BASH))

    assert "localStorage: なし" in report
    assert "verify-session" in report


def test_summary_offers_the_user_agent_as_a_config_value() -> None:
    """5.1: UAはハードコードせず設定値。作った側と実行側を揃えられるようにする。"""
    pasted = parse_curl(CHROME_BASH)
    report = summarize(storage_state_from_curl(CHROME_BASH), pasted.user_agent)

    assert "browser.user_agent" in report
    assert "Chrome/140" in report


# --- 計測タグを選んでしまう事故 (実際に起きた) --------------------------------

# Google アナリティクスの collect。**クエリに閲覧中のページURLが載っている**ので、
# 開発者ツールのフィルタに "customers.job-medley.com" と入れても消えない。
ANALYTICS_BEACON = (
    "curl 'https://www.google-analytics.com/g/collect?v=2&tid=G-XXXX"
    "&dl=https%3A%2F%2Fcustomers.job-medley.com%2F' "
    "-H 'cookie: _ga=GA1.2.3'"
)


def test_an_analytics_beacon_is_refused_even_though_it_mentions_the_platform() -> None:
    """フィルタを通り抜けてくる形。**手順書の注意書きだけでは防げない。**"""
    with pytest.raises(ConfigError, match="google-analytics.com"):
        storage_state_from_curl(ANALYTICS_BEACON)


def test_the_refusal_says_how_to_pick_the_right_row() -> None:
    """止めるだけで代替を言わないと、運用者はそこで詰む。"""
    with pytest.raises(ConfigError, match="Doc"):
        storage_state_from_curl(ANALYTICS_BEACON)


def test_platform_subdomains_are_allowed() -> None:
    """認証が customers 以外に置かれている可能性を、観測せずに否定しない。"""
    assert is_platform_host("customers.job-medley.com") is True
    assert is_platform_host("job-medley.com") is True
    assert is_platform_host("api.customers.job-medley.com") is True


def test_lookalike_hosts_are_refused() -> None:
    """末尾一致だけだと、塞いだつもりの穴が残る。"""
    assert is_platform_host("job-medley.com.example.invalid") is False
    assert is_platform_host("notjob-medley.com") is False
    assert is_platform_host("www.google-analytics.com") is False
