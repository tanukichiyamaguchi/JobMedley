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
    EmptyCandidate,
    ReadyValue,
    RowGroup,
    ancestors_or_self,
    click_locator,
    contains,
    empty_exclusions,
    empty_state_candidates,
    indices_with_token,
    list_region,
    lowest_common_ancestor,
    maximal_groups,
    post_load_markers,
    ready_values,
    repeated_child_groups,
    row_group_candidates,
    rows_present_union,
    safe_click_index,
    stable_tokens,
    subtree_sizes,
    token_counts,
    transient_tokens,
    vanished_tokens,
    zero_page_finished,
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


# --- 反証で見つかった2つの穴 (**どちらも推奨値として誤りが出ていた**) ------------


def test_a_contaminated_zero_page_slides_the_row_inward_but_never_to_a_frame() -> None:
    """**汚染された0件ページを採用すると、行の同定が内側へ滑る。**

    ``maximal_groups`` は「他の群の要素の内側に親がある群」を落とす規則なので、
    支配者 (カード群) が0件フィルタで先に消えると、その子孫が繰り上がって極大になる。

    汚染の第一防衛は :func:`recon.observe_list.zero_page_is_usable` (繰り返しが
    1つも消えていないページを拒否する) だが、**一部だけ消えたページは通りうる**
    (カードの骨だけ残した「おすすめ」等)。そのとき行は内側のトークンへ滑る --
    それでも次の2点が保たれることを、ここで固定する:

    1. 滑った先も「結果ページに2個以上・0件ページに0個」を満たす = 内容が出たら
       一致するトークンであり、**常に真になる枠には決してならない**
    2. 出力の不変条件 (行側 XOR 0件側) は破れない

    つまり最悪ケースでも失敗は「最適でない値」止まりで、静かなゼロ件には戻らない。
    残存の内訳は診断として印字され、構造スナップショットで手元から検証できる。
    """
    results = _tree(
        ("body", ("c-body",), -1),
        ("div", ("list",), 0),
        ("div", ("c-search-member-card",), 1),
        ("p", ("c-search-member-card-text",), 2),
        ("p", ("c-search-member-card-text",), 2),
        ("div", ("c-search-member-card",), 1),
        ("p", ("c-search-member-card-text",), 5),
        ("p", ("c-search-member-card-text",), 5),
    )
    contaminated = _tree(
        ("body", ("c-body",), -1),
        ("div", ("list",), 0),
        ("div", ("c-search-member-card",), 1),  # 骨だけの「おすすめ」
        ("div", ("c-search-member-card",), 1),
        ("div", ("c-search-empty",), 1),
    )
    rc, zc = token_counts(results), token_counts(contaminated)
    sizes = subtree_sizes(results)

    rows = row_group_candidates(results, sizes, [zc])
    empties = empty_state_candidates(contaminated, subtree_sizes(contaminated), rc, "div.list")
    values = ready_values(rows, empties, rc, [zc])

    # 行は内側へ滑る (カードは0件ページに残っているため候補から外れる)。
    assert rows[0].token == "p.c-search-member-card-text"
    # それでも枠は決して出ない。全トークンが XOR を満たす。
    assert values
    for value in values:
        for token in (value.row_token, value.empty_token):
            row_side = rc.get(token, 0) >= 2 and zc.get(token, 0) == 0
            empty_side = rc.get(token, 0) == 0 and zc.get(token, 0) >= 1
            assert row_side != empty_side, token
    assert "body.c-body" not in _emitted_tokens(values)


def test_a_loading_skeleton_never_becomes_the_empty_state() -> None:
    """**未描画のページが最良の0件ページに見える経路を塞ぐ。**

    読み込み中の骨組みは「結果ページに無く、0件ページに在る」を完璧に満たすので、
    0件表示として採用されうる。そして本番では **行より先に** 出るため、
    ``wait_for_selector`` が一覧の描画前に成功する -- このモジュールが潰すために
    書かれた失敗そのもの (原則2)。

    読み込み表示は遷移直後から在り、本物の0件表示は応答後に出る。**時間差は観測できる。**
    """
    zero = _tree(
        ("body", ("c-body",), -1),
        ("div", ("list",), 0),
        ("div", ("c-loading",), 1),  # 遷移直後から在る
        ("div", ("c-search-empty",), 1),  # 応答後に出る
    )
    results = {"body.c-body": 1, "div.list": 1}
    early = {"body.c-body": 1, "div.list": 1, "div.c-loading": 1}
    # このページでは何も消えていない (骨組みと内容の区別材料が無い) ので、
    # 除外は保守的に「遷移直後に在ったもの全部」になる。
    excluded = empty_exclusions(early, token_counts(zero), frozenset())

    without_guard = empty_state_candidates(zero, subtree_sizes(zero), results, "div.list")
    with_guard = empty_state_candidates(zero, subtree_sizes(zero), results, "div.list", excluded)

    assert "div.c-loading" in [c.token for c in without_guard]  # 守りが無ければ通る
    assert [c.token for c in with_guard] == ["div.c-search-empty"]


def test_a_prerendered_empty_state_survives_when_the_loader_was_seen_shedding() -> None:
    """**実測4回目の形。** SPA が速く、0件表示 (``div.c-not-found--searches``) は
    遷移直後の1枚に写り終わっていて、ローダーはその後に剥がれた。

    「early に在る」を理由に捨てると、実在する専用要素が UNRESOLVED になる --
    3回目の「この媒体に0件表示は無い」という結論は、この取り違えから生まれた
    誤りだった。剥がれたことを観測できたページでは、除外は観測済みの一時要素
    (消えたことが観測されたもの) だけでよい。
    """
    early = {"body.c-body": 1, "div.c-loader": 1, "div.c-not-found": 1}
    settled = {"body.c-body": 1, "div.c-not-found": 1}  # ローダーだけが剥がれた
    transients = frozenset({"div.c-loader"})

    excluded = empty_exclusions(early, settled, transients)

    assert "div.c-loader" in excluded
    assert "div.c-not-found" not in excluded


def test_alternatives_are_deduplicated_by_token() -> None:
    """同じトークンの群が親ごとに複数生き残ると、別案が全部同じ文字列になっていた。

    選択肢を出したつもりで何も出していない状態は、別案が無いより悪い。
    """
    rows = (
        RowGroup(token="div.row", parent=1, members=(2, 3), subtree_total=2),
        RowGroup(token="div.row", parent=9, members=(10, 11), subtree_total=2),
    )
    empties = (
        EmptyCandidate(token="div.empty", depth_from_anchor=1, counts_zero=(1,), scope="region"),
    )
    rc = {"div.row": 4, "div.empty": 0}
    zc = {"div.empty": 1}

    values = ready_values(rows, empties, rc, [zc])

    assert [v.selector() for v in values] == ["div.row, div.empty"]


# --- 読み込み後マーカー (**実測4回目で確定した形**) ------------------------------
#
# この媒体の0件ページには「0件表示」の専用要素が存在しない (結果テーブル領域ごと
# 消える)。行∨0件表示のペアは原理的に組めないので、「検索応答の描画後にのみ
# 現れる要素」を値にする。


def _marker_tree() -> DomTree:
    """結果ページの縮約: 枠 + 検索条件表示 + 一覧。"""
    return _tree(
        ("body", ("c-body",), -1),
        ("div", ("c-search-conditions",), 0),  # 応答の描画後にのみ現れる
        ("div", ("list",), 0),
        ("div", ("card",), 2),
        ("div", ("card",), 2),
    )


def test_the_marker_appears_only_after_the_response_rendered() -> None:
    tree = _marker_tree()
    rc = token_counts(tree)
    earlies = [{"body.c-body": 1, "div.c-loader": 1}]  # 遷移直後: 枠+ローダーのみ
    finished = [{"body.c-body": 1, "div.c-search-conditions": 1}]  # 完了した0件ページ

    markers = post_load_markers(tree, rc, earlies, finished)

    assert markers[0] == "div.c-search-conditions"
    # 枠は遷移直後から在るので落ち、行は0件ページに無いので落ちる。
    assert "body.c-body" not in markers
    assert "div.card" not in markers


def test_no_finished_zero_page_means_no_marker() -> None:
    """観測できていないものを値にしない (原則3)。"""
    tree = _marker_tree()
    assert post_load_markers(tree, token_counts(tree), [{"div.c-loader": 1}], []) == ()


def test_transients_are_derived_from_observation_not_vocabulary() -> None:
    """「同じ遷移の中で消えた」がローダーの定義。名前は見ない。

    「結果ページに無い」も見ない -- それは0件表示の定義そのものであって、
    ローダーである証拠ではない。実測4回目で、先に描画し終えた0件表示
    (``div.c-not-found--searches``) が旧定義でローダー扱いされた。
    """
    navigations = [
        # ローダーは剥がれた。0件表示は遷移直後から居て、残った。
        ({"div.c-loader-view": 1, "div.c-not-found": 1}, {"div.c-not-found": 1}, None),
    ]

    assert transient_tokens(navigations) == frozenset({"div.c-loader-view"})


def test_the_previous_pages_residue_is_not_evidence_of_a_transient() -> None:
    """**実測5回目の形。** SPA遷移では前ページの内容が遷移直後の1枚に写り込み、
    新しいページの描画で「消える」。pagination 変種の遷移直後の1枚には age 変種の
    0件表示 (div.c-not-found) が残像として写っていた。素朴な定義だとそれが
    一時要素として学ばれ、後続の遷移で本物の0件表示の消滅を待って満了する。

    直前ページの settled に在ったトークンの消滅は「前のページが消えた」であって
    「ローダーが剥がれた」ではない。
    """
    age_settled = {"div.c-not-found": 1, "body.c-body": 1}
    navigations = [
        # pagination 変種: 遷移直後 = age の残像 + ローダー。落ち着くと結果一覧。
        (
            {"div.c-not-found": 1, "div.c-loader-view": 1, "body.c-body": 1},
            {"div.card": 25, "body.c-body": 1},
            age_settled,
        ),
    ]

    transients = transient_tokens(navigations)

    assert "div.c-loader-view" in transients  # 本物のローダーは学ぶ
    assert "div.c-not-found" not in transients  # 残像は学ばない


def test_vanished_tokens_keep_the_residue_for_candidate_exclusion() -> None:
    """候補除外用の語彙 (vanished_tokens) は残像も含む -- **見える失敗側に倒す。**

    残像 (前ページの0件表示) と、前ページから残留したままのローダーは、件数の
    列からは区別できないことがある。候補除外で残像を免除すると、settled に
    ローダーが残った0件ページ + 差し戻しの組で「最後の防壁」からローダーが
    抜ける (反証レビューが毒の経路を実際に再現した)。
    """
    age_settled = {"div.c-loader-view": 1, "body.c-body": 1}  # ローダー残留のまま
    navigations = [
        (
            {"div.c-loader-view": 1, "body.c-body": 1},
            {"div.card": 25, "body.c-body": 1},
            age_settled,  # 直前 settled にも居る -- transient_tokens なら免除される
        ),
    ]

    assert "div.c-loader-view" not in transient_tokens(navigations)
    assert "div.c-loader-view" in vanished_tokens(navigations)


def test_residue_shedding_does_not_unlock_the_liberal_exclusion_regime() -> None:
    """残像が消えただけのページで、保守的な除外が解除されないこと。

    解除されると、遷移直後から居座る骨組みが0件表示の候補に残る。
    """
    prev_settled = {"div.card": 2}
    early = {"div.card": 2, "div.c-loading": 1}  # 前ページの残像 + 骨組み
    settled = {"div.c-loading": 1, "div.c-search-empty": 1}  # 残像だけ消えた

    excluded = empty_exclusions(early, settled, frozenset(), prev_settled)

    assert "div.c-loading" in excluded  # 保守体制のまま = 骨組みは除外
    assert "div.c-search-empty" not in excluded


def test_a_thin_early_snapshot_borrows_the_other_navigations_vocabulary() -> None:
    """遷移直後の1枚が薄すぎて自分から導けなくても、和集合が受け止める。

    実測で pagination 変種の直後スナップショットは26節点 (起動前の骨組み) で、
    ローダーはそこに写っていなかった。age 側の遷移で「消えた」を観測した語彙が
    供給され、pagination の settled にローダーが残っていること (= 未完了) を
    見抜ける。
    """
    navigations = [
        ({"div.c-loader-view": 1}, {"div.no-hit": 1}, None),  # age: 消滅を観測
        ({}, {"div.c-loader-view": 1, "div.no-hit": 1}, None),  # pagination: 薄い1枚
    ]
    transients = transient_tokens(navigations)

    assert zero_page_finished({"div.c-loader-view": 1}, transients) is False  # 未完了
    assert zero_page_finished({"div.no-hit": 1}, transients) is True  # 完了


def test_no_early_snapshot_means_no_marker() -> None:
    """遷移直後の1枚が無いと「early に不在」が空虚に真になり、全トークンが
    目印を名乗れてしまう。観測していない不在は不在の証拠ではない (原則3)。"""
    tree = _marker_tree()
    rc = token_counts(tree)
    finished = [{"body.c-body": 1, "div.c-search-conditions": 1}]

    assert post_load_markers(tree, rc, [], finished) == ()


def test_rows_present_union_only_absorbs_pages_that_show_rows() -> None:
    """行が見えているページだけを結果側に合流させる。

    遷移途中の1枚には本物の0件表示が写っていることがある (実測5回目の
    pagination 変種の直後の1枚)。行の見えないページまで合流させると、
    本物まで「結果側に在る」ことにされて殺される。
    """
    primary = {"div.card": 25, "body.c-body": 1}
    with_rows_and_tour = {"div.card": 25, "div.c-tour-guide": 1}  # クリック後の木
    no_rows_with_empty = {"div.c-not-found": 1}  # 遷移途中の1枚

    union = rows_present_union("div.card", primary, [with_rows_and_tour, no_rows_with_empty])

    assert union["div.c-tour-guide"] == 1  # 遅延マウントの要素は結果側に合流
    assert union.get("div.c-not-found", 0) == 0  # 行の見えないページは合流しない


def test_the_empty_side_absence_is_checked_against_the_union() -> None:
    """0件側の「結果ページに不在」は合流後の件数で判定する (実測5回目の穴)。"""
    rows = (RowGroup(token="div.card", parent=1, members=(2, 3), subtree_total=2),)
    empties = (
        EmptyCandidate(
            token="div.c-tour-guide", depth_from_anchor=1, counts_zero=(1,), scope="page"
        ),
        EmptyCandidate(
            token="div.c-not-found", depth_from_anchor=1, counts_zero=(1,), scope="page"
        ),
    )
    rc = {"div.card": 2}
    zc = {"div.c-tour-guide": 1, "div.c-not-found": 1}
    union = {"div.card": 2, "div.c-tour-guide": 1}  # クリック後の木にツアーが写った

    without_union = ready_values(rows, empties, rc, [zc])
    with_union = ready_values(rows, empties, rc, [zc], union)

    assert without_union[0].empty_token == "div.c-tour-guide"  # 穴があると騙される
    assert [v.empty_token for v in with_union] == ["div.c-not-found"]
