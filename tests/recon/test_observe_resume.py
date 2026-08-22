"""``observe-resume`` -- **押す偵察。だが送信は遮断で止まっている。**

レジュメはカードを押さないと飛ばないので、押さない ``observe-api`` では原理的に
観測できない。押すかわりに遮断を許可制にする (``BLOCK_SEND``)。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from jobmedley_scout.recon.api_shape import ObservedCall, describe_response
from jobmedley_scout.recon.gate import GateMode
from jobmedley_scout.recon.observe_resume import (
    ResumeObservation,
    ResumeStage,
    looks_like_a_resume,
    observe_resume,
)
from jobmedley_scout.recon.resume_keys import KeyPath

RESUME_URL = (
    "https://customers.job-medley.com/api/customers/graphql/MemberOnScoutProfileModalOfDesktop"
)


def _call(url: str, body: str, operation: str = "") -> ObservedCall:
    keys, reason, dropped = describe_response(body)
    return ObservedCall(
        operation=operation,
        redacted_url=url,
        method="POST",
        keys=keys,
        unread_reason=reason,
        dropped_keys=dropped,
        content_type="application/json",
    )


#: 実画面の項目に対応する形 (キー名だけ本物、値は捨て値)。
RESUME_BODY = (
    '{"data": {"member": {"selfPr": null, "careers": [], "educations": [],'
    ' "qualifications": [], "desiredJobCategories": [], "desiredSalary": 0,'
    ' "currentIncome": 0, "employmentStatus": "x", "scoutedAtList": []}}}'
)

#: 一覧の応答。**レジュメではない。** desired と scouted は出るが、それだけ。
LIST_BODY = '{"members": [{"id": 1, "scouted": false}], "search_uuid": "x", "total": 1}'


def test_a_resume_shaped_response_is_recognised() -> None:
    assert looks_like_a_resume(_call(RESUME_URL, RESUME_BODY))


def test_the_candidate_list_is_not_mistaken_for_a_resume() -> None:
    """**1語では名乗らせない。** 一覧にも desired / scouted は出る。"""
    assert not looks_like_a_resume(_call("https://x/members/search/", LIST_BODY))


def test_an_unreadable_response_is_never_a_resume() -> None:
    """読めなかったものを「レジュメらしい」と言わない (原則2)。"""
    call = ObservedCall(
        operation="x",
        redacted_url="https://x/",
        method="POST",
        unread_reason="JSONとして読めませんでした",
    )
    assert not looks_like_a_resume(call)


# ---------------------------------------------------------------------------
# 報告の単調性
# ---------------------------------------------------------------------------


def test_no_session_is_reported_before_anything_else() -> None:
    observed = ResumeObservation(requested_url="https://x/", session_present=False)
    assert observed.reached() is ResumeStage.NO_SESSION
    assert "段階1" in observed.render()


def test_rows_that_never_appeared_stop_the_chain() -> None:
    observed = ResumeObservation(requested_url="https://x/", note="行が現れませんでした。")
    assert observed.reached() is ResumeStage.NO_ROWS
    report = observed.render()
    assert "押す対象が無い" in report


def test_pressing_nothing_is_reported_as_such() -> None:
    from jobmedley_scout.recon.observe_resume import PressAttempt

    observed = ResumeObservation(
        requested_url="https://x/",
        list_rendered=True,
        attempts=(
            PressAttempt(selector="button.c-button", pressed=False, failure="届きませんでした"),
        ),
    )
    assert observed.reached() is ResumeStage.NOTHING_PRESSED
    assert "1つも押せませんでした" in observed.render()


def test_pressing_without_a_new_response_is_reported_as_such() -> None:
    """**「押した」を「取れた」にしない** (原則2)。"""
    from jobmedley_scout.recon.observe_resume import PressAttempt

    observed = ResumeObservation(
        requested_url="https://x/",
        list_rendered=True,
        attempts=(PressAttempt(selector="button.c-button", pressed=True),),
    )
    assert observed.reached() is ResumeStage.NOTHING_NEW_HEARD
    assert "新しい応答は1つも届きませんでした" in observed.render()


def test_a_broken_chain_raises_rather_than_reporting_a_lie() -> None:
    """行が無いのに押せた、は矛盾である。**報告せず落とす。**"""
    from jobmedley_scout.recon.observe_resume import PressAttempt

    observed = ResumeObservation(
        requested_url="https://x/",
        list_rendered=False,
        attempts=(PressAttempt(selector="b", pressed=True),),
    )
    with pytest.raises(ValueError, match="時系列と矛盾"):
        observed.reached()


def test_the_report_never_picks_the_coordinate_for_the_operator() -> None:
    """**機械は1つに決めない** (原則3)。"""
    from jobmedley_scout.recon.observe_resume import PressAttempt

    observed = ResumeObservation(
        requested_url="https://x/",
        list_rendered=True,
        attempts=(PressAttempt(selector="b", pressed=True, new_responses=1),),
        after=(_call(RESUME_URL, RESUME_BODY, "MemberOnScoutProfileModalOfDesktop"),),
    )
    assert observed.reached() is ResumeStage.HEARD
    report = observed.render()
    assert "api.resume.url_pattern: UNRESOLVED" in report
    assert "MemberOnScoutProfileModalOfDesktop" in report


def test_blocked_requests_are_reported_even_when_there_are_none() -> None:
    """0件でも書く (原則2)。黙ると「観測しなかった」と区別が付かない。"""
    from jobmedley_scout.recon.observe_resume import PressAttempt

    observed = ResumeObservation(
        requested_url="https://x/",
        list_rendered=True,
        attempts=(PressAttempt(selector="b", pressed=True, new_responses=1),),
        after=(_call(RESUME_URL, RESUME_BODY),),
    )
    assert "遮断が止めた通信: 0 件。" in observed.render()


# ---------------------------------------------------------------------------
# 実行そのもの
# ---------------------------------------------------------------------------


def test_the_command_uses_the_allowlisted_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """**素通しモードでは押してはいけない。** ここが緩めば送信が成立する。"""
    captured: list[Any] = []

    def _fake_install_gate(page: Any, gate: Any) -> None:
        captured.append(gate)

    monkeypatch.setattr("jobmedley_scout.recon.observe_resume.install_gate", _fake_install_gate)
    monkeypatch.setattr(
        "jobmedley_scout.recon.observe_resume.session_store.session_path",
        lambda _dir: Path(__file__),
    )

    class _Ctx:
        def __enter__(self) -> tuple[Any, Any]:
            raise RuntimeError("stop here")

        def __exit__(self, *_: object) -> bool:
            return False

    monkeypatch.setattr(
        "jobmedley_scout.recon.observe_resume.browser_context", lambda *_a, **_k: _Ctx()
    )
    with pytest.raises(RuntimeError, match="stop here"):
        observe_resume(_Config(), Path("/nonexistent"), "https://x/", "div.card")  # type: ignore[arg-type]


class _Config:
    selector_timeout_ms = 10
    headless = True

    def model_copy(self, **_: object) -> _Config:
        return self


def test_the_module_declares_the_allowlisted_mode() -> None:
    """遮断モードの選択は安全上の性質そのものなので、ソースで固定する。"""
    source = Path("src/jobmedley_scout/recon/observe_resume.py").read_text(encoding="utf-8")
    assert "GateMode.BLOCK_SEND" in source
    assert (
        "GateMode.BLOCK_THIRD_PARTY" not in source
    ), "素通しモードを使っています。このコマンドは押すので、押した先が送信でも成立します。"
    assert GateMode.BLOCK_SEND.value == "block_send"


def test_the_resume_hints_are_names_not_values() -> None:
    """当たりを付けるのはキー **名** であって値ではない (13.2)。"""
    from jobmedley_scout.recon.observe_resume import RESUME_KEY_HINTS

    assert all(hint.islower() and " " not in hint for hint in RESUME_KEY_HINTS)
    assert len(RESUME_KEY_HINTS) >= 8


def test_a_resume_needs_several_hints_not_one() -> None:
    """1語で名乗らせると、本命が候補に埋もれる。"""
    one_hint = _call("https://x/", '{"desired": {"a": 1}}')
    assert not looks_like_a_resume(one_hint)


def test_key_paths_carry_no_values() -> None:
    keys, _reason, _dropped = describe_response(RESUME_BODY)
    assert all(isinstance(path, KeyPath) for path in keys)
    assert all("<" in path.render() for path in keys)


# ===========================================================================
# 実測25回目で分かったこと
# ===========================================================================


def test_state_only_controls_are_not_pressed() -> None:
    """チェックボックスを押しても何も開かない。**予算を使うだけ。**

    実測25回目、探索は最初にチェックボックスを2回押した (``label`` と ``input``)。
    どちらも新しい応答は0件で、変わったのは候補者の選択状態だけである。
    """
    from jobmedley_scout.recon.observe_resume import _pressable
    from jobmedley_scout.recon.open_structure import ActionCandidate

    checkbox_label = ActionCandidate(
        index=1,
        tag="label",
        tokens=("label.c-checkbox", "label.c-checkbox--blue"),
        looks_like_send=False,
    )
    checkbox_input = ActionCandidate(
        index=2, tag="input", tokens=("input.c-checkbox__input",), looks_like_send=False
    )
    button = ActionCandidate(
        index=3,
        tag="button",
        tokens=("button.c-button", "button.c-button--small"),
        looks_like_send=False,
    )
    kept = _pressable((checkbox_label, checkbox_input, button))
    assert kept == (button,)


def test_the_walk_stops_on_a_resume_not_on_any_response() -> None:
    """**「届いた」は「取れた」ではない** (実測18回目と同じ形)。

    実測25回目は「何か届いたら止める」だった。最初に届いたのは
    ``members/mark_read`` の空の応答で、そこで打ち切ったせいでレジュメを
    待たずに終わった。
    """
    source = Path("src/jobmedley_scout/recon/observe_resume.py").read_text(encoding="utf-8")
    assert "if any(looks_like_a_resume(call) for call in listener.calls[seen_before:]):" in source
    assert (
        "        if arrived:\n" not in source
    ), "「何か届いたら止める」に戻っています。mark_read の空応答で打ち切ります。"


def test_a_run_that_accepted_a_write_says_so() -> None:
    """**何を書いたかを黙らない。** 分からないまま偵察が終わるのが一番悪い。"""
    from jobmedley_scout.recon.observe_resume import PressAttempt

    observed = ResumeObservation(
        requested_url="https://x/",
        list_rendered=True,
        attempts=(PressAttempt(selector="b", pressed=True, new_responses=1),),
        after=(_call(RESUME_URL, RESUME_BODY),),
        accepted_a_write=True,
        writes_passed=("POST https://customers.job-medley.com/api/customers/members/mark_read/",),
    )
    report = observed.render()
    assert "書き込みを1つ受け入れて再試行" in report
    assert "mark_read" in report


def test_a_strict_run_says_it_accepted_nothing() -> None:
    """0件でも書く (原則2)。黙ると「受け入れたか」が報告から決められない。"""
    from jobmedley_scout.recon.observe_resume import PressAttempt

    observed = ResumeObservation(
        requested_url="https://x/",
        list_rendered=True,
        attempts=(PressAttempt(selector="b", pressed=True, new_responses=1),),
        after=(_call(RESUME_URL, RESUME_BODY),),
    )
    assert "受け入れた書き込み: なし" in observed.render()


def test_the_accepted_write_is_only_the_read_marker() -> None:
    """**受け入れるのは1つだけ。** 増やすならレビューが要る。"""
    source = Path("src/jobmedley_scout/recon/observe_resume.py").read_text(encoding="utf-8")
    assert "frozenset({KNOWN_WRITE_MARK_READ})" in source
    assert (
        source.count("accepted_writes=frozenset({") == 1
    ), "受け入れる書き込みが増えています。1つずつ根拠を書いてください。"
