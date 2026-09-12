"""ワークフローの安全弁を、レビューではなく機械で固定する。

ここで固定する性質は、いずれも **壊しても CI は緑のまま** である。だからこそ
テストで押さえる必要がある。

実際に一度壊していた例:

    SCOUT_DRY_RUN: ${{ inputs.dry_run == false && 'false' || 'true' }}

スケジュール実行では ``inputs.dry_run`` が null になる。GitHub の式は型が違う比較を
**数値に変換して** 行うので、null も false も 0 になり、``null == false`` が true に
なる。つまり定時実行のたびに安全弁が外れる。手で外した覚えが無いのに外れている、
という 13.6 が禁じている形そのものだった。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
SCOUT = WORKFLOWS / "scout.yml"
RECON = WORKFLOWS / "recon.yml"
FIRST_SEND = WORKFLOWS / "first-send.yml"
DRYRUN = WORKFLOWS / "dryrun.yml"

#: **媒体へ触れるワークフロー。** ここに並んでいないものは検査を受けない。
#:
#: 以前は ``SCOUT`` と ``RECON`` の2本を手で並べていた。その結果、
#: **唯一送信する first-send.yml がどの検査にも掛かっていなかった。** 作った
#: ときに並べ忘れ、並べ忘れたことを誰も検知しなかった -- 手で並べる作りは、
#: 足したものを黙って素通りさせる。
#:
#: :func:`test_every_workflow_is_covered_by_these_checks` が、ディレクトリの
#: 実体とこの並びの一致を見張る。**次に足す1本は、足した瞬間に赤くなる。**
TOUCHES_THE_PLATFORM = (SCOUT, RECON, FIRST_SEND, DRYRUN)

#: 媒体へ触れないもの (CI 等)。ここに入れるのは明示的な判断である。
NOT_OUR_CONCERN = frozenset({"ci.yml"})


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _parsed(path: Path) -> dict:
    # PyYAML は無引用の `on:` を真偽値 True と読む。ここでは on の中身を見ないので
    # そのままでよいが、キー名で引くときは注意すること。
    return dict(yaml.safe_load(_text(path)))


def test_dry_run_cannot_be_disabled_by_a_scheduled_run() -> None:
    """13.6: 解除は **二重に明示的な操作** を要する。

    手動起動であること (``github.event_name``) の判定が式から消えると、
    スケジュール実行で安全弁が外れる。この文字列は消してはならない。
    """
    expression = _text(SCOUT)

    assert "SCOUT_DRY_RUN:" in expression
    dry_run_line = expression.split("SCOUT_DRY_RUN:", 1)[1]
    assert "github.event_name == 'workflow_dispatch'" in dry_run_line
    assert "inputs.dry_run == false" in dry_run_line


def test_the_state_loss_guard_is_passed_explicitly() -> None:
    """12.6: 参照実装ではガードが環境変数に渡っておらず、CIでは常に無効だった。"""
    assert 'SCOUT_STATE_LOSS_GUARD: "true"' in _text(SCOUT)


def test_running_executions_are_never_cancelled() -> None:
    """12.1: 送信後・状態保存前で落とすと送信記録が巻き戻る。56件消えた形。"""
    assert _parsed(SCOUT)["concurrency"]["cancel-in-progress"] is False


def test_preflight_is_not_allowed_to_fail() -> None:
    """12.6: 参照実装は起動前チェックが「失敗しても続行」で、失敗しても送信が続いた。"""
    steps = _parsed(SCOUT)["jobs"]["scout"]["steps"]
    preflight = [step for step in steps if step.get("name") == "Preflight"]

    assert preflight, "Preflight ステップが消えています"
    assert "continue-on-error" not in preflight[0]


@pytest.mark.parametrize("path", TOUCHES_THE_PLATFORM, ids=lambda p: p.name)
def test_credentials_are_never_cached(path: Path) -> None:
    """12.7: 既定ブランチのキャッシュは他ブランチからも復元できる。

    セッションをキャッシュに入れると、リポジトリに書ける者が媒体アカウントを
    乗っ取れる経路になる。
    """
    for step in _parsed(path)["jobs"].popitem()[1]["steps"]:
        if not str(step.get("uses", "")).startswith("actions/cache"):
            continue
        cached = str(step.get("with", {}).get("path", ""))
        assert "credential" not in cached
        assert cached.strip() == "state"


def test_the_recon_workflow_never_sends() -> None:
    """偵察と送信を同じワークフローに同居させない。

    同居させると、偵察のつもりで送信ステップを踏む事故が起きる。
    """
    recon = _text(RECON)

    for forbidden in ("scout send", "scout followup", "scout dryrun"):
        assert forbidden not in recon
    # 偵察でも安全弁は明示的に渡す。渡し忘れを既定値で救わない。
    assert 'SCOUT_DRY_RUN: "true"' in recon


def test_preflight_sees_the_same_credentials_as_a_real_run() -> None:
    """点検が **存在しない環境** を点検してはいけない。

    実際に踏んだ: 段階2の ``scout preflight`` を Recon (manual) から実行できるように
    したが、そのジョブに ``ANTHROPIC_API_KEY`` を渡していなかった。シークレットを
    正しく登録しても「APIキーが未設定」で必ず失敗する -- 設定したのに実行環境へ
    届いていない、という 12.6 の事故を、それを検知するための点検コマンド自身で
    やっていたことになる。

    片方に資格情報を足したらもう片方にも足すこと。**preflight の答えは、本番の
    実行環境についての答えでなければ意味がない。**
    """
    credentials = {"ANTHROPIC_API_KEY", "JOBMEDLEY_STORAGE_STATE_B64", "JOBMEDLEY_SESSION_CURL"}
    scout_env = set(_parsed(SCOUT)["jobs"]["scout"]["env"])
    recon_env = set(_parsed(RECON)["jobs"]["recon"]["env"])

    assert credentials <= scout_env
    missing = (scout_env & credentials) - recon_env
    assert not missing, f"Recon (manual) に渡っていない資格情報があります: {sorted(missing)}"


def test_every_recon_command_actually_runs_something() -> None:
    """**選択肢に在るのに実行するステップが無い、を禁じる。**

    実際に踏んだ (2026-08-18): ``read-bundle`` を選択肢と CLI には足したが、
    ワークフローのステップを足し忘れた。運用者が選んで実行すると、条件に
    一致するステップが1つも無いまま **ジョブは「成功」で終わる**。

    これは 原則2 の「静かなゼロ件」そのものである。失敗より悪い -- 失敗なら
    運用者は何かがおかしいと気づくが、成功と表示されれば「実行したのに何も
    出なかった」と受け取り、媒体側の問題を疑い始める。実際にこの1回で往復を
    まるごと1つ無駄にした。

    レビューでは見つからない。選択肢とステップは同じファイルの離れた場所に
    あり、片方だけ足しても構文は正しいままだからである。だから機械で押さえる。
    """
    parsed = _parsed(RECON)
    # PyYAML は無引用の `on:` を True と読む。
    trigger = parsed.get("on") or parsed[True]
    options = trigger["workflow_dispatch"]["inputs"]["command"]["options"]
    assert options, "command の選択肢が空になっている"

    steps = parsed["jobs"]["recon"]["steps"]
    guarded = " ".join(str(step.get("if", "")) for step in steps)

    missing = [option for option in options if f"'{option}'" not in guarded]
    assert not missing, (
        f"選択肢に在るのに実行するステップが無い command: {missing}。"
        " 運用者が選ぶと、何も実行しないまま「成功」で終わる (静かなゼロ件)。"
    )


def test_every_recon_step_belongs_to_an_offered_command() -> None:
    """逆向き: **選べないコマンドのステップが残っていないこと。**

    選択肢から消しただけでステップが残ると、二度と実行されない死んだ設定に
    なる。次に読む人は「この経路は動いている」と読み違える。
    """
    parsed = _parsed(RECON)
    trigger = parsed.get("on") or parsed[True]
    options = set(trigger["workflow_dispatch"]["inputs"]["command"]["options"])

    for step in parsed["jobs"]["recon"]["steps"]:
        condition = str(step.get("if", ""))
        if "inputs.command ==" not in condition:
            continue
        named = {part.strip("' ") for part in condition.split("==")[1:]}
        assert (
            named & options
        ), f"選択肢に無い command を条件にしたステップがある: {step.get('name')} ({condition})"


@pytest.mark.parametrize("path", TOUCHES_THE_PLATFORM, ids=lambda p: p.name)
def test_every_setup_step_has_its_own_time_limit(path: Path) -> None:
    """**詰まった段取りに、ジョブの持ち時間を全部食わせない。**

    実測14回目、``playwright install`` が25分ハングしてジョブごと打ち切られ、
    偵察は一度も走らないまま往復を1つ失った。段取りに上限が無いと、詰まりは
    「何もせず打ち切られた」という形でしか現れず、**原因がこちらのコードに見える**。

    上限があれば、詰まりはその段取りの失敗として見える (原則2)。
    """
    steps = _parsed(path)["jobs"].popitem()[1]["steps"]
    setup = [
        step
        for step in steps
        if "install" in str(step.get("name", "")).lower()
        or "pip install" in str(step.get("run", ""))
        or "playwright install" in str(step.get("run", ""))
    ]
    assert setup, "段取りのステップが見当たらない (この検査が空振りしている)"

    missing = [
        str(step.get("name", step.get("run", "?")))
        for step in setup
        if not step.get("timeout-minutes")
    ]
    assert not missing, f"時間の上限が無い段取り: {missing}"


def test_every_workflow_is_covered_by_these_checks() -> None:
    """**足した1本が黙って素通りしないこと。**

    ここが今回の要点である。以前は検査するファイルを手で2本並べており、
    ``first-send.yml`` -- **唯一実際に送信するワークフロー** -- がどの検査にも
    掛かっていなかった。作ったときに並べ忘れ、並べ忘れたことを誰も検知しなかった。

    実測48・49・51回目と同じ形である: **部品ごとには正しく、部品のあいだが
    繋がっていない。** 手で並べる作りは、その穴を構造的に作り続ける。
    """
    on_disk = {path.name for path in WORKFLOWS.glob("*.yml")}
    covered = {path.name for path in TOUCHES_THE_PLATFORM}
    uncovered = sorted(on_disk - covered - NOT_OUR_CONCERN)
    assert not uncovered, (
        f"検査の対象に入っていないワークフローがあります: {', '.join(uncovered)}。"
        " TOUCHES_THE_PLATFORM へ足すか、媒体へ触れないなら NOT_OUR_CONCERN へ"
        " 明示的に入れてください (既定で素通りさせない)。"
    )
    missing = sorted(name for name in covered if name not in on_disk)
    assert not missing, f"並びにあるのに実体が無いワークフロー: {', '.join(missing)}"


@pytest.mark.parametrize("path", TOUCHES_THE_PLATFORM, ids=lambda p: p.name)
def test_every_workflow_passes_the_safety_valves_explicitly(path: Path) -> None:
    """12.6: **渡し忘れを既定値で救わない。**

    「安全弁を作った」と「安全弁が効いている」は別物である。実測48回目で、
    環境変数が届いていないままコマンドが既定値を読んでいた事故を踏んでいる。
    """
    text = _text(path)
    assert "SCOUT_DRY_RUN:" in text, "安全弁を環境へ渡していない"
    assert "SCOUT_STATE_LOSS_GUARD:" in text, "状態消失ガードを環境へ渡していない"


@pytest.mark.parametrize("path", TOUCHES_THE_PLATFORM, ids=lambda p: p.name)
def test_no_workflow_cancels_a_running_execution(path: Path) -> None:
    """12.1: 送信後・状態保存前で落とすと送信記録が巻き戻る。56件消えた形。"""
    concurrency = _parsed(path).get("concurrency")
    if concurrency is None:
        return
    assert (
        concurrency.get("cancel-in-progress") is not True
    ), f"{path.name} が走行中の実行をキャンセルします"


def test_only_one_workflow_can_actually_send() -> None:
    """**送信できるワークフローを1本に保つ。**

    ``scout.yml`` は段階5を通過するまで schedule をコメントアウトしてあるので、
    手で起動しない限り走らない。実際に1通を送る口は ``first-send.yml`` だけで
    ある。増えていないことをここで見張る。
    """
    senders = [
        path.name
        for path in TOUCHES_THE_PLATFORM
        if "scout send" in _text(path) or "scout send-first" in _text(path)
    ]
    assert set(senders) <= {
        "scout.yml",
        "first-send.yml",
    }, f"送信コマンドを持つワークフローが増えています: {senders}"
