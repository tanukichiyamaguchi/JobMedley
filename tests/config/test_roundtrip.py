"""Behavior-invariance proof for the config validation layer.

7.6 の指示:

> **検証レイヤの導入時は、必ず「検証後の値が元のファイルと完全一致する」テストで
> 振る舞い不変を証明してから入れる** (検証が静かに既定値を注入して判定を変える
> 二次事故を防ぐため)。

つまりこのテストが証明するのは「設定が読める」ことではなく、**読み込みが値を
変えていない** ことである。年齢上限が黙って別の値に化けていないこと、リストの
要素が落ちていないこと。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from jobmedley_scout.config.coordinates import COORDINATES_BY_KEY
from jobmedley_scout.config.loader import load_behavior_config, load_site_coordinates
from jobmedley_scout.config.placeholders import UNRESOLVED_TOKEN, Unresolved
from jobmedley_scout.config.schema import UndeterminablePolicy
from jobmedley_scout.errors import ConfigError

REPO = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO / "config" / "config.yaml"
COORDINATES_PATH = REPO / "config" / "site_coordinates.yaml"


def _raw_config() -> dict[str, Any]:
    return dict(yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")))


def _normalize(value: Any) -> Any:
    """Compare YAML values against validated ones on equal footing.

    pydantic は list を tuple に、str を Path/StrEnum に変換する。それは **表現の
    変換であって値の変更ではない** ので、比較の前に揃える。ここで揃えてよいのは、
    情報が落ちない変換だけである。
    """
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_normalize(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bool | int | float) or value is None:
        return value
    return str(value)


def _walk(model: Any) -> dict[str, Any]:
    """Recursively dump a pydantic model to plain values."""
    if hasattr(model, "model_dump"):
        return {k: _normalize(v) for k, v in model.model_dump().items()}
    return {}


def test_every_config_value_survives_validation_unchanged() -> None:
    """検証後の値が元のYAMLと完全一致すること。

    ここが落ちるということは、検証レイヤが値を書き換えているということ。
    「既定値の静かな注入」は 7.6 が名指しで警告している二次事故である。
    """
    raw = _raw_config()
    config = load_behavior_config(CONFIG_PATH)

    mismatches: list[str] = []

    def compare(raw_node: Any, model_node: Any, path: str) -> None:
        if not isinstance(raw_node, dict):
            return
        dumped = _walk(model_node) if not isinstance(model_node, dict) else model_node
        for key, raw_value in raw_node.items():
            if key not in dumped:
                mismatches.append(f"{path}{key}: 検証後に消えている")
                continue
            model_value = dumped[key]
            if isinstance(raw_value, dict) and isinstance(model_value, dict):
                compare(raw_value, model_value, f"{path}{key}.")
                continue
            if _normalize(raw_value) != _normalize(model_value):
                mismatches.append(f"{path}{key}: YAML={raw_value!r} だが検証後は {model_value!r}")

    compare(raw, config, "")
    assert not mismatches, (
        "検証レイヤが値を書き換えています。7.6: 検証が静かに既定値を注入すると"
        "対象判定が変わります:\n  " + "\n  ".join(mismatches)
    )


def test_specific_targeting_thresholds_are_exactly_as_written() -> None:
    """特に効く値を名指しで固定する。

    参照実装の事故は「**年齢上限が無言で消える**」だった。抽象的な往復比較だけで
    なく、実際に事故った種類の値を名指しで押さえておく。

    その年齢上限を含む対象条件は 2026-08-12 に全廃した (経緯は
    ``targeting/rules.py``)。**消えた値のアサーションを ``.get()`` で生き延び
    させていない** -- 通るだけの緑は、消えた赤より悪い。いま名指しできるのは
    方針表だけなので、それを押さえる。

    「無言で消える」の防止そのものは
    :func:`test_every_config_value_survives_validation_unchanged` と
    ``targeting/registry.py`` の両方向検査が引き継いでいる。後者は、設定と実装の
    どちらか片方にしか無いルールIDを起動時の例外にする。
    """
    raw = _raw_config()["targeting"]
    targeting = load_behavior_config(CONFIG_PATH).targeting

    assert dict(targeting.undeterminable_policy) == {
        key: UndeterminablePolicy(value) for key, value in raw["undeterminable_policy"].items()
    }
    # 方針表が空になると全候補者が素通りする。**空は設定ミスとしてしか起きない。**
    assert targeting.undeterminable_policy


def test_safety_values_are_exactly_as_written() -> None:
    """安全弁は特に。既定値が効いていないことの確認でもある。"""
    raw = _raw_config()["safety"]
    safety = load_behavior_config(CONFIG_PATH).safety

    assert safety.dry_run is raw["dry_run"]
    assert safety.state_loss_guard is raw["state_loss_guard"]
    assert safety.ingest_cap == raw["ingest_cap"]
    assert str(safety.kill_switch_path) == raw["kill_switch_path"]


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    """打鍵ミスは「未知のキー」として落ちる (7.6)。"""
    raw = _raw_config()
    raw["targeting"]["age_maxx"] = 42  # タイポ
    bad = tmp_path / "config.yaml"
    bad.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    with pytest.raises(ConfigError) as excinfo:
        load_behavior_config(bad)
    assert "age_maxx" in str(excinfo.value)


def test_missing_safety_key_is_rejected(tmp_path: Path) -> None:
    """既定値が無いので、キーを落とすと「必須キーが無い」として落ちる。

    打鍵ミスが2つの独立した失敗になるのがこの設計の要点。
    """
    raw = _raw_config()
    del raw["safety"]["dry_run"]
    bad = tmp_path / "config.yaml"
    bad.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    with pytest.raises(ConfigError) as excinfo:
        load_behavior_config(bad)
    assert "dry_run" in str(excinfo.value)


def test_wrong_type_is_rejected(tmp_path: Path) -> None:
    raw = _raw_config()
    raw["safety"]["ingest_cap"] = "たくさん"
    bad = tmp_path / "config.yaml"
    bad.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    with pytest.raises(ConfigError):
        load_behavior_config(bad)


# --- 座標ファイル -------------------------------------------------------------


def test_every_registered_coordinate_is_accounted_for() -> None:
    """全キーが「確定」か「未確定」のどちらかとして存在すること。

    **件数を固定してはいけない。** かつてここは「50件すべて未確定」を固定して
    いたが、それはラダーを1段登るたびに落ちるテストであり、座標を埋めるという
    正常な進行を「失敗」として報告する。守るべき不変量は件数ではなく、
    *登録済みのキーが1つ残らずファイルに現れること* -- 抜けたキーが黙って
    既定値に落ちる経路が無いこと (7.6) である。
    """
    coordinates = load_site_coordinates(COORDINATES_PATH)

    accounted = set(coordinates.unresolved_keys()) | set(coordinates.resolved_keys())
    assert accounted == set(COORDINATES_BY_KEY)


def test_unfilled_coordinates_are_still_unresolved() -> None:
    """埋めていない座標が残っていること。**推測で埋めないという方針の表明。**

    全部埋まった状態は、この方針に反して埋めたか、ラダーを完走したかのどちらか。
    完走したなら、そのときにこのテストを消すこと -- 消さずに緩めると、
    「いつの間にか全部埋まっていた」が検知できなくなる。
    """
    coordinates = load_site_coordinates(COORDINATES_PATH)

    assert coordinates.unresolved_keys(), "全座標が確定しています。経緯を確認してください"


def test_missing_coordinate_key_is_rejected(tmp_path: Path) -> None:
    """キーの省略は打鍵ミスと同じく検証エラー (7.6)。"""
    raw = dict(yaml.safe_load(COORDINATES_PATH.read_text(encoding="utf-8")))
    del raw["auth.login_url"]
    bad = tmp_path / "coords.yaml"
    bad.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    with pytest.raises(ConfigError) as excinfo:
        load_site_coordinates(bad)
    assert "auth.login_url" in str(excinfo.value)


def test_null_rejected_where_not_nullable(tmp_path: Path) -> None:
    """null と UNRESOLVED は意味が違う。

    null は「確認した結果 存在しない」という **確定した答え**。
    nullable でない座標に null を書くのは、確定していないのに確定したと書くこと。
    """
    raw = dict(yaml.safe_load(COORDINATES_PATH.read_text(encoding="utf-8")))
    raw["auth.login_url"] = None
    bad = tmp_path / "coords.yaml"
    bad.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    with pytest.raises(ConfigError) as excinfo:
        load_site_coordinates(bad)
    assert UNRESOLVED_TOKEN in str(excinfo.value)


def test_nullable_coordinate_accepts_null(tmp_path: Path) -> None:
    """「確認したが存在しなかった」は書ける。"""
    raw = dict(yaml.safe_load(COORDINATES_PATH.read_text(encoding="utf-8")))
    raw["api.precheck.url_pattern"] = None
    good = tmp_path / "coords.yaml"
    good.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    coordinates = load_site_coordinates(good)
    # None は確定した答えなので、未確定リストには入らない。
    assert "api.precheck.url_pattern" not in coordinates.unresolved_keys()
    assert coordinates.optional_url("api.precheck.url_pattern") is None


def test_unresolved_coordinate_is_an_unresolved_instance() -> None:
    """未確定の座標が :class:`Unresolved` として読まれること。

    **特定のキーを名指ししない。** 以前は ``auth.login_url`` を使っていたが、
    その座標が確定した瞬間にこのテストが落ちた -- 検査したかったのは番兵の型で
    あって、あのキーが未確定であることではない。未確定のキーを実行時に選ぶ。
    """
    coordinates = load_site_coordinates(COORDINATES_PATH)
    key = coordinates.unresolved_keys()[0]

    value = coordinates.raw_items()[key]
    assert isinstance(value, Unresolved)
    assert value.key == key
    # repr は例外を出さない (デバッガと pytest が壊れるため)。
    assert key in repr(value)


def test_a_filled_coordinate_reads_back_as_its_value() -> None:
    """確定した座標が、番兵ではなく素の値として読めること。

    上のテストと対になる。片方だけだと「全部 Unresolved を返す」実装でも
    通ってしまう。
    """
    coordinates = load_site_coordinates(COORDINATES_PATH)

    assert coordinates.url("auth.login_url") == (
        "https://customers.job-medley.com/customers/sign_in/"
    )
    assert coordinates.boolean("auth.is_spa") is True
    assert coordinates.string_list("auth.submit_text_candidates") == ("ログイン",)
