"""Pure judgments about the shape of the scout **message form**.

:mod:`recon.open_structure` は「どこを押せば何かが開くか」を扱う -- 導線が
分からないうちの、目隠しの探索のための規則である。このモジュールは **導線が
分かったあと** の規則を扱う。運用者が実画面を示してくれたので、通る道はもう
探さなくてよい。

    一覧の「スカウトを送る」
      → 送信フォーム (左に経歴、右に入力欄)
        → **必須** スカウト対象求人 (入力すると候補が出る。1件選ぶ)
        → メッセージテンプレート (任意)
        → **必須** 本文
      → 「確認してスカウトを送る」
        → 確認の段
          → 「この内容でスカウトを送る」  ← ここで初めて送信が発火する

**それでも文言では指さない** (13.2)。ここに書く規則はすべてタグ名・クラス名・
親子関係だけを見る。画面の文言を条件に使うと、媒体が言い回しを変えた日に静かに
空振りし、しかもログには何も残らない (原則2)。

順序が仕様である理由も、この形から出る。求人を選ばなければテンプレートは効かず、
テンプレートが本文を埋めるので、**目印は最後に書かなければ上書きされる**。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from jobmedley_scout.browser.dom import DomTree
from jobmedley_scout.recon.list_structure import stable_tokens
from jobmedley_scout.recon.open_structure import (
    SEND_CLASS_HINTS,
    ActionCandidate,
    is_closing,
    is_disabled,
    is_forbidden,
    region_roots,
)

#: 本文欄のタグ。**``input`` を含めない。** 実測13回目、求人検索のサジェスト欄
#: (``input``) に本文用のダミー文を書き込み、54回の検索を空振りさせたうえで
#: 求人を1件も選べなかった。本文は ``textarea`` である。
BODY_TAGS: frozenset[str] = frozenset({"textarea"})

#: 求人を選ぶ欄のタグ。運用者の画面では「都道府県・施設名・募集職種をスペース
#: 区切りで検索」という1行の入力欄である。
QUERY_TAGS: frozenset[str] = frozenset({"input"})

#: 検索欄では **ありえない** ``input`` のクラス片。
#:
#: 実測18回目、フォームの中の ``input`` を順に押していったら、2番目と3番目は
#: ``input.c-checkbox__input`` -- 送信先のチェックボックスだった。押しても候補は
#: 出ないし、外せば送信先が消える。**検索欄を探しているのに、当たったら困るもの
#: まで順番待ちに並べていた。**
#:
#: これは安全弁ではなく **的の絞り込み** である。本当の判定はブラウザ側で
#: ``el.type`` を読んで行う (クラス名は媒体の都合で変わるが、``type`` は
#: HTML の意味そのものなので変わらない)。ここはその前段の粗い篩である。
NON_QUERY_CLASS_HINTS: tuple[str, ...] = ("checkbox", "radio", "hidden", "file", "toggle")

#: 押して先へ進める部品のタグ。
SUBMIT_TAGS: frozenset[str] = frozenset({"button", "a"})

#: 候補一覧の項目になりうるタグ。**優先順に並んでいる。**
#:
#: サジェストの1件は ``li`` であることが多いが、``a`` や ``div`` で組まれること
#: もある。全部を同列に扱うと、項目ではなく **項目を包む器** を押しうる。器を
#: 押しても値は入らないので、押せたのに進まないという分かりにくい失敗になる。
#: そこで「より項目らしいタグ」から順に探し、**最初に見つかった種類だけ** を使う。
SUGGESTION_TAGS: tuple[str, ...] = ("li", "a", "button", "div")


def _in_subtree(
    tree: DomTree, sizes: Sequence[int], root: int, tags: Iterable[str]
) -> tuple[ActionCandidate, ...]:
    """Nodes of the given tags inside one subtree, in document order. **Pure.**"""
    wanted = frozenset(tags)
    found: list[ActionCandidate] = []
    for index in range(root, root + sizes[root]):
        node = tree.nodes[index]
        if node.tag not in wanted:
            continue
        blob = " ".join(node.class_names).lower()
        found.append(
            ActionCandidate(
                index=index,
                tag=node.tag,
                tokens=stable_tokens(node.tag, node.class_names),
                looks_like_send=any(hint in blob for hint in SEND_CLASS_HINTS),
            )
        )
    return tuple(found)


def _collect(
    tree: DomTree, sizes: Sequence[int], region_tokens: Iterable[str], tags: Iterable[str]
) -> tuple[ActionCandidate, ...]:
    """Nodes of the given tags inside the region, deduplicated. **Pure.**"""
    seen: set[int] = set()
    found: list[ActionCandidate] = []
    for root in region_roots(tree, sizes, region_tokens):
        for candidate in _in_subtree(tree, sizes, root, tags):
            if candidate.index in seen:
                continue
            seen.add(candidate.index)
            found.append(candidate)
    return tuple(sorted(found, key=lambda c: c.index))


def form_root(tree: DomTree, sizes: Sequence[int], region_tokens: Iterable[str]) -> int | None:
    """The smallest node in the region that holds **both a body field and a button**.

    **フォームであることを、文言ではなく形で決める。** 「本文欄がある」だけでは
    検索欄と区別が付かず、「ボタンがある」だけならどの画面にも当てはまる。
    両方を含む最小の部分木は、送信フォームか、それを包む器のどちらかである。

    最小を採るのは、器を採ると **フォームの外の押せるもの** まで候補に入るから
    である。実測6回目、``body`` を領域として扱ったせいで画面中のボタンを片端から
    押し、最後は別画面へ遷移して探索が終わった。

    見つからなければ ``None``。**「たぶんこれだろう」を返さない** (原則3)。
    """
    bodies = {c.index for c in _collect(tree, sizes, region_tokens, BODY_TAGS)}
    if not bodies:
        return None
    buttons = {c.index for c in _collect(tree, sizes, region_tokens, SUBMIT_TAGS)}
    if not buttons:
        return None

    best: int | None = None
    for root in range(len(tree.nodes)):
        span = range(root, root + sizes[root])
        if not any(index in bodies for index in span):
            continue
        if not any(index in buttons for index in span):
            continue
        if best is None or sizes[root] < sizes[best]:
            best = root
    return best


def body_fields_in(tree: DomTree, sizes: Sequence[int], root: int) -> tuple[ActionCandidate, ...]:
    """The body fields inside the form, in document order. **Pure.**"""
    return _in_subtree(tree, sizes, root, BODY_TAGS)


def query_fields_in(tree: DomTree, sizes: Sequence[int], root: int) -> tuple[ActionCandidate, ...]:
    """The one-line **search** inputs inside the form, in document order. **Pure.**

    運用者の画面では、この欄はただ1つ (スカウト対象求人) である。複数あったら
    順に試す -- どれが求人の欄かは、触って候補が出たかどうかでしか分からない。

    ただし :data:`NON_QUERY_CLASS_HINTS` に当たるものは **最初から外す**。
    実測18回目、チェックボックスの ``input`` まで順番待ちに並び、「候補が出ない」
    という同じ失敗を3回繰り返して報告を水増ししていた。試す価値の無いものを
    試すのは、探索ではなく雑音である。
    """
    return tuple(
        candidate
        for candidate in _in_subtree(tree, sizes, root, QUERY_TAGS)
        if not any(
            hint in token.lower() for token in candidate.tokens for hint in NON_QUERY_CLASS_HINTS
        )
    )


def submit_candidates_in(
    tree: DomTree, sizes: Sequence[int], root: int
) -> tuple[ActionCandidate, ...]:
    """Buttons that move the form forward, **the送信らしいものから順に**. Pure.

    **:func:`~recon.open_structure._safest_first` とは逆順である。** 向きが逆な
    のには理由がある。

    あちらは *導線が分からない* 探索なので、送信らしい部品を最後に回す (先に
    押すと、まだ埋めていないフォームを送信しようとして弾かれる。実測12回目で
    実際にそうなった)。

    こちらは *導線が分かっている* 手順である。求人を選び、本文を書き、**そのうえで**
    この関数を呼ぶ。埋め終わったフォームに対しては、送信らしい部品こそが次の
    一手であって、後回しにする理由が無い。

    除くものは同じ:

    - 閉じる部品 -- 手順の逆向き (実測11回目、送信画面まで来て閉じた)
    - 無効な部品 -- 押しても何も起きず、満了ぶんの時間だけ失う
    - 危険な部品 -- ログアウト等 (実測9回目、押してセッションが死んだ)
    """
    allowed = [
        candidate
        for candidate in _in_subtree(tree, sizes, root, SUBMIT_TAGS)
        if not is_forbidden(candidate) and not is_closing(candidate) and not is_disabled(candidate)
    ]
    return tuple(sorted(allowed, key=lambda c: (not c.looks_like_send, c.index)))


def disabled_submits_in(
    tree: DomTree, sizes: Sequence[int], root: int
) -> tuple[ActionCandidate, ...]:
    """Buttons that are present but disabled. **押せないことも観測である。**

    運用者の画面では、必須欄が埋まるまで「確認してスカウトを送る」は無効である。
    つまり **無効なままなら、埋めたつもりの欄が埋まっていない**。
    「押せる部品が無かった」とだけ報告すると、この区別が消える。
    """
    return tuple(
        candidate
        for candidate in _in_subtree(tree, sizes, root, SUBMIT_TAGS)
        if is_disabled(candidate)
    )


def suggestion_items_in(
    tree: DomTree, sizes: Sequence[int], region_tokens: Iterable[str]
) -> tuple[ActionCandidate, ...]:
    """The items of a suggestion list that just appeared, in document order. **Pure.**

    :data:`SUGGESTION_TAGS` を **優先順に** 見て、最初に見つかった種類だけを返す。
    種類を混ぜると項目とその器が同じ列に並び、器を押して「押せたのに何も入らない」
    という分かりにくい失敗になる。

    候補が無ければ空。空であることは「候補が出なかった」という観測であって、
    別のものを押しに行く理由にはならない。
    """
    for tag in SUGGESTION_TAGS:
        if found := _collect(tree, sizes, region_tokens, (tag,)):
            return found
    return ()


def confirm_root(tree: DomTree, sizes: Sequence[int], region_tokens: Iterable[str]) -> int | None:
    """The smallest region that holds a pressable送信らしいボタン. **Pure.**

    確認の段には本文欄が無い -- 書いた内容を **読ませる** 段だからである。だから
    :func:`form_root` (本文欄とボタンの両方を要求する) では見つからない。ここで
    探すのは「押して先へ進める送信らしいボタンを含む、いちばん小さい領域」である。

    見つからなければ ``None``。確認の段が無い作り (押したら即座に送る) もありうる
    ので、**無いことは失敗ではない**。呼ぶ側はそれを踏まえて扱う。
    """
    best: int | None = None
    for root in region_roots(tree, sizes, region_tokens):
        pressable = any(c.looks_like_send for c in submit_candidates_in(tree, sizes, root))
        if pressable and (best is None or sizes[root] < sizes[best]):
            best = root
    return best
