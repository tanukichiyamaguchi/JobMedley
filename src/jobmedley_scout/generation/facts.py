"""Facts about one candidate, in the shape the prompt receives them.

8.3 対策1 (最重要). 参照実装がありもしない職歴を書いたのは、モデルが不誠実
だったからではない。**プロンプトに候補者の氏名と共通点しか渡していなかった。**
書くべき事実が渡っていない以上、モデルには創作以外の選択肢がなかった。
したがってここは「文面を良くするための機能」ではなく、嘘の発生経路を塞ぐための
構造である (原則3)。

設計上の要点が2つある。

1. **値が無い項目も必ず渡す。** 項目ごと落とすと、モデルからは「その事実が
   無い」のか「渡し忘れた」のかが区別できず、後者だと解釈した瞬間に補完
   (=創作) が始まる。無い項目は :data:`UNDISCLOSED` (「非公開」) と明示する。
2. **項目の取りこぼしを型で塞ぐ。** :class:`PromptFacts` の全フィールドは
   必須なので、:func:`build_facts` が1つでも埋め忘れれば構築時に落ちる。
   ラベルの網羅は :func:`_verify_label_coverage` が import 時に検査する。

語学欄 (``ResumeFacts.language_text``) は **意図的に渡していない**。7.2 の通り
あの欄は外国語ネイティブ判定にのみ使う欄であり、文面生成に渡すと国籍・母語への
言及を誘発する。ここに足したくなったら、まずその是非を人間に確認すること。
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Final

from pydantic import BaseModel, ConfigDict

from jobmedley_scout.models.candidate import Candidate
from jobmedley_scout.models.text_norm import normalize_ws

_STRICT = ConfigDict(extra="forbid", frozen=True)

#: 値が無い項目に入れる文字列。**空文字や項目の省略にしないこと** --
#: 「無い」と「渡し忘れ」をモデルが区別できなくなる (8.3 対策1)。
UNDISCLOSED: Final[str] = "非公開"

#: 複数値を1行に並べるときの区切り。
VALUE_SEPARATOR: Final[str] = "、"

#: 項目名 -> プロンプトに出す日本語ラベル。
#: :class:`PromptFacts` のフィールドと1対1であることを import 時に検査する。
FACT_LABELS: Final[dict[str, str]] = {
    "display_name": "氏名",
    "age": "年齢",
    "membership_status": "会員ステータス",
    "current_company": "現在の勤務先",
    "current_occupation": "現在の職種",
    "current_tenure_years": "現在の勤続年数",
    "previous_employers": "過去の勤務先",
    "previous_occupations": "過去の職種",
    "experienced_industries": "経験業界",
    "experienced_occupations": "経験職種",
    "specialty": "専門・得意分野",
    "summary": "職務要約",
    "school": "学校名",
    "faculty": "学部・学科",
    "education_level": "学歴区分",
    "desired_industries": "希望業界",
    "desired_occupations": "希望職種",
    "desired_locations": "希望勤務地",
}


class Fact(BaseModel):
    """One labelled fact. ``values`` が空なら「非公開」として描画される。

    ラベルを値と同じ器に入れているのは、ラベル無しで値だけが渡る経路を
    作らないため。プロンプトに出るのは常に ``ラベル: 値`` の形である。
    """

    model_config = _STRICT

    label: str
    #: 正規化済みの値。空タプル = その項目は取得できていない。
    values: tuple[str, ...]

    @property
    def disclosed(self) -> bool:
        """Whether this fact actually carries a value."""
        return bool(self.values)

    @property
    def rendered(self) -> str:
        """The value as it appears in the prompt (``非公開`` when absent)."""
        if not self.values:
            return UNDISCLOSED
        return VALUE_SEPARATOR.join(self.values)

    def as_line(self) -> str:
        """One ``ラベル: 値`` line."""
        return f"{self.label}: {self.rendered}"


class PromptFacts(BaseModel):
    """Everything the prompt is allowed to state about a candidate.

    **全フィールドが必須である。** 既定値を置くと :func:`build_facts` の
    埋め忘れが「常に非公開」として静かに通ってしまい、8.3 の事故
    (事実が渡らない → モデルが創作する) を自分で再現することになる。
    """

    model_config = _STRICT

    display_name: Fact
    age: Fact
    membership_status: Fact
    current_company: Fact
    current_occupation: Fact
    current_tenure_years: Fact
    previous_employers: Fact
    previous_occupations: Fact
    experienced_industries: Fact
    experienced_occupations: Fact
    specialty: Fact
    summary: Fact
    school: Fact
    faculty: Fact
    education_level: Fact
    desired_industries: Fact
    desired_occupations: Fact
    desired_locations: Fact

    def iter_facts(self) -> Iterator[Fact]:
        """Every fact, in declaration order.

        ``model_fields`` を回すことで、フィールドを足したのに描画側に足し忘れる
        という取りこぼしが構造的に起きない。
        """
        for name in type(self).model_fields:
            value = getattr(self, name)
            if not isinstance(value, Fact):  # pragma: no cover - 型で防いでいる
                raise TypeError(f"PromptFacts.{name} が Fact ではありません: {type(value)!r}")
            yield value

    def render_for_prompt(self) -> str:
        """The facts block handed to the model, one ``ラベル: 値`` line per field."""
        return "\n".join(fact.as_line() for fact in self.iter_facts())

    def disclosed_field_names(self) -> tuple[str, ...]:
        """Names of the fields that actually carry a value.

        値そのものではなく項目名だけを返す。偵察レポートや起動前チェックが
        「どの写像が確定済みか」を人間に見せるために使う (13.2)。
        """
        return tuple(
            name for name in type(self).model_fields if getattr(self, name).disclosed is True
        )

    def undisclosed_field_names(self) -> tuple[str, ...]:
        """Names of the fields that came back empty."""
        disclosed = set(self.disclosed_field_names())
        return tuple(name for name in type(self).model_fields if name not in disclosed)


def _clean(raw: Iterable[str | None]) -> tuple[str, ...]:
    """Normalize whitespace, drop empties, de-duplicate, keep order.

    重複除去は「株式会社A」が職歴に2回出たときに同じ勤務先を2回渡さないため。
    順序を保つのは、プロンプトが決定的でないと不具合の再現ができないため。
    """
    seen: dict[str, None] = {}
    for value in raw:
        if value is None:
            continue
        cleaned = normalize_ws(value)
        if not cleaned:
            continue
        seen.setdefault(cleaned, None)
    return tuple(seen)


def _fact(field: str, raw: Iterable[str | None]) -> Fact:
    """Build one fact, taking its label from :data:`FACT_LABELS`."""
    label = FACT_LABELS.get(field)
    if label is None:  # pragma: no cover - import 時の検査で先に落ちる
        raise RuntimeError(f"ラベル未定義の項目 {field!r} を渡そうとしました")
    return Fact(label=label, values=_clean(raw))


def _format_years(years: float | None) -> str | None:
    """``3.0`` -> ``3年``, ``2.5`` -> ``2.5年``, ``None`` -> ``None``."""
    if years is None:
        return None
    return f"{years:g}年"


def build_facts(candidate: Candidate) -> PromptFacts:
    """Collect everything the prompt may state about ``candidate``.

    レジュメのキー写像は 6.4 の手順で確定するまで ``None`` のままなので、
    現時点ではほとんどの項目が「非公開」になる。**それが正しい状態である** --
    渡せる事実が無いなら、モデルにはその事実を書かせない。
    """
    resume = candidate.resume
    current = resume.current_employment()
    # 現職以外を「過去の勤務先」とする。is_current が立っていない職歴しか無い
    # 場合、現職は「非公開」になり、全件が過去側に出る。推測で最新の1件を現職に
    # 昇格させないこと -- そこで作った1件がそのまま「現在◯◯にお勤めの」という
    # 嘘になる (8.3)。
    previous = tuple(item for item in resume.employments if not item.is_current)

    return PromptFacts(
        display_name=_fact("display_name", [candidate.display_name]),
        age=_fact("age", [None if resume.age is None else f"{resume.age}歳"]),
        membership_status=_fact("membership_status", [resume.membership_status]),
        current_company=_fact("current_company", [None if current is None else current.company]),
        current_occupation=_fact(
            "current_occupation", [None if current is None else current.occupation]
        ),
        current_tenure_years=_fact(
            "current_tenure_years",
            [None if current is None else _format_years(current.tenure_years)],
        ),
        previous_employers=_fact("previous_employers", [item.company for item in previous]),
        previous_occupations=_fact("previous_occupations", [item.occupation for item in previous]),
        experienced_industries=_fact("experienced_industries", resume.experienced_industries),
        experienced_occupations=_fact("experienced_occupations", resume.experienced_occupations),
        specialty=_fact("specialty", [resume.specialty]),
        summary=_fact("summary", [resume.summary]),
        school=_fact("school", [item.school for item in resume.educations]),
        faculty=_fact("faculty", [item.faculty for item in resume.educations]),
        education_level=_fact("education_level", [item.raw_level for item in resume.educations]),
        desired_industries=_fact("desired_industries", resume.desired_industries),
        desired_occupations=_fact("desired_occupations", resume.desired_occupations),
        desired_locations=_fact("desired_locations", resume.desired_locations),
    )


def _verify_label_coverage() -> None:
    """Fail at import time if a field has no label (or a label has no field).

    項目を足してラベルを足し忘れると、その項目はプロンプトに出ないまま
    「渡したつもり」になる。8.3 の事故はまさにその形だったので、
    レビュー任せにせず import 時に落とす。
    """
    fields = set(PromptFacts.model_fields)
    labels = set(FACT_LABELS)
    missing = sorted(fields - labels)
    orphaned = sorted(labels - fields)
    if missing or orphaned:
        raise RuntimeError(
            "FACT_LABELS と PromptFacts のフィールドが一致しません。"
            f" ラベル未定義: {missing} / 対応フィールドなし: {orphaned}"
        )


_verify_label_coverage()
