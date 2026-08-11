"""12.8: 恒久エラーは必ず異常終了させる。キルスイッチは異常ではない。

参照実装では認証切れで全APIが失敗しているのに CI は緑のままだった。例外を上まで
通しても、最後に 0 で終われば同じことなので、写像をここで固定する。
"""

from __future__ import annotations

import pytest

from jobmedley_scout.config.placeholders import LadderStage, Unresolved
from jobmedley_scout.errors import (
    ConfigError,
    GenerationError,
    KillSwitchEngaged,
    PermanentAuthError,
    ScoutError,
    SendFailed,
    StateIntegrityError,
    UnresolvedCoordinateError,
    WipeoutDetected,
)
from jobmedley_scout.runtime.exit_codes import (
    CLEAN_EXIT_CODES,
    ExitCode,
    exit_code_for,
    is_clean_exit,
)


def _unresolved_error() -> UnresolvedCoordinateError:
    coordinate: Unresolved = Unresolved(
        key="api.send.paid.url_pattern",
        stage=LadderStage.STAGE_3_RECON,
        how_to_obtain="scout recon capture-send",
    )
    return UnresolvedCoordinateError(coordinate, used_by="tests")


PERMANENT_CASES: list[tuple[ScoutError, ExitCode]] = [
    (PermanentAuthError("セッション失効", status=401), ExitCode.AUTH_EXPIRED),
    (_unresolved_error(), ExitCode.UNRESOLVED_COORDINATE),
    (ConfigError("未知のキー"), ExitCode.CONFIG_INVALID),
    (StateIntegrityError("DBが後退しています"), ExitCode.STATE_INTEGRITY),
    (WipeoutDetected("対象ありで送信0件"), ExitCode.WIPEOUT),
]


@pytest.mark.parametrize(("exc", "expected"), PERMANENT_CASES)
def test_each_permanent_error_gets_its_own_non_zero_code(
    exc: ScoutError, expected: ExitCode
) -> None:
    """恒久エラーは種類ごとに固有の非0コード。原因の切り分けが終了コードでつく。"""
    assert exit_code_for(exc) == int(expected)
    assert exit_code_for(exc) != int(ExitCode.OK)


def test_permanent_codes_are_all_distinct() -> None:
    """同じ番号を2つの原因に割り当てると、切り分けができなくなる。"""
    codes = [exit_code_for(exc) for exc, _ in PERMANENT_CASES]

    assert len(set(codes)) == len(codes)


def test_auth_error_is_not_swallowed_by_its_base_class() -> None:
    """表の順序が仕様。派生クラスが基底クラスの行に吸われると固有番号を失う。

    ``PermanentAuthError`` は ``PermanentError`` の派生である。表が基底クラスを
    先に並べていたら、認証切れと座標未確定が同じ番号になる。
    """
    assert exit_code_for(PermanentAuthError("x", status=401)) == int(ExitCode.AUTH_EXPIRED)
    assert exit_code_for(ConfigError("x")) == int(ExitCode.CONFIG_INVALID)


def test_kill_switch_is_a_clean_stop_not_an_error() -> None:
    """キルスイッチは **意図的な停止**。恒久エラーの帯へ落としてはならない。"""
    code = exit_code_for(KillSwitchEngaged("kill switch ファイルが存在します"))

    assert code == int(ExitCode.KILL_SWITCH)
    assert is_clean_exit(code)
    assert code not in {exit_code_for(exc) for exc, _ in PERMANENT_CASES}


def test_kill_switch_code_is_lower_than_every_permanent_error_code() -> None:
    """帯で分けてある: 低い番号は「止めた」、10以上は「壊れた」。"""
    assert all(int(ExitCode.KILL_SWITCH) < exit_code_for(exc) for exc, _ in PERMANENT_CASES)


def test_ok_is_the_only_zero() -> None:
    assert int(ExitCode.OK) == 0
    assert all(int(code) != 0 for code in ExitCode if code is not ExitCode.OK)


def test_unknown_exception_gets_a_generic_non_zero_code() -> None:
    """分からない例外を0で終わらせるのは「静かなゼロ件」そのもの。"""
    assert exit_code_for(RuntimeError("想定外")) == int(ExitCode.UNKNOWN)
    assert exit_code_for(ValueError("想定外")) != int(ExitCode.OK)


def test_transient_errors_also_end_non_zero_when_they_reach_the_top() -> None:
    """一時エラーは握りつぶしてよいが、**上まで来たなら** それは異常である。"""
    assert exit_code_for(GenerationError("生成失敗")) != int(ExitCode.OK)
    assert exit_code_for(SendFailed("送信失敗")) != int(ExitCode.OK)


def test_keyboard_interrupt_is_not_counted_as_clean() -> None:
    """送信の途中で中断された実行は、送信済みなのに状態を保存できていないかも
    しれない (12.1 の56件消失と同じ形)。人が止めたことと状態の健全性は別。"""
    code = exit_code_for(KeyboardInterrupt())

    assert code == int(ExitCode.INTERRUPTED)
    assert not is_clean_exit(code)
    assert ExitCode.INTERRUPTED not in CLEAN_EXIT_CODES


def test_clean_exit_codes_contains_only_ok_and_kill_switch() -> None:
    expected = frozenset({ExitCode.OK, ExitCode.KILL_SWITCH})
    assert CLEAN_EXIT_CODES == expected
