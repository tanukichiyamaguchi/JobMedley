"""10.6: 診断情報の質がイテレーション速度を決める。ノイズ除去を先に作る。"""

from __future__ import annotations

from jobmedley_scout.reply.diagnostics import (
    ExclusionReason,
    ResponseEntry,
    fallback_tokens,
    index_responses,
    is_static_asset,
    structure_digest,
)


def test_digest_replaces_non_ascii_runs_with_a_count_marker() -> None:
    """13.2: 氏名や本文がジョブログに落ちない。桁数だけ残す。"""
    digest = structure_digest('{"sender":"田中太郎","id":991}')

    assert "田中太郎" not in digest
    assert "[#4]" in digest
    assert '"sender"' in digest
    assert "991" in digest
    assert digest.isascii()


def test_digest_drops_head_script_and_style() -> None:
    html = (
        "<html><head><title>受信箱</title></head>"
        "<style>.row{color:red}</style>"
        "<body><div class='row'>本文</div></body>"
        "<script>window.__DATA__={secret:1}</script></html>"
    )

    digest = structure_digest(html)

    assert "color:red" not in digest
    assert "__DATA__" not in digest
    assert "<title>" not in digest
    assert "class='row'" in digest


def test_digest_survives_an_unterminated_script_tag() -> None:
    """キャプチャは途中で切れることがある。切れた断片でノイズが素通りしない。"""
    digest = structure_digest("<body><script>var a=1;")

    assert "var a=1" not in digest


def test_digest_masks_email_addresses() -> None:
    """メールアドレスはASCIIなので非ASCII置換をすり抜ける (13.2)。"""
    digest = structure_digest('{"mail":"tanaka.taro@example.com"}')

    assert "tanaka.taro@example.com" not in digest
    assert "[@]" in digest


def test_digest_is_capped() -> None:
    digest = structure_digest("a" * 500, max_chars=100)

    assert digest.startswith("a" * 100)
    assert "[+400chars]" in digest


def test_static_assets_are_excluded_from_the_response_index() -> None:
    """サイズ順に並べたらJSライブラリのバンドルが先頭に来た。除外が先 (10.6)。"""
    bundle = ResponseEntry(
        url="https://example.test/static/vendor.a1b2.js",
        content_type="application/javascript",
        size=980_000,
    )
    listing = ResponseEntry(
        url="https://example.test/api/inbox/list?page=1",
        content_type="application/json; charset=utf-8",
        size=12_000,
    )
    sprite = ResponseEntry(url="https://example.test/img/sprite.png", content_type="image/png")
    font = ResponseEntry(url="https://cdn.example.test/f/abcdef", content_type="font/woff2")

    index = index_responses([bundle, listing, sprite, font])

    assert index.candidates == (listing,)
    assert index.best() == listing
    assert {excluded.entry for excluded in index.excluded} == {bundle, sprite, font}
    assert {excluded.reason for excluded in index.excluded} == {ExclusionReason.STATIC_ASSET}


def test_data_responses_outrank_bigger_unknown_responses() -> None:
    """サイズを第1キーにしない。第1キーにすると同じ事故に戻る。"""
    big_html = ResponseEntry(
        url="https://example.test/mypage", content_type="text/html", size=90_000
    )
    small_json = ResponseEntry(
        url="https://example.test/api/inbox/list", content_type="application/json", size=800
    )

    index = index_responses([big_html, small_json])

    assert index.candidates == (small_json, big_html)


def test_ranking_is_stable_for_equal_entries() -> None:
    """実行ごとに順序が変わると「前回と何が違うのか」を比較できない。"""
    first = ResponseEntry(
        url="https://example.test/api/b", content_type="application/json", size=10
    )
    second = ResponseEntry(
        url="https://example.test/api/a", content_type="application/json", size=10
    )

    assert index_responses([first, second]).candidates == (second, first)


def test_source_maps_and_query_strings_do_not_confuse_the_asset_check() -> None:
    assert is_static_asset(ResponseEntry(url="https://example.test/a/app.js.map")) is True
    assert is_static_asset(ResponseEntry(url="https://example.test/a/main.css?v=3")) is True
    assert is_static_asset(ResponseEntry(url="https://example.test/api/inbox/list?p=1")) is False


def test_fallback_tokens_show_the_shape_of_a_non_json_body() -> None:
    """応答がJSONとは限らない。解析に失敗しても構造は目視できるようにする。"""
    tokens = fallback_tokens('<div data-row-id="991">田中太郎</div><div data-row-id="992">')

    assert "data" in tokens
    assert "row" in tokens
    assert "991" in tokens
    # 重複は畳む。並びは初出順なので、どこで形が変わったかが読める。
    assert tokens.count("data") == 1
    # 13.2: 非ASCIIは拾わない。
    assert all(token.isascii() for token in tokens)


def test_fallback_tokens_are_capped() -> None:
    tokens = fallback_tokens(" ".join(f"tok{index}" for index in range(50)), max_tokens=10)

    assert len(tokens) == 10
