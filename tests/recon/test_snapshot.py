"""構造スナップショットの往復を固定する。

守りたいのは1点: **保存 → 読み戻し → 解析が、実行時の解析と同じ答えを出すこと。**
再生 (replay) の存在意義は「再生で直れば実行でも直る」なので、往復のどこかで
情報が欠けると、その保証ごと崩れる。
"""

from __future__ import annotations

from pathlib import Path

from jobmedley_scout.browser.dom import DomNode, DomTree
from jobmedley_scout.recon.snapshot import (
    BLOCK_BEGIN,
    BLOCK_END,
    ListCapture,
    ZeroCapture,
    capture_from_payload,
    capture_to_payload,
    decode_block,
    encode_block,
    load_capture,
    render_snapshot_footer,
    save_capture,
    tree_from_payload,
    tree_to_payload,
)


def _tree(*rows: tuple[str, tuple[str, ...], int]) -> DomTree:
    return DomTree(
        nodes=tuple(DomNode(tag=t, class_names=c, parent=p) for t, c, p in rows),
        truncated=False,
        shadow_root_count=0,
    )


RESULTS = _tree(
    ("body", ("c-body",), -1),
    ("div", ("list",), 0),
    ("div", ("card",), 1),
    ("div", ("card",), 1),
)
ZERO = _tree(("body", ("c-body",), -1), ("div", ("list",), 0), ("div", ("empty",), 1))

CAPTURE = ListCapture(
    requested_url="https://customers.job-medley.com/customers/searches?age[to]=40",
    landed_url="https://customers.job-medley.com/customers/searches?age[to]=40",
    results=RESULTS,
    zeros=(
        ZeroCapture(
            kind="age",
            url="https://x/s?age[from]=120",
            landed_url="https://x/s?age[from]=120",
            early=None,
            settled=ZERO,
        ),
    ),
    after_click=None,
)


# --- 木の往復 ------------------------------------------------------------------


def test_a_tree_survives_the_payload_round_trip() -> None:
    assert tree_from_payload(tree_to_payload(RESULTS)) == RESULTS


def test_none_stays_none() -> None:
    """「読めなかった」は保存しても「読めなかった」のまま。空の木に化けない。"""
    assert tree_to_payload(None) is None
    assert tree_from_payload(None) is None


def test_a_corrupted_parent_is_rejected_on_load() -> None:
    """**読み戻しでも検証を省かない。** ファイルは編集できる。

    前順採番が壊れた木を通すと、包含判定が全部嘘になったまま解析が走る。
    """
    payload = tree_to_payload(RESULTS)
    assert isinstance(payload, dict)
    payload["nodes"][2][2] = 5  # 親が自分より後ろ = 前順採番の破れ

    assert tree_from_payload(payload) is None


def test_a_capture_survives_the_payload_round_trip() -> None:
    assert capture_from_payload(capture_to_payload(CAPTURE)) == CAPTURE


def test_an_unknown_format_is_refused() -> None:
    """知らない形式を黙って読まない。座標の材料になるデータなので。"""
    payload = capture_to_payload(CAPTURE)
    payload["format"] = "somebody-else@9"

    assert capture_from_payload(payload) is None


# --- ログ経由の往復 -------------------------------------------------------------


def test_the_encoded_block_survives_log_timestamp_prefixes() -> None:
    """**実行基盤のログは行頭にタイムスタンプを付ける。** その形から復元できること。

    ここが通らないと「ログ経由で回収できる」という前提が崩れ、
    アーティファクトを取得できない環境で往復が1回無駄になる。
    """
    block = encode_block(capture_to_payload(CAPTURE))
    stamped = "\n".join(f"2026-08-13T01:23:45.6789Z {line}" for line in block.splitlines())

    payload = decode_block(stamped)

    assert payload is not None
    assert capture_from_payload(payload) == CAPTURE


def test_load_capture_accepts_both_raw_json_and_log_text() -> None:
    import json

    payload = capture_to_payload(CAPTURE)

    assert load_capture(json.dumps(payload)) == CAPTURE
    assert load_capture("前置きの行\n" + encode_block(payload) + "\n後置きの行") == CAPTURE


def test_garbage_between_markers_yields_none_not_a_crash() -> None:
    assert decode_block(f"{BLOCK_BEGIN}\nこれはbase64ではない\n{BLOCK_END}") is None


def test_missing_markers_yield_none() -> None:
    assert decode_block("ただのログ") is None


# --- 保存と案内 -----------------------------------------------------------------


def test_save_and_load_through_a_real_file(tmp_path: Path) -> None:
    path = save_capture(CAPTURE, tmp_path)

    assert path.parent.name == "structure"
    assert load_capture(path.read_text(encoding="utf-8")) == CAPTURE


def test_the_footer_says_what_is_inside_and_that_pasting_is_not_needed(
    tmp_path: Path,
) -> None:
    """運用者への案内。**何が入っていて、何をしなくてよいか** を言う。

    巨大なブロックを見た運用者が「これを貼るのか」と迷うのが実害なので、
    貼り付け不要であることと、文言を含まないこと (13.2) を明記する。
    """
    path = save_capture(CAPTURE, tmp_path)
    footer = render_snapshot_footer(path, CAPTURE)

    assert "文言・id・属性値は含みません" in footer
    assert "貼り付け不要" in footer
    assert "replay-list" in footer
    assert BLOCK_BEGIN in footer and BLOCK_END in footer
