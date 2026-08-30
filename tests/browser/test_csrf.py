"""CSRFトークンをページから読む部分の検査。**値がどこにも出ないことが要点。**

実測41回目、レジュメAPIが5万字のHTMLを返した。実測42回目に理由が出た --
ブラウザは ``x-csrf-token`` を付けており、こちらは ``Content-Type`` しか付けて
いなかった。実測43回目に出所が出た (``meta[csrf-token]``)。

**トークンはそれだけで POST を通せる鍵である。** ログに出れば、ログを読んだ誰でも
この運用者として書き込める (12.7)。だから「有無」しか報告しない。
"""

from __future__ import annotations

from typing import Any

import pytest

from jobmedley_scout.browser.csrf import FOUND, NOT_FOUND, NOT_REQUIRED, csrf_headers
from jobmedley_scout.config.placeholders import Unresolved
from jobmedley_scout.errors import UnresolvedCoordinateError

#: 検査用のトークン。**報告のどこにも出てはいけない。**
TOKEN = "SECRET-CSRF-TOKEN-abc123"


def _unresolved(key: str) -> Unresolved:
    return Unresolved(key=key, stage="段階3", how_to_obtain="observe-headers で観測する")


class _Page:
    """A page that returns the token for the expected meta name."""

    def __init__(self, *, meta: str = "csrf-token", value: str | None = TOKEN) -> None:
        self._meta = meta
        self._value = value
        self.asked: list[str] = []

    def get_attribute(self, selector: str, name: str) -> str | None:
        self.asked.append(f"{selector}/{name}")
        return self._value if f'name="{self._meta}"' in selector else None


class _BrokenPage:
    def get_attribute(self, selector: str, name: str) -> str:
        raise RuntimeError(f"ページが壊れています: {TOKEN}")


def _call(page: Any, header: Any = "x-csrf-token", meta: Any = "csrf-token") -> tuple[Any, str]:
    return csrf_headers(page, header, meta, used_by="test")


def test_the_token_reaches_the_header_it_was_observed_on() -> None:
    headers, note = _call(_Page())
    assert headers == {"x-csrf-token": TOKEN}
    assert note == FOUND


def test_the_report_line_never_carries_the_token() -> None:
    """12.7。**これが漏れたら、誰でもこの運用者として書き込める。**"""
    _headers, note = _call(_Page())
    assert TOKEN not in note


def test_a_page_without_the_meta_tag_says_so_rather_than_sending_nothing_silently() -> None:
    """**黙って足さないのが一番危ない。**

    ヘッダの無い要求は弾かれて HTML が返り、それが「0件」として現れる (原則2)。
    実測41回目そのものである。
    """
    headers, note = _call(_Page(meta="something-else"))
    assert headers == {}
    assert note == NOT_FOUND


def test_an_empty_meta_content_counts_as_not_found() -> None:
    """空文字を載せて送っても弾かれる。**無いのと同じに扱う。**"""
    _headers, note = _call(_Page(value="   "))
    assert note == NOT_FOUND


def test_a_page_that_raises_does_not_leak_the_exception_text() -> None:
    """例外のメッセージにページの中身が混ざる経路を作らない (13.2)。"""
    headers, note = _call(_BrokenPage())
    assert headers == {}
    assert note == NOT_FOUND
    assert TOKEN not in note


def test_a_platform_that_does_not_need_the_header_is_a_settled_answer() -> None:
    """``null`` は「要らない」という確定した答えであり、未確定とは違う。"""
    headers, note = _call(_Page(), header=None, meta=None)
    assert headers == {}
    assert note == NOT_REQUIRED


@pytest.mark.parametrize(
    ("header", "meta"),
    [
        (_unresolved("api.csrf_header_name"), "csrf-token"),
        ("x-csrf-token", _unresolved("api.csrf_meta_name")),
        (_unresolved("a"), _unresolved("b")),
    ],
)
def test_an_unresolved_coordinate_stops_rather_than_guessing(header: Any, meta: Any) -> None:
    """**知らないまま送らない。** 送れば失敗が「0件」として現れる (原則2/原則3)。"""
    with pytest.raises(UnresolvedCoordinateError):
        _call(_Page(), header=header, meta=meta)


def test_the_meta_name_from_the_coordinate_is_the_one_asked_for() -> None:
    """座標を変えれば探す先も変わる。**綴りを書き起こさない。**"""
    page = _Page(meta="other-token")
    _call(page, meta="other-token")
    assert page.asked == ['meta[name="other-token"]/content']
