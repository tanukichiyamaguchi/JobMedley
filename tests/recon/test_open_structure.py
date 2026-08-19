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
    CLICK_FAILURE_KINDS,
    BlockedRequest,
    card_action_candidates,
    click_failure_kind,
    close_candidates_in,
    newly_present,
    opened_region,
    rank_send_candidates,
    redact_url,
    region_roots,
    revealed_controls,
    revealed_text_fields,
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


# --- revealed_controls (実測3回目で分かった導線) --------------------------------


def test_controls_revealed_by_a_press_become_the_next_candidates() -> None:
    """**実測3回目の形。** カードのチェックボックスを押すと一括スカウト用のバー
    (``div.c-sticky-scout-bar``) が現れ、その中にスカウトボタンがある。
    押した結果現れたものを辿らない限り、送信画面には到達しない。
    """
    after = _tree(
        ("body", ("c-body",), -1),  # 0
        ("div", ("c-search-member-card",), 0),  # 1
        ("label", ("c-checkbox",), 1),  # 2  さっき押したもの
        ("div", ("c-sticky-scout-bar",), 0),  # 3  <- 現れた領域
        ("div", ("c-search-count-display",), 3),  # 4
        ("button", ("c-button", "c-sticky-scout-bar__scout-button"), 3),  # 5  <- これが欲しい
    )

    found = revealed_controls(after, subtree_sizes(after), ["div.c-sticky-scout-bar"])

    assert [c.selector() for c in found] == ["button.c-button.c-sticky-scout-bar__scout-button"]
    # 送信を名乗る部品でも **落とさない** -- 遮断があるので押して正体を見る。
    assert found[0].looks_like_send is True


def test_revealed_controls_stay_inside_the_region_that_appeared() -> None:
    """画面全体へ広げない。広げるとヘッダ等を押しに行く (実測1回目のロゴと同じ失敗)。"""
    after = _tree(
        ("body", ("c-body",), -1),
        ("a", ("c-logo__image",), 0),  # 常駐しているヘッダのリンク
        ("div", ("c-sticky-scout-bar",), 0),
        ("button", ("c-sticky-scout-bar__scout-button",), 2),
    )

    found = revealed_controls(after, subtree_sizes(after), ["div.c-sticky-scout-bar"])

    assert [c.selector() for c in found] == ["button.c-sticky-scout-bar__scout-button"]
    assert all("logo" not in c.selector() for c in found)


def test_an_empty_region_reveals_nothing() -> None:
    after = _tree(("body", ("c-body",), -1), ("div", ("c-bar",), 0))
    assert revealed_controls(after, subtree_sizes(after), []) == ()


# --- click_failure_kind (実測4回目: 8個すべて完了しなかった) ---------------------


def test_each_playwright_failure_gets_its_own_label() -> None:
    """**理由を握り潰さない。** 実測4回目は8個すべて「完了しませんでした」としか
    言えず、何が起きているのか分からなかった。分類が次の手を決める。
    """
    cases = {
        '<div class="c-sticky-scout-bar">…</div> intercepts pointer events': (
            "覆われていて押下が届かない"
        ),
        "element is not stable - waiting...": "要素が動き続けている (アニメーション等)",
        "element is not visible": "要素が見えない",
        "element is not enabled": "要素が無効化されている",
        "strict mode violation: locator resolved to 2 elements": "同じセレクタに複数一致した",
        "Timeout 5000ms exceeded.": "満了 (理由の特定なし)",
        "something else entirely": "その他",
    }
    for message, expected in cases.items():
        assert click_failure_kind(message) == expected


def test_the_classification_never_leaks_page_text() -> None:
    """Playwright の例外には要素の outerHTML (= ページの文言) が混ざる。
    **分類名だけを返し、原文は返さない** (13.2)。
    """
    message = '<button class="x">山田太郎さんにスカウトを送る</button> intercepts pointer events'

    kind = click_failure_kind(message)

    assert kind in CLICK_FAILURE_KINDS
    assert "山田" not in kind


# --- 現れた領域の入力欄 ---------------------------------------------------------


def test_text_fields_are_taken_only_from_the_region_that_just_appeared() -> None:
    """**画面全体から集めない。**

    書き込みの目的は「遮断した非GETのどれが送信路かを見分けること」だけである。
    常駐している検索欄まで書き換えると、一覧そのものが変わって、以降に押している
    ものが別物になる (実測1回目でサイトのロゴを押したのと同じ種類の失敗)。
    """
    tree = _tree(
        ("body", ("c-body",), -1),  # 0
        ("form", ("c-search-form",), 0),  # 1  常駐している検索欄
        ("input", ("c-search-form__keyword",), 1),  # 2
        ("div", ("c-sticky-scout-bar",), 0),  # 3  押して現れた領域
        ("input", ("c-scout-form__subject",), 3),  # 4
        ("textarea", ("c-scout-form__body",), 3),  # 5
        ("button", ("c-sticky-scout-bar__scout-button",), 3),  # 6
    )
    sizes = subtree_sizes(tree)

    fields = revealed_text_fields(tree, sizes, ("div.c-sticky-scout-bar",))

    assert [f.index for f in fields] == [4, 5], "領域の外の入力欄を拾っている"
    assert [f.selector() for f in fields] == [
        "input.c-scout-form__subject",
        "textarea.c-scout-form__body",
    ]


def test_no_region_means_no_text_fields() -> None:
    """領域が空なら書き込む先も無い (カードの中の候補を押すとき)。"""
    tree = _tree(
        ("body", ("c-body",), -1),
        ("textarea", ("c-scout-form__body",), 0),
    )
    assert revealed_text_fields(tree, subtree_sizes(tree), ()) == ()


# --- 実測6回目: 「現れた領域」がページ全体になっていた --------------------------


def _big_tree(*rows: tuple[str, tuple[str, ...], int], filler_parent: int = 0) -> DomTree:
    """REGION_MIN_NODES を超える大きさの木 (割合の規則が効く大きさ)。

    詰め物を ``filler_parent`` にぶら下げることで、「どの部分木がページの大半を
    占めるか」をテストごとに決められる。
    """
    nodes = list(rows)
    while len(nodes) < 60:
        nodes.append(("span", (f"c-filler-{len(nodes)}",), filler_parent))
    return _tree(*nodes)


def test_a_region_that_spans_the_whole_page_is_not_a_region() -> None:
    """**body にクラスが付いただけのものを領域にしない。**

    実測6回目: スカウトのサイドカバーが開いたとき
    ``body.c-body--fixed-by-sidecover`` が増えた。body の部分木はページ全体なので、
    そこを領域として探索すると画面中のボタンを片端から押すことになる。実際そうなり、
    最後は別画面へ遷移して探索が終わった。
    """
    tree = _big_tree(
        ("body", ("c-body", "c-body--fixed-by-sidecover"), -1),  # 0
        ("div", ("c-side-cover",), 0),  # 1
        ("button", ("c-side-cover__send",), 1),  # 2
    )
    sizes = subtree_sizes(tree)

    # body は落ちる。サイドカバーは残る。
    assert 0 not in region_roots(tree, sizes, ("body.c-body--fixed-by-sidecover",))
    assert region_roots(tree, sizes, ("div.c-side-cover",)) == (1,)

    controls = revealed_controls(tree, sizes, ("body.c-body--fixed-by-sidecover",))
    assert controls == (), "ページ全体を領域として押しに行っている"


def test_a_wrapper_that_holds_most_of_the_page_is_not_a_region() -> None:
    """body でなくても、ほとんど全部を含むものは領域ではない。"""
    tree = _big_tree(
        ("body", ("c-body",), -1),  # 0
        ("div", ("c-app-wrapper",), 0),  # 1  ここに詰め物がぶら下がる
        ("button", ("c-somewhere",), 1),  # 2
        filler_parent=1,
    )
    sizes = subtree_sizes(tree)
    assert region_roots(tree, sizes, ("div.c-app-wrapper",)) == ()


def test_the_share_rule_does_not_apply_to_a_small_tree() -> None:
    """**小さな木で「半分以上」と言っても何も言っていない。**

    数個しか要素が無い画面では、正当な領域が簡単に半分を超える。この規則が
    防ぎたいのは「押せるものを片端から押す」ことなので、押すものが沢山ある木で
    だけ意味を持つ。
    """
    tree = _tree(
        ("body", ("c-body",), -1),
        ("div", ("c-sticky-scout-bar",), 0),
        ("button", ("c-sticky-scout-bar__scout-button",), 1),
    )
    sizes = subtree_sizes(tree)
    assert region_roots(tree, sizes, ("div.c-sticky-scout-bar",)) == (1,)


def test_the_tour_guide_is_never_a_region_to_explore() -> None:
    """ツアー案内は閉じる対象であって、ドロワーでも送信フォームでもない。"""
    tree = _big_tree(
        ("body", ("c-body",), -1),
        ("div", ("c-tour-guide",), 0),
        ("button", ("c-button",), 1),
    )
    sizes = subtree_sizes(tree)
    assert region_roots(tree, sizes, ("div.c-tour-guide",)) == ()
    assert revealed_controls(tree, sizes, ("div.c-tour-guide",)) == ()


# --- 実測9回目: 探索がヘッダのログアウトリンクを押した --------------------------


def test_a_token_that_already_existed_is_not_a_revealed_region() -> None:
    """**押しに行く先は「前には1つも無かった構造」に限る。**

    実測9回目の事故そのもの。``a.c-link`` のように画面の至る所に在るトークンは、
    押した結果どこかで1つ増えれば「増えた」に入る。それを領域として扱うと、
    ページ中の ``a.c-link`` がぜんぶ根になり、探索は画面全体へ散る。
    実際にヘッダのログアウトリンクを押し、セッションが切れて終わった。
    """
    before = {"a.c-link": 12, "div.c-card": 25}
    after = {"a.c-link": 13, "div.c-card": 25, "div.c-side-cover__body": 1}

    # 報告用: 何が変わったかの事実。**両方入る。**
    assert opened_region(before, after) == ("a.c-link", "div.c-side-cover__body")
    # 探索用: 押して初めて生まれたものだけ。
    assert newly_present(before, after) == ("div.c-side-cover__body",)


def test_a_token_that_vanished_is_not_newly_present() -> None:
    before = {"div.u-is-hidden": 3}
    after = {"div.u-is-hidden": 0, "div.c-modal__body": 1}
    assert newly_present(before, after) == ("div.c-modal__body",)


def test_the_logout_link_is_never_a_candidate() -> None:
    """**押せば偵察そのものが終わる部品は、順序ではなく除外で扱う。**

    遮断は送信を止めるが、ログアウトは止めない。
    """
    tree = _tree(
        ("body", ("c-body",), -1),  # 0
        ("div", ("c-search-member-card",), 0),  # 1
        ("button", ("c-scout-send",), 1),  # 2
        ("a", ("c-link", "c-link--alert", "c-header-menu__logout-link"), 1),  # 3
    )
    sizes = subtree_sizes(tree)

    candidates = card_action_candidates(tree, sizes, 1)
    selectors = [c.selector() for c in candidates]

    assert not any("logout" in s for s in selectors), f"ログアウトを押しに行く: {selectors}"
    # 送信を名乗る部品は **除外しない** (遮断があるので押して正体を見る)。
    assert any("c-scout-send" in s for s in selectors)


def test_a_revealed_region_also_excludes_the_logout_link() -> None:
    tree = _tree(
        ("body", ("c-body",), -1),
        ("div", ("c-side-cover",), 0),
        ("a", ("c-header-menu__logout-link",), 1),
        ("button", ("c-side-cover__send",), 1),
    )
    sizes = subtree_sizes(tree)

    selectors = [c.selector() for c in revealed_controls(tree, sizes, ("div.c-side-cover",))]

    assert not any("logout" in s for s in selectors)
    assert any("c-side-cover__send" in s for s in selectors)


# --- 実測10回目: 現れた領域の中では、送信を名乗るものを先に押す ------------------


def test_inside_a_revealed_region_the_send_control_comes_first() -> None:
    """**現れた領域の中では、送信を名乗る部品こそ探しているものである。**

    カードの中とは逆順になる。カードでは「安全そうな方から押せばドロワーが先に
    開くかもしれない」に意味があったが、開いた先では違う -- 後ろへ回すと、
    無関係な部品を押しているうちに上限や画面遷移に当たって到達しない
    (実測8〜10回目はいずれもそれで終わった)。

    安全性の緩和ではない。遮断は武装したままで、スカウト送信は GraphQL の
    ``mutation`` なので通す条件に当たらない。
    """
    tree = _tree(
        ("body", ("c-body",), -1),  # 0
        ("div", ("c-side-cover",), 0),  # 1
        ("a", ("c-side-cover__close-btn",), 1),  # 2
        ("button", ("c-side-cover__scout-send",), 1),  # 3
        ("button", ("c-side-cover__cancel",), 1),  # 4
    )
    sizes = subtree_sizes(tree)

    order = [c.selector() for c in revealed_controls(tree, sizes, ("div.c-side-cover",))]

    assert "scout-send" in order[0], f"送信を名乗る部品が先頭でない: {order}"


def test_inside_a_card_the_send_control_still_comes_last() -> None:
    """カードの中の順序は **変えていない**。"""
    tree = _tree(
        ("body", ("c-body",), -1),
        ("div", ("c-search-member-card",), 0),
        ("button", ("c-button", "js-tour-guide-scout-button"), 1),
        ("button", ("c-button", "c-button--small"), 1),
    )
    sizes = subtree_sizes(tree)

    order = [c.selector() for c in card_action_candidates(tree, sizes, 1)]

    assert "scout" in order[-1], f"カードの中で送信部品が先に来ている: {order}"


# --- 実測11回目: 送信フォームまで到達したのに、閉じるボタンを押して全部閉じた ----


def test_a_closing_control_is_never_explored() -> None:
    """**閉じる操作は探索の逆向きである。**

    実測11回目、探索は送信フォームまで到達し、目印の書き込みにも成功した。その
    直後に ``a.c-modal__closer`` を押し、486種の構造が一度に消えた -- 開いたものが
    全部閉じた。以降の押下は全て「要素が無い」で満了して終わった。

    順序を下げるだけでは足りない。順序が下がっても、いずれ押せば同じことが起きる。
    """
    tree = _tree(
        ("body", ("c-body",), -1),
        ("div", ("c-side-cover",), 0),
        ("a", ("c-modal__closer",), 1),
        ("button", ("c-side-cover__cancel",), 1),
        ("button", ("c-button--important",), 1),
    )
    sizes = subtree_sizes(tree)

    selectors = [c.selector() for c in revealed_controls(tree, sizes, ("div.c-side-cover",))]

    assert not any("closer" in s for s in selectors), f"閉じる部品を押しに行く: {selectors}"
    assert not any("cancel" in s for s in selectors), f"取り消す部品を押しに行く: {selectors}"
    # 送信らしき部品は残る。
    assert any("important" in s for s in selectors)


def test_close_candidates_are_still_collected_for_the_coordinate() -> None:
    """押さないだけで、**候補としては拾う**。座標はこれで埋める (探索の後で試す)。"""
    tree = _tree(
        ("body", ("c-body",), -1),
        ("div", ("c-side-cover",), 0),
        ("a", ("c-side-cover__close-btn",), 1),
    )
    sizes = subtree_sizes(tree)

    assert close_candidates_in(tree, sizes, ("div.c-side-cover",)) == ("a.c-side-cover__close-btn",)
