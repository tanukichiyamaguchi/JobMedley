"""既定値なしを構造で強制する (7.1)。宣言と実装は両方向で検査する。"""

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
    assert_policies_complete(make_targeting_config()) is None


def test_missing_policy_is_a_config_error() -> None:
    policies = dict(DEFAULT_POLICIES)
    del policies["current_tenure"]
    cfg = make_targeting_config(undeterminable_policy=policies)
    with pytest.raises(ConfigError) as excinfo:
        assert_policies_complete(cfg)
    assert "current_tenure" in str(excinfo.value)


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


def test_shipped_config_keeps_education_on_the_include_side() -> None:
    # 6.5: 大学卒以上を取りこぼさないという業務判断。非対称であることが仕様なので
    # 「揃っていないのは間違いだ」と直されないようテストで固定する (8.5)。
    raw: dict[str, Any] = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    declared = raw["targeting"]["undeterminable_policy"]
    assert declared["education"] == "include"
    assert declared["current_tenure"] == "exclude"
