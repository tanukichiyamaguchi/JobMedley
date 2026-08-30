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
    #: こだわり条件 (「社会保険完備」「ネイルOK」「育児支援あり」等)。
    #:
    #: **氏名が取れない媒体では、ここが個別化の主材料になる。** 候補者が自分で
    #: 選んだ条件なので、求人側の特徴と突き合わせれば「その人向け」の理由が書ける。
    desired_features: tuple[str, ...] = ()

    # --- 資格 -------------------------------------------------------------
    #: **取得済みの** 資格。
    #:
    #: 取得予定と分けてあるのは 6.4 と同じ理由である。混ぜて「◯◯をお持ちの方へ」
    #: と書けば、まだ持っていない人にそう言うことになる。**型で混ざらないように
    #: してある。**
    qualifications: tuple[str, ...] = ()
    #: **取得予定の** 資格 (``isScheduled``)。保有と同じ扱いにしないこと。
    qualifications_scheduled: tuple[str, ...] = ()

    # --- 自己PR -----------------------------------------------------------
    #: 候補者自身が書いた自己PR。
    #:
    #: **職務要約 (summary) とは別物である。** 職務要約は職歴の要約、自己PRは
    #: 自己売り込みで、この媒体に職務要約の欄は無い。片方をもう片方の欄へ入れると
    #: モデルには違うラベルで渡る (6.4)。
    #:
    #: 未入力の候補者が多い。媒体は画面に「未入力」と出すが、それが値なのか
    #: UIの文言なのかは確認していない -- どちらであっても
    #: :func:`recon.masked.unmask` を通してから入れること。
    self_pr: str | None = None

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
    #: 画面に出る **会員番号** (一覧の ``members[].code``)。
    #:
    #: ``candidate_id`` とは別物である。``candidate_id`` はAPIが使う内部の番号
    #: (``members[].id``) で、こちらは運用者と候補者が画面で目にする番号である。
    #: 実測では ``id=3323741`` に対して ``code="01613058"`` だった。
    #:
    #: **氏名の代わりではない。** ただし運用者のプロンプトは、氏名が取れない
    #: この媒体で **宛名を会員番号にすると決めている** (STEP3 (2))。断り書きを
    #: 添えたうえで呼びかけるので、番号を名前と偽ることにはならない。
    #: だから :attr:`display_name` は空のままで、こちらを別に持つ。
    member_code: str | None = None
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
    #: **2026-08-22 運用者が確定させた**: 「氏名は取れない。取得できるのは
    #: 会員番号のみである。」レジュメ側にも無い。つまりこの欄は **この媒体では
    #: 永久に空である** -- 「まだ知らない」ではなく、確定した答えとしての
    #: 「無い」である。埋める実装を後から足す必要は無い。
    display_name: str | None = None
    #: 居住地。一覧の ``members[].short_address`` から取る。
    #:
    #: **``ResumeFacts`` ではなく ``Candidate`` に置いてある。** レジュメを
    #: 取り込むとき ``model_copy(update={"resume": ...})`` で ``resume`` が丸ごと
    #: 差し替わるので、あちらに置くと一覧から取った値が黙って消える。
    #:
    #: **粒度は観測していない。** 画面の「居住地」は「神奈川県川崎市多摩区」の
    #: ように都道府県＋市区町村だが、``short_address`` が同じ粒度で返るかは
    #: 確かめていない (値を出さない方針で観測したため、種別が文字列であることしか
    #: 分かっていない)。運用者のプロンプトはここから通勤時間を見積もるので、
    #: **1通目の文面でこの粒度を確認すること** (原則3)。
    #:
    #: 空なら :data:`generation.facts.UNDISCLOSED` として渡り、プロンプトの
    #: STEP1 は「都道府県レベルまでしか分からない場合」の書き方に落ちる。
    #: 空欄を勝手に埋めない。
    residence: str | None = None
    resume: ResumeFacts = Field(default_factory=ResumeFacts)
