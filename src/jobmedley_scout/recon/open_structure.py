"""段階3の探索を「純粋な判断」と「ブラウザ操作」に割るための、判断の側。

**この工程が何を解こうとしているのか。**

段階2の観測は ``nav.drawer_close_selectors`` を確定できずに終わった (実測5〜7回目)。
理由は語彙不足でも待ち不足でもなく、**構造的な行き止まり** である:

* 候補者ドロワーはカード本体のクリックでは開かない (7回目に確定。ツアーを完走
  させてクリックは完了したが、新出要素は無かった)
* 開くのはカードの中のボタンだが、カードのボタンは2つあり、片方は
  ``button.js-tour-guide-scout-button`` = **スカウト送信そのもの** である
* どちらがどちらかは、文言を読まない限り構造からは決まらない。そして文言は
  実行ログに出せない (13.2) し、文言で分岐すれば媒体の言い回し変更で壊れる

つまり「押してよいボタン」を **観測だけで安全に選ぶことはできない**。

**そこで前提をひっくり返す。** 段階3の偵察は、そもそも「押せば送信されるボタンを
押すための仕組み」である (3章)。送信ボタンを押す直前に fail-closed の遮断を武装し、
非GETを記録して中断する。**遮断を先に武装しておけば、どのボタンを押しても送信は
物理的に起こらない。** 押してよいか分からないボタンは、押せない理由ではなく、
**遮断してから押す理由** になる。

そして中断された非GETは、そのまま段階3の成果物である -- どのボタンが送信路かが、
推測ではなく観測で分かる (``api.send.*``)。ドロワーを開くボタンの方は非GETを
起こさないか、起こしても送信ではないので、**両方が1回の実行で同時に確定する**。

本モジュールはその判断部分だけを持つ。ブラウザには一切触れない (13.4)。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from jobmedley_scout.browser.dom import DomTree
from jobmedley_scout.recon.list_structure import contains, stable_tokens

#: クラス名がスカウト送信を名乗っている部品のトークン。**押す順の判断にのみ使う。**
#: 除外には使わない -- 遮断を武装した状態では、これこそ押して正体を見たい部品である。
SEND_CLASS_HINTS: tuple[str, ...] = ("scout", "send", "offer")

#: 操作部品とみなすタグ。``label`` はチェックボックスの当たり判定を兼ねるので含める。
ACTION_TAGS: frozenset[str] = frozenset({"a", "button", "label", "input", "select", "textarea"})

#: 「現れた領域」の根になれない要素。**ページそのものは領域ではない。**
_PAGE_TAGS: frozenset[str] = frozenset({"html", "body"})

#: 領域として扱ってよい広さの上限 (木全体に対する割合)。
#:
#: capture-open 6回目: スカウトのサイドカバーが開いたとき、増えたトークンに
#: ``body.c-body--fixed-by-sidecover`` が含まれていた。body の部分木は **ページ
#: 全体** なので、そこを領域として探索すると画面中のボタンを片端から押すことに
#: なる -- 実際にそうなり、最後は別画面へ遷移して探索が終わった。
#:
#: 「ほとんど全部を含むもの」は、現れた領域ではなく **ページの状態が変わったこと**
#: を指す目印である。目印は目印として役に立つが、押しに行く先ではない。
REGION_MAX_SHARE = 0.5

#: 広さの割合を持ち出してよい木の大きさ。
#:
#: **小さな木で「半分以上」と言っても何も言っていない。** 数個の要素しか無い
#: 画面では、正当な領域が簡単に半分を超える。この規則が防ごうとしているのは
#: 「画面中の押せるものを片端から押す」ことなので、押すものが沢山ある木でだけ
#: 意味を持つ。下回る木では :data:`_PAGE_TAGS` だけで判断する。
REGION_MIN_NODES = 40

#: ツアー案内。押して現れても **そこは探索先ではない** (閉じる対象であって、
#: ドロワーでも送信フォームでもない)。
_TOUR_TOKEN = "tour-guide"

#: 文字を書き込めるかもしれないタグ。``input`` の種別 (text / checkbox / hidden) は
#: 木からは分からない -- 書き込みは失敗しうるが、**失敗しても何も起きない** ので
#: 種別で絞らずに試す。書き込みは送信ではない。
TEXT_FIELD_TAGS: frozenset[str] = frozenset({"textarea", "input"})

#: URLの中で個人を指しうる部分 (数値ID・長い英数字のトークン)。報告では伏せる (13.2)。
_ID_SEGMENT = re.compile(r"(?<=/)(\d+|[0-9a-f]{8,})(?=/|$)", re.IGNORECASE)
_QUERY_VALUE = re.compile(r"(?<==)[^&]+")


@dataclass(frozen=True)
class ActionCandidate:
    """A clickable inside one card, and how it should be treated.

    ``looks_like_send`` は **クラス名がそう名乗っているか** であって、送信路である
    ことの証明ではない。押す順を決めるためだけに使う (安全そうな方から押して、
    ドロワーが先に開けば送信部品を押さずに済む)。遮断は常に武装しているので、
    この判定が外れても送信は起きない。
    """

    #: 木の中の添字 (前順)。
    index: int
    tag: str
    #: ``tag.class`` 形式の安定トークン。指せない部品は空。
    tokens: tuple[str, ...]
    #: クラス名がスカウト送信を名乗っているか。
    looks_like_send: bool

    def selector(self) -> str:
        """CSS セレクタ。**安定クラスを全部連結する。**

        先頭の1つだけでは足りない。実測のカードのボタン2つは
        ``c-button u-wd-100p c-button--small`` を共有し、違いは
        ``js-tour-guide-scout-button`` の有無だけである -- 先頭トークンで指すと
        ``button.c-button`` になり、**送信ボタンにも一致する**。押し間違いは
        取り消せないので、指す側を最大限に絞る (13.6)。

        連結しても一意とは限らない (汎用ボタンのクラス集合は送信ボタンの部分集合
        でありうる) ので、呼び出し側は文書順の ``nth`` と併用する。
        """
        return self.tag + "".join("." + token.split(".", 1)[1] for token in self.tokens)


def _candidates_in(tree: DomTree, sizes: Sequence[int], root: int) -> list[ActionCandidate]:
    """Clickables inside one subtree, in document order. **Pure.**"""
    found: list[ActionCandidate] = []
    for index in range(root, root + sizes[root]):
        node = tree.nodes[index]
        if node.tag not in ACTION_TAGS:
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
    return found


def region_roots(
    tree: DomTree, sizes: Sequence[int], region_tokens: Iterable[str]
) -> tuple[int, ...]:
    """Indices that a press's revealed region actually starts at. **Pure.**

    増えたトークンに一致する要素のうち、**領域として意味を持つものだけ** を返す。
    落とすのは2種類:

    * ページ全体を覆うもの (``body`` / 木の半分以上を占める部分木)。これは
      「領域が現れた」ではなく「ページの状態が変わった」の目印である
      (:data:`REGION_MAX_SHARE`)
    * ツアー案内。現れても探索先ではない

    落とした結果 **空になることはありうる**。そのときは「押した結果として探索を
    続けられる領域は無かった」が事実であり、無理に広い領域を採らない。
    """
    wanted = {token for token in region_tokens if _TOUR_TOKEN not in token}
    if not wanted:
        return ()
    total = len(tree.nodes)
    limit = total * REGION_MAX_SHARE if total >= REGION_MIN_NODES else float("inf")
    found: list[int] = []
    for index, node in enumerate(tree.nodes):
        if node.tag in _PAGE_TAGS or sizes[index] > limit:
            continue
        if wanted.intersection(stable_tokens(node.tag, node.class_names)):
            found.append(index)
    return tuple(found)


def _safest_first(candidates: Sequence[ActionCandidate]) -> tuple[ActionCandidate, ...]:
    """並びは (送信を名乗るものは後ろ, 文書順)。**除外はしない。**

    遮断を武装した状態では送信部品こそ押して正体を見たい -- 中断された非GETが
    そのまま ``api.send.*`` の観測になる。ここが決めるのは順序だけである。

    **ただし1種類だけ除外する。** 押せば偵察そのものが終わる部品
    (:data:`FORBIDDEN_CLASS_HINTS`) は、順序ではなく除外で扱う。遮断は送信を
    止めるが、ログアウトは止めない。
    """
    allowed = [c for c in candidates if not is_forbidden(c)]
    return tuple(sorted(allowed, key=lambda c: (c.looks_like_send, c.index)))


#: **絶対に押してはいけない部品のクラス断片。**
#:
#: 実測9回目、探索はヘッダのログアウトリンク
#: (``a.c-link.c-link--alert.c-header-menu__logout-link``) を押した。セッションが
#: 切れ、以降の観測は全て無意味になり、運用者の実行を1回まるごと消費した。
#:
#: これは「安全そうかどうか」の判断ではない。**押せば偵察そのものが終わる操作**
#: であり、押す理由が1つも無い。同じ理由で退会・解約に類する語も入れてある。
#:
#: 押す順の判断 (:data:`SEND_CLASS_HINTS`) とは性質が違うことに注意。あちらは
#: 「後ろに回す」であって除外ではない -- 遮断があるので押して正体を見たい。
#: こちらは **除外** である。遮断は送信を止めるが、ログアウトは止めない。
FORBIDDEN_CLASS_HINTS: tuple[str, ...] = ("logout", "signout", "sign-out", "withdraw")


def is_forbidden(candidate: ActionCandidate) -> bool:
    """Whether pressing this would end the reconnaissance itself. **Pure.**"""
    blob = " ".join(candidate.tokens).lower()
    return any(hint in blob for hint in FORBIDDEN_CLASS_HINTS)


def card_action_candidates(
    tree: DomTree, sizes: Sequence[int], row_index: int
) -> tuple[ActionCandidate, ...]:
    """Clickables inside one card, **safest-looking first**. **Pure.**"""
    return _safest_first(_candidates_in(tree, sizes, row_index))


def revealed_controls(
    tree: DomTree, sizes: Sequence[int], region_tokens: Iterable[str]
) -> tuple[ActionCandidate, ...]:
    """Clickables inside the region that a press just revealed. **Pure.**

    **これが送信路への導線である。** 実測3回目で分かった形: カードの
    チェックボックスを押すと一括スカウト用のバー (``div.c-sticky-scout-bar``) が
    現れ、その中にスカウトボタンがある。押した結果現れたものを押さない限り、
    送信画面には到達しない。

    探索は現れた領域の中だけに限る。画面全体へ広げると、常駐している無関係な
    UI (ヘッダやサイドバー) まで押しに行く -- 実測1回目でサイトのロゴを押した
    のと同じ失敗になる。

    並びは :func:`_safest_first` と同じ (送信を名乗るものは後ろ)。ただし
    **除外はしない** -- 遮断があるので、送信ボタンこそ押して正体を見る。
    """
    found: list[ActionCandidate] = []
    seen: set[int] = set()
    for root in region_roots(tree, sizes, region_tokens):
        for candidate in _candidates_in(tree, sizes, root):
            if candidate.index not in seen:
                seen.add(candidate.index)
                found.append(candidate)
    return _safest_first(found)


def revealed_text_fields(
    tree: DomTree, sizes: Sequence[int], region_tokens: Iterable[str]
) -> tuple[ActionCandidate, ...]:
    """Text fields inside the region that a press just revealed, in document order. **Pure.**

    **なぜ書き込む必要があるのか。**

    段階3の成果物は ``api.send.paid.url_pattern`` である。しかし遮断した非GETは
    1押しにつき複数出る (計測ビーコン、画面を開くための通信、そして送信)。
    **どれが送信路かを区別する根拠は1つしかない** -- 自分で書き込んだ目印が
    その本文に載っていることである (:mod:`recon.sentinel`)。

    書き込まなければ、区別する根拠は永遠に手に入らない。それでも「たぶんこれが
    送信路だろう」と書けば、それは推測で座標を埋めることになる (原則3)。だから
    **押す前に書ける欄には書く**。書き込みは送信ではないし、書き込んだ内容を
    保存しようとする通信があれば、それも遮断されている。

    探索範囲を「現れた領域の中」に限るのは :func:`revealed_controls` と同じ理由。
    画面全体へ広げると、常駐している検索欄のような無関係な入力まで書き換えて
    しまい、一覧そのものが変わって探索が別物になる。
    """
    found: list[ActionCandidate] = []
    seen: set[int] = set()
    for root in region_roots(tree, sizes, region_tokens):
        for index in range(root, root + sizes[root]):
            child = tree.nodes[index]
            if child.tag not in TEXT_FIELD_TAGS or index in seen:
                continue
            seen.add(index)
            blob = " ".join(child.class_names).lower()
            found.append(
                ActionCandidate(
                    index=index,
                    tag=child.tag,
                    tokens=stable_tokens(child.tag, child.class_names),
                    looks_like_send=any(hint in blob for hint in SEND_CLASS_HINTS),
                )
            )
    return tuple(sorted(found, key=lambda c: c.index))


def opened_region(
    before: Mapping[str, int], after: Mapping[str, int], *, minimum: int = 1
) -> tuple[str, ...]:
    """Tokens that appeared after a click. **Pure.**

    ``newly_visible_clickables`` (押せる要素の多重集合差) と違い、こちらは
    **構造トークンの差** を見る。ドロワーが ``u-is-hidden`` の付け外しだけで
    現れる作りだと、押せる要素の総数は変わらないのに内容は変わる -- 実測7回目の
    結果ページには ``div.c-side-cover.u-is-hidden`` が最初から在った。
    """
    gained = [token for token, count in after.items() if count - before.get(token, 0) >= minimum]
    return tuple(sorted(gained))


def newly_present(before: Mapping[str, int], after: Mapping[str, int]) -> tuple[str, ...]:
    """Tokens that **did not exist at all** before the press. **Pure.**

    :func:`opened_region` との違いが、実測9回目の事故そのものである。

    ``opened_region`` は「数が増えたトークン」を返す。報告にはそれで良い --
    何が変わったかの事実だからである。**しかし押しに行く先を決めるのに使うと
    破綻する。** ``a.c-link`` のように画面の至る所に在るトークンは、押した結果
    どこかで1つ増えれば「現れた」に入る。するとページ中の ``a.c-link`` が
    ぜんぶ「現れた領域」の根になり、探索は画面全体へ散る。

    実測9回目はそれで **ヘッダのログアウトリンクを押した**。セッションが切れ、
    探索はそこで終わった。「書き込める部品が38個」も同じ原因である -- 38個は
    ドロワーの中の数ではなく、ページ全体の数だった。

    **押しに行く先は「前には1つも無かった構造」に限る。** 押して初めて生まれた
    ものだけが、その押下が開いた領域を指している。
    """
    fresh = (token for token, count in after.items() if count > 0 and not before.get(token))
    return tuple(sorted(fresh))


def vanished_region(before: Mapping[str, int], after: Mapping[str, int]) -> tuple[str, ...]:
    """Tokens that disappeared after a click. **Pure.**

    「閉じた」ことの観測に使う。開いた後に閉じる操作を試したとき、開いたときに
    増えたトークンがそのまま消えれば、その操作は閉じる操作である。
    """
    return tuple(sorted(token for token, count in before.items() if after.get(token, 0) < count))


def close_candidates_in(
    tree: DomTree,
    sizes: Sequence[int],
    region_tokens: Iterable[str],
    *,
    class_hints: Sequence[str] = ("close", "closer", "dismiss", "cancel"),
) -> tuple[str, ...]:
    """Selectors that look like the close control of the opened region. **Pure.**

    探索は **開いた領域の中だけ**。画面全体から「閉じる」を集めると、常駐している
    別のモーダルの閉じるボタンが混ざる (実測: 結果ページに ``a.c-modal__closer`` が
    5個、``a.c-side-cover__close-btn`` が1個、いずれも ``u-is-hidden`` で待機)。

    並びは文書順。**文言は読まない** -- 読めば実行ログに出す誘惑が生まれるし
    (13.2)、媒体の言い回し変更で壊れる。クラス名が用途を名乗っているものだけを
    採る。見つからなければ空を返す (推測で埋めない, 原則3)。
    """
    roots = region_roots(tree, sizes, region_tokens)
    found: list[str] = []
    for root in roots:
        for index in range(root, root + sizes[root]):
            node = tree.nodes[index]
            if node.tag not in ACTION_TAGS:
                continue
            blob = " ".join(node.class_names).lower()
            if not any(hint in blob for hint in class_hints):
                continue
            for token in stable_tokens(node.tag, node.class_names):
                if any(hint in token.lower() for hint in class_hints) and token not in found:
                    found.append(token)
    return tuple(found)


def redact_url(url: str) -> str:
    """A URL safe to print: path IDs and query values masked. **Pure.**

    段階3の成果物はAPIのURL形 (``api.send.paid.url_pattern``) なので、URL自体は
    出さないと意味が無い。しかし実URLには会員IDが載る -- パスの数値/長い16進と
    クエリの値を伏せる (13.2)。**保存する構造ダンプには原文を残す** (アーティ
    ファクトは保持3日、実行ログとは別扱い)。
    """
    masked = _ID_SEGMENT.sub("{id}", url)
    return _QUERY_VALUE.sub("{value}", masked)


@dataclass(frozen=True)
class BlockedRequest:
    """One non-GET that the armed gate recorded and aborted."""

    method: str
    url: str
    carried_sentinel: bool

    def redacted(self) -> str:
        return f"{self.method} {redact_url(self.url)}"


def rank_send_candidates(blocked: Sequence[BlockedRequest]) -> tuple[BlockedRequest, ...]:
    """Blocked requests most likely to be the send call, best first. **Pure.**

    センチネル (件名・本文に混ぜた目印) を持つものが最有力。次に、媒体自身の
    オリジンを向いた非GET。計測ビーコンは他所のオリジンへ飛ぶので後ろへ回る。
    **落とさない** -- 段階3では送信URLが未知なので、絞り込みは順位付けまでに
    とどめる (gate の docstring と同じ理由)。
    """

    def key(entry: BlockedRequest) -> tuple[int, int, str]:
        own_origin = "job-medley" in entry.url
        return (not entry.carried_sentinel, not own_origin, entry.url)

    return tuple(sorted(blocked, key=key))


def subtree_holds(tree: DomTree, sizes: Sequence[int], root: int, index: int) -> bool:
    """Whether ``index`` sits inside ``root``'s subtree. **Pure.** (薄い別名)"""
    return contains(sizes, root, index)


#: クリックが完了しなかった理由の分類。**この集合以外は出さない。**
#: Playwright の例外メッセージには要素の outerHTML (= ページの文言) が混ざるので、
#: **生のメッセージは決して印字しない** (13.2)。分類名だけを持ち回る。
CLICK_FAILURE_KINDS: tuple[str, ...] = (
    "覆われていて押下が届かない",
    "要素が動き続けている (アニメーション等)",
    "要素が見えない",
    "要素が無効化されている",
    "同じセレクタに複数一致した",
    "満了 (理由の特定なし)",
    "その他",
)


def click_failure_kind(message: str) -> str:
    """Classify why a click did not complete. **Pure. 文言は返さない。**

    実測4回目で8個すべてのクリックが完了しなかった。理由を握り潰していたので
    「押せなかった」以上のことが分からず、次の手が打てなかった。**失敗の理由は
    観測であり、捨ててはいけない。**

    返すのは :data:`CLICK_FAILURE_KINDS` の定型句のみ。入力のメッセージには
    要素の outerHTML が混ざりうるので、そのまま外へ出さない (13.2)。
    """
    lowered = message.lower()
    if "intercepts pointer events" in lowered:
        return "覆われていて押下が届かない"
    if "not stable" in lowered:
        return "要素が動き続けている (アニメーション等)"
    if "not visible" in lowered:
        return "要素が見えない"
    if "not enabled" in lowered:
        return "要素が無効化されている"
    if "strict mode violation" in lowered:
        return "同じセレクタに複数一致した"
    if "timeout" in lowered:
        return "満了 (理由の特定なし)"
    return "その他"
