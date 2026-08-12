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

行とコンテナの見分け方
----------------------

``nav.list_ready_selector`` の正しい候補は「検索結果があるページと、0件のページの
**両方** に存在する要素」である。結果ページにしか無い要素は、繰り返し出現する
「行」であり、0件検索では消えるので使えない。

0件のページを作るために、``nav.candidate_list_url`` の年齢帯を人間が存在しない
範囲 (``age[from]=120`` 等) にずらした変種を **その場で** 作って比較する。これは
座標として保存する値ではなく、比較のためだけの一時的なURLである。年齢帯の
パラメータが元のURLに無い場合はこの比較ができないので、その旨を報告して
UNRESOLVED のまま人間に委ねる。

ドロワーの開き方
----------------

「行らしいが0件で消える」候補のうち最有力のものを実際にクリックし、新しい要素の
出現 (:func:`browser.dom.wait_for_more_clickables`) を観測してドロワー/モーダルが
開いたと判断する。開けなかった場合や、開いた後に閉じるボタンらしき要素が
見つからなかった場合は、それぞれ理由を添えて UNRESOLVED のまま報告する。

判定ロジックは純粋関数に置いてある (13.4)。本モジュールはそれらへ値を運ぶだけで、
**判断はしない**。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from jobmedley_scout.browser import session_store
from jobmedley_scout.browser.context import browser_context
from jobmedley_scout.browser.dom import (
    ClassedElement,
    SelectField,
    classed_elements,
    clickables,
    login_form_visible,
    select_fields,
    wait_for_interactive,
    wait_for_more_clickables,
    wait_for_structure_to_settle,
)
from jobmedley_scout.browser.navigation import goto
from jobmedley_scout.config.placeholders import UNRESOLVED_TOKEN, Coord, require
from jobmedley_scout.config.schema import BrowserConfig
from jobmedley_scout.recon.manual_login import (
    MarkerCandidate,
    form_field_selector_candidates,
    is_stable_class_name,
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
        (key, "120")
        if key == "age[from]"
        else (key, "121")
        if key == "age[to]"
        else (key, value)
        for key, value in params
    ]
    # ``safe="[]"`` で元のURLの書式 (角括弧を%エンコードしない) を保つ。
    return urlunsplit(parts._replace(query=urlencode(replaced, safe="[]")))


def class_frequency(elements: Iterable[ClassedElement]) -> dict[str, int]:
    """How many times each ``tag.class`` token appears. **Pure.**

    ハッシュめいたクラス名は数えない -- ビルドのたびに変わるので、頻度を数えても
    次のデプロイで意味を失う候補になる (:func:`recon.manual_login.is_stable_class_name`)。
    """
    counts: dict[str, int] = {}
    for element in elements:
        for name in element.class_names:
            if not is_stable_class_name(name):
                continue
            token = f"{element.tag}.{name}"
            counts[token] = counts.get(token, 0) + 1
    return counts


def list_ready_candidates(
    with_results: Mapping[str, int], zero_result: Mapping[str, int]
) -> tuple[str, ...]:
    """Container candidates for ``nav.list_ready_selector``. **Pure.**

    **行そのものではなく行のコンテナを選ぶと空結果でも待てる。** 検索結果ページと
    比較用の0件ページの **両方** に存在するトークンだけを候補にする -- 結果ページに
    しか無いトークンは、0件の検索を「まだ描画されていない」と誤読させる行である
    (:func:`rows_that_vanish_on_empty_results` が別枠で報告する)。

    件数の変動が小さいものを優先する。真のコンテナは検索件数によらず件数が
    安定する (通常1個のまま) はずだからである。
    """
    candidates = [token for token in with_results if zero_result.get(token, 0) > 0]

    def priority(token: str) -> tuple[int, int]:
        return (abs(with_results[token] - zero_result.get(token, 0)), with_results[token])

    return tuple(sorted(candidates, key=priority))


def rows_that_vanish_on_empty_results(
    with_results: Mapping[str, int], zero_result: Mapping[str, int]
) -> tuple[str, ...]:
    """Tokens that repeat on the results page but vanish on the empty one. **Pure.**

    **これは ``nav.list_ready_selector`` には使えない。** 0件の検索を「まだ描画
    されていない」と誤読させるので、あえて別枠で「避けるべき候補」として報告する。
    2回以上observedのものだけを対象にする -- 1回しか無いものは行ではなく単発の
    見出し等である可能性が高い。

    同時に、これが「候補者の行」の最有力候補でもある。ドロワーを開く実験
    (:func:`observe_list`) はここで最も件数の多いトークンをクリック対象に使う。
    """
    candidates = [
        token
        for token, count in with_results.items()
        if count >= 2 and zero_result.get(token, 0) == 0
    ]
    return tuple(sorted(candidates, key=lambda token: -with_results[token]))


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
    #: 0件になる検索条件と比較できたか。
    zero_result_comparable: bool = False
    #: nav.list_ready_selector の候補 (0件検索でも残る)。
    list_ready_candidates: tuple[str, ...] = ()
    #: 行らしいが0件検索で消える候補。**nav.list_ready_selector には使えない。**
    list_ready_vanishing_rows: tuple[str, ...] = ()
    #: ドロワーを開くために実際にクリックしたセレクタ。試さなかった場合は空文字。
    drawer_row_selector_tried: str = ""
    #: クリック自体を試みたか。
    drawer_attempted: bool = False
    #: クリック後に新しい要素の出現を検知できたか。
    drawer_opened: bool = False
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
        if not self.zero_result_comparable:
            out = [
                f"  nav.list_ready_selector: {UNRESOLVED_TOKEN}",
                "    # 0件になる検索条件と比較できませんでした",
                "    # (URLに age[from]/age[to] が見当たりません)。",
                "    # 0件の検索結果でも残る要素を手で確認してください。",
                "    # **行そのものではなく行のコンテナを選ぶこと** -- 行は0件で消えます。",
            ]
        elif self.list_ready_candidates:
            out = [f"  nav.list_ready_selector: {_scalar(self.list_ready_candidates[0])}"]
            out.append("    # 検索結果0件のページと比較し、両方に残っていた要素です。")
            out.extend(f"    # 別案: {alt}" for alt in self.list_ready_candidates[1:])
        else:
            out = [
                f"  nav.list_ready_selector: {UNRESOLVED_TOKEN}",
                "    # 0件でも残るコンテナ候補が見つかりませんでした。",
            ]
        if self.list_ready_vanishing_rows:
            out.append("    # 避けるべき候補 (行らしいが0件検索で消えるため使えません):")
            out.extend(f"    #   - {token}" for token in self.list_ready_vanishing_rows)
        return out

    def _drawer_lines(self) -> list[str]:
        if not self.drawer_attempted:
            return [
                f"  nav.drawer_close_selectors: {UNRESOLVED_TOKEN}",
                "    # 候補者の行を特定できなかったため、ドロワーを試せませんでした。",
                "    # 候補者を1件クリックして開き、閉じるボタンを",
                "    # 開発者ツールで確認してください。",
            ]
        if not self.drawer_opened:
            return [
                f"  nav.drawer_close_selectors: {UNRESOLVED_TOKEN}",
                f"    # 行 ({self.drawer_row_selector_tried}) をクリックしましたが、",
                "    # 新しい要素の出現を検知できませんでした。",
                "    # 実画面でドロワー/モーダルが開くか確認してください。",
            ]
        if not self.close_candidates:
            out = [
                f"  nav.drawer_close_selectors: {UNRESOLVED_TOKEN}",
                f"    # 行 ({self.drawer_row_selector_tried}) をクリックして新しい要素は",
                "    # 出現しましたが、閉じるボタンらしき要素が見つかりませんでした。",
            ]
            if self.drawer_evidence:
                out.append(f"    # 開いた後にあった構造 ({len(self.drawer_evidence)}種):")
                out.extend(f"    #   - {token}" for token in self.drawer_evidence)
            return out

        primary = [candidate.selectors[0] for candidate in self.close_candidates]
        out = [f"  nav.drawer_close_selectors: {_scalar(primary)}"]
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
    """Open the authenticated candidate list and observe stage 2's remaining coordinates."""
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
                requested_url=requested_url,
                session_expired=True,
                landed_url=page.url,
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

        # --- 選択ステップは無い。一覧の描画完了を待ってから構造を数える -----------
        wait_for_structure_to_settle(page, config.selector_timeout_ms)
        with_results = class_frequency(classed_elements(page))

        zero_url = zero_result_variant(requested_url)
        recommended: tuple[str, ...] = ()
        vanishing: tuple[str, ...] = ()

        if zero_url is not None:
            goto(page, zero_url, config)
            wait_for_interactive(page, config.selector_timeout_ms)
            wait_for_structure_to_settle(page, config.selector_timeout_ms)
            zero_result = class_frequency(classed_elements(page))
            recommended = list_ready_candidates(with_results, zero_result)
            vanishing = rows_that_vanish_on_empty_results(with_results, zero_result)

            # 結果ページへ戻る。ドロワーを開くには実際の候補者の行が要る。
            goto(page, requested_url, config)
            wait_for_interactive(page, config.selector_timeout_ms)
            wait_for_structure_to_settle(page, config.selector_timeout_ms)

        # --- ドロワーの閉じ方を観測する (行が特定できた場合のみ) --------------------
        row_selector = vanishing[0] if vanishing else ""
        drawer_opened = False
        close_candidates: tuple[MarkerCandidate, ...] = ()
        drawer_evidence: tuple[str, ...] = ()

        if row_selector:
            before = clickables(page)
            try:
                page.click(row_selector, timeout=config.selector_timeout_ms)
            except Exception:
                drawer_opened = False
            else:
                drawer_opened = wait_for_more_clickables(
                    page, len(before), config.selector_timeout_ms
                )
                if drawer_opened:
                    after = clickables(page)
                    delta = tuple(element for element in after if element not in before)
                    close_candidates = marker_candidates_from(
                        delta, text_hints=CLOSE_TEXT_HINTS, purpose_tokens=CLOSE_CLASS_TOKENS
                    )
                    drawer_evidence = structure_sample(delta)

        return ObservedList(
            requested_url=requested_url,
            landed_url=landed_url,
            selection_required=False,
            zero_result_comparable=zero_url is not None,
            list_ready_candidates=recommended,
            list_ready_vanishing_rows=vanishing,
            drawer_row_selector_tried=row_selector,
            drawer_attempted=bool(row_selector),
            drawer_opened=drawer_opened,
            close_candidates=close_candidates,
            drawer_evidence=drawer_evidence,
        )
