"""段階1の座標を「観測して印字する」部分を固定する。

守りたいのは2点。

1. **観測できたものだけを値として出す。** 出力はそのまま YAML に貼られるので、
   ここでそれらしい値を作ると、推測が座標として定着する (原則3)
2. **観測できなかったものは ``UNRESOLVED`` のまま出す。** 空欄を「無い」で埋めると、
   未確定と確定済みの区別が消える -- 本システムが前提にしている区別そのもの
"""

from __future__ import annotations

import textwrap

import yaml

from jobmedley_scout.config.coordinates import COORDINATES_BY_KEY
from jobmedley_scout.config.placeholders import LadderStage
from jobmedley_scout.recon.manual_login import MarkerCandidate, form_field_selector_candidates
from jobmedley_scout.recon.observe_login import ObservedLogin


def _observed(**overrides: object) -> ObservedLogin:
    defaults: dict[str, object] = {
        "login_url": "https://customers.job-medley.com/customers/sign_in/",
        "login_form_in_served_html": True,
        "email_selectors": ('input[name="customer[email]"]',),
        "password_selectors": ('input[name="customer[password]"]',),
        "submit_selectors": ('button[type="submit"]',),
        "submit_texts": ("ログイン",),
        "marker_candidates": (
            MarkerCandidate("ログアウト", ("#logout", 'a:has-text("ログアウト")')),
        ),
        "authenticated_observation": True,
    }
    defaults.update(overrides)
    return ObservedLogin(**defaults)  # type: ignore[arg-type]


# --- セレクタ候補の並び ------------------------------------------------------


def test_id_beats_name_beats_type() -> None:
    """``type`` が最下位なのは、フォーム内で一意とは限らないから。"""
    assert form_field_selector_candidates(
        "input", "customer_email", "customer[email]", "email"
    ) == (
        "#customer_email",
        'input[name="customer[email]"]',
        'input[type="email"]',
    )


def test_bracketed_name_attributes_are_quoted() -> None:
    """``customer[email]`` は引用符で囲まないとCSSセレクタとして壊れる。"""
    assert form_field_selector_candidates("input", None, "customer[email]", None) == (
        'input[name="customer[email]"]',
    )


def test_hashed_ids_are_rejected_here_too() -> None:
    """生成されたidを座標に書くと、次のデプロイで静かに空振りする。"""
    candidates = form_field_selector_candidates("input", "input-a1b2c3d4", "email", "email")

    assert "#input-a1b2c3d4" not in candidates
    assert candidates[0] == 'input[name="email"]'


def test_nothing_observed_yields_no_candidates() -> None:
    """手掛かりが無いなら候補も出さない。**それらしい物を作らない。**"""
    assert form_field_selector_candidates("input", None, None, None) == ()


# --- 出力 --------------------------------------------------------------------


def test_the_pasteable_block_really_parses_as_yaml() -> None:
    """**文字列一致では足りない。** 実際に読み込ませる。

    素朴に ``f'"{value}"'`` で囲むと ``"input[name="customer[email]"]"`` になり、
    引用符が入れ子になって YAML として壊れる。セレクタが二重引用符を含むのは
    例外ではなく普通なので、この検査が無いと確実に踏む。
    """
    parsed = yaml.safe_load(textwrap.dedent(_observed().yaml_block()))

    assert parsed == {
        "auth.login_url": "https://customers.job-medley.com/customers/sign_in/",
        "auth.is_spa": False,
        "auth.email_selector": 'input[name="customer[email]"]',
        "auth.password_selector": 'input[name="customer[password]"]',
        "auth.submit_selector": 'button[type="submit"]',
        "auth.submit_text_candidates": ["ログイン"],
        "auth.success_marker_selector": "#logout",
        "auth.twofa_kind": "UNRESOLVED",
    }


def test_the_block_covers_every_stage_one_coordinate() -> None:
    """1つでも欠けると、運用者は欠けた分を自力で探すことになる。"""
    parsed = yaml.safe_load(textwrap.dedent(_observed().yaml_block()))
    stage_one = {
        key for key, spec in COORDINATES_BY_KEY.items() if spec.stage is LadderStage.STAGE_1_LOGIN
    }

    assert set(parsed) == stage_one


def test_notes_never_leak_into_a_value() -> None:
    """注記を値の右へ書くと、その文言まで値の一部として読まれる。"""
    parsed = yaml.safe_load(textwrap.dedent(_observed().yaml_block()))

    assert parsed["auth.twofa_kind"] == "UNRESOLVED"


def test_alternatives_are_kept_as_comments() -> None:
    """先頭候補だけを出して残りを捨てない。**選ぶのは人間。**"""
    report = _observed().render()

    assert 'a:has-text("ログアウト")' in report


def test_two_fa_is_never_filled_in() -> None:
    """観測できない唯一の座標。**観測できないものを観測したことにしない** (原則3)。"""
    report = _observed().render()

    assert "auth.twofa_kind: UNRESOLVED" in report
    assert "自分で書いて" in report


def test_unobservable_fields_stay_unresolved() -> None:
    """空を「無い」で埋めない。未確定と確定済みの区別が消える。"""
    report = _observed(email_selectors=(), submit_texts=()).render()

    assert "auth.email_selector: UNRESOLVED" in report
    assert "auth.submit_text_candidates: UNRESOLVED" in report
    # 一方、観測できたものは値として出ていること (全部UNRESOLVEDでは意味がない)。
    assert 'auth.password_selector: "input[name=\\"customer[password]\\"]"' in report


def test_spa_detection_flips_the_flag_and_the_warning() -> None:
    report = _observed(login_form_in_served_html=False).render()

    assert "auth.is_spa: true" in report
    assert "SPA" in report


def test_marker_is_unresolved_when_the_session_could_not_be_used() -> None:
    """認証済みの観測ができなかったのに候補を出すと、嘘の値が座標になる。"""
    report = _observed(authenticated_observation=False, marker_candidates=()).render()

    assert "auth.success_marker_selector: UNRESOLVED" in report
    assert "verify-session" in report


def test_marker_is_unresolved_when_nothing_logout_like_was_found() -> None:
    """入れてはいるが候補ゼロ、という場合。**認証失敗と混同させない。**"""
    report = _observed(marker_candidates=()).render()

    assert "auth.success_marker_selector: UNRESOLVED" in report
    assert "アカウント名の表示" in report


def test_report_points_at_the_strict_verification() -> None:
    """記入して終わりではない。厳密判定で締めるところまで案内する (5.5)。"""
    report = _observed().render()

    assert "verify-session" in report
    assert "厳密判定" in report
