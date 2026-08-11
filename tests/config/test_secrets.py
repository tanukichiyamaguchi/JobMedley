"""持ち込み経路が2つあることを、点検と復元の両方で固定する。

守りたいのは食い違いである。点検が「認証経路がありません」と言うのに実行は通る、
あるいはその逆。点検が実態と違うことを言い出したら、点検は嘘をつく仕組みになり、
運用者は点検を見なくなる (12.6)。
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from jobmedley_scout.config.secrets import (
    ENV_SESSION_CURL,
    ENV_STORAGE_STATE_B64,
    load_secrets,
    restore_storage_state,
)
from jobmedley_scout.errors import ConfigError

CURL = "curl 'https://customers.job-medley.com/api/x' -H 'cookie: _jm_session=abc'"
REAL_STATE = {"cookies": [{"name": "from_b64", "value": "v", "domain": "d", "path": "/"}]}


def _b64(state: dict[str, object]) -> str:
    return base64.b64encode(json.dumps(state).encode("utf-8")).decode("ascii")


def test_either_route_counts_as_having_a_session() -> None:
    assert load_secrets({ENV_STORAGE_STATE_B64: _b64(REAL_STATE)}).has_saved_session() is True
    assert load_secrets({ENV_SESSION_CURL: CURL}).has_saved_session() is True
    assert load_secrets({}).has_saved_session() is False


def test_blank_values_do_not_count() -> None:
    """未設定のシークレットは空文字で入ってくる。空を「あり」と数えない。"""
    assert load_secrets({ENV_SESSION_CURL: "   "}).has_saved_session() is False


def test_curl_route_materializes_a_usable_storage_state(tmp_path: Path) -> None:
    destination = tmp_path / "creds" / "storage_state.json"

    restored = restore_storage_state(load_secrets({ENV_SESSION_CURL: CURL}), destination)

    assert restored == destination
    state = json.loads(destination.read_text(encoding="utf-8"))
    assert [cookie["name"] for cookie in state["cookies"]] == ["_jm_session"]


def test_restored_session_is_owner_only(tmp_path: Path) -> None:
    """12.7: 資格情報。他のプロセス・他のユーザから読めてはならない。"""
    destination = tmp_path / "storage_state.json"

    restore_storage_state(load_secrets({ENV_SESSION_CURL: CURL}), destination)

    assert destination.stat().st_mode & 0o077 == 0


def test_the_richer_route_wins_when_both_are_set(tmp_path: Path) -> None:
    """本物の storage_state には localStorage もクッキー属性も欠けていない。

    より完全な材料があるのに不完全な方で走ると、原因の分からない復元失敗になる。
    """
    destination = tmp_path / "storage_state.json"
    secrets = load_secrets({ENV_STORAGE_STATE_B64: _b64(REAL_STATE), ENV_SESSION_CURL: CURL})

    restore_storage_state(secrets, destination)

    state = json.loads(destination.read_text(encoding="utf-8"))
    assert [cookie["name"] for cookie in state["cookies"]] == ["from_b64"]


def test_nothing_set_restores_nothing(tmp_path: Path) -> None:
    """**空のファイルを作らない。** 作ると「セッションはある」と読まれる。"""
    destination = tmp_path / "storage_state.json"

    assert restore_storage_state(load_secrets({}), destination) is None
    assert not destination.exists()


def test_a_malformed_curl_secret_stops_instead_of_writing_junk(tmp_path: Path) -> None:
    destination = tmp_path / "storage_state.json"
    secrets = load_secrets({ENV_SESSION_CURL: "curl 'https://customers.job-medley.com/'"})

    with pytest.raises(ConfigError, match="Cookie"):
        restore_storage_state(secrets, destination)
    assert not destination.exists()


def test_a_malformed_b64_secret_names_the_variable(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=ENV_STORAGE_STATE_B64):
        restore_storage_state(
            load_secrets({ENV_STORAGE_STATE_B64: "not base64!!"}), tmp_path / "s.json"
        )
