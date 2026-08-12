"""The live rule set must be visible outside the config file.

2026-08-12 にビズリーチ由来の6ルールを全廃した。**その種の変更が運用面から
見えないことが問題だった** -- 対象条件は `config/config.yaml` を開いた人にしか
見えず、「消したつもりが残っている」も「残したつもりが消えている」も、実行結果
からは分からなかった。

起動前チェックが毎回印字することで、実行ログがそのまま対象条件の記録になる。
"""

from __future__ import annotations

from pathlib import Path

from jobmedley_scout.config.loader import load_all
from jobmedley_scout.config.secrets import Secrets
from jobmedley_scout.runtime.preflight import run_preflight
from jobmedley_scout.targeting.registry import ALL_RULE_IDS

REPO = Path(__file__).resolve().parents[2]


def _report() -> str:
    config, coordinates = load_all(
        REPO / "config" / "config.yaml", REPO / "config" / "site_coordinates.yaml"
    )
    secrets = Secrets(
        anthropic_api_key=None,
        platform_email=None,
        platform_password=None,
        storage_state_b64=None,
        session_curl=None,
    )
    return run_preflight(config, coordinates, secrets).render()


def test_preflight_names_every_live_rule() -> None:
    """走っているルールが1つ残らず印字されること。"""
    report = _report()

    for rule_id in ALL_RULE_IDS:
        assert rule_id in report, f"{rule_id} が起動前チェックに出ていません"


def test_preflight_says_the_list_is_exhaustive() -> None:
    """「これが全部」と言い切らないと、他にも条件があるように読める。

    対象の絞り込みは媒体側の検索条件が持つので、そこも併せて示す。
    """
    report = _report()

    assert "これが全部です" in report
    assert "媒体側の検索条件" in report


def test_a_deleted_rule_is_not_still_advertised() -> None:
    """削除したルールが印字に残っていたら、無い安全弁を有ると言うことになる。"""
    report = _report()

    for gone in ("age", "longest_tenure", "current_tenure", "job_change_count", "education"):
        assert f"{gone}(" not in report, f"削除済みのルール {gone} が印字されています"
