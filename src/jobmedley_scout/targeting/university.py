"""Overseas / domestic university classification by script.

7.4 の事故: **「文字種イコール出自」は日本語では必ず破れる。** 参照実装は学校名が
日本語の文字で書かれていることをもって国内と判定しており、「スタンフォード大学」が
国内大学に分類された。カタカナは外来語の表記であって出自ではない。

ここでの設計:

1. 接尾辞 (大学院大学 / 大学院 / 大学校 / 大学) を **長い順に** 落として核を取る。
   順序は仕様である -- 「大学」を先に落とすと「◯◯大学院」が「◯◯院」になる。
2. 核が漢字・ひらがなを含めば国内。核がカタカナだけなら海外。
3. 「ノートルダム清心女子大学」のような **核までカタカナの国内校** は許可リストで
   吸収する。許可リストは **設定ファイル** に置く (コード内定数にすると、
   1校追加するたびにデプロイが必要になる)。

残差の向き: 漢字表記の海外大学 (北京大学など) は国内側に落ちる。すなわち誤りは
**「送る」側** に倒れる。これは意図した非対称である -- 7.4 の事故で問題になったのは
「日本語表記だから国内」と決めつけたことであり、判定できないものを海外側 (=除外) に
倒すと、今度は日本人を静かに取りこぼす。
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict

from jobmedley_scout.models.text_norm import fold_width, normalize_name, normalize_ws
from jobmedley_scout.targeting.determination import Determination

_STRICT = ConfigDict(extra="forbid", frozen=True)

#: **長い順。この順序が仕様である** (7.4)。「大学院」より先に「大学」を試すと
#: 「◯◯大学院」の核が「◯◯院」になり、以降の文字種判定が全部ずれる。
SCHOOL_SUFFIXES: tuple[str, ...] = ("大学院大学", "大学院", "大学校", "大学")

_KANJI_HIRAGANA = re.compile(r"[\u3040-\u309f\u3400-\u4dbf\u4e00-\u9fff\u3005]")
_KATAKANA = re.compile(r"[\u30a0-\u30ff]")
#: カタカナ・長音・中黒・空白だけで構成されているか。
_KATAKANA_ONLY = re.compile(r"^[\u30a0-\u30ff\s]+$")


class UniversityClassification(BaseModel):
    """Classification verdict with the core name it was decided on."""

    model_config = _STRICT

    determination: Determination
    core: str
    evidence: str


def strip_school_suffixes(name: str) -> str:
    """Strip school suffixes longest-first and return the core name.

    「スタンフォード大学」-> 「スタンフォード」、「◯◯大学院」-> 「◯◯」。
    接尾辞は末尾に限らず落とす (「◯◯大学 経済学部」のような表記があるため)。
    """
    core = normalize_ws(fold_width(name))
    for suffix in SCHOOL_SUFFIXES:
        # 長い順に **全出現** を落とす。短い接尾辞が長い接尾辞の一部を食う
        # (「大学」が「大学院」の前半を食う) 事故を、順序だけで防いでいる。
        core = core.replace(suffix, "")
    return normalize_ws(core)


def _allowlist_keys(allowlist: tuple[str, ...]) -> frozenset[str]:
    """Normalized full names *and* cores of the domestic katakana allowlist."""
    keys: set[str] = set()
    for entry in allowlist:
        normalized = normalize_name(entry)
        if normalized:
            keys.add(normalized)
        core = normalize_name(strip_school_suffixes(entry))
        if core:
            keys.add(core)
    return frozenset(keys)


def classify_university(name: str | None, allowlist: tuple[str, ...]) -> UniversityClassification:
    """Classify a school name as overseas (``MATCH``) / domestic (``NO_MATCH``)."""
    if name is None or not normalize_ws(fold_width(name)):
        # 学校名が無いことは「国内校である」根拠にならない (7.1)。
        return UniversityClassification(
            determination=Determination.UNDETERMINABLE, core="", evidence="学校名が未取得または空"
        )
    core = strip_school_suffixes(name)
    if not core:
        # 「大学」だけのような値。核が残らないので判定材料が無い。
        return UniversityClassification(
            determination=Determination.UNDETERMINABLE,
            core=core,
            evidence=f"接尾辞を除くと核が残らない: 「{normalize_ws(fold_width(name))}」",
        )
    keys = _allowlist_keys(allowlist)
    if normalize_name(name) in keys or normalize_name(core) in keys:
        # 7.4: 核までカタカナの国内校。コード定数ではなく設定で吸収する。
        return UniversityClassification(
            determination=Determination.NO_MATCH,
            core=core,
            evidence=f"国内カタカナ大学の許可リストに一致: 「{core}」",
        )
    if _KANJI_HIRAGANA.search(core):
        return UniversityClassification(
            determination=Determination.NO_MATCH,
            core=core,
            evidence=f"核に漢字/ひらがなを含む: 「{core}」",
        )
    if _KATAKANA_ONLY.match(core) and _KATAKANA.search(core):
        return UniversityClassification(
            determination=Determination.MATCH,
            core=core,
            evidence=f"核がカタカナのみ: 「{core}」",
        )
    # ラテン文字表記 ("Stanford University") や記号のみなど。**国内に畳まない。**
    # ここを NO_MATCH にすると 7.4 の事故を別の文字種で再演することになる。
    return UniversityClassification(
        determination=Determination.UNDETERMINABLE,
        core=core,
        evidence=f"文字種から出自を判定できない核: 「{core}」",
    )


def is_overseas_university(name: str | None, allowlist: tuple[str, ...]) -> Determination:
    """Three-valued verdict: ``MATCH`` means the school looks overseas."""
    return classify_university(name, allowlist).determination
