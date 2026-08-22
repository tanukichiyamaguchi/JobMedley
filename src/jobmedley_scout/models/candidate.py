"""Candidate and resume models.

**すべてのレジュメ項目が Optional である。** これは手抜きではなく 6.4 の帰結:

> 確定の手順: (1) キー一覧だけを初回1回だけログに出す (2) 本番を1巡させて
> 実際のキー名を確定する (3) **確定するまで、そのフィールドは空のままにする**

空ならプロンプトに出ず、モデルは言及できない。嘘の発生経路が構造的に塞がる
(原則3)。ジョブメドレーのレジュメのキー写像はまだ1つも確定していないため、
現状これらは常に ``None`` である。

**命名について。** 参照実装の最大の罠は、レジュメのトップレベルの「業界」「職種」が
*経験してきた* もので、希望条件オブジェクト配下の同名キーが *希望する* もの
だったこと。これを取り違えて「ご希望の◯◯業界」と書き、運用者から「嘘が多い」と
指摘された。そこで本モデルでは ``experienced_`` と ``desired_`` を接頭辞として
分け、**同名のフィールドが存在しえない** ようにしてある。写像を書く人が
どちらか迷ったら、それは実データで確認すべきという合図である。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from jobmedley_scout.models.ids import CandidateId

_STRICT = ConfigDict(extra="forbid", frozen=True)


class Employment(BaseModel):
    """One employment entry."""

    model_config = _STRICT

    company: str | None = None
    occupation: str | None = None
    tenure_years: float | None = None
    is_current: bool = False


class Education(BaseModel):
    """One education entry."""

    model_config = _STRICT

    school: str | None = None
    #: 生値。enum への写像は api.enums_map の3段構えで行い、判別不能なら
    #: EducationLevel.UNKNOWN に落として生値をログに出す (6.5)。
    raw_level: str | None = None
    faculty: str | None = None


class ResumeFacts(BaseModel):
    """Facts extracted from a candidate's resume.

    ここに値が入るのは、実データでキーパスを確定した項目だけ。未確定の項目は
    ``None`` のままにすること -- 推測で埋めると、その嘘がそのまま文面に出る。
    """

    model_config = _STRICT

    # --- 経験 (実際にしてきたこと) -------------------------------------
    experienced_industries: tuple[str, ...] = ()
    experienced_occupations: tuple[str, ...] = ()
    employments: tuple[Employment, ...] = ()
    educations: tuple[Education, ...] = ()
    specialty: str | None = None
    summary: str | None = None

    # --- 希望 (これから望むこと) ---------------------------------------
    # 経験側と接頭辞で分離してある。理由は module docstring を参照。
    desired_industries: tuple[str, ...] = ()
    desired_occupations: tuple[str, ...] = ()
    desired_locations: tuple[str, ...] = ()

    # --- 語学欄 -----------------------------------------------------------
    #: 外国語ネイティブ判定は **この欄にのみ** 適用する (7.2)。職務要約や
    #: 職歴本文へ広げると「ネイティブ広告」「クラウドネイティブ」が引っかかり、
    #: 日本人を誤って除外する。適用範囲を絞ったことで複合語の除外リスト自体が
    #: 不要になった。ただし絞ると「この欄が実際に取れているか」が新たな前提に
    #: なるため、抽出実装とログ確認をセットで行うこと。
    language_text: str | None = None

    # --- 属性 -------------------------------------------------------------
    age: int | None = None
    #: 会員ステータスは正規化済みの生文字列。enum にしていない理由は
    #: models.enums の docstring を参照 (実値未観測のため)。
    membership_status: str | None = None

    def current_employment(self) -> Employment | None:
        for employment in self.employments:
            if employment.is_current:
                return employment
        return None

    def known_field_names(self) -> tuple[str, ...]:
        """Names of fields that actually carry a value.

        起動前チェックと偵察レポートが「どの写像が確定済みか」を人間に見せる
        ために使う。値そのものは出さない (13.2)。
        """
        known: list[str] = []
        for name in type(self).model_fields:
            value = getattr(self, name)
            if value is None or value == () or value == "":
                continue
            known.append(name)
        return tuple(known)


class Candidate(BaseModel):
    """A candidate as ingested from the platform."""

    model_config = _STRICT

    #: ``CandidateId`` は Annotated 型で正規化バリデータが載っている。
    #: これにより、どの取り込み経路も正規化を迂回できない (9.3 の1点目)。
    candidate_id: CandidateId
    #: 正規化前の、媒体から実際に返ってきた表記。``id_aliases`` に蓄積して
    #: 「両表記を試す」照合 (9.3 の4点目) に使う。
    raw_id_observed: str
    #: 氏名。**この媒体では埋まらない。**
    #:
    #: 参照実装は氏名がある前提で、ここを必須にしていた。2026-08-22
    #: observe-api 4回目で候補者一覧の応答のキーを全部読んだところ、
    #: **氏名の欄が無かった**::
    #:
    #:     id / code / age / gender_name / short_address / desired_cities
    #:     qualifications[] / member_desired_job_categories[] / ...
    #:
    #: ``code`` は会員番号であって名前ではない。必須のまま残せば、取り込みは
    #: 「何かを入れる」しかなくなり、入るのは ``code`` になる。それは
    #: :mod:`generation.facts` を通って「氏名: 3323741」としてモデルに渡り、
    #: モデルはそれを名前として文面に書く -- 6.4 の業界/職種の取り違えと
    #: 同じ事故である。
    #:
    #: そこで **空を許す** (6.4 の手順3: 確定するまで空のままにする)。空なら
    #: :data:`generation.facts.UNDISCLOSED` として渡るので、モデルは名前に
    #: 言及できない。嘘の発生経路が構造的に塞がる。
    #:
    #: レジュメ側に在るかは未確認である (``api.resume.url_pattern`` が未確定)。
    #: 在ると分かったらそのとき埋める。
    display_name: str | None = None
    resume: ResumeFacts = Field(default_factory=ResumeFacts)
