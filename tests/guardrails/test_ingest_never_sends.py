"""**取り込みは1通も送らない。** それをソース走査で固定する。

``ingest`` はブラウザを開く。原則1 のとおり、認証済みの通信路をそのまま使う
ためである -- が、開いた以上「押せる」状態にはなっている。

このコマンドが送らないことの根拠は3つあり、**どれも構造で保たれている**。

1. **ボタンを押さない。** DOM を触る呼び出しがモジュールに1つも無い
2. **送信のエンドポイントを引かない。** ``SEND_PAID`` / ``SEND_FREE`` を参照しない
3. **mutation を組み立てない。** ``mutation`` の語がコードに現れない

同じ手法をこのリポジトリは既に4箇所で使っている
(``test_source_conventions`` / ``test_workflow_safety`` / ``test_docs_match_the_cli``
/ ``test_observe_only_never_presses``)。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

INGEST = Path("src/jobmedley_scout/runtime/commands/ingest.py")
TRANSPORT = Path("src/jobmedley_scout/browser/transport.py")

#: 押す操作。``recon`` 側の一覧と揃えてある。
PRESSING_CALLS: tuple[str, ...] = (
    ".click(",
    ".dispatch_event(",
    ".press(",
    ".tap(",
    ".check(",
    ".select_option(",
    ".fill(",
)

#: 送信のエンドポイント。**取り込みが引いてはいけない。**
SEND_ENDPOINTS: tuple[str, ...] = ("SEND_PAID", "SEND_FREE")


def _code(path: Path) -> str:
    """Source with docstrings and comments removed. 説明のために語が出るのは正常。"""
    text = path.read_text(encoding="utf-8")
    text = re.sub(r'"""(?:.|\n)*?"""', "", text)
    return re.sub(r"^\s*#.*$", "", text, flags=re.MULTILINE)


@pytest.mark.parametrize("call", PRESSING_CALLS)
def test_ingest_presses_nothing(call: str) -> None:
    """**DOM を1つも触らない。** ブラウザを開くのは Cookie を載せるためだけである。"""
    assert call not in _code(INGEST), (
        f"取り込みに押す呼び出し {call} があります。"
        f"このコマンドは内部APIを呼ぶだけで、画面は操作しません (原則1)。"
    )


@pytest.mark.parametrize("endpoint", SEND_ENDPOINTS)
def test_ingest_never_reaches_for_a_send_endpoint(endpoint: str) -> None:
    assert endpoint not in _code(
        INGEST
    ), f"取り込みが送信のエンドポイント {endpoint} を引いています。"


def test_ingest_builds_no_mutation() -> None:
    """この媒体の送信は GraphQL の mutation である。**組み立てない。**"""
    assert "mutation" not in _code(INGEST).lower()


def test_ingest_only_reads_the_two_observed_endpoints() -> None:
    """引いてよいのは一覧とレジュメだけ。**増えたら理由を書くこと。**"""
    code = _code(INGEST)
    assert "CANDIDATE_LIST" in code
    assert "RESUME" in code


def test_the_transport_says_plainly_that_the_gate_does_not_cover_it() -> None:
    """**``context.request`` はルート傍受を通らない。**

    偵察の遮断はページの通信に掛かるもので、この通信路には掛からない。
    「遮断があるから安全」と読まれると、送信路をここへ足したときに誰も止め
    られない。docstring にそう書いてあることを固定する。
    """
    text = TRANSPORT.read_text(encoding="utf-8")
    assert "遮断" in text
    assert "止まらない" in text


def test_the_transport_does_not_retry() -> None:
    """12.5: **送信APIに自動リトライを掛けない。**

    通信路にリトライを埋めると、上の層が何をしても二重送信が起きうる。
    """
    code = _code(TRANSPORT)
    for word in ("retry", "Retry", "for attempt", "while True"):
        assert word not in code, f"通信路にリトライらしきもの ({word}) があります (12.5)"
