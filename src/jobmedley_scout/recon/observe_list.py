"""Observe stage 2's remaining coordinates instead of asking a human to read them.

段階2の座標は6個ある。うち2個 (``nav.mypage_url`` / ``nav.candidate_list_url``) は
段階1の観測で既に確定している。残る4個は:

* ``context.selection_required`` / ``context.selector`` -- ログイン直後にグループ/
  拠点/求人アカウントなどの選択ステップが挟まるか
* ``nav.list_ready_selector`` -- 候補者一覧の描画完了を表す要素。**行そのものでは
  なく行のコンテナを選ぶと空結果でも待てる** (`config/coordinates.py` の
  ``how_to_obtain``)。行を選んでしまうと、0件の検索結果を「まだ描画されて
  いない」と誤読する
* ``nav.drawer_close_selectors`` -- 候補者ドロワー/モーダルを閉じるコントロール
  (5.7 の総当たりフォールバック用の候補列)

いずれも認証済みセッションと、既に確定している ``nav.candidate_list_url`` だけで
観測できる。**推測はしない** -- 観測できなかったものは UNRESOLVED のまま報告する
(原則3)。

行と枠の見分け方 (**2026-08-13 に作り直した**)
--------------------------------------------

当初は「結果ページと0件ページの **両方に存在する** 要素」を
``nav.list_ready_selector`` の候補にしていた。**これは間違いだった。** 画面の枠
(ヘッダ・サイドバー・``body`` そのもの) はすべてこの条件を満たすので、実測では
278トークンが合格し、``body.c-body`` を推奨した。それは常時あるので待機が常に
即座に成功し、一覧が描画される前に0件と読む (原則2)。

いまは述語を反転してある。詳細と根拠は :mod:`recon.list_structure` の冒頭にある。
値は ``"<行トークン>, <0件表示トークン>"`` というセレクタリスト (論理和) で、
**行が出た、または0件表示が出た** で成立する。どちらも描画前には存在しない。

ドロワーの開き方
----------------

行 (カード) の中で、**操作部品を1つも含まない最大の領域** をクリックする。
実測の行の中には ``button.js-tour-guide-scout-button`` があり、スカウト送信
そのものの可能性がある -- Playwright は要素の中心を押すので、行を素朴に
クリックすると中心を覆う子がこれを受け取りうる。**取り消せない外向き操作を
偵察で踏まない。** 押せる領域が見つからなければクリックしない。

開けなかった場合や、開いた後に閉じるボタンらしき要素が見つからなかった場合は、
それぞれ理由を添えて UNRESOLVED のまま報告する。

判定ロジックは純粋関数に置いてある (13.4)。本モジュールはそれらへ値を運ぶだけで、
**判断はしない**。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from jobmedley_scout.browser import session_store
from jobmedley_scout.browser.context import browser_context
from jobmedley_scout.browser.dom import (
    Clickable,
    DomTree,
    SelectField,
    clickables,
    covering_rows,
    dom_tree,
    login_form_visible,
    select_fields,
    wait_for_all_detached,
    wait_for_interactive,
    wait_for_new_clickables,
    wait_for_structure_to_settle,
)
from jobmedley_scout.browser.navigation import goto
from jobmedley_scout.config.placeholders import UNRESOLVED_TOKEN, Coord, require
from jobmedley_scout.config.schema import BrowserConfig
from jobmedley_scout.recon.list_structure import (
    EmptyCandidate,
    ReadyValue,
    RowGroup,
    click_locator,
    empty_exclusions,
    empty_state_candidates,
    list_region,
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
from jobmedley_scout.recon.manual_login import (
    MarkerCandidate,
    form_field_selector_candidates,
    marker_candidates_from,
    structure_sample,
)
from jobmedley_scout.recon.snapshot import ListCapture, ZeroCapture
from jobmedley_scout.recon.yaml_paste import yaml_scalar as _scalar

#: 閉じる系コントロールの探索語。ログアウトと同じ仕組み
#: (:func:`recon.manual_login.marker_candidates_from`) を語彙だけ差し替えて使う。
CLOSE_TEXT_HINTS: tuple[str, ...] = ("閉じる", "close", "Close", "×", "✕", "✖")
#: クラス名が「閉じるための要素」と名乗っているか。
CLOSE_CLASS_TOKENS: tuple[str, ...] = ("close", "dismiss")

#: 0件ページが「描画された」と認めるための、サイト共通要素の最低残存率。
#: 実測: 実ページ0.77 vs 起動前スケルトン0.05。桁が違うので境界は鋭敏でない。
CHROME_OVERLAP_MIN = 0.15

#: このモジュールが埋める段階2の座標キー。**この4個より多くも少なくも出さない。**
STAGE_2_KEYS: tuple[str, ...] = (
    "context.selection_required",
    "context.selector",
    "nav.list_ready_selector",
    "nav.drawer_close_selectors",
)


# --- 純粋関数 (テスト可能) ---------------------------------------------------


def selection_redirected(requested_url: str, landed_url: str) -> bool:
    """Whether navigating directly to ``requested_url`` sent us somewhere else.

    比較はホスト+パスのみ (末尾スラッシュは無視)。クエリは無視する -- 検索条件の
    クエリはリダイレクト時に正規化・整形されることがあり、クエリの一致まで求めると
    正しく到達していても誤判定する。
    """
    req = urlsplit(requested_url)
    got = urlsplit(landed_url)
    return (req.netloc, req.path.rstrip("/")) != (got.netloc, got.path.rstrip("/"))


def zero_result_variant(url: str) -> str | None:
    """Build a URL that should return zero candidates, or ``None`` if we cannot.

    実在の座標を書き換えるのではなく、``nav.list_ready_selector`` の比較のためだけに
    その場で作る変種である。年齢帯を人間が存在しない範囲にずらすのが最も汎用的 --
    検索条件がどう組まれていても、120歳を超える帯を指定すれば結果は必ず空になる。
    ``age[from]``/``age[to]`` を含まない検索URLでは作れないので、その場合は
    呼び出し側が「変種を作れなかった」と分かるよう ``None`` を返す。
    """
    parts = urlsplit(url)
    params = parse_qsl(parts.query, keep_blank_values=True)
    if not any(key in ("age[from]", "age[to]") for key, _ in params):
        return None
    replaced = [
        (key, "120") if key == "age[from]" else (key, "121") if key == "age[to]" else (key, value)
        for key, value in params
    ]
    # ``safe="[]"`` で元のURLの書式 (角括弧を%エンコードしない) を保つ。
    return urlunsplit(parts._replace(query=urlencode(replaced, safe="[]")))


# **削除した3つの関数について。**
#
# ``class_frequency`` / ``list_ready_candidates`` / ``rows_that_vanish_on_empty_results``
# は 2026-08-13 に削除した。フォールバックとしても残していない。
#
# ``list_ready_candidates`` は「両ページに存在する」を候補の条件にしていた当の関数で、
# 行が同定できなかったときにここへ落ちると ``body.c-body`` がそのまま戻ってくる。
# **行が同定できないなら値を出さない方が正しい** (原則2 + 原則3)。
# ``rows_that_vanish_on_empty_results`` は「最多出現」で行を選んでいた当の関数で、
# 1カードに10個ある文字要素がカード本体に勝っていた。
# 置き換え先は :mod:`recon.list_structure` にある。


@dataclass(frozen=True)
class DomTreeSnapshot:
    """A zero-result page's tree, its subtree sizes, and its exclusion set."""

    tree: DomTree
    sizes: tuple[int, ...]
    #: このページで0件表示を名乗れないトークン (list_structure.empty_exclusions が
    #: 観測から決める)。読み込み骨組みを0件表示と取り違えないために持ち回る。
    excluded_tokens: frozenset[str]


@dataclass(frozen=True)
class ZeroVariant:
    """A URL that should return zero candidates, and how it was built."""

    kind: str
    url: str


def zero_result_variants(url: str) -> tuple[ZeroVariant, ...]:
    """Every zero-result URL we can build from ``url``. **Pure.**

    座標として保存する値ではなく、比較のためだけにその場で作る変種である。

    **独立な2つの機構を使う。** 年齢帯の変種とページ番号の変種は、媒体側で
    別の経路を通る。それでも同じ「0件表示」が出るなら、その要素は検索条件の
    作り方に依らない本物である可能性が高い。逆に片方でしか出ない要素は
    「年齢の検証エラー画面」のような別物かもしれない。**これが「その0件ページは
    本物か」に対する、観測だけで出せる唯一の答えである。**
    """
    parts = urlsplit(url)
    params = parse_qsl(parts.query, keep_blank_values=True)
    found: list[ZeroVariant] = []

    def rebuilt(replaced: list[tuple[str, str]]) -> str:
        # ``safe="[]"`` で元のURLの書式 (角括弧を%エンコードしない) を保つ。
        return urlunsplit(parts._replace(query=urlencode(replaced, safe="[]")))

    if any(key in ("age[from]", "age[to]") for key, _ in params):
        # 年齢帯を人間が存在しない範囲へずらす。検索条件がどう組まれていても、
        # 120歳を超える帯を指定すれば結果は必ず空になる。
        found.append(
            ZeroVariant(
                "age",
                rebuilt(
                    [
                        (k, "120") if k == "age[from]" else (k, "121") if k == "age[to]" else (k, v)
                        for k, v in params
                    ]
                ),
            )
        )
    if any(key == "pagination[page]" for key, _ in params):
        found.append(
            ZeroVariant(
                "pagination",
                rebuilt([(k, "9999") if k == "pagination[page]" else (k, v) for k, v in params]),
            )
        )
    return tuple(found)


@dataclass(frozen=True)
class ZeroPage:
    """One zero-result page, and everything needed to decide whether to trust it."""

    kind: str
    landed_url: str
    tree_read: bool
    tree_truncated: bool
    counts: Mapping[str, int]
    #: 結果ページで繰り返していたトークンのうち、このページで完全に消えた種類の数。
    #:
    #: **0なら、このページは結果ページから変わっていない** (遷移が失敗して
    #: 結果ページのままか、条件が差し戻されて同等の一覧が出ている)。判定は
    #: この1点に絞る。
    #:
    #: 経緯 (3代目である):
    #: 1. 「選ばれた行トークンの件数」-- 行の同定を誤ると0件ページまで巻き添えで
    #:    捨てられた (実測)
    #: 2. 「最も重い繰り返し群の件数」-- 部分木の重さは入れ子の外側を優遇する。
    #:    一覧を **囲む** 区画 (``div.c-segment`` ×2) が中身ごと数えられて
    #:    カード25枚より重くなり、正常な0件ページを2枚とも拒否した
    #:    (variant電池が実機の前に検出)
    #: 3. 現在: 消えた種類が1つも無い = 変わっていない、という向きの判定。
    #:    入れ子にも行の誤同定にも依存しない
    #:
    #: この判定は「一覧の一部が残ったまま別の一部が消えた」ページを通しうる。
    #: その場合でも出力の不変条件 (行側 XOR 0件側) は保たれ、値は「内容が出たら
    #: 一致する」トークンになる -- 静かな常真にはならない。残存の内訳は
    #: 診断として印字し、構造スナップショットで手元から検証できる。
    vanished_repeated_count: int
    #: 残存した繰り返しの種類数 (診断のみ。判定には使わない)。
    remaining_repeated_count: int
    #: 描画される前 (遷移直後) のトークン件数。0件表示の候補から読み込み表示を外す。
    early_counts: Mapping[str, int]
    #: 結果ページの **非繰り返し** トークン (サイトの共通要素 = ヘッダ・サイド
    #: バー・検索条件など) のうち、このページにも在る割合。**「そもそも描画された
    #: か」の信号。** 実測7回目で pagination 変種が26節点の起動前スケルトンのまま
    #: 撮影され (別URLへ転送された可能性)、繰り返しは全滅したので「使える」と
    #: 誤判定された。共通要素をほとんど含まないページは、0件を返したのではなく
    #: **読み込まれていない**。実ページは0.77、スケルトンは0.05と桁で違う。
    chrome_overlap: float = 1.0


def zero_page_is_usable(page: ZeroPage, requested_url: str) -> tuple[bool, str]:
    """Whether this zero-result page can be trusted, and why not. **Pure.**

    **参照ページ側にだけ厳密な検査を課してはいけない。** 判定の土台である0件ページを
    無検査で信頼すると、遷移に失敗して結果ページのままだった場合に「全トークンが
    両ページに存在する」ことになる。

    :func:`browser.navigation.goto` は遷移失敗を握り潰す (5.3 のため意図的にそう
    してある) ので、この検査が無いと失敗が静かに通る。
    """
    # **セッション切れもここで捕まる。** 失効するとサインイン画面へ転送されるので、
    # パスワード欄をわざわざ待たなくても転送の検査で落ちる。観測していない
    # 「パスワード欄を見た」をフィールドとして持たないのは意図的である --
    # 持てば、いつか誰かがそれを観測値として読む。
    if selection_redirected(requested_url, page.landed_url):
        return False, f"別画面へ転送されました (到達URL: {page.landed_url})"
    if not page.tree_read:
        return False, "DOMの木を読めませんでした (要素が無かったのではありません)"
    if page.tree_truncated:
        return False, "木が上限で打ち切られました"
    if page.vanished_repeated_count == 0:
        return False, (
            "結果ページで繰り返していた要素が1つも消えていません "
            "(遷移が失敗したか、条件が差し戻されて一覧が出たままの可能性)"
        )
    if page.chrome_overlap < CHROME_OVERLAP_MIN:
        # **繰り返しが全滅 = 0件、とは限らない。** ページごと読み込まれていない
        # (起動前の骨組み or 転送) 場合も繰り返しは全滅する。サイトの共通要素を
        # ほとんど含まないなら、0件を返したのではなく描画されていない (実測7回目)。
        return False, (
            f"サイトの共通要素をほとんど含みません (残存率 {page.chrome_overlap:.0%}) "
            "-- 読み込まれなかったか別画面へ転送された可能性。0件ページとして使いません"
        )
    return (
        True,
        f"消えた繰り返し {page.vanished_repeated_count}種 / 残存 {page.remaining_repeated_count}種",
    )


def newly_visible_clickables(
    before: Sequence[Clickable], after: Sequence[Clickable]
) -> tuple[Clickable, ...]:
    """Clickables that appeared, as a **multiset** difference. **Pure.**

    素朴な ``[e for e in after if e not in before]`` は値等価の集合差なので、
    既存のボタンと tag/class/文言が同一な要素が増えても検知できない。
    ドロワーの中に一覧と同じ形のボタンが並ぶ作りは珍しくない。
    """
    remaining = Counter(before)
    fresh: list[Clickable] = []
    for element in after:
        if remaining.get(element, 0) > 0:
            remaining[element] -= 1
        else:
            fresh.append(element)
    return tuple(fresh)


def select_selector_candidates(fields: Iterable[SelectField]) -> tuple[str, ...]:
    """Candidate selectors for ``context.selector``. **Pure.**

    選択肢が1個しか無い ``<select>`` は「選ぶ」コントロールではない (実質固定値) ので
    除外する。
    """
    candidates: list[str] = []
    for select_field in fields:
        if select_field.option_count <= 1:
            continue
        found = form_field_selector_candidates(
            "select", select_field.element_id, select_field.name, None
        )
        if found:
            candidates.append(found[0])
    return tuple(dict.fromkeys(candidates))


@dataclass(frozen=True)
class ObservedList:
    """Everything stage 2's remaining coordinates could observe."""

    requested_url: str
    #: 保存セッションのファイルが存在したか。**「認証できた」ではない。**
    session_present: bool = True
    #: 認証済みで開いたはずの画面にパスワード欄があった (セッションが効いていない)。
    session_expired: bool = False
    #: 実際に着地したURL。
    landed_url: str = ""
    #: グループ/拠点等の選択ステップが挟まったか。
    selection_required: bool = False
    #: 選択ステップの画面で見つかった <select> の候補 (selection_required時のみ)。
    select_candidates: tuple[str, ...] = ()
    #: 選択ステップの画面にあった構造 (証拠。文言は含まない, 13.2)。
    landing_structure: tuple[str, ...] = ()
    #: 結果ページのDOM木を読めたか。**「空だった」ではなく「読めなかった」を区別する。**
    tree_read: bool = False
    #: 木が上限で打ち切られたか。打ち切られたら値を出さない (末尾の行が欠けている)。
    tree_truncated: bool = False
    #: 影DOMを持つ要素の数。**中は走査できない。** 見えなかったことを数として残す。
    shadow_root_count: int = 0
    #: 行として同定できた繰り返し群 (上位のみ)。実測値付きの証拠。
    row_groups: tuple[RowGroup, ...] = ()
    #: 行が0件検索で消えることを確認できたか。0件ページを1枚も使えないと偽。
    rows_confirmed_vanishing: bool = False
    #: 0件表示の候補。
    empty_candidates: tuple[EmptyCandidate, ...] = ()
    #: 0件ページの試行結果 (kind / 使えたか / 使えないなら理由)。
    zero_pages: tuple[tuple[str, bool, str], ...] = ()
    #: 0件ページを1種類しか使えなかったか。値は出すが、その旨を必ず添える。
    empty_state_single_variant: bool = False
    #: 一覧領域のアンカー。**値には使わない** -- 探索範囲を切っただけ。
    anchor_token: str = ""
    #: 0件表示を探した範囲 (``"region"`` / ``"page"`` / 未探索なら空)。
    empty_state_scope: str = ""
    #: 行トークンが一覧の外にもあった数。共通祖先が跳ね上がっている手掛かり。
    rows_outside_group: int = 0
    #: 読み込み後マーカー単独の値 (検索応答の描画後にのみ現れる要素)。これが在れば
    #: **最優先** -- 件数に依らず成立し、読み込み中には存在しないことを観測済み。
    loaded_marker: str = ""
    #: マーカーの別案 (同じ3条件を満たす他のトークン)。
    loaded_marker_alternatives: tuple[str, ...] = ()
    #: nav.list_ready_selector の推奨値と別案。**先頭が推奨。**
    ready: tuple[ReadyValue, ...] = ()
    #: ドロワーを試さなかった明示的な理由 (再生など)。空なら状況から推定して印字する。
    drawer_skip_reason: str = ""
    #: ドロワーを開くために実際に押した ``(セレクタ, 文書順)``。押していなければ None。
    drawer_click_locator: tuple[str, int] | None = None
    #: クリック自体を試みたか。
    drawer_attempted: bool = False
    #: クリックが完了しなかったか (Playwright が操作可能性の検査で満了した等)。
    #: **「押したが無反応」とは別の事実。** 実測4回目でこの区別を怠り、完了して
    #: いないクリックを「押しましたが新出要素なし」と報告して診断を誤導した。
    drawer_click_failed: bool = False
    #: クリック地点 (対象の中心) を覆っていた要素とその祖先の構造トークン。
    #: クリックが遮られたときの一次証拠。文言は含まない (13.2)。
    drawer_covering: tuple[str, ...] = ()
    #: ツアー案内の閉じ操作を試みたが閉じられなかったか。
    tour_dismiss_failed: bool = False
    #: クリック後に新しい要素の出現を検知できたか。
    drawer_opened: bool = False
    #: クリックがドロワーではなくページ遷移だったか。
    drawer_url_changed: bool = False
    #: 閉じるボタンの候補。
    close_candidates: tuple[MarkerCandidate, ...] = ()
    #: ドロワーが開いた後に増えた要素の構造 (証拠。文言は含まない, 13.2)。
    drawer_evidence: tuple[str, ...] = ()

    def _all_unresolved(self, *reason_lines: str) -> str:
        """Every stage 2 key as UNRESOLVED, with a shared reason.

        **1つでも欠けると、運用者は欠けた分を自力で探すことになる。** 認証が
        できていない・セッションが無いといった早期の失敗でも、4つの座標キー
        すべてを印字してから理由を添える。
        """
        lines = ["段階2の観測結果", ""]
        lines.extend(f"  {key}: {UNRESOLVED_TOKEN}" for key in STAGE_2_KEYS)
        lines.append("")
        lines.extend(f"    # {reason}" for reason in reason_lines)
        return "\n".join(lines)

    def render(self) -> str:
        if not self.session_present:
            return self._all_unresolved(
                "保存セッションがありません。シークレット JOBMEDLEY_SESSION_CURL",
                "(または JOBMEDLEY_STORAGE_STATE_B64) を設定してください。",
            )

        if self.session_expired:
            # **ここを「座標が見つからない」と報告してはいけない。** 段階1の
            # observe_login で踏んだ取り違えと同じ形: パスワード欄が見えている
            # なら、見ていたのは一覧ではなくログイン画面である。
            return self._all_unresolved(
                "**セッションが効いていません。** 認証済みで開いたはずの一覧URLに",
                "パスワード欄がありました。",
                f"到達URL: {self.landed_url or '(記録なし)'}",
                "セッションを取り直してから、もう一度実行してください",
                "(docs/ladder.md「セッションが切れたときの取り直し方」)。",
            )

        lines = [
            "段階2の観測結果",
            "",
            "config/site_coordinates.yaml の該当行を、下記で置き換えてください。",
            "**値は観測されたものであり、推測ではありません。**",
            "",
            f"  # 到達URL: {self.landed_url}",
            "",
            f"  context.selection_required: {str(self.selection_required).lower()}",
        ]

        if self.selection_required:
            lines.append("    # 直接遷移すると別の画面へ転送されました。選択が必要です。")
            lines.extend(self._select_lines())
            lines.extend(
                [
                    "",
                    "  # 選択ステップの先の一覧画面まで到達できなかったため、",
                    "  # nav.list_ready_selector と nav.drawer_close_selectors は",
                    "  # この実行では観測できませんでした。context.selector を",
                    "  # 実装で使えるようにしてから、もう一度実行してください。",
                    f"  nav.list_ready_selector: {UNRESOLVED_TOKEN}",
                    f"  nav.drawer_close_selectors: {UNRESOLVED_TOKEN}",
                ]
            )
            return "\n".join([*lines, "", "記入したら `scout preflight` を実行してください。"])

        lines.append("    # 直接遷移して一覧が返りました。選択ステップはありません。")
        lines.append(f"  context.selector: {_scalar(None)}")
        lines.append("    # 選択が不要と確認できたため null。")
        lines.append("")
        lines.extend(self._list_ready_lines())
        lines.append("")
        lines.extend(self._drawer_lines())

        return "\n".join(
            [
                *lines,
                "",
                "記入したら `scout preflight` を実行して、"
                "未確定座標が減ったことを確認してください。",
            ]
        )

    def _select_lines(self) -> list[str]:
        if self.select_candidates:
            out = [f"  context.selector: {_scalar(self.select_candidates[0])}"]
            out.extend(f"    # 別案: {alt}" for alt in self.select_candidates[1:])
        else:
            out = [
                f"  context.selector: {UNRESOLVED_TOKEN}",
                "    # 選択コントロール (<select>) が見つかりませんでした。",
                "    # 開発者ツールで探してください。",
            ]
        if self.landing_structure:
            out.append(f"    # 転送先の画面にあった構造 ({len(self.landing_structure)}種):")
            out.extend(f"    #   - {token}" for token in self.landing_structure)
        return out

    def _list_ready_lines(self) -> list[str]:
        """``nav.list_ready_selector``。**値が出せないときは理由を必ず添える。**"""
        key = "nav.list_ready_selector"
        if not self.tree_read:
            return [
                f"  {key}: {UNRESOLVED_TOKEN}",
                "    # DOMの木を読めませんでした。**要素が無かったのではありません。**",
                f"    # 到達URL: {self.landed_url}",
            ]
        if self.tree_truncated:
            return [
                f"  {key}: {UNRESOLVED_TOKEN}",
                "    # 木が上限で打ち切られました。末尾の行が欠けている可能性が",
                "    # あるため、値を出しません。",
            ]
        if not self.row_groups:
            out = [
                f"  {key}: {UNRESOLVED_TOKEN}",
                "    # 行らしい繰り返し構造を見つけられませんでした。",
            ]
            out.extend(self._zero_page_lines())
            if self.shadow_root_count:
                out.append(f"    # 影DOMを持つ要素が {self.shadow_root_count} 個ありました。")
                out.append("    #   影DOM/iframe の中は走査できません。一覧がその中にあると")
                out.append("    #   こう見えます。")
            return out

        row = self.row_groups[0].token
        if not self.ready and self.loaded_marker:
            # ペアが組めなかった媒体のための控え。ペアが組めたときはペアが本命 --
            # 行∨0件表示は「答えが見えた」を直接に意味し、0件側の証拠も
            # (観測できたすべての0件ページに存在) マーカーより厚い。
            out = [f"  {key}: {_scalar(self.loaded_marker)}"]
            out.append("    # **検索応答の描画後にのみ現れる要素です。** 結果ページと、")
            out.append("    # 読み込みが完了した0件ページの両方に存在し、遷移直後の")
            out.append("    # スナップショットには存在しないことを観測済み -- 件数に")
            out.append("    # 依らず「描画が終わった」を待てます。")
            for token in self.loaded_marker_alternatives:
                out.append(f"    # 別案: {token}")
            out.extend(self._zero_page_lines())
            out.extend(self._caveat_lines())
            return out
        if not self.ready:
            out = [f"  {key}: {UNRESOLVED_TOKEN}"]
            if self.rows_confirmed_vanishing:
                # 0件ページで消えることを確認できた行だけ、貼り付け用に出す。
                out.append(
                    f"    # 行: {row} ({len(self.row_groups[0].members)} 個) "
                    f"-- 0件検索で消えることを確認済み"
                )
                out.extend(self._zero_page_lines())
                out.extend(
                    [
                        "    # 0件表示の要素を特定できなかったため、値を出しません。",
                        "    # **行トークン単独を値にしないこと** -- 0件の検索が永久に",
                        "    # 待たされます。手で0件表示のセレクタを確認し、この形で記入を:",
                        f'    #   {key}: "{row}, <0件表示のセレクタ>"',
                    ]
                )
                return out
            # **0件ページが1枚も使えなかったときは、貼り付け用の形を出さない。**
            # 実測でここが牙を剥いた: 行が div.c-segment (画面の区画) と誤判定された
            # まま `"div.c-segment, <0件表示>"` という記入例を印字していた。
            # c-segment は描画前から常に在るので、指示どおり貼れば常に真になる目印が
            # 座標に入る -- このモジュールが潰すはずの失敗を、こちらから勧めていた。
            out.extend(
                [
                    "    # **0件ページを1枚も使えなかったため、行を確認できていません。**",
                    "    # 下は「繰り返し出現した構造」であって、0件検索で消えることは",
                    "    # 確認していません。**そのまま座標に書かないでください。**",
                ]
            )
            out.extend(
                f"    #   参考: {group.token} ({len(group.members)} 個)"
                for group in self.row_groups[:3]
            )
            out.extend(self._zero_page_lines())
            out.append("    # 0件ページを作れるようにするか、手で確認してください。")
            return out

        out = [f"  {key}: {_scalar(self.ready[0].selector())}"]
        out.append("    # 「行が出た **または** 0件表示が出た」で成立します (カンマは論理和)。")
        out.append(f"    #   行     : {self.ready[0].row_token}  (結果ページのみに存在)")
        out.append(f"    #   0件表示: {self.ready[0].empty_token}  (0件ページのみに存在)")
        for alternative in self.ready[1:]:
            out.append(f"    # 別案: {alternative.selector()}")
        if self.loaded_marker:
            out.append(f"    # 別案 (読み込み後にのみ現れる要素): {self.loaded_marker}")
        # **成功時も診断を出す。** 0件ページに何が残っていたかは、値が妥当かを
        # 後から検証する唯一の材料になる (構造スナップショットと突き合わせる)。
        out.extend(self._zero_page_lines())
        out.extend(self._caveat_lines())
        return out

    def _zero_page_lines(self) -> list[str]:
        """What happened to each zero-result page. **Absence needs a reason.**"""
        if not self.zero_pages:
            return [
                "    # 0件になる検索条件を1つも作れませんでした",
                "    # (URLに age[from]/age[to] も pagination[page] も見当たりません)。",
            ]
        out = ["    # 0件ページの試行:"]
        for kind, usable, reason in self.zero_pages:
            if usable:
                note = f" ({reason})" if reason else ""
                out.append(f"    #   - {kind}: 使えました{note}")
            else:
                out.append(f"    #   - {kind}: 使えません — {reason}")
        return out

    def _caveat_lines(self) -> list[str]:
        """Everything we could **not** confirm. 黙って値だけ出さない。"""
        out: list[str] = []
        if self.empty_state_single_variant:
            out.extend(
                [
                    "    # 0件ページは1種類しか使えませんでした。本番の0件表示が別の",
                    "    # 描画である可能性を排除できていません。外れた場合は待機の満了 =",
                    "    # **見える失敗** になり、静かなゼロ件にはなりません。",
                ]
            )
        if not self.rows_confirmed_vanishing:
            out.append("    # この行トークンが0件検索で消えることは確認していません。")
        if self.empty_state_scope == "page":
            out.extend(
                [
                    "    # アンカーが0件ページで一意に見つからなかったため、0件表示は",
                    "    # 画面全体から探しました。一覧領域の外の要素が混じっている",
                    "    # 可能性があります。",
                ]
            )
        if self.rows_outside_group > 0:
            out.append(f"    # 行トークンは一覧の外にも {self.rows_outside_group} 個ありました。")
        out.extend(
            [
                "    # この語は『行が1つ出た』で成立します。全件の描画完了を待ちたい場合は",
                "    # 構造が落ち着くまでの待機と組で使ってください。",
                "    # クラス名が次のデプロイまで生き残るかは、1回の観測では分かりません。",
            ]
        )
        return out

    def _drawer_lines(self) -> list[str]:
        key = "nav.drawer_close_selectors"
        if not self.drawer_attempted:
            if self.drawer_skip_reason:
                # 明示的な理由がある場合 (オフライン再生など)、状況から推定した
                # 文面を出さない。**再生でクリックしなかったのは実装の判断ではなく
                # 再生の性質** なので、推定文は嘘になる。
                return [f"  {key}: {UNRESOLVED_TOKEN}", f"    # {self.drawer_skip_reason}"]
            reason = (
                "候補者の行を特定できなかったため"
                if not self.row_groups
                else (
                    "行の中に、操作部品 (a/button/input/label/select/textarea) を"
                    "1つも含まない押せる領域が見つからなかったため"
                )
            )
            return [
                f"  {key}: {UNRESOLVED_TOKEN}",
                f"    # {reason}、ドロワーを試せませんでした。",
                "    # `button.js-tour-guide-scout-button` のような **取り消せない",
                "    # 外向き操作** を避けるため、押せる場所が確実でなければ押しません。",
                "    # 候補者を1件開き、閉じるボタンを開発者ツールで確認してください。",
            ]

        pressed = (
            f"{self.drawer_click_locator[0]} の {self.drawer_click_locator[1]} 番目"
            if self.drawer_click_locator
            else "(記録なし)"
        )
        if self.drawer_click_failed:
            # **押せていないことを「押したが無反応」と言わない。** 事実は
            # 「クリックが完了しなかった」であり、遮り要素があればそれが証拠。
            out = [
                f"  {key}: {UNRESOLVED_TOKEN}",
                f"    # {pressed} へのクリックが **完了しませんでした**",
                "    # (操作可能になるのを待って満了)。押せていないので、",
                "    # ドロワーが開くかどうかはまだ分かっていません。",
            ]
            if self.drawer_covering:
                out.append("    # クリック地点を覆っていた要素 (内側→外側):")
                out.extend(f"    #   - {token}" for token in self.drawer_covering)
                out.append("    #   (文言は出しません 13.2)")
                if any("tour" in token or "overlay" in token for token in self.drawer_covering):
                    out.append(
                        "    # ツアー案内らしき要素が画面を覆っています。実画面で一度"
                        "案内を閉じてから再実行すると通る可能性があります。"
                    )
            if self.tour_dismiss_failed:
                out.append(
                    "    # ツアーの閉じる操作を試みましたが、閉じられませんでした"
                    " (閉じる/スキップの文言を持つ部品が見つからないか、押しても"
                    "消えませんでした)。"
                )
            return out
        if self.drawer_url_changed:
            return [
                f"  {key}: {UNRESOLVED_TOKEN}",
                f"    # {pressed} を押しましたが、ドロワーではなく **ページ遷移** でした",
                f"    # (到達URL: {self.landed_url})。詳細が別画面で開く作りなら、",
                "    # この座標は不要かもしれません。",
            ]
        if not self.drawer_opened:
            return [
                f"  {key}: {UNRESOLVED_TOKEN}",
                f"    # {pressed} を押しましたが、新しい要素の出現を検知できませんでした。",
                "    # 実画面でドロワー/モーダルが開くか確認してください。",
            ]
        if not self.close_candidates:
            out = [
                f"  {key}: {UNRESOLVED_TOKEN}",
                f"    # {pressed} を押して新しい要素は出現しましたが、",
                "    # 閉じるボタンらしき要素が見つかりませんでした。",
            ]
            if self.drawer_evidence:
                out.append(f"    # 開いた後に増えた構造 ({len(self.drawer_evidence)}種):")
                out.extend(f"    #   - {token}" for token in self.drawer_evidence)
                out.append("    #   (文言は出しません。個人データを実行ログに残さないため 13.2)")
            return out

        primary = [candidate.selectors[0] for candidate in self.close_candidates]
        out = [f"  {key}: {_scalar(primary)}"]
        out.append(
            "    # 総当たりして駄目なら Escape、それでも消えなければ一覧へ再遷移"
            "してください (5.7)。上から順に試す前提の並びです。"
        )
        for candidate in self.close_candidates:
            out.append(f"    # 「{candidate.text}」候補: {', '.join(candidate.selectors)}")
        return out

    def yaml_block(self) -> str:
        """Only the lines meant to be pasted. **Must parse as YAML.**"""
        return "\n".join(
            line for line in self.render().splitlines() if line.startswith("  ") and line.strip()
        )


# --- ブラウザ依存部 (私は検証できない。運用者の実機確認に委ねる) ----------------


def analyze_candidate_list(capture: ListCapture, *, drawer_skip_reason: str = "") -> ObservedList:
    """Analyze a capture (live or replayed) into the stage-2 report. **Pure.**

    **実行時とオフライン再生が、この同じ関数を通る。** 解析コードが2系統に
    分かれると「再生では直ったが実行では直っていない」が起きるので、値の決定は
    ここに一本化してある。ブラウザ側 (:func:`observe_list`) は木を集めて渡すだけ、
    再生側 (``scout recon replay-list``) は保存された木を渡すだけである。

    ドロワーの観測は実際のクリックが要るので、ここには含まれない。再生では
    ``drawer_skip_reason`` にその旨を渡す。
    """
    observed, _usable = _analyze(capture)
    if drawer_skip_reason:
        observed = replace(observed, drawer_skip_reason=drawer_skip_reason)
    return observed


def _analyze(capture: ListCapture) -> tuple[ObservedList, list[Mapping[str, int]]]:
    """The shared analysis. Returns the report and the usable zero-page counts."""
    requested_url, landed_url = capture.requested_url, capture.landed_url
    tree = capture.results
    if tree is None:
        # **読めなかったのを「空だった」と読まない。** それが原則2の再生産になる。
        return (
            ObservedList(requested_url=requested_url, landed_url=landed_url, tree_read=False),
            [],
        )

    base = ObservedList(
        requested_url=requested_url,
        landed_url=landed_url,
        tree_read=True,
        tree_truncated=tree.truncated,
        shadow_root_count=tree.shadow_root_count,
    )
    if tree.truncated:
        return base, []

    sizes = subtree_sizes(tree)
    counts = token_counts(tree)

    # --- 0件ページの検査 -------------------------------------------------------
    # **行の同定にも入れ子の形にも依存しない量で判定する** (ZeroPage の docstring
    # に経緯)。結果ページで繰り返していたトークンの集合は、結果ページだけから決まる。
    repeated_tokens = {g.token for g in repeated_child_groups(tree, sizes)}
    # サイトの共通要素 (繰り返さないトークン = 什器)。0件ページがこれを保持して
    # いるかで「そもそも描画されたか」を測る (ZeroPage.chrome_overlap)。
    chrome_tokens = {token for token in counts if token not in repeated_tokens}

    # 消滅語彙は2系統 (経緯と使い分けは list_structure.transient_tokens /
    # vanished_tokens)。結果ページの遷移も語彙の供給源に含める。第3要素は
    # **直前ページの settled** -- SPA遷移では前ページの内容が遷移直後の1枚に
    # 写り込んで「消える」ため、残像の消滅を完了判定の証拠から外す (実測5回目)。
    navigations: list[tuple[Mapping[str, int], Mapping[str, int], Mapping[str, int] | None]] = []
    if capture.results_early is not None:
        navigations.append((token_counts(capture.results_early), counts, None))
    previous_settled: Mapping[str, int] | None = counts  # 0件変種の直前は結果ページ
    zero_prev: dict[str, Mapping[str, int] | None] = {}
    for z in capture.zeros:
        zero_prev[z.kind] = previous_settled
        if z.settled is not None:
            settled_counts = token_counts(z.settled)
            if z.early is not None:
                navigations.append((token_counts(z.early), settled_counts, previous_settled))
            previous_settled = settled_counts
    transients = transient_tokens(navigations)  # 完了判定用 (残像ガード付き)
    vanished = vanished_tokens(navigations)  # 0件表示候補の除外用 (ガード無し)

    zero_reports: list[tuple[str, bool, str]] = []
    usable_counts: list[Mapping[str, int]] = []
    usable_pages: list[DomTreeSnapshot] = []
    for zero in capture.zeros:
        zero_counts = token_counts(zero.settled) if zero.settled is not None else {}
        early_counts = token_counts(zero.early) if zero.early is not None else {}
        report = ZeroPage(
            kind=zero.kind,
            landed_url=zero.landed_url,
            tree_read=zero.settled is not None,
            tree_truncated=bool(zero.settled and zero.settled.truncated),
            counts=zero_counts,
            vanished_repeated_count=sum(
                1 for token in repeated_tokens if zero_counts.get(token, 0) == 0
            ),
            remaining_repeated_count=sum(
                1 for token in repeated_tokens if zero_counts.get(token, 0) > 0
            ),
            early_counts=early_counts,
            chrome_overlap=(
                sum(1 for token in chrome_tokens if zero_counts.get(token, 0) > 0)
                / len(chrome_tokens)
                if chrome_tokens
                else 1.0
            ),
        )
        usable, why = zero_page_is_usable(report, zero.url)
        if zero.loader_cleared is False:
            # 拒否はしない (高速に描画し終えたページと区別できないため) が、事実は
            # 必ず診断に残す。**「読み込み表示が残った」とは書かない** -- 実測4回目
            # で残っていたのは先に描画された0件表示であり、ローダーは剥がれていた。
            why = (why + " / " if why else "") + "遷移直後からの要素が一部残りました"
        zero_reports.append((zero.kind, usable, why))
        if usable and zero.settled is not None:
            usable_counts.append(zero_counts)
            usable_pages.append(
                DomTreeSnapshot(
                    zero.settled,
                    subtree_sizes(zero.settled),
                    empty_exclusions(early_counts, zero_counts, vanished, zero_prev.get(zero.kind)),
                )
            )

    rows = row_group_candidates(tree, sizes, usable_counts)

    # --- 行が見えている観測ページの和 (0件側の「結果ページに不在」判定用) ------
    # 実測5回目: ツアー案内 div.c-tour-guide は結果ページの撮影時にはまだ無く、
    # 0件ページの撮影時には在った。settled 2枚のXORを完璧に満たし、0件表示として
    # 推奨された -- 実際には数秒後の結果ページにも出る (クリック後の木で観測)。
    # 「行が1枚でも見えている観測」を全部結果側に合流させて、これを塞ぐ。
    rows_present = dict(counts)
    if rows:
        aux_counts: list[Mapping[str, int]] = []
        for aux in (capture.results_early, capture.after_click):
            if aux is not None:
                aux_counts.append(token_counts(aux))
        aux_counts.extend(token_counts(z.settled) for z in capture.zeros if z.settled is not None)
        rows_present = rows_present_union(rows[0].token, counts, aux_counts)

    # --- 0件表示 ---------------------------------------------------------------
    empties: tuple[EmptyCandidate, ...] = ()
    anchor_token = ""
    rows_outside = 0
    scope = ""
    if rows:
        region = list_region(tree, sizes, counts, rows[0])
        if region is not None:
            anchor_token = region.anchor_token
            rows_outside = region.rows_outside_group
        for snapshot in usable_pages:
            found = empty_state_candidates(
                snapshot.tree,
                snapshot.sizes,
                rows_present,
                anchor_token,
                snapshot.excluded_tokens,
            )
            if not found:
                continue
            scope = found[0].scope
            if not empties:
                empties = found
            else:
                # **すべての0件ページに在るものだけを残す。** 片方でしか出ない
                # 要素は「年齢の検証エラー画面」のような別物かもしれない。
                keep = {c.token for c in found}
                empties = tuple(c for c in empties if c.token in keep)

    ready = ready_values(rows, empties, counts, usable_counts, rows_present)

    # 読み込み後マーカー (ペアが組めない媒体のための控え。recon/list_structure.
    # post_load_markers 参照)。根拠に使う0件ページは **使用可能なものだけ**。
    # 検索条件が差し戻されて結果が並んだままのページを根拠にすると、本物の0件
    # 描画には現れない要素が目印になり得る -- 0件の検索が永久に待たされる (原則2)。
    # 「遷移直後に不在」の検査には0件変種の early だけを使う。結果ページの early は
    # 実測で既に描画後だった (25枚のカードごと写っていた) ので、そこに在ることは
    # 「遷移直後から在る」証拠にならない。
    zero_earlies = [token_counts(z.early) for z in capture.zeros if z.early is not None]
    finished_settleds = [zc for zc in usable_counts if zero_page_finished(zc, transients)]
    markers = post_load_markers(tree, counts, zero_earlies, finished_settleds)

    return (
        ObservedList(
            requested_url=requested_url,
            landed_url=landed_url,
            tree_read=True,
            shadow_root_count=tree.shadow_root_count,
            row_groups=rows[:5],
            rows_confirmed_vanishing=bool(usable_counts),
            empty_candidates=empties[:5],
            zero_pages=tuple(zero_reports),
            empty_state_single_variant=len(usable_pages) == 1,
            anchor_token=anchor_token,
            empty_state_scope=scope,
            rows_outside_group=rows_outside,
            loaded_marker=markers[0] if markers else "",
            loaded_marker_alternatives=markers[1:4],
            ready=ready,
        ),
        usable_counts,
    )


def _wait_out_transients(
    page: object,
    early: DomTree | None,
    reference_counts: Mapping[str, int],
    timeout_ms: int,
    learned: Collection[str] = (),
) -> bool | None:
    """Wait for observed transient chrome (loaders) to detach.

    待つ対象は2系統の和集合。どちらもこの実行自身の観測から導く (推測しない)。

    * **遷移直後のこのページに在り、読み込み完了後の結果ページには無い** トークン。
      枠は結果ページにも在るので含まれない。ただしこれは近似で、先に描画し終えた
      0件表示も混ざる (実測4回目: ``div.c-not-found--searches``)。その場合は満了
      まで待って False が返るだけで、撮影は続行される -- 時間を失うが嘘は撮らない。
    * ``learned`` -- **同じ実行の先行する遷移で「消えた」ことを観測済み** の語彙。
      遷移直後の1枚が起動前の骨組みしか写さないページ (実測の pagination 変種は
      26節点) では上の系統が空になり、待ち無しで読み込み途中を撮影していた。
      先行遷移で剥がれるのを見たローダーは、ここでも剥がれるまで待てる。

    戻り値: 消えた=True / 満了しても残った=False / 待つ対象が無かった=None。
    False は拒否理由にしない (先に描画し終えた0件表示と区別できないため)。
    診断として印字されるだけである。
    """
    if early is None:
        return None
    proxy = {
        token
        for token, count in token_counts(early).items()
        if count > 0 and reference_counts.get(token, 0) == 0
    }
    transients = sorted(proxy | set(learned))
    if not transients:
        return None
    return wait_for_all_detached(page, transients, timeout_ms)


#: ツアー案内の「閉じる」系の文言。部分一致で探す (小文字化して比較)。
TOUR_DISMISS_TEXTS = ("スキップ", "閉じる", "とじる", "終了", "あとで", "後で", "close", "skip")
#: ツアー案内のUI部品を指すセレクタ (実測5回目の覆い要素から)。
TOUR_SHELL_SELECTORS = ("a.c-tour-guide__overlay", "div.c-tour-guide")


def _dismiss_tour(page: Any, timeout_ms: int) -> bool | None:
    """Try to close the on-page tour guide. Returns None when no tour is present.

    実測5回目で、ツアー案内 (``div.c-tour-guide``) がカードを覆ってクリックを
    遮っていたことが確定した (覆い要素の直接観測)。実測6回目のスナップショットで
    吹き出しの中身も確定した: **汎用の ``button.c-button`` が1つだけ** で、
    「閉じる/スキップ」の部品は存在しない = 「次へ」型のツアーである。だから
    文言ヒントだけでは構造的に閉じられなかった。

    **閉じる操作は外向きの送信ではない** (画面案内のUIをこのアカウントの画面から
    消すだけ) ので、偵察が自分で押してよい数少ない操作に入る。3段の梯子で試す:

    1. 文言が「閉じる/スキップ」系の部品 (文言は読むが印字しない, 13.2)
    2. **進めて終わらせる**: 吹き出しの最後のボタン (戻る/次へ の並びなら次へ) を
       押し続ける。ツアーは有限の段数なので、最後まで進めれば消える。上限10回
    3. 背景 (``a.c-tour-guide__overlay``) を1度だけ押す

    どの段でも: 探索は **マウントされたツアー領域 (div.c-tour-guide) の中だけ**。
    ツアー本体の外に同じ吹き出しの隠しテンプレートが常駐しており (実測6回目)、
    範囲を広げるとそれを押そうとして満了する。class に ``scout`` を含む要素は
    文言に関係なく押さない -- ツアーはスカウトボタンを対象に係留されており
    (``js-tour-guide-scout-button``)、領域内にボタン実体が入り込む可能性を潰す
    (13.6)。ツアーの各段は対象を強調するだけで、対象そのものを押すことはない。
    """
    try:
        if not any(page.locator(s).count() > 0 for s in TOUR_SHELL_SELECTORS):
            return None

        def shell_gone(wait_ms: int) -> bool:
            return wait_for_all_detached(page, list(TOUR_SHELL_SELECTORS), wait_ms)

        # 1) 閉じる/スキップの文言を持つ部品 (最優先。現状の媒体には無いが、
        #    画面が変わって現れたら「進めて終わらせる」より1手で済む)
        candidates = page.locator("div.c-tour-guide a, div.c-tour-guide button")
        for index in range(min(candidates.count(), 12)):
            element = candidates.nth(index)
            class_attr = (element.get_attribute("class") or "").lower()
            if "scout" in class_attr:
                continue
            text = (element.inner_text() or "").strip().lower()
            if not text or not any(hint in text for hint in TOUR_DISMISS_TEXTS):
                continue
            element.click(timeout=timeout_ms)
            return shell_gone(timeout_ms)

        # 2) 進めて終わらせる。押すのは吹き出しの中のボタンだけ (オーバーレイや
        #    強調対象は押さない)。毎回セレクタを引き直す -- 段が進むと吹き出しは
        #    作り直される。
        for _ in range(10):
            buttons = page.locator("div.c-tour-guide div.c-tour-guide__tooltip button")
            advanced = False
            for index in range(buttons.count() - 1, -1, -1):
                element = buttons.nth(index)
                if "scout" in (element.get_attribute("class") or "").lower():
                    continue
                element.click(timeout=timeout_ms)
                advanced = True
                break
            if not advanced:
                break
            if shell_gone(1500):
                return True

        # 3) 背景を1度だけ押す (作りによっては背景クリックで閉じる)。
        overlay = page.locator("a.c-tour-guide__overlay")
        if overlay.count() > 0:
            with suppress(Exception):
                overlay.first.click(timeout=timeout_ms)
        return shell_gone(timeout_ms)
    except Exception:
        return False


def _covering_tokens(page: object, locator: tuple[str, int]) -> tuple[str, ...]:
    """Structure tokens of whatever sits under the click point, innermost first.

    読めなかったとき (None) と「遮り無し = 自分自身が居た」を混同しない --
    どちらも空でない結果を返せないが、報告では「覆っていた要素」を印字しない
    だけで済む。タグとクラスのみで、文言は含まない (13.2)。
    """
    rows = covering_rows(page, locator[0], locator[1])
    if rows is None:
        return ()
    out: list[str] = []
    for tag, class_names in rows:
        tokens = stable_tokens(tag, class_names)
        out.append(tokens[0] if tokens else tag)
    return tuple(dict.fromkeys(out))[:8]


def _second_row_locator(
    tree: DomTree, sizes: tuple[int, ...], row_members: tuple[int, ...]
) -> tuple[str, int] | None:
    """A safe click locator inside the second row, if any."""
    if len(row_members) < 2:
        return None
    target = safe_click_index(tree, sizes, row_members[1])
    if target is None:
        return None
    return click_locator(tree, target)


def observe_list(
    config: BrowserConfig,
    credentials_dir: Path,
    candidate_list_url: Coord[str],
) -> tuple[ObservedList, ListCapture | None]:
    """Open the authenticated candidate list and observe stage 2's remaining coordinates.

    **1回の実行で取れるものを全部取る。** 開発コンテナから媒体へ到達できないので、
    検証は運用者が GitHub Actions で行う -- つまり往復1回が運用者の手間1回である。
    途中で観測できないものがあっても、そこで打ち切らずに残りを続行する。

    戻り値の :class:`~recon.snapshot.ListCapture` は、この実行が読んだDOM構造の
    丸ごとである (結果ページ・0件ページ・クリック後)。呼び出し側がこれを保存すれば、
    **値が出なかった実行も、手元で解析を直す材料になる**。木を読めた場合は必ず返す。
    """
    requested_url = require(candidate_list_url, used_by="recon.observe_list.observe_list")

    session = session_store.session_path(credentials_dir)
    if not session.exists():
        return ObservedList(requested_url=requested_url, session_present=False), None

    with browser_context(config, storage_state=session) as (_context, page):
        goto(page, requested_url, config)
        wait_for_interactive(page, config.selector_timeout_ms)

        # **ここを「座標が見つからない」で片付けてはいけない。** 段階1で踏んだ
        # 取り違えと同じ形 (recon/observe_login.py 参照)。
        if login_form_visible(page, config.selector_timeout_ms):
            return (
                ObservedList(
                    requested_url=requested_url, session_expired=True, landed_url=page.url
                ),
                None,
            )

        landed_url = page.url
        if selection_redirected(requested_url, landed_url):
            return (
                ObservedList(
                    requested_url=requested_url,
                    landed_url=landed_url,
                    selection_required=True,
                    select_candidates=select_selector_candidates(select_fields(page)),
                    landing_structure=structure_sample(clickables(page)),
                ),
                None,
            )

        # --- 結果ページの構造 -------------------------------------------------
        # 遷移直後の1枚は診断用 (ローダー語彙の導出と、読み込み前後の区別の材料)。
        results_early = dom_tree(page)
        wait_for_structure_to_settle(page, config.selector_timeout_ms)
        tree = dom_tree(page)

        # --- 0件ページ (結果の木が読めた場合のみ意味がある) --------------------
        zeros: list[ZeroCapture] = []
        # 「消えたことを観測済み」のローダー語彙。遷移をまたいで蓄積する --
        # 遷移直後の1枚が薄いページ (起動前の骨組みだけ) では自分の語彙を導けず、
        # 待ち無しで読み込み途中を撮影してしまう (実測4回目の pagination 変種)。
        # **直前ページの残像の消滅は学ばない** -- SPA遷移では前ページの内容が
        # 遷移直後の1枚に写り込んで「消える」。それを学ぶと、以後の遷移で本物の
        # 0件表示の消滅を待って満了する (実測5回目で c-not-found がそうなりかけた)。
        learned: set[str] = set()
        if tree is not None and not tree.truncated:
            reference_counts = token_counts(tree)
            if results_early is not None:
                learned |= {
                    token
                    for token, count in token_counts(results_early).items()
                    if count > 0 and reference_counts.get(token, 0) == 0
                }
            previous_settled_counts: Mapping[str, int] = reference_counts
            for variant in zero_result_variants(requested_url):
                goto(page, variant.url, config)
                wait_for_interactive(page, config.selector_timeout_ms)
                # **落ち着く前に1枚撮る。** 読み込み中の骨組みはここに写る。0件表示の
                # 候補からそれを外さないと、未描画のページが最良の0件ページに見える。
                early_tree = dom_tree(page)
                # **読み込み表示の消滅を待つ。** 実測で、構造の静止だけでは XHR 待ちの
                # ローダー画面を「完了」と誤認した (0件ページ2枚ともローダーのまま
                # 撮影され、0件表示を一度も観測できなかった)。どの要素がローダーかは
                # 推測しない -- この実行自身の観測 (このページの遷移直後∖結果ページ、
                # および先行遷移で消滅を観測済みの語彙) の消滅を待つ。
                cleared = _wait_out_transients(
                    page, early_tree, reference_counts, config.selector_timeout_ms, learned
                )
                wait_for_structure_to_settle(page, config.selector_timeout_ms)
                settled_tree = dom_tree(page)
                if settled_tree is not None:
                    settled_counts = token_counts(settled_tree)
                    if early_tree is not None:
                        learned |= {
                            token
                            for token, count in token_counts(early_tree).items()
                            if count > 0
                            and settled_counts.get(token, 0) == 0
                            and previous_settled_counts.get(token, 0) == 0
                        }
                    # 直前ページは early の有無に依らず更新する。ここを怠ると、
                    # early を読めなかった変種を挟んだとき残像ガードが古いページと
                    # 比較して誤る。
                    previous_settled_counts = settled_counts
                zeros.append(
                    ZeroCapture(
                        kind=variant.kind,
                        url=variant.url,
                        landed_url=page.url,
                        early=early_tree,
                        settled=settled_tree,
                        loader_cleared=cleared,
                    )
                )

        capture = ListCapture(
            requested_url=requested_url,
            landed_url=landed_url,
            results=tree,
            zeros=tuple(zeros),
            results_early=results_early,
        )
        observed, usable_counts = _analyze(capture)
        if tree is None or tree.truncated:
            return observed, (capture if tree is not None else None)

        rows = observed.row_groups

        # --- ドロワーの閉じ方 --------------------------------------------------
        if not rows:
            return observed, capture

        # 結果ページへ戻る (0件ページを見た後なので)。**同じ待ちを掛ける** --
        # 実測でここが素通りし、ローダー画面から行を探して「行が現れない」まま
        # ドロワーを諦めていた (報告は誤ってクリック領域の不在を理由にしていた)。
        goto(page, requested_url, config)
        wait_for_interactive(page, config.selector_timeout_ms)
        fresh_early = dom_tree(page)
        _wait_out_transients(
            page, fresh_early, token_counts(tree), config.selector_timeout_ms, learned
        )
        wait_for_structure_to_settle(page, config.selector_timeout_ms)
        fresh_tree = dom_tree(page)
        if fresh_tree is None:
            return (
                replace(observed, drawer_skip_reason="再遷移後のDOMの木を読めませんでした。"),
                capture,
            )
        fresh_sizes = subtree_sizes(fresh_tree)
        members = [
            g
            for g in row_group_candidates(fresh_tree, fresh_sizes, usable_counts)
            if g.token == rows[0].token
        ]
        if not members:
            return (
                replace(
                    observed,
                    drawer_skip_reason=(
                        "再遷移した結果ページに行が現れませんでした "
                        "(読み込みが完了しなかった可能性)。"
                    ),
                ),
                capture,
            )

        # **操作部品を含まない領域だけを押す。** 行の中には送信ボタンがありうる。
        target = safe_click_index(fresh_tree, fresh_sizes, members[0].members[0])
        if target is None:
            return (
                replace(
                    observed,
                    drawer_skip_reason=(
                        "行の中に、操作部品 (a/button/input等) を1つも含まない押せる"
                        "領域が見つかりませんでした。取り消せない外向き操作を避けるため"
                        "押していません。"
                    ),
                ),
                capture,
            )
        locator = click_locator(fresh_tree, target)
        if locator is None:
            return (
                replace(
                    observed,
                    drawer_skip_reason="クリック対象を一意に指すセレクタを作れませんでした。",
                ),
                capture,
            )

        # ツアー案内がカードを覆い、クリックが完了しないことがある (実測5回目:
        # a.c-tour-guide__overlay が遮っていた)。Escape はキー入力1つで安全な
        # 閉じる試み。ツアーが居れば専用の閉じ操作も試す (_dismiss_tour)。
        with suppress(Exception):
            page.keyboard.press("Escape")
        tour_dismissed = _dismiss_tour(page, config.selector_timeout_ms)

        before = clickables(page)
        before_visible = sum(1 for c in before if c.visible)
        url_before = page.url
        covering: tuple[str, ...] = ()

        def _try_click(target_locator: tuple[str, int]) -> bool:
            try:
                page.locator(target_locator[0]).nth(target_locator[1]).click(
                    timeout=config.selector_timeout_ms
                )
            except Exception:
                return False
            return True

        clicked = _try_click(locator)
        if not clicked:
            # **押せなかった事実を「押したが無反応」にすり替えない** (実測4回目の
            # 報告がこれをやり、診断を誤導した)。何が遮っていたかを DOM から直接
            # 読み、クリック時点の木ごと持ち帰る。
            covering = _covering_tokens(page, locator)
            # ツアーが撮影後に遅れてマウントされることがある (実測5回目)。
            # 遮られたと分かったこの時点でもう一度閉じを試す。**失敗 (False) も
            # 捨てずに記録する** -- 捨てると「閉じを試みたが閉じられなかった」が
            # 報告から消える (反証レビューで確認された欠落)。
            second_dismiss = _dismiss_tour(page, config.selector_timeout_ms)
            if second_dismiss is not None:
                tour_dismissed = second_dismiss
            if second_dismiss:
                clicked = _try_click(locator)
        if not clicked:
            # 吹き出しは1枚目のカードに係留されがちなので、2枚目で1度だけ再試行。
            retry = _second_row_locator(fresh_tree, fresh_sizes, members[0].members)
            if retry is not None and _try_click(retry):
                clicked = True
                locator = retry
        if not clicked:
            # **クリック後の木を加えた最終形で解析をやり直す。** 行が見えている
            # ページの合流 (rows_present_union) はクリック後の木も証拠に使う。
            # クリック前の解析のまま報告すると、遅延マウントの要素 (ツアー) を
            # 0件表示として推奨する欠陥がライブの報告にだけ残る (replay では
            # 直っているのに)。報告と replay は必ず同じ解析を通す。
            capture = replace(capture, after_click=dom_tree(page))
            final, _ = _analyze(capture)
            return (
                replace(
                    final,
                    drawer_attempted=True,
                    drawer_click_locator=locator,
                    drawer_click_failed=True,
                    drawer_covering=covering,
                    tour_dismiss_failed=tour_dismissed is False,
                ),
                capture,
            )

        opened = wait_for_new_clickables(
            page,
            before_total=len(before),
            before_visible=before_visible,
            timeout_ms=config.selector_timeout_ms,
        )
        after = clickables(page)
        delta = tuple(c for c in newly_visible_clickables(before, after) if c.visible)
        # クリック後の木も持ち帰り、最終形で解析をやり直す (上の分岐と同じ理由)。
        capture = replace(capture, after_click=dom_tree(page))
        final, _ = _analyze(capture)
        return (
            replace(
                final,
                landed_url=page.url,
                drawer_attempted=True,
                drawer_click_locator=locator,
                drawer_opened=opened,
                drawer_url_changed=page.url != url_before,
                close_candidates=marker_candidates_from(
                    delta, text_hints=CLOSE_TEXT_HINTS, purpose_tokens=CLOSE_CLASS_TOKENS
                ),
                drawer_evidence=structure_sample(delta),
            ),
            capture,
        )
