"""``is_own_origin`` -- **部分一致で判定していた穴を塞いだので、それを固定する。**

実測23回目の報告に、こういう行が並んでいた::

    一覧を開いている間に媒体のオリジンへ飛んだ非GET: 13 件 (**止めていません**)
      POST https://www.google.com/ccm/collect?...
      POST https://www.google-analytics.com/g/collect?...

**媒体のオリジンではない。** 止めたつもりのものを通し、通した事実を
違う名前で報告していた。

原因は ``own_host in url.lower()`` である。計測ビーコンは「どのページから
送ったか」を ``dl=`` に載せるので、URLの文字列の中には媒体のホスト名が
そのまま入っている::

    ...&dl=https%3A%2F%2Fcustomers.job-medley.com%2Fcustomers%2Fsearches...

これは 12回目に ``redact_url`` で塞いだのと同じ形の穴である
(「URLの中の一部分を見て全体の性質を決めた」)。同じ失敗を2度している。
"""

from __future__ import annotations

import pytest

from jobmedley_scout.recon.gate import GateDecision, GateMode, SendGate, is_own_origin

#: 実測23回目に素通ししてしまった形。**クエリに媒体のホスト名が入っている。**
BEACON_CARRYING_THE_MEDIA_HOST = (
    "https://www.google-analytics.com/g/collect?v=2&tid=G-X"
    "&dl=https%3A%2F%2Fcustomers.job-medley.com%2Fcustomers%2Fsearches%3Flg%3D0"
)


@pytest.mark.parametrize(
    "url",
    [
        "https://customers.job-medley.com/api/customers/members/search/",
        "https://job-medley.com/",
        "https://JOB-MEDLEY.COM/api/",
        "https://customers.job-medley.com:443/api/",
    ],
)
def test_the_media_itself_is_recognised(url: str) -> None:
    assert is_own_origin(url, "job-medley.com")


@pytest.mark.parametrize(
    "url",
    [
        BEACON_CARRYING_THE_MEDIA_HOST,
        "https://www.google.com/ccm/collect?dl=https%3A%2F%2Fjob-medley.com%2F",
        # **接尾辞が同じだけの別ドメイン。** 部分一致でも完全一致でもなく、
        # 「.」の直前で切れているかを見ないと通してしまう。
        "https://evil-job-medley.com/",
        "https://job-medley.com.attacker.example/",
        "https://example.test/job-medley.com/path",
        "data:text/html,job-medley.com",
        "",
    ],
)
def test_everyone_else_is_not(url: str) -> None:
    assert not is_own_origin(url, "job-medley.com")


def test_an_empty_own_host_matches_nothing() -> None:
    """**空欄は「全部通す」ではない。**

    部分一致版では ``"" in url`` が常に真だったので、``own_host=""`` の1行で
    遮断が丸ごと消えていた。fail-closed の方針からすれば逆向きの既定である。
    """
    assert not is_own_origin("https://customers.job-medley.com/", "")


def test_the_gate_no_longer_passes_a_beacon_that_names_the_media() -> None:
    """穴が塞がったことを、判定関数ではなく **遮断そのもの** で確かめる。"""
    gate = SendGate(mode=GateMode.BLOCK_THIRD_PARTY)
    gate.arm()
    try:
        decision = gate.decide("POST", BEACON_CARRYING_THE_MEDIA_HOST, "{}")
    finally:
        gate.disarm()
    assert decision is not GateDecision.PASS, (
        "計測ビーコンが素通りしています (実測23回目の穴)。"
        "URL全体の部分一致ではなくホスト名で判定してください。"
    )
    assert not gate.passed_reads, "通していないのに『通した』側へ記録されています"
    assert gate.recorded, "止めたのに記録が残っていません"
