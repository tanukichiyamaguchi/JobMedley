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
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from jobmedley_scout.browser import session_store
from jobmedley_scout.browser.context import browser_context
from jobmedley_scout.browser.dom import (
    Clickable,
    DomTree,
    SelectField,
    clickables,
    dom_tree,
    login_form_visible,
    select_fields,
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
    empty_state_candidates,
    list_region,
    ready_values,
    repeated_child_groups,
    row_group_candidates,
    safe_click_index,
    subtree_sizes,
    token_counts,
)
from jobmedley_scout.recon.manual_login import (
    MarkerCandidate,
    form_field_selector_candidates,
    marker_candidates_from,
    structure_sample,
)
from jobmedley_scout.recon.yaml_paste import yaml_scalar as _scalar

#: 閉じる系コントロールの探索語。ログアウトと同じ仕組み
#: (:func:`recon.manual_login.marker_candidates_from`) を語彙だけ差し替えて使う。
CLOSE_TEXT_HINTS: tuple[str, ...] = ("閉じる", "close", "Close", "×", "✕", "✖")
#: クラス名が「閉じるための要素」と名乗っているか。
CLOSE_CLASS_TOKENS: tuple[str, ...] = ("close", "dismiss")

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
    """A zero-result page's tree with its precomputed subtree sizes."""

    tree: DomTree
    sizes: tuple[int, ...]


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
    #: 結果ページで繰り返していた群のうち、このページで消えたものの数。
    #:
    #: **特定の行トークンに依存させない。** 当初は「選ばれた行トークンがこのページに
    #: 何個あるか」で判定していたが、行の同定を誤ると0件ページまで巻き添えで捨てられる。
    #: 実測で起きた: 行が ``div.c-segment`` と誤判定され、それが0件ページにも1個
    #: 残っていたため、**0件ページが2枚とも「0件になっていない」と判定された**。
    #: 行が正しいかに関わらず「結果ページで繰り返していた何かが消えた」は観測できる。
    vanished_group_count: int


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
    if page.vanished_group_count == 0:
        return False, (
            "結果ページで繰り返していた要素が1つも消えていません "
            "(遷移が失敗して結果ページのままの可能性)"
        )
    return True, ""


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
    #: nav.list_ready_selector の推奨値と別案。**先頭が推奨。**
    ready: tuple[ReadyValue, ...] = ()
    #: ドロワーを開くために実際に押した ``(セレクタ, 文書順)``。押していなければ None。
    drawer_click_locator: tuple[str, int] | None = None
    #: クリック自体を試みたか。
    drawer_attempted: bool = False
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
        if not self.ready:
            out = [
                f"  {key}: {UNRESOLVED_TOKEN}",
                f"    # 行は同定できました: {row} ({len(self.row_groups[0].members)} 個)",
            ]
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

        out = [f"  {key}: {_scalar(self.ready[0].selector())}"]
        out.append("    # 「行が出た **または** 0件表示が出た」で成立します (カンマは論理和)。")
        out.append(f"    #   行     : {self.ready[0].row_token}  (結果ページのみに存在)")
        out.append(f"    #   0件表示: {self.ready[0].empty_token}  (0件ページのみに存在)")
        for alternative in self.ready[1:]:
            out.append(f"    # 別案: {alternative.selector()}")
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
            out.append(f"    #   - {kind}: {'使えました' if usable else f'使えません — {reason}'}")
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


def observe_list(
    config: BrowserConfig,
    credentials_dir: Path,
    candidate_list_url: Coord[str],
) -> ObservedList:
    """Open the authenticated candidate list and observe stage 2's remaining coordinates.

    **1回の実行で取れるものを全部取る。** 開発コンテナから媒体へ到達できないので、
    検証は運用者が GitHub Actions で行う -- つまり往復1回が運用者の手間1回である。
    途中で観測できないものがあっても、そこで打ち切らずに残りを続行する。
    """
    requested_url = require(candidate_list_url, used_by="recon.observe_list.observe_list")

    session = session_store.session_path(credentials_dir)
    if not session.exists():
        return ObservedList(requested_url=requested_url, session_present=False)

    with browser_context(config, storage_state=session) as (_context, page):
        goto(page, requested_url, config)
        wait_for_interactive(page, config.selector_timeout_ms)

        # **ここを「座標が見つからない」で片付けてはいけない。** 段階1で踏んだ
        # 取り違えと同じ形 (recon/observe_login.py 参照)。
        if login_form_visible(page, config.selector_timeout_ms):
            return ObservedList(
                requested_url=requested_url, session_expired=True, landed_url=page.url
            )

        landed_url = page.url
        if selection_redirected(requested_url, landed_url):
            return ObservedList(
                requested_url=requested_url,
                landed_url=landed_url,
                selection_required=True,
                select_candidates=select_selector_candidates(select_fields(page)),
                landing_structure=structure_sample(clickables(page)),
            )

        # --- 結果ページの構造 -------------------------------------------------
        wait_for_structure_to_settle(page, config.selector_timeout_ms)
        tree = dom_tree(page)
        if tree is None:
            # **読めなかったのを「空だった」と読まない。** それが原則2の再生産になる。
            return ObservedList(requested_url=requested_url, landed_url=landed_url, tree_read=False)

        base = ObservedList(
            requested_url=requested_url,
            landed_url=landed_url,
            tree_read=True,
            tree_truncated=tree.truncated,
            shadow_root_count=tree.shadow_root_count,
        )
        if tree.truncated:
            return base

        sizes = subtree_sizes(tree)
        counts = token_counts(tree)

        # --- 0件ページを、作れるだけ作って検査する ----------------------------
        # **暫定の行トークンに依存させない。** 行の同定を誤ると0件ページまで
        # 巻き添えで捨てられ、何も確定しないまま終わる (実測でそうなった)。
        # 「結果ページで繰り返していた群のどれかが消えた」は行の正しさに関わらず
        # 観測できるので、そちらを判定に使う。
        repeated_tokens = {g.token for g in repeated_child_groups(tree, sizes)}

        zero_reports: list[tuple[str, bool, str]] = []
        usable_counts: list[Mapping[str, int]] = []
        usable_pages: list[tuple[str, DomTreeSnapshot]] = []
        for variant in zero_result_variants(requested_url):
            goto(page, variant.url, config)
            wait_for_interactive(page, config.selector_timeout_ms)
            wait_for_structure_to_settle(page, config.selector_timeout_ms)
            zero_tree = dom_tree(page)
            zero_counts = token_counts(zero_tree) if zero_tree is not None else {}
            report = ZeroPage(
                kind=variant.kind,
                landed_url=page.url,
                tree_read=zero_tree is not None,
                tree_truncated=bool(zero_tree and zero_tree.truncated),
                counts=zero_counts,
                vanished_group_count=sum(
                    1 for token in repeated_tokens if zero_counts.get(token, 0) == 0
                ),
            )
            usable, why = zero_page_is_usable(report, variant.url)
            zero_reports.append((variant.kind, usable, why))
            if usable and zero_tree is not None:
                usable_counts.append(zero_counts)
                usable_pages.append(
                    (variant.kind, DomTreeSnapshot(zero_tree, subtree_sizes(zero_tree)))
                )

        rows = row_group_candidates(tree, sizes, usable_counts)

        # --- 0件表示 ----------------------------------------------------------
        empties: tuple[EmptyCandidate, ...] = ()
        anchor_token = ""
        rows_outside = 0
        scope = ""
        if rows:
            region = list_region(tree, sizes, counts, rows[0])
            if region is not None:
                anchor_token = region.anchor_token
                rows_outside = region.rows_outside_group
            for _kind, snapshot in usable_pages:
                found = empty_state_candidates(snapshot.tree, snapshot.sizes, counts, anchor_token)
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

        ready = ready_values(rows, empties, counts, usable_counts)

        observed = ObservedList(
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
            ready=ready,
        )

        # --- ドロワーの閉じ方 --------------------------------------------------
        if not rows:
            return observed

        # 結果ページへ戻る (0件ページを見た後なので)。
        goto(page, requested_url, config)
        wait_for_interactive(page, config.selector_timeout_ms)
        wait_for_structure_to_settle(page, config.selector_timeout_ms)
        fresh_tree = dom_tree(page)
        if fresh_tree is None:
            return observed
        fresh_sizes = subtree_sizes(fresh_tree)
        members = [
            g
            for g in row_group_candidates(fresh_tree, fresh_sizes, usable_counts)
            if g.token == rows[0].token
        ]
        if not members:
            return observed

        # **操作部品を含まない領域だけを押す。** 行の中には送信ボタンがありうる。
        target = safe_click_index(fresh_tree, fresh_sizes, members[0].members[0])
        if target is None:
            return observed
        locator = click_locator(fresh_tree, target)
        if locator is None:
            return observed

        before = clickables(page)
        before_visible = sum(1 for c in before if c.visible)
        url_before = page.url
        try:
            page.locator(locator[0]).nth(locator[1]).click(timeout=config.selector_timeout_ms)
        except Exception:
            return replace(observed, drawer_attempted=True, drawer_click_locator=locator)

        opened = wait_for_new_clickables(
            page,
            before_total=len(before),
            before_visible=before_visible,
            timeout_ms=config.selector_timeout_ms,
        )
        after = clickables(page)
        delta = tuple(c for c in newly_visible_clickables(before, after) if c.visible)
        return replace(
            observed,
            landed_url=page.url,
            drawer_attempted=True,
            drawer_click_locator=locator,
            drawer_opened=opened,
            drawer_url_changed=page.url != url_before,
            close_candidates=marker_candidates_from(
                delta, text_hints=CLOSE_TEXT_HINTS, purpose_tokens=CLOSE_CLASS_TOKENS
            ),
            drawer_evidence=structure_sample(delta),
        )
