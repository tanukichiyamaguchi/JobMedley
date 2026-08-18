"""Read the DOM once, as plain data.

このモジュールが存在する理由は、実際に起きた誤診である。

``scout recon verify-session`` は認証済みのページで「ログアウト」を見つけたのに、
数分後の ``scout recon observe-login`` は同じセッションで **何も見つけなかった**。
両者の走査ロジックは同じ DOM に対しては同じ答えを返すので、差は DOM の側にあった。

原因は2つ重なっている。

1. **``query_selector_all`` は待たない。** 自動待機するのは ``wait_for_selector`` と
   ロケータAPIだけで、``context.set_default_timeout`` はこの呼び出しに効かない。
   SPA (``auth.is_spa: true``) では ``domcontentloaded`` の時点でヘッダがまだ
   描画されていないことがあり、そこを覗くと空に見える
2. **要素ハンドルは再描画で無効になる。** 従来の走査は1要素につき
   ``inner_text`` / ``id`` / ``class`` と3回ブラウザへ往復しており、その途中で
   ハイドレーションがヘッダを差し替えると、握っていたハンドルが無効化される。
   例外は握りつぶされるので、要素は **黙って** 候補から消える。往復1回で済む
   走査より、構造的に取りこぼしやすかった

対処は、**1回の ``evaluate`` で必要な属性をまとめて取り出し、以降はただのデータとして
扱う** こと。ハンドルを持ち歩かないので無効化されようがなく、判定は純粋関数へ移せる
(13.4)。速度も往復回数に比例して改善する。

**ここで待つのは「要素」であって「通信の静止」ではない** (5.3)。待つ対象は
``a, button`` という汎用の目印であり、媒体固有のセレクタではない -- 探している
マーカーそのものを待つのは循環参照になる。
"""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

#: クリック可能な要素をまとめて取り出す。**1回の往復で完結させる。**
#: ``innerText`` は表示されていない要素では ``textContent`` に落ちる (HTML仕様) ので、
#: 折り畳まれたメニューの中のリンクも読める。``aria-label`` も併せて取る -- アイコンのみの
#: 閉じるボタンには文言が無く、意味は aria-label にしか出ていないことが珍しくない。
_CLICKABLE_SCRIPT = """
() => Array.from(document.querySelectorAll('a, button')).map((el) => ({
  tag: el.tagName.toLowerCase(),
  id: el.id || null,
  classes: typeof el.className === 'string'
    ? el.className.split(/\\s+/).filter(Boolean)
    : [],
  text: (el.innerText || el.textContent || '').trim(),
  ariaLabel: el.getAttribute('aria-label') || '',
  visible: el.getClientRects().length > 0,
}))
"""

#: 走査の前に出現を待つ汎用の目印。**媒体固有のセレクタではない。**
INTERACTIVE_SELECTOR = "a, button"

#: ログインフォームの目印。HTMLの入力種別であって媒体固有の座標ではない。
PASSWORD_INPUT = 'input[type="password"]'


@dataclass(frozen=True)
class Clickable:
    """One anchor or button, as plain data. No browser handle is retained."""

    tag: str
    element_id: str | None
    class_names: tuple[str, ...]
    text: str
    #: アイコンのみのコントロールでは文言が空なことがある。既定は空文字
    #: (未観測ではなく「無かった」) -- 座標のUNRESOLVEDとは違う話なので、
    #: ここは Coord 型を使わない。
    aria_label: str = ""
    #: ``getClientRects().length > 0``。ドロワーが「最初からDOMに在って隠れている」
    #: 作りだと、開いても ``a, button`` の **総数** が増えない。可視性まで見ないと
    #: 「開かなかった」と「開いたが数が変わらなかった」を取り違える。
    #: 既定 ``True`` は未観測の意味ではなく、可視性を取らない呼び出し側との互換。
    visible: bool = True


def wait_for_interactive(page: Any, timeout_ms: int) -> bool:
    """Wait until the page has rendered something clickable.

    SPA が描画を終える前に走査すると、空の DOM を「要素が存在しない」と読んでしまう。
    かといってマーカーそのものを待つことはできない -- それが探している当のものだから。
    そこで **汎用の目印** の出現を待つ。

    見つからなくても例外にしない。目印が本当に無いページ (エラーページなど) でも
    走査は続行し、「見つからなかった」として報告する方が、原因の切り分けに役立つ。
    """
    try:
        page.wait_for_selector(INTERACTIVE_SELECTOR, timeout=timeout_ms, state="attached")
    except Exception:
        return False
    return True


#: 候補者一覧の描画完了を表す専用の目印はまだ確定していない (それこそが段階2の
#: 探索対象)。代わりに「class属性を持つ要素数が変化しなくなったこと」を汎用の
#: 完了シグナルとして使う。ポーリングは Playwright 側の ``wait_for_function`` に
#: 行わせる -- ``page.wait_for_timeout`` は :mod:`browser.waits` 専用と決めてある
#: (``tests/guardrails/test_source_conventions.py``)。
_STRUCTURE_SETTLED_SCRIPT = """
() => {
  const key = '__jmScoutStructureSettle';
  const current = document.querySelectorAll('[class]').length;
  const state = window[key];
  if (!state || state.count !== current) {
    window[key] = { count: current, stableChecks: 0 };
    return false;
  }
  state.stableChecks += 1;
  return state.stableChecks >= 3;
}
"""


def wait_for_structure_to_settle(page: Any, timeout_ms: int) -> bool:
    """Wait until the count of classed elements stops changing between polls.

    **待つのは「通信の静止」ではない** (5.3)。ここで直接測っているのは、実際に
    知りたいこと (一覧の構造が変化しなくなったか) そのものであり、通信を
    代理指標にするより直接的である。
    """
    try:
        page.wait_for_function(_STRUCTURE_SETTLED_SCRIPT, timeout=timeout_ms)
    except Exception:
        return False
    return True


def wait_for_content_to_arrive(page: Any, timeout_ms: int) -> bool:
    """Wait until a press's content has actually mounted, not just settled.

    **構造の静止だけでは足りない場面がある** (capture-open 6回目)。

    媒体は押下のあと読み込み表示 (``div.c-loader``) を出し、GraphQL で中身を
    取ってから差し替える。**読み込み表示が出ている間、DOM の構造は静止している**
    -- 要素数は変わらないので :func:`wait_for_structure_to_settle` は「落ち着いた」
    と答える。その瞬間に測ると、押して現れたものは読み込み表示そのものになる。
    実際、6回目の観測で「増えた構造」は ``div.c-loader`` 一色だった。

    構造だけを見ていては、この2つを区別できない。だから **ここでだけ通信の静止を
    併用する**。1度落ち着くのを待ち、通信が止まるのを待ち、もう1度落ち着くのを
    待つ -- 差し替え後の構造を測るには、この順序が要る。

    通信の静止は待てないことがある (計測ビーコンが鳴り続ける等)。**待てなくても
    失敗にしない** -- 待ちは観測の精度を上げるためのもので、ここで諦めても
    後段は「何が増えたか」を測れる。
    """
    wait_for_structure_to_settle(page, timeout_ms)
    with suppress(Exception):
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    return wait_for_structure_to_settle(page, timeout_ms)


#: ドロワー/モーダルが開いたことの手掛かり。何が増えるかは媒体依存で分からないので、
#: 汎用の目印 (a, button) の変化という形でしか問えない。
#:
#: **総数と可視数の両方を見る。** 総数だけだと、ドロワーが最初からDOMに在って
#: 隠れているだけの作りで永久に増えない。可視数だけだと、遅延生成の作りで
#: 生成直後のまだ描画されていない一瞬を取り逃す。どちらの作りかは観測していないので、
#: 断定せずに両方を1つの述語で待つ (待機は1回、満了も1回)。
_NEW_CLICKABLES_SCRIPT = """
(arg) => {
  const all = Array.from(document.querySelectorAll('a, button'));
  const visible = all.filter((el) => el.getClientRects().length > 0).length;
  return all.length > arg.total || visible > arg.visible;
}
"""


def wait_for_new_clickables(
    page: Any, *, before_total: int, before_visible: int, timeout_ms: int
) -> bool:
    """Wait until clickable elements appear, or become visible, beyond the given counts.

    候補者ドロワー/モーダルが開いたことを検知する (5.3 と同じ理屈: 待つのは
    「通信の静止」ではなく「要素の変化」)。
    """
    try:
        page.wait_for_function(
            _NEW_CLICKABLES_SCRIPT,
            arg={"total": before_total, "visible": before_visible},
            timeout=timeout_ms,
        )
    except Exception:
        return False
    return True


#: 与えられたセレクタが全て0件になるのを待つ。**語彙は呼び出し側が観測から導く。**
_ALL_DETACHED_SCRIPT = """
(sels) => sels.every((s) => {
  try { return document.querySelectorAll(s).length === 0; }
  catch (e) { return true; }
})
"""


def wait_for_all_detached(page: Any, selectors: list[str], timeout_ms: int) -> bool:
    """Wait until none of ``selectors`` matches anything.

    読み込み中にだけ存在する要素 (ローダー) の消滅を待つために使う。**どの要素が
    ローダーかは推測しない** -- 呼び出し側が「遷移直後には在り、読み込み完了後の
    ページには無い」という観測から導いたセレクタを渡す (5.3 と同じ理屈: 待つのは
    通信の静止ではなく、要素の状態)。

    満了しても例外にしない。ローダーが残り続けるページ (読み込みが完了しない
    変種) は実在するので、「消えなかった」は観測として呼び出し側へ返す。
    """
    if not selectors:
        return True
    try:
        page.wait_for_function(_ALL_DETACHED_SCRIPT, arg=selectors, timeout=timeout_ms)
    except Exception:
        return False
    return True


#: 与えられたセレクタの **どれか1つ** が0件になるのを待つ。
_ANY_DETACHED_SCRIPT = """
(sels) => sels.some((s) => {
  try { return document.querySelectorAll(s).length === 0; }
  catch (e) { return false; }
})
"""


def wait_for_any_detached(page: Any, selectors: list[str], timeout_ms: int) -> bool:
    """Wait until **at least one** of ``selectors`` stops matching anything.

    押下の直後に現れたものの中には、読み込み表示のように **すぐ消えるもの** が
    混ざる。全部が消えるのを待つと、残るべきもの (開いた領域そのもの) がある
    以上いつまでも満たされず、必ず満了する。逆に何も待たなければ、読み込み表示を
    「押して現れたもの」として測ってしまう (capture-open 6・7回目)。

    **「どれか1つが消えた」は、消えるものが消え終わった合図として使える。**
    残るものは残ったまま先へ進めるので、良い場合は即座に返る。

    どのセレクタが読み込み表示かは **推測しない**。呼び出し側が「押す前には無く、
    押した直後に現れた」という観測から渡す。名前で当てにいかないのは、媒体が
    クラス名を変えても壊れないようにするためである。

    満了しても例外にしない。消えるものが無い押下 (静的に開くだけの領域) は
    正常にありうる。
    """
    if not selectors:
        return True
    try:
        page.wait_for_function(_ANY_DETACHED_SCRIPT, arg=selectors, timeout=timeout_ms)
    except Exception:
        return False
    return True


#: クリック対象の中心に実際に居る要素 (= ポインタを受け取る要素) と、その祖先を
#: たどって [タグ, クラス配列] の列で返す。**タグとクラスしか読まない** (13.2)。
_COVERING_SCRIPT = """
([css, nth]) => {
  const target = document.querySelectorAll(css)[nth];
  if (!target) return null;
  const r = target.getBoundingClientRect();
  let node = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
  if (!node) return null;
  const rows = [];
  for (let i = 0; i < 8 && node && node.tagName; i++) {
    rows.push([node.tagName.toLowerCase(), Array.from(node.classList || [])]);
    node = node.parentElement;
  }
  return rows;
}
"""


def covering_rows(page: Any, css: str, nth: int) -> tuple[tuple[str, tuple[str, ...]], ...] | None:
    """Who is actually under the pointer at the click target's center.

    クリックが「操作可能になるのを待って満了」したとき、**何が遮っていたのか** は
    例外メッセージの推定ではなく DOM から直接読める (`elementFromPoint`)。対象が
    自分自身なら遮りは無い (別の理由で押せていない)。読めなければ None -- 読めない
    ことを「遮り無し」と報告しないため、空タプルとは区別する。
    """
    try:
        rows = page.evaluate(_COVERING_SCRIPT, [css, nth])
    except Exception:
        return None
    if not rows:
        return None
    return tuple((str(tag), tuple(str(c) for c in classes)) for tag, classes in rows)


def page_title(page: Any) -> str:
    """The page's title, or empty. Used as evidence of *which* page we were on."""
    try:
        return str(page.title() or "").strip()
    except Exception:
        return ""


def one_line(text: str) -> str:
    """Collapse a DOM string to a single line.

    **改行を含んだまま持ち回らせない。** この文字列はセレクタ候補
    (``a:has-text("...")``) と、貼り付け用YAMLのコメント行に埋め込まれる。改行が
    残っていると、コメントの ``#`` が1行目にしか付かず、2行目以降が YAML の
    行として解釈される -- 貼り付けた設定が壊れるか、意図しないキーが生える。

    ボタンの表示文字は改行やタブを含むことが普通にある (``<button>ログ\\n
    アウト</button>``) ので、これは想定外ではなく通常のケースである。
    """
    return " ".join(text.split())


def clickables(page: Any) -> tuple[Clickable, ...]:
    """Every anchor and button on the page, read in a single round trip."""
    try:
        raw = page.evaluate(_CLICKABLE_SCRIPT)
    except Exception:
        return ()
    found: list[Clickable] = []
    for item in raw or ():
        try:
            found.append(
                Clickable(
                    tag=one_line(str(item.get("tag", ""))),
                    element_id=one_line(str(item.get("id") or "")) or None,
                    class_names=tuple(one_line(str(name)) for name in item.get("classes") or ()),
                    # **ここで1行に潰す。** 以降どこへ埋め込まれても改行は出ない。
                    text=one_line(str(item.get("text") or "")),
                    aria_label=one_line(str(item.get("ariaLabel") or "")),
                    visible=bool(item.get("visible", True)),
                )
            )
        except AttributeError:  # pragma: no cover - defensive
            continue
    return tuple(found)


#: ``<select>`` 要素をまとめて取り出す。段階2の ``context.selector`` (グループ/拠点の
#: 選択コントロール) 探索で使う。
_SELECT_SCRIPT = """
() => Array.from(document.querySelectorAll('select')).map((el) => ({
  id: el.id || null,
  name: el.getAttribute('name') || null,
  optionCount: el.options.length,
}))
"""


@dataclass(frozen=True)
class SelectField:
    """One ``<select>`` element, as plain data."""

    element_id: str | None
    name: str | None
    option_count: int


def select_fields(page: Any) -> tuple[SelectField, ...]:
    """Every ``<select>`` on the page, read in a single round trip."""
    try:
        raw = page.evaluate(_SELECT_SCRIPT)
    except Exception:
        return ()
    found: list[SelectField] = []
    for item in raw or ():
        try:
            found.append(
                SelectField(
                    element_id=one_line(str(item.get("id") or "")) or None,
                    name=one_line(str(item.get("name") or "")) or None,
                    option_count=int(item.get("optionCount") or 0),
                )
            )
        except (AttributeError, TypeError, ValueError):  # pragma: no cover - defensive
            continue
    return tuple(found)


#: 木の節点数の上限。超えたら **値を出さない**。前順で切ると末尾の行が欠け、
#: 行が欠けたまま共通祖先を計算しかねない -- 「一部しか見ていない」ことに
#: 気づけない形の誤りになる。
DOM_TREE_NODE_CAP = 60_000

#: 要素の木を1往復で取る。前順 (pre-order) DFS で採番するのが要点である。
#: 前順だと節点 ``i`` の子孫がちょうど添字区間 ``[i, i+size[i])`` を占めるので、
#: 以降の包含判定が整数比較2回で済み、**判定を純粋関数へ追い出せる** (13.4)。
#: **文言・id・href・属性値は取らない** (13.2)。この走査は指定された画面を
#: 無差別に読むので、構造以外を持ち出さない。
_DOM_TREE_SCRIPT = """
(cap) => {
  const out = [];
  let truncated = false;
  let shadow = 0;
  const stack = [[document.documentElement, -1]];
  while (stack.length) {
    const entry = stack.pop();
    const el = entry[0], parent = entry[1];
    const i = out.length;
    if (el.shadowRoot) shadow += 1;
    out.push({
      tag: el.tagName.toLowerCase(),
      classes: typeof el.className === 'string'
        ? el.className.split(/\\s+/).filter(Boolean)
        : [],
      parent: parent,
    });
    if (out.length >= cap) { truncated = true; break; }
    const kids = el.children;
    for (let k = kids.length - 1; k >= 0; k--) stack.push([kids[k], i]);
  }
  return { truncated: truncated, shadowRoots: shadow, nodes: out };
}
"""


@dataclass(frozen=True)
class DomNode:
    """One element. ``parent`` is an index into :attr:`DomTree.nodes` (root is ``-1``)."""

    tag: str
    class_names: tuple[str, ...]
    parent: int


@dataclass(frozen=True)
class DomTree:
    """The element tree as plain data, in pre-order."""

    nodes: tuple[DomNode, ...]
    truncated: bool
    #: 影DOMを持つ要素の数。**中は走査できない。** 見えなかったことを数として残す --
    #: 「一覧が影DOMの中にあって空に見えた」を運用者が切り分けられるように。
    shadow_root_count: int


def build_tree(
    rows: Iterable[tuple[str, tuple[str, ...], int]],
    *,
    truncated: bool,
    shadow_root_count: int,
) -> DomTree | None:
    """Validate pre-order numbering and build a tree, or ``None``.

    前順採番の前提 (根だけが ``-1``、それ以外は ``0 <= parent < i``) をここで
    自己検査する。破れていたら以降の包含判定が全部嘘になるので、**黙って先へ
    進まず ``None`` を返す**。実行時の走査 (:func:`dom_tree`) と、保存された
    スナップショットの読み戻し (:mod:`recon.snapshot`) の **両方がこの1つの
    検査を通る** -- 検証器を2つ持つと、片方だけ緩む。
    """
    nodes: list[DomNode] = []
    for index, (tag, classes, parent) in enumerate(rows):
        if index == 0:
            if parent != -1:
                return None
        elif not 0 <= parent < index:
            return None
        nodes.append(DomNode(tag=tag, class_names=tuple(classes), parent=parent))
    if not nodes:
        return None
    return DomTree(nodes=tuple(nodes), truncated=truncated, shadow_root_count=shadow_root_count)


def dom_tree(page: Any, *, cap: int = DOM_TREE_NODE_CAP) -> DomTree | None:
    """Read the whole element tree in a single round trip, or ``None``.

    **``None`` は「読めなかった」であって「空だった」ではない。** この区別が
    このモジュールの存在理由そのものである。読めなかったのを「何も無かった」と
    読むと、いま塞ごうとしている静かなゼロ件 (原則2) を新しい経路で再生産する。
    呼び出し側は ``None`` を必ず UNRESOLVED へ落とすこと。
    """
    try:
        raw = page.evaluate(_DOM_TREE_SCRIPT, cap)
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    rows: list[tuple[str, tuple[str, ...], int]] = []
    for item in raw.get("nodes") or ():
        try:
            rows.append(
                (
                    one_line(str(item.get("tag", ""))),
                    tuple(one_line(str(name)) for name in item.get("classes") or ()),
                    int(item.get("parent", -1)),
                )
            )
        except (AttributeError, TypeError, ValueError):
            return None
    return build_tree(
        rows,
        truncated=bool(raw.get("truncated")),
        shadow_root_count=int(raw.get("shadowRoots") or 0),
    )


def login_form_visible(page: Any, timeout_ms: int) -> bool:
    """Whether a password field is on the page. **Waits before saying "no".**

    「ログイン画面に戻された」ことの手掛かり。認証済みのつもりで走査した結果が
    空だったとき、これが真なら **セッションが効いていない** のであって、
    「マーカーが存在しない」のではない。この2つを取り違えると、原則2の
    「静かなゼロ件」がそのまま座標の欠落として定着する。

    **待つAPIで問うのが要点である。** ここは *偽* の側が判断材料になる関数で、
    「まだ描画されていない」と「存在しない」を取り違えると、最初に塞いだはずの
    誤報がそのまま戻ってくる -- しかも塞いだつもりの経路から。
    :func:`wait_for_interactive` は代わりにならない。サインイン画面でも ``a`` や
    ``button`` はパスワード欄より先に付きうるので、その待機を通過した時点では
    フォームの有無について何も分かっていない。

    ``timeout_ms`` は **呼び出し側が予算として明示する**。ログイン済みの画面では
    パスワード欄は永遠に現れないので、この待機は必ず満了する。だから呼び出し側は、
    その待ち時間を払う価値があるときにだけ呼ぶこと (:mod:`recon.observe_login` は
    マーカーが見つからなかったときだけ問い合わせる)。
    """
    try:
        page.wait_for_selector(PASSWORD_INPUT, timeout=timeout_ms, state="attached")
    except Exception:
        return False
    return True
