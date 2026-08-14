"""段階3の探索の「判断」を固定する。

守りたいのは3点。

1. **押す順は決めるが、除外はしない。** 遮断を武装した状態では、送信を名乗る
   ボタンこそ押して正体を見たい (中断された非GETが ``api.send.*`` の観測になる)。
   ここで除外すると、段階3が永久に終わらない
2. **閉じるボタンは開いた領域の中だけから探す。** 画面全体から集めると、常駐して
   いる別モーダルの閉じるボタンが混ざる (実測: 結果ページに ``a.c-modal__closer``
   が5個、いずれも待機状態で在った)
3. **印字するURLから個人を消す。** 段階3の成果物はURL形なので出さざるを得ないが、
   実URLには会員IDが載る (13.2)
"""

from __future__ import annotations

from jobmedley_scout.browser.dom import DomNode, DomTree
from jobmedley_scout.recon.list_structure import subtree_sizes
from jobmedley_scout.recon.open_structure import (
    BlockedRequest,
    card_action_candidates,
    close_candidates_in,
    opened_region,
    rank_send_candidates,
    redact_url,
    vanished_region,
)


def _tree(*rows: tuple[str, tuple[str, ...], int]) -> DomTree:
    return DomTree(
        nodes=tuple(DomNode(tag=t, class_names=c, parent=p) for t, c, p in rows),
        truncated=False,
        shadow_root_count=0,
    )


def _card_tree() -> DomTree:
    """実測7回目のカード構造の縮約: チェックボックス + ボタン2つ (片方がスカウト)。"""
    return _tree(
        ("body", ("c-body",), -1),  # 0
        ("div", ("c-search-member-card",), 0),  # 1  カード
        ("div", ("c-search-member-card__checkbox-area",), 1),  # 2
        ("label", ("c-checkbox",), 2),  # 3
        ("input", ("c-checkbox__input",), 3),  # 4
        ("div", ("c-search-member-card__main-content",), 1),  # 5
        ("p", ("c-search-member-card-text",), 5),  # 6
        ("div", ("c-search-member-card__buttons",), 1),  # 7
        ("button", ("c-button", "u-wd-100p"), 7),  # 8  用途不明 (レジュメ?)
        ("button", ("c-button", "js-tour-guide-scout-button"), 7),  # 9  スカウト送信
    )


# --- card_action_candidates ----------------------------------------------------


def test_send_looking_buttons_are_ordered_last_but_never_dropped() -> None:
    """**除外しない。** 遮断を武装しているので、送信を名乗るボタンこそ押して
    正体を見る -- 中断された非GETがそのまま段階3の成果物になる。

    順序だけを決める: 安全そうな方を先に押し、ドロワーが先に開けば送信部品を
    押さずに済む。この判定が外れても遮断があるので送信は起きない。
    """
    tree = _card_tree()
    candidates = card_action_candidates(tree, subtree_sizes(tree), 1)

    tokens = [c.selector() for c in candidates]
    assert any("js-tour-guide-scout-button" in t for t in tokens)  # 落とされていない
    assert tokens[-1] == "button.c-button.js-tour-guide-scout-button"  # 最後に回される
    assert [c.looks_like_send for c in candidates][-1] is True
    # 用途不明のボタンや当たり判定 (label) は先に来る。
    assert tokens[0] in ("label.c-checkbox", "input.c-checkbox__input", "button.c-button")


def test_the_selector_chains_every_stable_class_so_it_cannot_hit_the_send_button() -> None:
    """**押し間違いは取り消せない** (13.6)。実測のカードのボタン2つは
    ``c-button`` を共有し、違いは ``js-tour-guide-scout-button`` の有無だけ。
    先頭トークンだけで指すと ``button.c-button`` になり、送信ボタンにも一致する。
    """
    tree = _tree(
        ("body", ("c-body",), -1),
        ("div", ("c-search-member-card",), 0),
        ("div", ("c-search-member-card__buttons",), 1),
        ("button", ("c-button", "u-wd-100p"), 2),
        ("button", ("c-button", "u-wd-100p", "js-tour-guide-scout-button"), 2),
    )
    candidates = card_action_candidates(tree, subtree_sizes(tree), 1)

    send = next(c for c in candidates if c.looks_like_send)
    plain = next(c for c in candidates if not c.looks_like_send)

    assert send.selector() == "button.c-button.u-wd-100p.js-tour-guide-scout-button"
    # 汎用ボタン側は送信ボタンのクラス集合の部分集合なので単独では絞りきれない --
    # だからこそ呼び出し側は nth と併用する。その前提をここで固定しておく。
    assert plain.selector() == "button.c-button.u-wd-100p"
    assert send.index != plain.index


def test_only_elements_inside_the_card_are_offered() -> None:
    """カードの外にあるものは押さない (別の候補者の部品を押さないため)。"""
    tree = _tree(
        ("body", ("c-body",), -1),
        ("div", ("c-search-member-card",), 0),
        ("button", ("c-button",), 1),
        ("button", ("c-header-button",), 0),  # カードの外
    )
    candidates = card_action_candidates(tree, subtree_sizes(tree), 1)

    assert [c.selector() for c in candidates] == ["button.c-button"]


def test_a_card_without_controls_yields_nothing() -> None:
    tree = _tree(("body", ("c-body",), -1), ("div", ("c-card",), 0), ("p", ("t",), 1))

    assert card_action_candidates(tree, subtree_sizes(tree), 1) == ()


# --- opened_region / vanished_region -------------------------------------------


def test_a_region_revealed_by_toggling_a_class_is_still_detected() -> None:
    """**実測7回目の要点。** ドロワーが ``u-is-hidden`` の付け外しで現れる作りだと、
    押せる要素の総数は変わらないのに内容は変わる。構造トークンの差なら見える。
    """
    before = {"div.c-side-cover": 1, "div.u-is-hidden": 22}
    after = {"div.c-side-cover": 1, "div.u-is-hidden": 21, "div.c-side-cover__body": 1}

    assert opened_region(before, after) == ("div.c-side-cover__body",)
    assert "div.u-is-hidden" in vanished_region(before, after)


def test_nothing_new_means_nothing_opened() -> None:
    counts = {"div.a": 1}
    assert opened_region(counts, counts) == ()


# --- close_candidates_in -------------------------------------------------------


def _opened_tree() -> DomTree:
    """開いたドロワー (c-side-cover) と、常駐している別モーダルが同居する画面。"""
    return _tree(
        ("body", ("c-body",), -1),  # 0
        ("div", ("c-side-cover",), 0),  # 1  開いた領域
        ("header", ("c-side-cover__head",), 1),  # 2
        ("a", ("c-side-cover__close-btn",), 2),  # 3  <= これが欲しい
        ("div", ("c-side-cover__body",), 1),  # 4
        ("div", ("c-modal", "u-is-hidden"), 0),  # 5  常駐している別モーダル
        ("a", ("c-modal__closer",), 5),  # 6  <= これは混ぜてはいけない
    )


def test_close_controls_come_only_from_the_region_that_opened() -> None:
    tree = _opened_tree()

    found = close_candidates_in(tree, subtree_sizes(tree), ["div.c-side-cover"])

    assert found == ("a.c-side-cover__close-btn",)
    assert "a.c-modal__closer" not in found


def test_no_close_control_in_the_region_returns_empty() -> None:
    """**推測で埋めない** (原則3)。無いなら空を返し、報告が UNRESOLVED を出す。"""
    tree = _tree(
        ("body", ("c-body",), -1),
        ("div", ("c-side-cover",), 0),
        ("p", ("c-text",), 1),
    )

    assert close_candidates_in(tree, subtree_sizes(tree), ["div.c-side-cover"]) == ()


def test_an_empty_region_means_no_search_at_all() -> None:
    tree = _opened_tree()
    assert close_candidates_in(tree, subtree_sizes(tree), []) == ()


# --- redact_url ----------------------------------------------------------------


def test_member_ids_in_the_path_are_masked() -> None:
    """段階3の成果物はURL形なので出す。ただし会員IDは伏せる (13.2)。"""
    masked = redact_url("https://customers.job-medley.com/customers/members/48211/scouts")

    assert "48211" not in masked
    assert masked.endswith("/customers/members/{id}/scouts")


def test_query_values_are_masked_but_keys_survive() -> None:
    """キー名は payload 形の手がかりとして要る。値だけを伏せる。"""
    masked = redact_url("https://a.example/api/send?member_id=48211&job_offer_id=77")

    assert "48211" not in masked and "77" not in masked
    assert "member_id={value}" in masked and "job_offer_id={value}" in masked


def test_a_url_without_ids_is_unchanged() -> None:
    url = "https://customers.job-medley.com/api/scouts"
    assert redact_url(url) == url


# --- rank_send_candidates ------------------------------------------------------


def test_the_sentinel_carrying_request_ranks_first() -> None:
    """センチネルを持つ非GETは、ほぼ確実に送信路である。"""
    beacon = BlockedRequest("POST", "https://analytics.example/collect", carried_sentinel=False)
    own = BlockedRequest("POST", "https://customers.job-medley.com/api/x", carried_sentinel=False)
    send = BlockedRequest(
        "POST", "https://customers.job-medley.com/api/scouts", carried_sentinel=True
    )

    ranked = rank_send_candidates([beacon, own, send])

    assert ranked[0] is send
    assert ranked[1] is own  # 自オリジンが計測ビーコンより前
    assert len(ranked) == 3  # **落とさない** (段階3では送信URLが未知)


def test_ranking_keeps_everything_even_without_a_sentinel() -> None:
    beacon = BlockedRequest("POST", "https://analytics.example/collect", carried_sentinel=False)

    assert rank_send_candidates([beacon]) == (beacon,)
