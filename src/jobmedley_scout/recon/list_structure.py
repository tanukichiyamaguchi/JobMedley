"""Identify the candidate list's structure from a DOM tree. **All pure** (13.4).

このモジュールは、実際に起きた誤りを直すために作られた。

``scout recon observe-list`` は座標 ``nav.list_ready_selector`` に **``body.c-body``**
を推奨した。これは全ページに常時ある枠なので、``wait_for_selector`` が常に即座に
成功し、**一覧が描画される前に走査して0件と読む** 経路を作る (原則2)。

原因は述語にあった。旧実装は「結果ページと0件ページの **両方に存在する**」
トークンを候補にしていた。ヘッダもサイドバーもフッタも body も、画面の枠は
すべてこの条件を満たす -- 実測で 278 トークンが合格した。順位付けをどう直しても、
先頭の枠を落とせば次の枠が繰り上がるだけである。

    **順位付けは排除ではない。** (docs/incidents.md「常に真になる目印を3度作り込んだ」)

述語を反転する
--------------

出力に現れるトークンは、次の **どちらか一方だけ** を満たす。

* **行側**  : 結果ページに2個以上あり、使えたすべての0件ページで **0個**
* **0件側** : 結果ページに **0個** で、使えたすべての0件ページに1個以上ある

両ページに存在するトークンは、値にも別案にも参考にも一度も現れない。
``body.c-body`` は両方に1個ずつあるので **構造的に落ちる**。同じ理由で
``div.o-wrapper`` / ``header.c-header`` / ``main.o-main`` / ``div.o-content`` /
``div.js-infinity-scroll-outer-el`` も落ちる。250行の別案が消えるのは順位の調整では
なく、述語が反転したからである。

値は ``"<行トークン>, <0件表示トークン>"`` という CSS セレクタリスト
(カンマ = 論理和) にする。**一覧が用意できた = 行が出た、または0件表示が出た。**
どちらも描画前には存在しない。

なぜ両方向に安全か
------------------

* **早すぎる方向 (静かなゼロ件)**: 行トークンは行そのものなので、行が0個の時点で
  一致することは原理的にない
* **遅すぎる方向 (0件を未描画と誤読)**: 0件表示トークンは **実際の0件ページで
  存在を観測した** ものだけを使う
* 万一0件表示が本番の描画と食い違っても、失敗は待機の満了 = **見える失敗** になる。
  **この設計の誤りは全部うるさい側に倒れる**

木は「選ぶため」には使わない
----------------------------

祖先関係を値の選定に使うと、鎖の上に載っている枠 (``div.o-content__inner`` など) が
そのまま候補になる。だから木の用途を3つに限定する。

1. **行の同定** -- 極大な繰り返し兄弟群 (最多出現ではない)
2. **0件表示の探索範囲の限定** -- アンカーの内側だけを見る
3. **クリック対象の安全確保** -- 操作部品を含まない領域を選ぶ

**名前から意味を読まない。** ``list`` / ``result`` / ``outer`` / ``scroll`` のような
語彙表は持たない。判断材料は木の形と、2枚以上のページでの件数だけである (原則3)。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from jobmedley_scout.browser.dom import DomTree
from jobmedley_scout.recon.manual_login import is_stable_class_name

#: 繰り返しとみなす最小の兄弟数。
MIN_ROW_GROUP_SIZE = 2

#: 別案の上限。250行の一覧は読めない。
MAX_ALTERNATIVES = 3

#: クリックすると何かが起きうる要素。**この中を押さない。**
#: 実測の行の中には ``button.js-tour-guide-scout-button`` があり、スカウト送信
#: そのものの可能性がある。取り消せない外向き操作を偵察で踏むわけにいかない。
CONTROL_TAGS = frozenset(
    {"a", "button", "input", "label", "select", "textarea", "summary", "details"}
)


# --- 木の基本演算 -------------------------------------------------------------


def stable_tokens(tag: str, class_names: Iterable[str]) -> tuple[str, ...]:
    """``tag.class`` 形式の安定なトークン。ハッシュ的な名前は捨てる。"""
    return tuple(f"{tag}.{name}" for name in class_names if is_stable_class_name(name))


def subtree_sizes(tree: DomTree) -> tuple[int, ...]:
    """Number of nodes in each subtree, itself included.

    前順採番なので親は必ず子より小さい添字を持つ。後ろから1回舐めれば足りる。
    """
    size = [1] * len(tree.nodes)
    for index in range(len(tree.nodes) - 1, 0, -1):
        size[tree.nodes[index].parent] += size[index]
    return tuple(size)


def contains(sizes: Sequence[int], ancestor: int, node: int) -> bool:
    """Whether ``node`` sits inside ``ancestor``'s subtree (itself counts).

    前順採番だと子孫がちょうど区間 ``[i, i+size[i])`` を占めるので、整数比較2回で済む。
    """
    return ancestor <= node < ancestor + sizes[ancestor]


def ancestors_or_self(tree: DomTree, index: int) -> tuple[int, ...]:
    """From ``index`` up to the root."""
    chain: list[int] = []
    current = index
    while current != -1:
        chain.append(current)
        current = tree.nodes[current].parent
    return tuple(chain)


def lowest_common_ancestor(tree: DomTree, sizes: Sequence[int], indices: Sequence[int]) -> int:
    """The deepest node containing every index. ``-1`` when ``indices`` is empty."""
    if not indices:
        return -1
    for candidate in ancestors_or_self(tree, indices[0]):
        if all(contains(sizes, candidate, other) for other in indices):
            return candidate
    return 0  # pragma: no cover - 根は必ず全部を含む


def token_counts(tree: DomTree) -> dict[str, int]:
    """How many times each stable ``tag.class`` token appears."""
    counts: dict[str, int] = {}
    for node in tree.nodes:
        for token in stable_tokens(node.tag, node.class_names):
            counts[token] = counts.get(token, 0) + 1
    return counts


def indices_with_token(tree: DomTree, token: str) -> tuple[int, ...]:
    """Every node carrying ``token``, in document order."""
    return tuple(
        index
        for index, node in enumerate(tree.nodes)
        if token in stable_tokens(node.tag, node.class_names)
    )


# --- 行の同定 -----------------------------------------------------------------


@dataclass(frozen=True)
class RowGroup:
    """Sibling elements repeating under one parent, keyed by a single token."""

    token: str
    parent: int
    members: tuple[int, ...]
    #: 群の要素の部分木サイズ合計。「数だけ多い薄い群」に負けないための順位キー。
    subtree_total: int


def repeated_child_groups(tree: DomTree, sizes: Sequence[int]) -> tuple[RowGroup, ...]:
    """Groups of >= 2 siblings sharing one token, under the same parent.

    **群のキーをクラス集合ではなく1トークンにする。** 実測では
    ``div.c-search-member-card`` と ``div.c-search-member-card--scouted`` の両方が
    あり、クラス集合で群を作ると25枚が18枚と7枚に割れる。修飾クラスで行が
    分裂すると、行の同定そのものが失敗する。
    """
    buckets: dict[tuple[int, str], list[int]] = {}
    for index, node in enumerate(tree.nodes):
        if node.parent == -1:
            continue
        for token in stable_tokens(node.tag, node.class_names):
            buckets.setdefault((node.parent, token), []).append(index)

    groups = [
        RowGroup(
            token=token,
            parent=parent,
            members=tuple(members),
            subtree_total=sum(sizes[m] for m in members),
        )
        for (parent, token), members in buckets.items()
        if len(members) >= MIN_ROW_GROUP_SIZE
    ]
    return tuple(sorted(groups, key=lambda g: g.members[0]))


def maximal_groups(groups: Sequence[RowGroup], sizes: Sequence[int]) -> tuple[RowGroup, ...]:
    """Drop groups whose parent sits inside another group's member.

    **「最多出現ではダメだった」への直接の答え。** 実測では1枚のカードに10個ある
    ``p.c-search-member-card-text`` (計250個) が、25枚しかないカード本体
    ``div.c-search-member-card`` に件数で勝ち、文字要素をクリックして
    「何も開かなかった」で終わった。文字要素の親はカードの子孫なので、ここで落ちる。

    **極大性は0件フィルタの *後* に掛ける。順序を逆にすると壊れる。**

    当初は「先に極大性、後で0件フィルタ」にしていた。実測で破綻した --
    ``div.c-segment`` が画面に2つあり (検索条件パネルと一覧を囲む区画)、カードの親は
    その内側なので **カード群が極大でなくなって落ちた**。結果、行が
    ``div.c-segment`` (2個) と誤判定され、0件ページの検査もその誤った行で行われて
    2枚とも捨てられ、何も確定しなかった。

    この規則は「内側を落とす」ので、素朴に全群へ掛けると **最も外側の繰り返し群
    だけが残る**。一覧と無関係な外側の繰り返し (ページの区画・2カラムのレイアウト)
    があれば必ずそれが勝つ。

    先に「0件検索で消える」で絞れば、一覧と無関係な群はそこで落ちる
    (``div.c-segment`` は0件ページにも残るので消えない)。残った中で極大を取れば、
    カードが文字要素に勝つ -- これが本来欲しかった比較である。
    """
    return tuple(
        group
        for group in groups
        if not any(
            other is not group and any(contains(sizes, m, group.parent) for m in other.members)
            for other in groups
        )
    )


def row_group_candidates(
    tree: DomTree,
    sizes: Sequence[int],
    zero_counts: Sequence[Mapping[str, int]],
) -> tuple[RowGroup, ...]:
    """Repeating groups that vanish on every usable zero-result page, then maximal.

    **この順序が要点である** (:func:`maximal_groups` の docstring を参照)。
    先に極大性を掛けると、一覧と無関係な外側の繰り返し群が必ず勝ってしまう。

    ``zero_counts`` が空 (0件ページを作れなかった) なら消える群を絞れないので、
    全群に対して極大性を掛ける。これは実測で破綻した順序と同じなので、
    **行を誤る可能性がある**。呼び出し側は「消えることは確認していない」と
    明記すること -- 確認できていない値を、確認したものと同じ顔で出さない。
    """
    vanishing = [
        group
        for group in repeated_child_groups(tree, sizes)
        if all(counts.get(group.token, 0) == 0 for counts in zero_counts)
    ]
    live = list(maximal_groups(vanishing, sizes))
    # 部分木の重い群を優先する。``<br class="x">`` を30個並べたような薄い群に
    # 負けないため。順位を誤っても **安全側** である -- どの行トークンも
    # 「0件ページに存在しない」ことを観測済みなので、枠になることはない。
    return tuple(sorted(live, key=lambda g: (-g.subtree_total, -len(g.members), g.members[0])))


# --- 一覧領域のアンカー (探索範囲を切るためだけに使う) --------------------------


@dataclass(frozen=True)
class ListRegion:
    """Where the rows live. **Never used as a value** -- only to scope the search."""

    container: int
    container_tokens: tuple[str, ...]
    anchor_token: str
    #: 行トークンの全一致数 - 群の要素数。0より大きいなら一覧の外にも行がある。
    rows_outside_group: int


def list_region(
    tree: DomTree,
    sizes: Sequence[int],
    counts: Mapping[str, int],
    group: RowGroup,
) -> ListRegion | None:
    """The rows' common ancestor, and the deepest ancestor token unique on this page.

    ``html`` / ``body`` で打ち切るのは、``body.c-body`` をアンカーにすると
    「アンカーの内側」が全画面になり、範囲の限定が無意味になるから。
    **値には使わないので、ここで枠が出ても害はない** -- 探索範囲が広がるだけである。
    """
    container = lowest_common_ancestor(tree, sizes, group.members)
    if container == -1:
        return None
    anchor = ""
    for index in ancestors_or_self(tree, container):
        node = tree.nodes[index]
        if node.tag in ("html", "body"):
            break
        for token in stable_tokens(node.tag, node.class_names):
            if counts.get(token, 0) == 1:
                anchor = token
                break
        if anchor:
            break
    if not anchor:
        return None
    return ListRegion(
        container=container,
        container_tokens=stable_tokens(
            tree.nodes[container].tag, tree.nodes[container].class_names
        ),
        anchor_token=anchor,
        rows_outside_group=counts.get(group.token, 0) - len(group.members),
    )


# --- 0件表示の同定 ------------------------------------------------------------


@dataclass(frozen=True)
class EmptyCandidate:
    """A token seen only on the zero-result page, inside the list region."""

    token: str
    depth_from_anchor: int
    counts_zero: tuple[int, ...]
    #: ``"region"`` (アンカーの内側) か ``"page"`` (画面全体に落とした)。
    scope: str


def empty_state_candidates(
    zero_tree: DomTree,
    zero_sizes: Sequence[int],
    results_counts: Mapping[str, int],
    anchor_token: str,
) -> tuple[EmptyCandidate, ...]:
    """Tokens present on the zero-result page but absent from the results page.

    アンカーが0件ページに **ちょうど1個** あればその部分木だけを見る。0個か2個以上なら
    画面全体に落とし、``scope="page"`` で明示する (黙って広げない)。

    並びは (アンカーからの相対深さ, 0件ページでの件数, 文書順)。**最も外側の新出要素が
    先頭** = 0件表示のブロック本体で、内側の文字要素は後ろへ回る。件数最少 + 辞書順の
    タイブレークは使わない -- 辞書順は推測ですらなく乱択であり、ロードごとに出没する
    装飾を先頭に据えうる。
    """
    anchors = indices_with_token(zero_tree, anchor_token) if anchor_token else ()
    if len(anchors) == 1:
        root, scope = anchors[0], "region"
    else:
        root, scope = 0, "page"

    seen: dict[str, tuple[int, int]] = {}
    counts: dict[str, int] = {}
    for index in range(root, root + zero_sizes[root]):
        node = zero_tree.nodes[index]
        depth = len(ancestors_or_self(zero_tree, index)) - len(ancestors_or_self(zero_tree, root))
        for token in stable_tokens(node.tag, node.class_names):
            counts[token] = counts.get(token, 0) + 1
            if token not in seen:
                seen[token] = (depth, index)

    fresh = [
        EmptyCandidate(
            token=token,
            depth_from_anchor=depth,
            counts_zero=(counts[token],),
            scope=scope,
        )
        for token, (depth, _order) in seen.items()
        if results_counts.get(token, 0) == 0
    ]
    return tuple(
        sorted(fresh, key=lambda c: (c.depth_from_anchor, c.counts_zero[0], seen[c.token][1]))
    )


# --- クリック対象の安全確保 ----------------------------------------------------


def safe_click_index(tree: DomTree, sizes: Sequence[int], row: int) -> int | None:
    """The largest descendant of ``row`` whose subtree holds no control element.

    Playwright は要素の中心を押すので、行を素朴にクリックすると中心を覆う子が
    スカウトボタンを受け取りうる。「操作部品を含まない領域」は包含関係だけで
    書けるので、語彙の当てずっぽうに頼らず安全側へ倒せる。

    指せない (安定トークンを1つも持たない) 節点は対象にしない。
    見つからなければ ``None`` -- そのときは **クリックしない**。
    """
    best: int | None = None
    for index in range(row, row + sizes[row]):
        node = tree.nodes[index]
        if not stable_tokens(node.tag, node.class_names):
            continue
        subtree = range(index, index + sizes[index])
        if any(tree.nodes[k].tag in CONTROL_TAGS for k in subtree):
            continue
        if best is None or sizes[index] > sizes[best]:
            best = index
    return best


def click_locator(tree: DomTree, target: int) -> tuple[str, int] | None:
    """``(css, nth)`` pointing at exactly ``target``, or ``None``.

    素朴に ``page.click(token)`` すると Playwright の非strict先頭一致になり、
    解析した節点と押した要素が別物になりうる。それでは「開かなかった」が
    何の観測なのか分からなくなる。文書順の位置まで指定して同一性を保つ
    (``querySelectorAll`` も前順DFSもどちらも文書順)。

    トークンは **ページ全体の一致数が最小のもの** を選ぶ。名前の意味は見ない --
    件数は観測できる。
    """
    node = tree.nodes[target]
    tokens = stable_tokens(node.tag, node.class_names)
    if not tokens:
        return None
    counts = token_counts(tree)
    token = min(tokens, key=lambda t: (counts.get(t, 0), t))
    matches = indices_with_token(tree, token)
    if target not in matches:
        return None  # pragma: no cover - defensive
    return token, matches.index(target)


# --- 値の組み立て (唯一の出口) -------------------------------------------------


@dataclass(frozen=True)
class ReadyValue:
    """``nav.list_ready_selector`` の値。行 **または** 0件表示で成立する。"""

    row_token: str
    empty_token: str

    def selector(self) -> str:
        """CSS セレクタリスト。カンマは論理和なので「どちらかが出たら」になる。"""
        return f"{self.row_token}, {self.empty_token}"


def _row_side_holds(
    token: str, results: Mapping[str, int], zeros: Sequence[Mapping[str, int]]
) -> bool:
    return results.get(token, 0) >= MIN_ROW_GROUP_SIZE and all(z.get(token, 0) == 0 for z in zeros)


def _empty_side_holds(
    token: str, results: Mapping[str, int], zeros: Sequence[Mapping[str, int]]
) -> bool:
    return results.get(token, 0) == 0 and bool(zeros) and all(z.get(token, 0) >= 1 for z in zeros)


def ready_values(
    rows: Sequence[RowGroup],
    empties: Sequence[EmptyCandidate],
    results_counts: Mapping[str, int],
    zero_counts: Sequence[Mapping[str, int]],
) -> tuple[ReadyValue, ...]:
    """The recommended value and up to :data:`MAX_ALTERNATIVES` alternatives.

    **ここが唯一の出口であり、不変条件を強制する唯一の場所である。** 出す前に
    各トークンが行側か0件側かの **どちらか一方だけ** を満たすことを検査する。
    破れていたら値を出さない -- 破れるのはプログラミングエラーなので、握り潰すと
    ``body.c-body`` の再来になる。
    """
    if not rows or not empties:
        return ()

    pairs: list[ReadyValue] = []
    head_row, head_empty = rows[0], empties[0]
    pairs.append(ReadyValue(head_row.token, head_empty.token))
    for row in rows[1 : 1 + 2]:
        pairs.append(ReadyValue(row.token, head_empty.token))
    for empty in empties[1:2]:
        pairs.append(ReadyValue(head_row.token, empty.token))

    kept: list[ReadyValue] = []
    for value in pairs[: 1 + MAX_ALTERNATIVES]:
        if not _row_side_holds(value.row_token, results_counts, zero_counts):
            continue
        if not _empty_side_holds(value.empty_token, results_counts, zero_counts):
            continue
        kept.append(value)
    return tuple(kept)
