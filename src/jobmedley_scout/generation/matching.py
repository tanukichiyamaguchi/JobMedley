"""Finding genuine commonalities between a candidate and the person introduced.

8.3 対策3: **照合の緩さは、そのままハルシネーションになる。** 共通点として
渡した以上、モデルはそれを本文で断定する。したがってここで拾い過ぎることは、
「嘘を書け」と指示するのとほぼ同義である。

以下の4つのルールは、それぞれ実際に起きた誤一致に対応している。設定値で
緩められるようにはしてあるが、**既定値を緩める前に、その誤一致がなぜ許容
できるのかを説明できること**。

* 順方向のみの部分一致 -- 双方向にしたら「トヨタ自動車」と
  「トヨタ自動車直系の販売会社」が一致した (別の会社である)。
* 後方一致の最小文字数 -- 「営業」が12名の「法人営業」に一致し、
  12名全員が「同じ職種の先輩」になった。
* 企業名一致から業種の総称を除外 -- 「サービス業」が
  「株式会社サービスプロダクト」に一致した。
* 職種一致から役職語を除外 -- 「課長」が「共通の職種」になった。

**緩い一致は ``confident=False`` にしかならない。** 呼び出し側はこれを
「近い経歴」のような断定しないラベルでのみ描画してよく、「同じ」「同様に」と
書いてはならない (その語は :mod:`generation.validators` が機械的に弾く)。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict

from jobmedley_scout.config.schema import MatchingConfig
from jobmedley_scout.generation.facts import PromptFacts
from jobmedley_scout.models.text_norm import fold_width, strip_all_ws

_STRICT = ConfigDict(extra="forbid", frozen=True)


class CommonalityKind(StrEnum):
    """What the candidate and the introduced person have in common."""

    COMPANY = "company"
    OCCUPATION = "occupation"
    INDUSTRY = "industry"
    SCHOOL = "school"
    #: 希望勤務地であって **出身地ではない**。「同郷」と書いてはならない。
    LOCATION = "location"


class MatchRule(StrEnum):
    """Which rule produced a commonality. 事故調査のために必ず残す。"""

    EXACT = "exact"
    #: 候補者側の値がマスタ側の値を含む (「トヨタ自動車株式会社」⊃「トヨタ自動車」)。
    FORWARD_SUBSTRING = "forward_substring"
    #: マスタ側の値が候補者側の値で終わる (「法人営業」→「営業」)。修飾語が
    #: 前に付いただけの同種語だが、同じとは限らないので断定しない。
    SUFFIX = "suffix"
    #: マスタ側の値が候補者側の値を含む。**既定では無効** (8.3)。
    REVERSE_SUBSTRING = "reverse_substring"


#: 断定してよい一致規則。ここに規則を足すときは、それが「本人が言われて
#: 事実と違うと感じない」水準かどうかで判断すること。
_CONFIDENT_RULES: Final[frozenset[MatchRule]] = frozenset(
    {MatchRule.EXACT, MatchRule.FORWARD_SUBSTRING}
)

_KIND_NOUNS: Final[dict[CommonalityKind, str]] = {
    CommonalityKind.COMPANY: "勤務先",
    CommonalityKind.OCCUPATION: "職種",
    CommonalityKind.INDUSTRY: "業界",
    CommonalityKind.SCHOOL: "学校",
    CommonalityKind.LOCATION: "エリア",
}

#: 判定順。決定的な順序にしておかないと、同じ入力でプロンプトが変わり
#: 不具合の再現ができなくなる。
_KIND_ORDER: Final[tuple[CommonalityKind, ...]] = (
    CommonalityKind.COMPANY,
    CommonalityKind.SCHOOL,
    CommonalityKind.OCCUPATION,
    CommonalityKind.INDUSTRY,
    CommonalityKind.LOCATION,
)


class SenderProfile(BaseModel):
    """One person from the introduction master.

    :class:`generation.self_exclusion.PersonLike` を満たすので、そのまま
    :func:`generation.self_exclusion.exclude_self` に渡せる。**渡すこと** --
    本人がここに含まれたまま共通点を計算すると、自分自身との完全一致という
    最強の共通点が出て必ず選ばれる (8.4)。
    """

    model_config = _STRICT

    person_id: str
    display_name: str
    companies: tuple[str, ...] = ()
    occupations: tuple[str, ...] = ()
    industries: tuple[str, ...] = ()
    schools: tuple[str, ...] = ()
    locations: tuple[str, ...] = ()


class Commonality(BaseModel):
    """One shared attribute, with the evidence that produced it."""

    model_config = _STRICT

    kind: CommonalityKind
    #: 候補者側の元の表記 (正規化前)。本文に出すならこちらを使う。
    candidate_value: str
    #: マスタ側の元の表記 (正規化前)。
    sender_value: str
    #: 実際に一致した正規化済みの文字列。除外リストの判定はこれに掛かる。
    matched_on: str
    #: 断定してよいか。False の一致を「同じ」と書かせないこと (8.3 対策3)。
    confident: bool
    rule: MatchRule

    def describe(self) -> str:
        """The only sanctioned rendering of this commonality.

        ``confident=False`` を断定形で描くと 8.3 の再発になる。文言を変える
        必要が出たら、断定しない側の語 (「近い」) を保ったまま変えること --
        断定語は :mod:`generation.validators` が最終本文で弾く。
        """
        noun = _KIND_NOUNS[self.kind]
        if self.confident:
            return f"同じ{noun}（{self.candidate_value}）"
        return f"近い{noun}（{self.candidate_value} / {self.sender_value}）"


@dataclass(frozen=True)
class _Evidence:
    matched_on: str
    rule: MatchRule


def _normalize_for_match(value: str) -> str:
    """Canonical form for comparing organisation / occupation strings.

    :mod:`models.text_norm` の部品だけで組む。ここに独自の畳み込み
    (「株式会社」の除去など) を足したくなったら、それは **観測してから**
    にすること -- 推測で畳むと別法人が同一視される (9.3 と同じ失敗の形)。
    """
    return strip_all_ws(fold_width(value)).casefold()


def _find_evidence(
    candidate_value: str, sender_value: str, cfg: MatchingConfig
) -> _Evidence | None:
    """Compare one candidate value against one master value.

    判定順そのものが仕様である。特に **長さゲートは双方向フラグより強い** --
    「営業」が短すぎて弾かれた後に、双方向一致で拾い直されてはならない。
    """
    candidate_key = _normalize_for_match(candidate_value)
    sender_key = _normalize_for_match(sender_value)
    if not candidate_key or not sender_key:
        return None

    if candidate_key == sender_key:
        return _Evidence(matched_on=sender_key, rule=MatchRule.EXACT)

    # 順方向: 候補者側の表記がマスタ側を丸ごと含む。
    # 「トヨタ自動車株式会社」⊃「トヨタ自動車」は同じ会社と見てよい。
    if sender_key in candidate_key:
        return _Evidence(matched_on=sender_key, rule=MatchRule.FORWARD_SUBSTRING)

    # 後方一致: 「法人営業」は「営業」で終わる。修飾語が前に付いた同種の語で
    # あることが多いが、同じ職種とは限らないので断定しない。
    # 短い語は無条件で捨てる -- 「営業」(2文字) が12名の「法人営業」に一致し、
    # 全員が「同じ職種」として紹介された (8.3)。
    if sender_key.endswith(candidate_key):
        if len(candidate_key) < cfg.min_token_length_for_suffix_match:
            return None
        return _Evidence(matched_on=candidate_key, rule=MatchRule.SUFFIX)

    # 逆方向: マスタ側が候補者側を含む。**既定では無効**。有効にすると
    # 「トヨタ自動車」(候補者) と「トヨタ自動車直系の販売会社」(マスタ) が
    # 一致する -- 別の会社である。有効時も断定はさせない。
    if cfg.bidirectional_substring and candidate_key in sender_key:
        if len(candidate_key) < cfg.min_token_length_for_suffix_match:
            return None
        return _Evidence(matched_on=candidate_key, rule=MatchRule.REVERSE_SUBSTRING)

    return None


def _excluded_evidence(kind: CommonalityKind, matched_on: str, cfg: MatchingConfig) -> bool:
    """Whether ``matched_on`` is too generic to count as evidence.

    マスタには企業名欄に業種の総称が、職種欄に役職が混ざっている。それらを
    根拠にすると、無関係な相手が「同じ勤務先」「同じ職種」になる。
    """
    if kind is CommonalityKind.COMPANY:
        # 「サービス業」が「株式会社サービスプロダクト」に一致した (8.3)。
        return any(matched_on == _normalize_for_match(term) for term in cfg.industry_generic_terms)
    if kind is CommonalityKind.OCCUPATION:
        # 「課長」が「共通の職種」になった (8.3)。役職は職種ではない。
        return any(matched_on == _normalize_for_match(term) for term in cfg.job_title_stopwords)
    return False


def _candidate_values(facts: PromptFacts, kind: CommonalityKind) -> tuple[str, ...]:
    """Candidate-side values compared for ``kind``."""
    if kind is CommonalityKind.COMPANY:
        return facts.current_company.values + facts.previous_employers.values
    if kind is CommonalityKind.OCCUPATION:
        return (
            facts.current_occupation.values
            + facts.previous_occupations.values
            + facts.experienced_occupations.values
        )
    if kind is CommonalityKind.INDUSTRY:
        return facts.experienced_industries.values
    if kind is CommonalityKind.SCHOOL:
        return facts.school.values
    # 希望勤務地。**経験ではなく希望** なので、ここから「同郷」を導かないこと。
    return facts.desired_locations.values


def _sender_values(profile: SenderProfile, kind: CommonalityKind) -> tuple[str, ...]:
    """Master-side values compared for ``kind``."""
    if kind is CommonalityKind.COMPANY:
        return profile.companies
    if kind is CommonalityKind.OCCUPATION:
        return profile.occupations
    if kind is CommonalityKind.INDUSTRY:
        return profile.industries
    if kind is CommonalityKind.SCHOOL:
        return profile.schools
    return profile.locations


def find_commonalities(
    facts: PromptFacts, sender_profile: SenderProfile, cfg: MatchingConfig
) -> tuple[Commonality, ...]:
    """Every defensible commonality between the candidate and one master entry.

    希望条件と経験を突き合わせないこと。参照実装は「経験業界」と「希望業界」の
    キーを取り違えて「ご希望の◯◯業界」と書き、運用者から嘘が多いと指摘された
    (models.candidate の docstring 参照)。ここでは同種の項目同士しか比較しない。
    """
    found: list[Commonality] = []
    seen: set[tuple[CommonalityKind, str]] = set()

    for kind in _KIND_ORDER:
        for candidate_value in _candidate_values(facts, kind):
            for sender_value in _sender_values(sender_profile, kind):
                evidence = _find_evidence(candidate_value, sender_value, cfg)
                if evidence is None:
                    continue
                if _excluded_evidence(kind, evidence.matched_on, cfg):
                    continue
                key = (kind, evidence.matched_on)
                if key in seen:
                    continue
                seen.add(key)
                found.append(
                    Commonality(
                        kind=kind,
                        candidate_value=candidate_value,
                        sender_value=sender_value,
                        matched_on=evidence.matched_on,
                        confident=evidence.rule in _CONFIDENT_RULES,
                        rule=evidence.rule,
                    )
                )

    # 断定できる共通点を先に置く。プロンプトが先頭から採るような実装になっても
    # 嘘にならない順序にしておく。sort は安定なので、同じ確度の中では
    # _KIND_ORDER の順序が保たれる。
    found.sort(key=lambda item: not item.confident)
    return tuple(found)


def has_confident_commonality(commonalities: tuple[Commonality, ...]) -> bool:
    """Whether any commonality may be stated as a fact.

    :func:`generation.validators.validate` の ``had_commonality`` にはこれを
    渡すこと。緩い一致しか無いのに ``True`` を渡すと、断定語の検査が
    素通りする -- 8.3 の事故が検出されないまま送信される。
    """
    return any(item.confident for item in commonalities)
