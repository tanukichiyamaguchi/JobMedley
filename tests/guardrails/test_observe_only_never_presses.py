"""``BLOCK_THIRD_PARTY`` を使うコマンドは、ボタンを1つも押さないこと。

**この緩和は送信に対する保護ではない。**

``GateMode.BLOCK_THIRD_PARTY`` は媒体自身のオリジンへの通信を全部通す。送信API
(``.../graphql/SendSingleScout``) も同じオリジンにあるので、**このモードで送信
ボタンを押せば送信は成立する**。取り消せない (13.6)。

それでも要る理由は、実測22回目で分かった::

    POST /api/customers/customer_search_conditions/search_manual/
    POST /api/customers/received_favorites/search/
    POST /api/customers/scouted_members/search/

**この媒体の読み取りは GraphQL ではなく REST の POST である。** 遮断から見れば
書き込みと区別が付かないので、``BLOCK_WRITES`` は候補者を取ってくる通信ごと
止めていた。観測したかったものを、観測のための仕掛けが止めていた。

だから緩和する。**そのかわり、押さない。**

守っているのは遮断ではなく「押さないこと」なので、それを人間の規律ではなく
ソース走査で固定する。同じ手法をこのリポジトリは既に3箇所で使っている
(``test_source_conventions.py`` / ``test_workflow_safety.py`` /
``test_docs_match_the_cli.py``)。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SOURCE = Path("src/jobmedley_scout")

#: 押す操作。**この語がモジュールに現れたら、緩和を使ってはいけない。**
#:
#: ``dispatch_event`` を含めるのは、実測18回目に分かったとおり、それが押下を
#: 届ける経路そのものだからである (通常のクリックが通らない画面で使っている)。
PRESSING_CALLS: tuple[str, ...] = (
    ".click(",
    ".dispatch_event(",
    ".press(",
    ".tap(",
    ".check(",
    ".select_option(",
)

#: 緩和の名前。
RELAXATION = "GateMode.BLOCK_THIRD_PARTY"


def _modules_using(needle: str) -> tuple[Path, ...]:
    return tuple(
        path for path in sorted(SOURCE.rglob("*.py")) if needle in path.read_text(encoding="utf-8")
    )


def test_the_relaxation_is_used_by_exactly_the_commands_that_never_press() -> None:
    """**緩和を使うモジュールを数え上げ、名前で固定する。**

    増えたらこの試験が落ちる。落ちたときに考えるべきは「押さないか」であって、
    「一覧に足すか」ではない。
    """
    users = {path.name for path in _modules_using(RELAXATION)}
    # gate.py は定義そのもの。残りが使用者である。
    #
    # observe_api.py         一覧を開いて読み取りの形を聴く
    # observe_job_offers.py  一覧を開いて求人IDを読む
    #
    # **どちらも押す操作が存在しない。** それが緩和の安全性の全部であり、
    # 下の試験がモジュールごとに確かめている。
    assert users == {"gate.py", "observe_api.py", "observe_job_offers.py"}, (
        f"{RELAXATION} の使用者が変わりました: {sorted(users)}。"
        f"このモードは媒体のオリジンを素通しするので、**送信も通ります**。"
        f"押さないコマンドだけが使えます (13.6)。"
    )


@pytest.mark.parametrize("path", _modules_using(RELAXATION))
def test_a_module_using_the_relaxation_contains_no_pressing_call(path: Path) -> None:
    """**押す呼び出しが1つも無いこと。**

    ここが緩和の安全性の全部である。遮断は送信を止めない -- 止めているのは
    「押さないこと」だけなので、押す呼び出しが混ざった時点で保護は消える。
    """
    if path.name == "gate.py":
        pytest.skip("定義側。押す操作とは無関係")
    text = path.read_text(encoding="utf-8")
    # docstring と注記は除いて数える (説明のために語が出るのは正常)。
    code = re.sub(r'"""(?:.|\n)*?"""', "", text)
    code = re.sub(r"^\s*#.*$", "", code, flags=re.MULTILINE)
    found = [call for call in PRESSING_CALLS if call in code]
    assert not found, (
        f"{path} は {RELAXATION} を使っているのに、押す呼び出しがあります: {found}。"
        f"このモードは媒体のオリジンを素通しするので、押せば送信は成立します。"
        f"押す必要があるなら BLOCK_WRITES に戻してください (13.6)。"
    )


def test_the_relaxation_passes_the_media_but_stops_everyone_else() -> None:
    """緩和の効き方そのものを固定する。**広げすぎても狭めすぎても駄目。**"""
    from jobmedley_scout.recon.gate import GateDecision, GateMode, SendGate

    gate = SendGate(mode=GateMode.BLOCK_THIRD_PARTY)
    gate.arm()
    try:
        media = gate.decide(
            "POST", "https://customers.job-medley.com/api/customers/x/search/", "{}"
        )
        beacon = gate.decide("POST", "https://www.google-analytics.com/g/collect", "{}")
    finally:
        gate.disarm()
    assert media is GateDecision.PASS, "媒体の読み取りを止めている (実測22回目の失敗)"
    assert beacon is not GateDecision.PASS, "他所への通信まで通している"


def test_the_default_mode_is_still_the_strict_one() -> None:
    """**緩和は名前で明示したときだけ効く。** 既定が緩んでいたら意味が無い。"""
    from jobmedley_scout.recon.gate import GateMode, SendGate

    assert SendGate().mode is GateMode.BLOCK_ALL


def test_the_media_host_cannot_be_widened_by_a_caller() -> None:
    """**既定のホストは媒体に固定してある。**

    ここが呼び出し側から自由に広げられると、``own_host=""`` の1行で
    「全部通す」になる -- しかも試験は通ったままになる。
    """
    from jobmedley_scout.recon.gate import SendGate

    assert SendGate().own_host == "job-medley.com"
    source = (SOURCE / "recon" / "observe_api.py").read_text(encoding="utf-8")
    assert "own_host" not in source, (
        "observe_api が own_host を渡しています。緩和の範囲は呼び出し側で"
        "広げられないようにしてあります (gate.py の編集とレビューを要する)。"
    )
