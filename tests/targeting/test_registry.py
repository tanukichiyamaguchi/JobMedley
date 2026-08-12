"""The registry contract: no rule may run without a declared policy.

7.1: 既定値なしを構造で強制する。宣言 (YAML) と実装 (ALL_RULES) は独立に
編集されるので、両方向のずれを検査する。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from jobmedley_scout.config.schema import UndeterminablePolicy
from jobmedley_scout.errors import ConfigError
from jobmedley_scout.targeting.registry import ALL_RULE_IDS, ALL_RULES, assert_policies_complete
from tests.targeting.factories import DEFAULT_POLICIES, make_targeting_config

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"


def test_complete_policies_pass() -> None:
    # 例外を出さないことが仕様。戻り値は無い。
    assert_policies_complete(make_targeting_config())


def test_missing_policy_is_a_config_error() -> None:
    policies = dict(DEFAULT_POLICIES)
    del policies["membership_status"]
    cfg = make_targeting_config(undeterminable_policy=policies)
    with pytest.raises(ConfigError) as excinfo:
        assert_policies_complete(cfg)
    assert "membership_status" in str(excinfo.value)


@pytest.mark.parametrize("rule_id", ALL_RULE_IDS)
def test_every_single_rule_id_is_required(rule_id: str) -> None:
    policies = {k: v for k, v in DEFAULT_POLICIES.items() if k != rule_id}
    with pytest.raises(ConfigError):
        assert_policies_complete(make_targeting_config(undeterminable_policy=policies))


def test_unknown_rule_id_is_a_config_error() -> None:
    # 打鍵ミス。宣言したつもりのルールが存在しない状態を検知する (7.6)。
    policies = dict(DEFAULT_POLICIES)
    policies["current_tenure_"] = UndeterminablePolicy.EXCLUDE
    with pytest.raises(ConfigError) as excinfo:
        assert_policies_complete(make_targeting_config(undeterminable_policy=policies))
    assert "current_tenure_" in str(excinfo.value)


def test_rule_ids_are_unique_and_stable() -> None:
    assert len(set(ALL_RULE_IDS)) == len(ALL_RULE_IDS)
    for rule_id, _rule in ALL_RULES:
        assert rule_id == rule_id.strip().lower()


def test_shipped_config_declares_a_policy_for_every_rule() -> None:
    """実ファイルとの整合。ここが赤いなら本番は起動時に落ちる。"""
    raw: dict[str, Any] = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    declared = raw["targeting"]["undeterminable_policy"]
    assert set(declared) == set(ALL_RULE_IDS)


def test_the_shipped_policy_map_is_the_whole_rule_inventory() -> None:
    """設定に並ぶ行が、走っているルールの全部であること。

    ここには 6.5 の非対称 (学歴だけ include) を固定するテストがあった。8.5 に
    従い「揃っていないのは間違いだ」と直されないよう置いていたもので、**学歴
    ルールごと削除したので消えた**。方針が1本しか無い今、非対称性を実行可能な形で
    示す場所はもう無い -- その事実は docs/incidents.md に残してある。
    """
    raw: dict[str, Any] = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    declared = raw["targeting"]["undeterminable_policy"]

    assert set(declared) == set(ALL_RULE_IDS)
    assert declared["membership_status"] == "exclude"
