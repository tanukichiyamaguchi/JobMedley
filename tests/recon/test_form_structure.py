"""送信フォームの形についての純粋な判定。

導線が分かったので、押す先は探すのではなく **形で決める**。ここで固定するのは
その形の規則である。文言は一切見ない (13.2)。
"""

from __future__ import annotations

from jobmedley_scout.browser.dom import DomNode, DomTree
from jobmedley_scout.recon.form_structure import (
    body_fields_in,
    disabled_submits_in,
    form_root,
    query_fields_in,
    submit_candidates_in,
    suggestion_items_in,
)
from jobmedley_scout.recon.list_structure import subtree_sizes


def _tree(*rows: tuple[str, tuple[str, ...], int]) -> DomTree:
    return DomTree(
        nodes=tuple(DomNode(tag=t, class_names=c, parent=p) for t, c, p in rows),
        truncated=False,
        shadow_root_count=0,
    )


#: 運用者の画面をそのまま写した木。左が経歴、右が入力欄。
def _form_tree() -> DomTree:
    return _tree(
        ("body", ("c-body", "c-body--fixed-by-sidecover"), -1),  # 0
        ("div", ("c-sidecover",), 0),  # 1
        ("div", ("c-sidecover__profile",), 1),  # 2   左: 経歴 (押すものは無い)
        ("dd", ("c-definition-table__body",), 2),  # 3
        ("div", ("c-scout-form",), 1),  # 4   右: 入力欄  ← これが form_root
        ("input", ("c-text-field",), 4),  # 5   スカウト対象求人
        ("select", ("c-select",), 4),  # 6   メッセージテンプレート
        ("textarea", ("c-textarea",), 4),  # 7   本文
        ("button", ("c-button", "c-button--important", "c-button--center"), 4),  # 8  送信へ
    )


def test_the_form_is_the_smallest_region_holding_both_a_body_and_a_button() -> None:
    """**フォームであることを、文言ではなく形で決める。**

    本文欄だけなら検索欄と区別が付かず、ボタンだけならどの画面にも当てはまる。
    両方を含む最小の部分木を採る -- 器を採ると、フォームの外の押せるものまで
    候補に入る (実測6回目、``body`` を領域にして画面中のボタンを押した)。
    """
    tree = _form_tree()
    sizes = subtree_sizes(tree)
    assert form_root(tree, sizes, ("div.c-sidecover",)) == 4


def test_a_region_without_a_body_field_is_not_a_form() -> None:
    """**「たぶんこれだろう」を返さない** (原則3)。"""
    tree = _tree(
        ("body", ("c-body",), -1),
        ("div", ("c-modal",), 0),
        ("button", ("c-button",), 1),
        ("a", ("c-modal__closer",), 1),
    )
    assert form_root(tree, subtree_sizes(tree), ("div.c-modal",)) is None


def test_the_body_field_is_a_textarea_and_never_an_input() -> None:
    """実測13回目、求人検索のサジェスト欄に本文用のダミー文を書き込んだ。

    54回の検索を空振りさせたうえで、求人は1件も選べなかった。本文は
    ``textarea`` である。
    """
    tree = _form_tree()
    sizes = subtree_sizes(tree)
    bodies = body_fields_in(tree, sizes, 4)
    assert [c.tag for c in bodies] == ["textarea"]
    queries = query_fields_in(tree, sizes, 4)
    assert [c.tag for c in queries] == ["input"]


def test_the_submit_order_is_the_reverse_of_the_blind_explorer() -> None:
    """**向きが逆なのには理由がある。**

    目隠しの探索では送信らしい部品を最後に回す (まだ埋めていないフォームを
    送信しようとして弾かれる -- 実測12回目)。こちらは求人を選び本文を書いた
    **あと** に呼ぶので、送信らしい部品こそが次の一手である。
    """
    tree = _tree(
        ("body", ("c-body",), -1),
        ("div", ("c-scout-form",), 0),
        ("button", ("c-button", "c-button--secondary"), 1),  # 2  ただのボタン
        ("textarea", ("c-textarea",), 1),  # 3
        ("button", ("c-button", "c-button--important"), 1),  # 4  送信を名乗る
    )
    order = submit_candidates_in(tree, subtree_sizes(tree), 1)
    assert [c.index for c in order] == [4, 2]


def test_the_submit_order_drops_close_disabled_and_dangerous_controls() -> None:
    """除く理由はそれぞれ違う。

    - 閉じる部品 -- 手順の逆向き (実測11回目、送信画面まで来て閉じた)
    - 無効な部品 -- 押しても何も起きず、満了ぶんの時間だけ失う
    - 危険な部品 -- ログアウト等 (実測9回目、押してセッションが死んだ)
    """
    tree = _tree(
        ("body", ("c-body",), -1),
        ("div", ("c-scout-form",), 0),
        ("a", ("c-modal__closer",), 1),  # 2  閉じる
        ("button", ("c-button", "c-button--disabled"), 1),  # 3  無効
        ("a", ("c-header__logout",), 1),  # 4  危険
        ("textarea", ("c-textarea",), 1),  # 5
        ("button", ("c-button", "c-button--important"), 1),  # 6
    )
    sizes = subtree_sizes(tree)
    assert [c.index for c in submit_candidates_in(tree, sizes, 1)] == [6]


def test_a_disabled_submit_is_reported_rather_than_silently_dropped() -> None:
    """**押せないことも観測である。**

    必須欄が埋まるまで「確認してスカウトを送る」は無効である。無効なままなら、
    埋めたつもりの欄が埋まっていない -- 「押せる部品が無かった」とだけ報告すると
    この区別が消える (原則2)。
    """
    tree = _tree(
        ("body", ("c-body",), -1),
        ("div", ("c-scout-form",), 0),
        ("textarea", ("c-textarea",), 1),
        ("button", ("c-button", "c-button--disabled"), 1),
    )
    sizes = subtree_sizes(tree)
    assert submit_candidates_in(tree, sizes, 1) == ()
    assert [c.index for c in disabled_submits_in(tree, sizes, 1)] == [3]


def test_suggestions_use_one_tag_so_a_wrapper_is_never_pressed() -> None:
    """候補の項目と、それを包む器を同じ列に並べない。

    器を押しても値は入らないので、「押せたのに進まない」という分かりにくい
    失敗になる。より項目らしいタグから順に探し、最初に見つかった種類だけを使う。
    """
    tree = _tree(
        ("body", ("c-body",), -1),
        ("div", ("c-typeahead",), 0),  # 1  器
        ("ul", ("c-typeahead__list",), 1),  # 2
        ("li", ("c-typeahead__item",), 2),  # 3  項目
        ("li", ("c-typeahead__item",), 2),  # 4  項目
    )
    items = suggestion_items_in(tree, subtree_sizes(tree), ("div.c-typeahead",))
    assert [c.tag for c in items] == ["li", "li"]
    assert [c.index for c in items] == [3, 4]


def test_suggestions_fall_back_to_links_when_there_are_no_list_items() -> None:
    """``li`` で組まれていない作りもある。**優先順であって決め打ちではない。**"""
    tree = _tree(
        ("body", ("c-body",), -1),
        ("div", ("c-typeahead",), 0),
        ("a", ("c-typeahead__item",), 1),
        ("a", ("c-typeahead__item",), 1),
    )
    items = suggestion_items_in(tree, subtree_sizes(tree), ("div.c-typeahead",))
    assert [c.tag for c in items] == ["a", "a"]


def test_no_suggestion_region_yields_nothing_rather_than_a_guess() -> None:
    """候補が出なかったことは観測である。別のものを押しに行く理由にはしない。"""
    tree = _form_tree()
    assert suggestion_items_in(tree, subtree_sizes(tree), ()) == ()
