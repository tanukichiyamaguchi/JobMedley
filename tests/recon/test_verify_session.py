"""段階1の合格条件の判定を固定する。

守りたいのは1点。**「判定できない」を「復元できた」に丸めないこと。**
丸めた瞬間、段階2以降が「入れているつもりで0件」で進む -- 原則2の静かなゼロ件が
ラダーの1段目から仕込まれることになる。
"""

from __future__ import annotations

from pathlib import Path

from jobmedley_scout.recon.verify_session import (
    Verdict,
    VerifyMethod,
    VerifyResult,
    heuristic_verdict,
)

SESSION = Path("/tmp/creds/storage_state.json")


def test_logout_link_without_password_field_means_restored() -> None:
    assert (
        heuristic_verdict(logout_hits=("ログアウト",), password_field_present=False)
        is Verdict.RESTORED
    )


def test_password_field_without_logout_link_means_not_restored() -> None:
    assert heuristic_verdict(logout_hits=(), password_field_present=True) is Verdict.NOT_RESTORED


def test_both_signals_present_is_indeterminate() -> None:
    """公開ページのフッタに「ログアウト」がある媒体で、常に成功になるのを防ぐ。"""
    assert (
        heuristic_verdict(logout_hits=("ログアウト",), password_field_present=True)
        is Verdict.INDETERMINATE
    )


def test_neither_signal_present_is_indeterminate() -> None:
    """何も見えないのは「入れている」証拠ではない。描画前かもしれない。"""
    assert heuristic_verdict(logout_hits=(), password_field_present=False) is Verdict.INDETERMINATE


def test_only_restored_counts_as_passed() -> None:
    """``passed`` は RESTORED だけ。**判定不能は合格ではない。**"""
    for verdict in Verdict:
        result = VerifyResult(
            verdict=verdict,
            method=VerifyMethod.LOGOUT_HEURISTIC,
            landed_url="https://customers.job-medley.com/",
            session_path=SESSION,
        )
        assert result.passed is (verdict is Verdict.RESTORED)


def test_heuristic_report_says_it_is_a_substitute() -> None:
    """代用判定を黙って行うと、運用者は厳密判定が済んだものとして次へ進む。"""
    report = VerifyResult(
        verdict=Verdict.RESTORED,
        method=VerifyMethod.LOGOUT_HEURISTIC,
        landed_url="https://customers.job-medley.com/",
        session_path=SESSION,
        logout_hits=("ログアウト",),
    ).render()

    assert "代用判定" in report
    assert "auth.success_marker_selector" in report


def test_strict_report_says_which_selector_decided_it() -> None:
    report = VerifyResult(
        verdict=Verdict.RESTORED,
        method=VerifyMethod.MARKER,
        landed_url="https://customers.job-medley.com/",
        session_path=SESSION,
    ).render()

    assert "厳密判定" in report
    assert "代用判定" not in report


def test_failure_report_names_the_retreat_condition() -> None:
    """復元できないなら設計変更の相談が要る。そこまで書いてあること (段階1の撤退条件)。"""
    report = VerifyResult(
        verdict=Verdict.NOT_RESTORED,
        method=VerifyMethod.MARKER,
        landed_url="https://customers.job-medley.com/customers/sign_in/",
        session_path=SESSION,
    ).render()

    assert "撤退条件" in report
    assert "scout recon login" in report


def test_indeterminate_report_does_not_claim_either_outcome() -> None:
    report = VerifyResult(
        verdict=Verdict.INDETERMINATE,
        method=VerifyMethod.LOGOUT_HEURISTIC,
        landed_url="https://customers.job-medley.com/",
        session_path=SESSION,
        logout_hits=("ログアウト",),
        password_field_present=True,
    ).render()

    assert "判定できませんでした" in report
    assert "撤退条件" not in report


def test_missing_session_points_back_at_stage_one() -> None:
    report = VerifyResult(
        verdict=Verdict.NO_SESSION,
        method=VerifyMethod.NONE,
        landed_url="",
        session_path=SESSION,
    ).render()

    assert "scout recon login" in report


def test_heuristic_success_does_not_declare_stage_one_finished() -> None:
    """代用判定で「段階2へ」と言うと、厳密判定を通らないまま先へ進むことになる。"""
    report = VerifyResult(
        verdict=Verdict.RESTORED,
        method=VerifyMethod.LOGOUT_HEURISTIC,
        landed_url="https://customers.job-medley.com/",
        session_path=SESSION,
        logout_hits=("ログアウト",),
    ).render()

    assert "まだ閉じていません" in report
    assert "observe-login" in report
    assert "preflight" not in report


def test_strict_success_sends_the_operator_to_stage_two() -> None:
    """厳密判定で通ったら、段階2で **実際に次にやること** を出す。

    以前は「段階2 `scout preflight` (Actions からは Recon (manual) ではなく
    docs/ladder.md の手順)」と出していた。2つ間違っている -- preflight は
    Recon (manual) から実行でき、かつ preflight の前に observe-list で段階2の
    残り4座標を埋めないと、未確定のまま警告を読むだけになる。
    """
    report = VerifyResult(
        verdict=Verdict.RESTORED,
        method=VerifyMethod.MARKER,
        landed_url="https://customers.job-medley.com/",
        session_path=SESSION,
    ).render()

    assert "段階1は完了です" in report
    # 順序が要点。観測してから点検する。
    assert report.index("observe-list") < report.index("preflight")
    # 実行できる場所を偽らない (Recon (manual) から両方走る)。
    assert "Recon (manual)" in report


def test_the_export_advice_is_marked_optional() -> None:
    """クッキー持ち込みで運用している人に、実行不能な手順を必須として出さない。

    実行環境は使い捨てで、そもそも手元にCLIが無いこともある。「次にこれをやれ」と
    書かれた手順が実行できないと、そこで止まってしまう。
    """
    report = VerifyResult(
        verdict=Verdict.RESTORED,
        method=VerifyMethod.MARKER,
        landed_url="https://customers.job-medley.com/",
        session_path=SESSION,
    ).render()

    assert "補足:" in report
    assert "手元にPython環境がある場合のみ" in report
