"""Persist one run's DOM structure so development can continue offline.

このモジュールが存在する理由は、往復の値段である。

開発コンテナから媒体へ到達できないため、``observe-list`` の検証は運用者が
GitHub Actions で1回ずつ実行する。これまでの回り方は「アルゴリズムを送る →
実画面で失敗する → 失敗の要約だけを頼りに直す → もう1回実行してもらう」だった。
**1回の失敗ごとに運用者の往復が1回増え、しかも直しは要約からの再構成になる。**

そこで、実行が読んだDOM構造 (タグ・クラス・親子だけ) を丸ごと持ち帰る。

* 実行のたびに ``artifacts/recon/structure/`` へJSONを保存する
* 同じ内容を gzip+base64 のブロックとして実行ログにも印字する (実行基盤の
  アーティファクトを取得できない環境からでも、ログ経由で回収できるように)
* ``scout recon replay-list <file>`` が、保存された構造に対して **実行時と同一の
  解析コード** を走らせる

これで、値が出なかった実行も **無駄にならない** -- その実行が持ち帰った実データに
対して手元で解析を直し、直った解析を replay で確かめてから次の実行を頼める。
アルゴリズムの欠陥1つにつき往復1回、という比例関係が切れる。

13.2 (個人データ) について
--------------------------

スナップショットに入るのは :class:`browser.dom.DomTree` の内容だけである。
これは設計上 **タグ名・クラス名・親子関係しか持たない** (文言・id・href・属性値は
走査の時点で取っていない)。クラス名は制作者が書いた識別子であって個人データでは
ない。つまりこのファイルは 13.2 が言う「構造ダイジェスト」であり、レジュメの
生HTML (保持3日の最機微ダンプ) とは別物である。それでも置き場所は同じ
``recon_dump_dir`` に置き、同じ保持期間の管理下に入れる。
"""

from __future__ import annotations

import base64
import gzip
import json
from dataclasses import dataclass
from pathlib import Path

from jobmedley_scout.browser.dom import DomTree, build_tree

#: スナップショットの形式名。読み戻し側はこれを検査し、知らない形式を黙って読まない。
FORMAT = "jobmedley-scout/list-structure@1"

BLOCK_BEGIN = "----BEGIN JOBMEDLEY STRUCTURE v1----"
BLOCK_END = "----END JOBMEDLEY STRUCTURE v1----"

#: ログに埋め込むブロックの上限 (base64後の文字数)。超えたらログには出さず、
#: ファイル (アーティファクト) だけに頼る。ログを人間も読むので、際限なく
#: 膨らませない。
BLOCK_CHAR_LIMIT = 900_000


@dataclass(frozen=True)
class ZeroCapture:
    """One zero-result probe: what we asked for and what came back."""

    kind: str
    url: str
    landed_url: str
    #: 落ち着く前のスナップショット。読み込み表示の除外に使う。
    early: DomTree | None
    #: 落ち着いた後のスナップショット。
    settled: DomTree | None
    #: 読み込み表示 (観測から導いた一時要素) が消えるのを待てたか。
    #: None = 一時要素が無かった / 記録の無い旧スナップショット。
    loader_cleared: bool | None = None


@dataclass(frozen=True)
class ListCapture:
    """Everything one ``observe-list`` run read. The unit of offline replay."""

    requested_url: str
    landed_url: str
    results: DomTree | None
    zeros: tuple[ZeroCapture, ...]
    #: 結果ページの遷移直後のスナップショット (診断用。無い旧形式は None)。
    results_early: DomTree | None = None
    #: 行をクリックした後の木 (クリックできた場合のみ)。閉じるボタンの再解析用。
    after_click: DomTree | None = None


# --- DomTree <-> JSON ---------------------------------------------------------


def tree_to_payload(tree: DomTree | None) -> object:
    if tree is None:
        return None
    return {
        "truncated": tree.truncated,
        "shadow_roots": tree.shadow_root_count,
        # 配列3要素 [tag, classes, parent] の列。名前付きより2〜3割小さい。
        "nodes": [[n.tag, list(n.class_names), n.parent] for n in tree.nodes],
    }


def tree_from_payload(payload: object) -> DomTree | None:
    """Rebuild a tree, re-validating pre-order numbering.

    **読み戻しでも検証を省かない。** ファイルは編集できるので、実行時と同じ
    :func:`browser.dom.build_tree` の検査を通す。壊れていれば ``None`` --
    「読めなかった」であって「空だった」ではない。
    """
    if not isinstance(payload, dict):
        return None
    raw_nodes = payload.get("nodes")
    if not isinstance(raw_nodes, list):
        return None
    rows: list[tuple[str, tuple[str, ...], int]] = []
    for item in raw_nodes:
        if not (isinstance(item, list) and len(item) == 3):
            return None
        tag, classes, parent = item
        if not (isinstance(tag, str) and isinstance(classes, list) and isinstance(parent, int)):
            return None
        rows.append((tag, tuple(str(name) for name in classes), parent))
    return build_tree(
        rows,
        truncated=bool(payload.get("truncated")),
        shadow_root_count=int(payload.get("shadow_roots") or 0),
    )


# --- ListCapture <-> JSON -----------------------------------------------------


def capture_to_payload(capture: ListCapture) -> dict[str, object]:
    return {
        "format": FORMAT,
        "requested_url": capture.requested_url,
        "landed_url": capture.landed_url,
        "results": tree_to_payload(capture.results),
        "zero_pages": [
            {
                "kind": zero.kind,
                "url": zero.url,
                "landed_url": zero.landed_url,
                "early": tree_to_payload(zero.early),
                "settled": tree_to_payload(zero.settled),
                "loader_cleared": zero.loader_cleared,
            }
            for zero in capture.zeros
        ],
        "after_click": tree_to_payload(capture.after_click),
        "results_early": tree_to_payload(capture.results_early),
    }


def capture_from_payload(payload: object) -> ListCapture | None:
    if not isinstance(payload, dict) or payload.get("format") != FORMAT:
        return None
    raw_zeros = payload.get("zero_pages")
    if not isinstance(raw_zeros, list):
        return None
    zeros: list[ZeroCapture] = []
    for item in raw_zeros:
        if not isinstance(item, dict):
            return None
        zeros.append(
            ZeroCapture(
                kind=str(item.get("kind", "")),
                url=str(item.get("url", "")),
                landed_url=str(item.get("landed_url", "")),
                early=tree_from_payload(item.get("early")),
                settled=tree_from_payload(item.get("settled")),
                loader_cleared=item.get("loader_cleared")
                if isinstance(item.get("loader_cleared"), bool)
                else None,
            )
        )
    return ListCapture(
        requested_url=str(payload.get("requested_url", "")),
        landed_url=str(payload.get("landed_url", "")),
        results=tree_from_payload(payload.get("results")),
        zeros=tuple(zeros),
        after_click=tree_from_payload(payload.get("after_click")),
        results_early=tree_from_payload(payload.get("results_early")),
    )


# --- ログへの埋め込み ----------------------------------------------------------

_B64_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")


def encode_block(payload: dict[str, object]) -> str:
    """gzip+base64 のブロック。実行ログに印字して、後からログ経由で回収する。"""
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    encoded = base64.b64encode(gzip.compress(raw)).decode("ascii")
    lines = [encoded[i : i + 120] for i in range(0, len(encoded), 120)]
    return "\n".join([BLOCK_BEGIN, *lines, BLOCK_END])


def decode_block(text: str) -> dict[str, object] | None:
    """Recover the payload from log text, or ``None``.

    **実行基盤のログはタイムスタンプ等を行頭に付ける** ので、各行の最後の
    空白区切りトークンだけを base64 として拾う。マーカーの外は読まない。
    """
    lines = text.splitlines()
    try:
        begin = next(i for i, line in enumerate(lines) if BLOCK_BEGIN in line)
        end = next(i for i, line in enumerate(lines) if BLOCK_END in line and i > begin)
    except StopIteration:
        return None
    chunks: list[str] = []
    for line in lines[begin + 1 : end]:
        token = line.split()[-1] if line.split() else ""
        if token and set(token) <= _B64_CHARS:
            chunks.append(token)
    try:
        raw = gzip.decompress(base64.b64decode("".join(chunks)))
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


# --- 保存と案内 ----------------------------------------------------------------


def save_capture(capture: ListCapture, recon_dump_dir: Path) -> Path:
    """Write the snapshot under ``recon_dump_dir`` and return its path.

    ファイル名は固定 (最新の1枚だけを保つ)。実行基盤は使い捨てなので、履歴は
    アーティファクトの保持期間が持つ。
    """
    directory = recon_dump_dir / "structure"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "observe_list.json"
    payload = capture_to_payload(capture)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def load_capture(text: str) -> ListCapture | None:
    """Read a capture from raw JSON, or from log text containing an encoded block."""
    if BLOCK_BEGIN in text:
        payload = decode_block(text)
    else:
        try:
            payload = json.loads(text)
        except ValueError:
            return None
    return capture_from_payload(payload) if payload is not None else None


def render_snapshot_footer(path: Path, capture: ListCapture) -> str:
    """The note printed after the report: where the structure went, and why."""
    block = encode_block(capture_to_payload(capture))
    lines = [
        "--- 構造スナップショット ---",
        f"この実行が読んだDOM構造を保存しました: {path}",
        "内容はタグ名・クラス名・親子関係のみで、文言・id・属性値は含みません (13.2)。",
        "**この保存があるので、値が出なかった実行も無駄になりません** --",
        "保存された実データに対して解析を手元で直し (scout recon replay-list)、",
        "直ったことを確かめてから次の実行を頼めます。",
        "",
        "下のブロックは同じ内容の圧縮版です (ログ経由の回収用)。**貼り付け不要です。**",
    ]
    if len(block) > BLOCK_CHAR_LIMIT:
        lines.append(
            f"(ブロックが上限 {BLOCK_CHAR_LIMIT} 文字を超えたため印字しません。"
            f"アーティファクト {path.name} を使ってください)"
        )
    else:
        lines.append(block)
    return "\n".join(lines)
