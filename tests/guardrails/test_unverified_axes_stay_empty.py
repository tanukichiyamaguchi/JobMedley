"""参照実装由来の軸を、観測せずに埋めさせない。

指示書は **参照実装** の事故から学んだ教訓でできている。その副作用として、
座標ファイルには参照実装の *形* がそのまま入っている -- 業界という軸、
有料枠と無料枠の別、送信時に指定する追客日数。

**この媒体にそれらが在るかは、まだ観測していない。**

運用者が示した実画面 (2026-08-19) に並んでいたのは、希望職種・経験職種・資格・
希望勤務地・希望勤務形態・希望入職時期・就業状況・最終学歴・こだわり条件・自己PR
である。業界の欄は見ていない。段階3で観測した送信 payload の5項目にも、追客に
当たるものは無い。

だからといって「無い」と書くのは推測である (原則3)。**空のままにしておく。**
空ならプロンプトに出ず、モデルは言及できない -- それが 6.4 の事故
(「ご希望の◯◯業界」という虚偽) に対する唯一の構造的な防御である。

この試験が守るのは1点だけ:

    **観測していない軸を、それらしい値で埋めていないこと。**

いちばん起こりそうな埋め方は「職種のキーパスを業界の欄にも入れておく」である。
取り違えではなく善意でそれをやると、6.4 の虚偽を意図的に作ることになる。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

COORDINATES = Path("config/site_coordinates.yaml")

#: この媒体に在るか未確認の軸。**埋めるなら観測の根拠を添えること。**
UNVERIFIED_AXES: tuple[str, ...] = (
    "resume.fields.experienced_industries",
    "resume.fields.desired_industries",
    "api.send.free.url_pattern",
    "api.send.free.success_statuses",
    "api.send.free.payload_template",
    "followup.native_supported",
    "followup.param_name",
    "followup.allowed_days",
)

#: 職種の軸。**業界の欄へ流用してはいけない。**
OCCUPATION_AXES: tuple[str, ...] = (
    "resume.fields.experienced_occupations",
    "resume.fields.desired_occupations",
)


def _loaded() -> dict[str, object]:
    return yaml.safe_load(COORDINATES.read_text(encoding="utf-8")) or {}


@pytest.mark.parametrize("key", UNVERIFIED_AXES)
def test_an_unverified_axis_carries_a_note_saying_so(key: str) -> None:
    """**未確認であることが、ファイルを読んだ人間に見えること。**

    UNRESOLVED は「まだ埋めていない」としか言わない。「そもそも在るか分からない」
    は別の情報で、それが見えないと、次に埋める人は素直に探しにいってしまう。
    """
    text = COORDINATES.read_text(encoding="utf-8")
    head = text.split(f"\n{key}:")[0]
    # 直近の注記ブロックに、未確認である旨が書かれていること。
    recent = head[-2000:]
    assert "この媒体に在るかは未確認" in recent, (
        f"{key} の近くに「参照実装由来。この媒体に在るかは未確認」の注記がありません。"
        f"UNRESOLVED だけでは『まだ埋めていない』としか読めません。"
    )


def test_the_industry_axis_is_not_filled_with_the_occupation_axis() -> None:
    """**6.4 の虚偽を、取り違えではなく意図的に作らない。**

    業界の欄に職種のキーパスを入れると、生成されるのは「ご希望の◯◯業界」
    という、この候補者が一度も言っていない文である。
    """
    loaded = _loaded()
    occupations = {str(loaded.get(key)) for key in OCCUPATION_AXES if loaded.get(key) is not None}
    for key in ("resume.fields.experienced_industries", "resume.fields.desired_industries"):
        value = loaded.get(key)
        if value is None or value == "UNRESOLVED":
            continue
        assert str(value) not in occupations, (
            f"{key} に職種のキーパスが入っています。業界と職種は別の軸です -- "
            f"取り違えたまま文面を書かせると 6.4 の虚偽になります。"
        )


@pytest.mark.parametrize("key", UNVERIFIED_AXES)
def test_filling_an_unverified_axis_requires_evidence_in_the_file(key: str) -> None:
    """埋めてよい。**ただし観測の根拠を隣に書くこと。**

    「観測した」と書けない値は、推測で入れた値である (原則3)。
    """
    loaded = _loaded()
    value = loaded.get(key)
    if value is None or value == "UNRESOLVED":
        return  # 空のままなら何も要求しない
    text = COORDINATES.read_text(encoding="utf-8")
    recent = text.split(f"\n{key}:")[0][-2000:]
    assert "観測" in recent, (
        f"{key} に値が入っていますが、近くに観測した旨の記述がありません。"
        f"根拠を書けない値は、推測で入れた値です (原則3)。"
    )
