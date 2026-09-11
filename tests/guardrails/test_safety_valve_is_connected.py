"""**安全弁が「効いている」ことを検査する** (12.6)。

参照実装の事故はこう記録されている:

> 状態消失ガードが実行基盤の環境変数に渡っておらず、**ドキュメントには手順が
> あるのにCIでは常に無効** だった。
> **「安全弁を作った」と「安全弁が効いている」は別物です。**

**そして私は同じ穴を掘った。** ``config/effective.py`` は ``SCOUT_DRY_RUN`` を
正しく解釈し、``preflight`` は実効値と由来を正しく印字していた。既存の検査も
すべて緑だった。それでも **どのコマンドにも値は届いていなかった** -- 唯一の
利用者が preflight の *印字* で、判定は ``config.yaml`` の生値を読んでいたため。

2026-09-11、1通目の送信で発覚した。ワークフローは ``SCOUT_DRY_RUN=false`` を
渡し、ログにもそう出ていたのに、コマンドは「dry_run が有効です」で止まった。

**既存の検査が緑のまま欠陥が出荷された理由** は、検査していたのが

* ワークフローのYAMLが環境変数を渡していること
* ``resolve_safety_settings`` が環境変数を正しく解釈すること
* ``preflight`` が実効値を印字すること

の3つで、**その値が判定に使われること** を誰も検査していなかったからである。
部品ごとには正しく、繋がっていなかった。だからこのファイルは
**繋がっていること自体** を検査する。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jobmedley_scout.config.effective import ENV_DRY_RUN, ENV_STATE_LOSS_GUARD
from jobmedley_scout.config.loader import load_all

CONFIG = Path("config/config.yaml")
COORDINATES = Path("config/site_coordinates.yaml")


def _config(env: dict[str, str]):
    config, _ = load_all(CONFIG, COORDINATES, env=env)
    return config


def test_the_env_valve_reaches_the_value_commands_actually_read() -> None:
    """``SCOUT_DRY_RUN=false`` が ``config.safety.dry_run`` に届くこと。

    **これが欠けていた検査である。** 部品は全部正しかったのに、コマンドが読む
    属性には届いていなかった。
    """
    assert _config({ENV_DRY_RUN: "false"}).safety.dry_run is False


def test_the_env_valve_can_also_turn_the_guard_back_on() -> None:
    """**危険な向きも塞ぐ。**

    今回外れたのは安全な向き (送れなくなる) だった。だが同じ穴は逆向きにも
    開いていた -- ``config.yaml`` に ``dry_run: false`` と書かれていたら、
    ``SCOUT_DRY_RUN=true`` で止めようとしても止まらなかった。
    **止める側が効かない方が重い。**
    """
    assert _config({ENV_DRY_RUN: "true"}).safety.dry_run is True


def test_without_the_env_the_file_still_decides() -> None:
    """環境変数が無ければ ``config.yaml`` の値が使われること。

    上書きが「常に効く」だけでは足りない。**上書きしていないときに勝手な既定値へ
    落ちない** ことも同じだけ重要である (7.6: 検証が静かに既定値を注入する事故)。
    """
    from jobmedley_scout.config.loader import load_behavior_config

    on_file = load_behavior_config(CONFIG).safety.dry_run
    assert _config({}).safety.dry_run is on_file


def test_the_state_loss_guard_is_wired_the_same_way() -> None:
    """状態消失ガードも同じ経路に載っていること。

    参照実装で実際に外れていたのは **こちら** である (12.6 の原文)。
    dry_run だけ繋いで満足すると、原文そのままの事故が残る。
    """
    assert _config({ENV_STATE_LOSS_GUARD: "false"}).safety.state_loss_guard is False
    assert _config({ENV_STATE_LOSS_GUARD: "true"}).safety.state_loss_guard is True


def test_an_uninterpretable_value_stops_the_run() -> None:
    """解釈できない値を黙って false にしないこと。

    ``dry_run="maybe"`` が「本番送信」に解釈されるのが最悪の失敗である。
    """
    from jobmedley_scout.errors import ConfigError

    with pytest.raises(ConfigError):
        _config({ENV_DRY_RUN: "maybe"})


def test_the_send_gate_sees_what_the_loader_produced() -> None:
    """**入口から門まで、値が同じであること。**

    ここまでの検査は loader の出力を見ている。門が読むのは
    ``safety.dry_run`` なので、その一致を最後に結ぶ。これが繋がっていなければ
    上の検査が全部緑でも送信は起きない (実際そうだった)。
    """
    from jobmedley_scout.runtime.commands.send_first import FirstSendReport, FirstSendStage

    safety = _config({ENV_DRY_RUN: "false"}).safety
    report = FirstSendReport(dry_run=safety.dry_run, acknowledged=True)
    assert report.reached() is not FirstSendStage.DRY_RUN_ON


def test_the_refusal_names_where_the_value_came_from() -> None:
    """止めたときに **由来** を出すこと (12.6)。

    値だけの報告は、届いていない配線を隠す。「dry_run が有効です」とだけ書かれた
    ログからは、設定ファイルがそう言っているのか、環境変数が届いていないのかが
    区別できない。実測48回目は、まさにその区別がつかずに調査が要った。
    """
    from jobmedley_scout.config.effective import SOURCE_CONFIG
    from jobmedley_scout.runtime.commands.send_first import FirstSendReport

    rendered = FirstSendReport(
        dry_run=True, dry_run_source=SOURCE_CONFIG, acknowledged=True
    ).render()
    assert SOURCE_CONFIG in rendered
    assert "環境変数が届いていません" in rendered
