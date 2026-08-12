"""ラダーの1歩目が実際に始められることを固定する。

これが無いと再発する事故は具体的である。``recon-login`` の必須座標に段階1の
``auth.*`` を並べた瞬間、``scout recon login`` は終了コード11で止まる --
**そのコマンドが発見するはずの座標が無いという理由で**。循環なので、運用者は
何をしても抜けられない。エラーメッセージは親切なままなので、壊れていることが
かえって分かりにくい。

REQUIRED_BY_COMMAND は「そのコマンドの入力」を宣言するものであって、「そのコマンドが
関係する座標」ではない。偵察コマンドについては、この違いがそのまま可否になる。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jobmedley_scout.config.audit import assert_ready_for
from jobmedley_scout.config.coordinates import (
    COORDINATES,
    COORDINATES_BY_KEY,
    REQUIRED_BY_COMMAND,
)
from jobmedley_scout.config.loader import load_site_coordinates
from jobmedley_scout.config.placeholders import LadderStage, Unresolved
from jobmedley_scout.config.site_coordinates import SiteCoordinates
from jobmedley_scout.errors import UnresolvedCoordinateError

REPO = Path(__file__).resolve().parents[2]
COORDINATES_PATH = REPO / "config" / "site_coordinates.yaml"


@pytest.fixture
def shipped_coordinates() -> SiteCoordinates:
    """リポジトリに入っている座標ファイル。**全件が未確定である状態。**"""
    return load_site_coordinates(COORDINATES_PATH)


def test_manual_login_requires_no_coordinates() -> None:
    """段階1は発見の工程。入力として座標を要求してはならない。"""
    assert REQUIRED_BY_COMMAND["recon-login"] == frozenset()


def test_manual_login_runs_with_every_coordinate_unresolved() -> None:
    """座標が1つも埋まっていない状態 = クローン直後でも、段階1は開始できる。

    **リポジトリ内の座標ファイルを使わない。** 以前はそれを使い「全件未確定である
    こと」を前提に確認していたが、その前提はラダーを1段登った瞬間に崩れ、テストは
    「段階1が開始できるか」ではなく「まだ何も埋まっていないか」を検査するものに
    成り下がっていた。検査したいのは前者なので、全件未確定の座標をここで組み立てる。
    """
    nothing_known = SiteCoordinates(
        {spec.key: Unresolved(spec.key, spec.stage, spec.how_to_obtain) for spec in COORDINATES}
    )

    assert nothing_known.resolved_keys() == ()
    assert_ready_for(nothing_known, "recon-login")  # 例外が出ないこと


def test_stage_one_coordinates_are_never_required_by_recon_commands() -> None:
    """段階1の座標を入力に要求する偵察コマンドが1つも無いこと。

    ``recon-login`` だけを名指しで検査すると、``recon-capture-send`` 側に
    ``auth.login_url`` を足したときに素通りする。**段階で見る。**
    保存セッションで入るので、偵察に段階1の座標は要らない -- 唯一の例外が
    ``auth.success_marker_selector`` で、これは「入れているか」の判定に要る。
    """
    allowed = {"auth.success_marker_selector"}
    for command, keys in REQUIRED_BY_COMMAND.items():
        if not command.startswith("recon-"):
            continue
        stage_one = {
            key for key in keys if COORDINATES_BY_KEY[key].stage is LadderStage.STAGE_1_LOGIN
        }
        assert stage_one <= allowed, f"{command} が段階1の座標を要求しています: {stage_one}"


def test_a_command_that_does_need_coordinates_still_stops(
    shipped_coordinates: SiteCoordinates,
) -> None:
    """緩めたのは段階1だけ。他が黙って0件で成功しないことを併せて固定する (原則2)。"""
    with pytest.raises(UnresolvedCoordinateError):
        assert_ready_for(shipped_coordinates, "send")
