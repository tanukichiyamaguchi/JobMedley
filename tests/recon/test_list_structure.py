"""一覧の構造の同定を固定する。

**このテストが守っているのは1つの不変条件である。**

    出力に現れるトークンは「行側」か「0件側」の **どちらか一方だけ** を満たす。

旧実装は「両ページに存在する」を候補の条件にしていたため、画面の枠がすべて合格し、
``body.c-body`` を推奨した。それは全ページに常時あるので待機が常に即座に成功し、
一覧が描画される前に0件と読む (原則2)。順位付けをどう直しても、先頭の枠を落とせば
次の枠が繰り上がるだけだった。だから **述語そのものを反転した** 。

ここでは実測 (2026-08-13 の observe-list) に現れた構造を縮めて再現し、
`body.c-body` が値にも別案にも一度も現れないことを検査する。
"""

from __future__ import annotations

from jobmedley_scout.browser.dom import DomNode, DomTree
from jobmedley_scout.recon.list_structure import (
    ReadyValue,
    RowGroup,
    ancestors_or_self,
    click_locator,
    contains,
    empty_state_candidates,
    indices_with_token,
    list_region,
    lowest_common_ancestor,
    maximal_groups,
    ready_values,
    repeated_child_groups,
    row_group_candidates,
    safe_click_index,
    stable_tokens,
    subtree_sizes,
    token_counts,
)


def _tree(*rows: tuple[str, tuple[str, ...], int]) -> DomTree:
    """``(tag, classes, parent)`` の並びから木を作る。前順であること。"""
    return DomTree(
        nodes=tuple(DomNode(tag=t, class_names=c, parent=p) for t, c, p in rows),
        truncated=False,
        shadow_root_count=0,
    )


# --- 実測を縮めた木 ------------------------------------------------------------
#
# 実測の構造 (2026-08-13):
#   body.c-body > div.o-wrapper > main.o-main > div.o-content__inner
#     > div.js-infinity-scroll-outer-el
#       > div.c-search-member-card        x2  (実際は25枚)
#         > div.c-search-member-card__field
#           > p.c-search-member-card-text x2  (実際は1枚あたり10個)
#         > button.js-tour-guide-scout-button  ← **押してはいけない**
#
# 枠 (body / o-wrapper / o-main / o-content__inner / js-infinity-scroll-outer-el) は
# 0件ページにも同じだけ存在する。


def _results_tree() -> DomTree:
    return _tree(
        ("body", ("c-body",), -1),  # 0
        ("div", ("o-wrapper",), 0),  # 1
        ("main", ("o-main",), 1),  # 2
        ("div", ("o-content__inner",), 2),  # 3
        ("div", ("js-infinity-scroll-outer-el",), 3),  # 4
        ("div", ("c-search-member-card",), 4),  # 5  行1
        ("div", ("c-search-member-card__field",), 5),  # 6
        ("p", ("c-search-member-card-text",), 6),  # 7
        ("p", ("c-search-member-card-text",), 6),  # 8
        ("button", ("js-tour-guide-scout-button",), 5),  # 9  **危険**
        ("div", ("c-search-member-card", "c-search-member-card--scouted"), 4),  # 10 行2
        ("div", ("c-search-member-card__field",), 10),  # 11
        ("p", ("c-search-member-card-text",), 11),  # 12
        ("p", ("c-search-member-card-text",), 11),  # 13
        ("button", ("js-tour-guide-scout-button",), 10),  # 14
    )


def _zero_tree() -> DomTree:
    """同じ枠。行は消え、代わりに0件表示だけが出る。"""
    return _tree(
        ("body", ("c-body",), -1),  # 0
        ("div", ("o-wrapper",), 0),  # 1
        ("main", ("o-main",), 1),  # 2
        ("div", ("o-content__inner",), 2),  # 3
        ("div", ("js-infinity-scroll-outer-el",), 3),  # 4
        ("div", ("c-search-empty",), 4),  # 5  0件表示のブロック
        ("p", ("c-search-empty__message",), 5),  # 6  その中の文字
    )


# --- 木の基本演算 -------------------------------------------------------------


def test_subtree_sizes_count_the_node_itself() -> None:
    sizes = subtree_sizes(_results_tree())
    assert sizes[0] == 15  # 全体
    assert sizes[5] == 5  # カード1 = 自身 + field + p + p + button
    assert sizes[7] == 1  # 葉


def test_containment_is_an_index_interval() -> None:
    tree = _results_tree()
    sizes = subtree_sizes(tree)
    assert contains(sizes, 5, 7) is True  # カード1 は p を含む
    assert contains(sizes, 5, 5) is True  # 自分自身を含む
    assert contains(sizes, 5, 12) is False  # カード1 はカード2の中身を含まない


def test_ancestors_run_from_the_node_to_the_root() -> None:
    assert ancestors_or_self(_results_tree(), 7) == (7, 6, 5, 4, 3, 2, 1, 0)


def test_the_common_ancestor_of_the_rows_is_the_list_container() -> None:
    tree = _results_tree()
    assert lowest_common_ancestor(tree, subtree_sizes(tree), (5, 10)) == 4


def test_hashy_class_names_never_become_tokens() -> None:
    """生成されたクラス名を座標に書くと、次のデプロイで静かに空振りする。"""
    assert stable_tokens("span", ("css-1a2b3c4d", "c-real")) == ("span.c-real",)


def test_the_hashy_filter_does_not_catch_every_generated_name() -> None:
    """**篩の限界を、通るテストとして固定しておく。**

    ``is_stable_class_name`` が弾くのは16進が6文字以上並ぶ名前だけである。
    実測には ``span.css-iwj8j9`` / ``span.ekpvocy0`` (emotion由来と思われる) が
    あり、これらは16進の並びを含まないので **篩を通る**。

    これは直せる欠陥ではなく、1回の観測では判別できないことである
    (「そのクラス名が次のデプロイまで生き残るか」は観測できない)。
    直したつもりにならないよう、通過することを明示的に固定する。
    出力側では「クラス名の寿命は1回の観測では分からない」と添えて運用者に委ねる。
    """
    assert stable_tokens("span", ("css-iwj8j9",)) == ("span.css-iwj8j9",)


# --- 行の同定 (**「最多出現ではダメだった」への答え**) --------------------------


def test_the_row_is_the_card_not_the_text_inside_it() -> None:
    """**実際に起きた誤りそのもの。**

    1枚のカードに複数ある文字要素が、カード本体より件数で勝っていた。
    実測ではカード25枚に対し文字要素250個で、文字をクリックして何も開かなかった。
    """
    tree = _results_tree()
    sizes = subtree_sizes(tree)

    groups = maximal_groups(repeated_child_groups(tree, sizes), sizes)
    tokens = {g.token for g in groups}

    assert "div.c-search-member-card" in tokens
    # 文字要素の親はカードの子孫なので、極大性で落ちる。
    assert "p.c-search-member-card-text" not in tokens


def test_a_modifier_class_does_not_split_the_row_group() -> None:
    """``--scouted`` が付いたカードも同じ群に入ること。

    群のキーをクラス集合にすると、実測の25枚が18枚と7枚に割れる。
    行が分裂すると同定そのものが失敗する。
    """
    tree = _results_tree()
    sizes = subtree_sizes(tree)

    card = next(
        g for g in repeated_child_groups(tree, sizes) if g.token == "div.c-search-member-card"
    )

    assert card.members == (5, 10)


def test_rows_must_vanish_on_every_zero_page() -> None:
    tree = _results_tree()
    sizes = subtree_sizes(tree)
    zero = token_counts(_zero_tree())

    rows = row_group_candidates(tree, sizes, [zero])

    assert rows[0].token == "div.c-search-member-card"
    # 枠は群ですらないが、念のため候補に1つも枠が無いこと。
    assert not any("o-wrapper" in r.token or "c-body" in r.token for r in rows)


def test_without_a_zero_page_rows_are_still_identified() -> None:
    """0件ページを作れなくても、行の同定だけは成立する (ドロワー観測は続行できる)。"""
    tree = _results_tree()
    rows = row_group_candidates(tree, subtree_sizes(tree), [])

    assert rows[0].token == "div.c-search-member-card"


def test_a_thin_but_numerous_group_does_not_outrank_the_cards() -> None:
    """``<br class="x">`` を30個並べたような群に、部分木の重いカードが負けないこと。"""
    tree = _tree(
        ("body", ("c-body",), -1),
        ("div", ("list",), 0),
        ("div", ("card",), 1),
        ("p", ("t",), 2),
        ("p", ("t",), 2),
        ("div", ("card",), 1),
        ("p", ("t",), 5),
        ("p", ("t",), 5),
        ("div", ("thin",), 1),
        ("div", ("thin",), 1),
        ("div", ("thin",), 1),
    )
    sizes = subtree_sizes(tree)

    rows = row_group_candidates(tree, sizes, [])

    assert rows[0].token == "div.card"


# --- 0件表示の同定 ------------------------------------------------------------


def test_the_empty_state_block_comes_before_its_inner_text() -> None:
    """最も外側の新出要素が先頭。内側の文字要素は後ろへ。"""
    zero = _zero_tree()
    results = token_counts(_results_tree())

    empties = empty_state_candidates(
        zero, subtree_sizes(zero), results, "div.js-infinity-scroll-outer-el"
    )

    assert [c.token for c in empties] == ["div.c-search-empty", "p.c-search-empty__message"]
    assert empties[0].scope == "region"


def test_frame_tokens_never_become_empty_state_candidates() -> None:
    """枠は結果ページにも存在するので、0件側の条件 (結果ページに0個) を満たさない。"""
    zero = _zero_tree()
    results = token_counts(_results_tree())

    tokens = {
        c.token
        for c in empty_state_candidates(
            zero, subtree_sizes(zero), results, "div.js-infinity-scroll-outer-el"
        )
    }

    assert "body.c-body" not in tokens
    assert "div.o-wrapper" not in tokens
    assert "div.js-infinity-scroll-outer-el" not in tokens


def test_a_missing_anchor_widens_the_scope_and_says_so() -> None:
    """黙って画面全体へ広げない。広げたことを ``scope`` で表明する。"""
    zero = _zero_tree()
    results = token_counts(_results_tree())

    empties = empty_state_candidates(zero, subtree_sizes(zero), results, "div.not-here")

    assert empties[0].scope == "page"


# --- アンカー -----------------------------------------------------------------


def test_the_anchor_is_the_deepest_unique_ancestor_and_stops_at_body() -> None:
    tree = _results_tree()
    sizes = subtree_sizes(tree)
    counts = token_counts(tree)
    card = next(g for g in row_group_candidates(tree, sizes, []) if g.token.endswith("card"))

    region = list_region(tree, sizes, counts, card)

    assert region is not None
    assert region.anchor_token == "div.js-infinity-scroll-outer-el"
    assert region.rows_outside_group == 0


# --- クリック対象 -------------------------------------------------------------


def test_the_click_target_never_contains_a_control() -> None:
    """**取り消せない外向き操作を偵察で踏まない。**

    実測の行の中には ``button.js-tour-guide-scout-button`` があり、スカウト送信
    そのものの可能性がある。Playwright は要素の中心を押すので、行を素朴に
    クリックすると中心を覆う子がこれを受け取りうる。
    """
    tree = _results_tree()
    sizes = subtree_sizes(tree)

    target = safe_click_index(tree, sizes, 5)

    assert target is not None
    subtree = range(target, target + sizes[target])
    assert not any(tree.nodes[k].tag == "button" for k in subtree)
    # 押せる中で最大の領域 = カード内の field (p を2つ含む)。
    assert tree.nodes[target].class_names == ("c-search-member-card__field",)


def test_a_row_made_entirely_of_controls_yields_no_click_target() -> None:
    """見つからなければ ``None``。**そのときはクリックしない。**"""
    tree = _tree(
        ("body", ("c-body",), -1),
        ("div", ("list",), 0),
        ("div", ("row",), 1),
        ("button", ("send",), 2),
    )
    sizes = subtree_sizes(tree)

    assert safe_click_index(tree, sizes, 2) is None


def test_the_click_locator_pins_the_document_position() -> None:
    """素朴な先頭一致だと、解析した節点と押した要素が別物になりうる。"""
    tree = _results_tree()

    assert click_locator(tree, 11) == ("div.c-search-member-card__field", 1)
    assert indices_with_token(tree, "div.c-search-member-card__field") == (6, 11)


# --- 値の組み立て (**不変条件**) -----------------------------------------------


def _emitted_tokens(values: tuple[ReadyValue, ...]) -> set[str]:
    tokens: set[str] = set()
    for value in values:
        tokens.add(value.row_token)
        tokens.add(value.empty_token)
    return tokens


def test_body_never_appears_in_any_emitted_value() -> None:
    """**このテストが本命である。**

    実際に推奨してしまった ``body.c-body`` が、値にも別案にも一度も現れないこと。
    順位が後ろに回るのでは足りない -- 運用者は別案からも選べるため。
    """
    results, zero = _results_tree(), _zero_tree()
    rc, zc = token_counts(results), token_counts(zero)
    sizes = subtree_sizes(results)
    rows = row_group_candidates(results, sizes, [zc])
    empties = empty_state_candidates(
        zero, subtree_sizes(zero), rc, "div.js-infinity-scroll-outer-el"
    )

    values = ready_values(rows, empties, rc, [zc])

    assert values
    assert "body.c-body" not in _emitted_tokens(values)


def test_every_emitted_token_satisfies_exactly_one_side() -> None:
    """出力の不変条件そのもの。**両ページに存在するトークンは一度も現れない。**"""
    results, zero = _results_tree(), _zero_tree()
    rc, zc = token_counts(results), token_counts(zero)
    sizes = subtree_sizes(results)
    rows = row_group_candidates(results, sizes, [zc])
    empties = empty_state_candidates(
        zero, subtree_sizes(zero), rc, "div.js-infinity-scroll-outer-el"
    )

    values = ready_values(rows, empties, rc, [zc])

    for token in _emitted_tokens(values):
        row_side = rc.get(token, 0) >= 2 and zc.get(token, 0) == 0
        empty_side = rc.get(token, 0) == 0 and zc.get(token, 0) >= 1
        assert row_side != empty_side, token


def test_the_value_is_a_selector_list_so_either_signal_satisfies_it() -> None:
    """カンマは論理和。**行が出た、または0件表示が出た** で待機が解ける。"""
    value = ReadyValue("div.c-search-member-card", "div.c-search-empty")

    assert value.selector() == "div.c-search-member-card, div.c-search-empty"


def test_no_empty_state_means_no_value_at_all() -> None:
    """行だけで値を出すと、0件検索が永久に待たされる。**出さない方が正しい。**"""
    results = _results_tree()
    rc = token_counts(results)
    rows = row_group_candidates(results, subtree_sizes(results), [])

    assert ready_values(rows, (), rc, []) == ()


def test_no_rows_means_no_value_at_all() -> None:
    """行が同定できないなら UNRESOLVED。旧実装の「両方に存在」へ落ちない。"""
    zero = _zero_tree()
    rc = token_counts(_results_tree())
    empties = empty_state_candidates(zero, subtree_sizes(zero), rc, "div.not-here")

    assert ready_values((), empties, rc, [token_counts(zero)]) == ()


def test_alternatives_are_capped() -> None:
    """250行の別案は読めない。実測の出力がまさにそれだった。"""
    results, zero = _results_tree(), _zero_tree()
    rc, zc = token_counts(results), token_counts(zero)
    rows = row_group_candidates(results, subtree_sizes(results), [zc])
    empties = empty_state_candidates(
        zero, subtree_sizes(zero), rc, "div.js-infinity-scroll-outer-el"
    )

    values = ready_values(rows, empties, rc, [zc])

    assert len(values) <= 4  # 推奨1 + 別案3


def test_a_broken_invariant_drops_the_value_instead_of_emitting_it() -> None:
    """不変条件が破れるのはプログラミングエラー。**握り潰さない。**

    行トークンが0件ページにも存在する (= 実は枠だった) 状況を作ると、
    その値は出力から落ちること。
    """
    rc = {"div.row": 3, "div.empty": 0}
    zc = {"div.row": 3, "div.empty": 1}  # 行が0件ページにも在る = 枠
    rows = (RowGroup(token="div.row", parent=0, members=(1, 2, 3), subtree_total=3),)
    from jobmedley_scout.recon.list_structure import EmptyCandidate

    empties = (
        EmptyCandidate(token="div.empty", depth_from_anchor=1, counts_zero=(1,), scope="region"),
    )

    assert ready_values(rows, empties, rc, [zc]) == ()


# --- 外側の繰り返し群に行を奪われないこと (**2回目の実測で壊れた形**) -----------


def _segmented_results() -> DomTree:
    """実測の形。``div.c-segment`` が画面に2つあり、その片方が一覧を囲む。

    検索条件パネルと一覧の区画がどちらも ``c-segment`` で、カードの親はその内側。
    """
    return _tree(
        ("body", ("c-body",), -1),  # 0
        ("div", ("c-segment",), 0),  # 1  検索条件パネル
        ("p", ("c-filter-text",), 1),  # 2
        ("div", ("c-segment",), 0),  # 3  一覧を囲む区画
        ("div", ("js-infinity-scroll-outer-el",), 3),  # 4
        ("div", ("c-search-member-card",), 4),  # 5
        ("div", ("c-search-member-card__field",), 5),  # 6
        ("p", ("c-search-member-card-text",), 6),  # 7
        ("p", ("c-search-member-card-text",), 6),  # 8
        ("div", ("c-search-member-card",), 4),  # 9
        ("div", ("c-search-member-card__field",), 9),  # 10
        ("p", ("c-search-member-card-text",), 10),  # 11
        ("p", ("c-search-member-card-text",), 10),  # 12
    )


def _segmented_zero() -> DomTree:
    """0件でも ``c-segment`` は2つとも残る。消えるのはカードと中身だけ。"""
    return _tree(
        ("body", ("c-body",), -1),
        ("div", ("c-segment",), 0),
        ("p", ("c-filter-text",), 1),
        ("div", ("c-segment",), 0),
        ("div", ("js-infinity-scroll-outer-el",), 3),
        ("div", ("c-search-empty",), 4),
    )


def test_an_unrelated_outer_repeating_group_does_not_steal_the_row() -> None:
    """**2回目の実測で実際に壊れた形。**

    「極大性 → 0件フィルタ」の順だと、``maximal_groups`` は内側を落とす規則なので
    **最も外側の繰り返し群だけが残る**。カードの親は ``c-segment`` の内側なので
    カード群が消え、行が ``div.c-segment`` (2個) と誤判定された。

    そのうえ0件ページの検査もその誤った行で行われ、``c-segment`` は0件ページにも
    残っているため **0件ページが2枚とも捨てられ**、何も確定しないまま終わった。

    先に「0件で消える」で絞れば ``c-segment`` はそこで落ちる。
    """
    results, zero = _segmented_results(), _segmented_zero()
    sizes = subtree_sizes(results)

    rows = row_group_candidates(results, sizes, [token_counts(zero)])

    assert rows, "行が1つも残らないなら、0件フィルタが効きすぎている"
    assert rows[0].token == "div.c-search-member-card"
    assert not any(r.token == "div.c-segment" for r in rows)


def test_the_row_still_beats_the_text_inside_it_after_the_reorder() -> None:
    """順序を入れ替えても、当初の目的 (行 > 行の中身) は保たれること。"""
    results, zero = _segmented_results(), _segmented_zero()
    rows = row_group_candidates(results, subtree_sizes(results), [token_counts(zero)])

    tokens = [r.token for r in rows]
    assert "p.c-search-member-card-text" not in tokens


def test_the_segmented_page_still_yields_a_safe_value() -> None:
    """この形でも、値は行と0件表示の論理和になり枠は現れないこと。"""
    results, zero = _segmented_results(), _segmented_zero()
    rc, zc = token_counts(results), token_counts(zero)
    rows = row_group_candidates(results, subtree_sizes(results), [zc])
    empties = empty_state_candidates(
        zero, subtree_sizes(zero), rc, "div.js-infinity-scroll-outer-el"
    )

    values = ready_values(rows, empties, rc, [zc])

    assert values[0].selector() == "div.c-search-member-card, div.c-search-empty"
    assert "div.c-segment" not in _emitted_tokens(values)
    assert "body.c-body" not in _emitted_tokens(values)
