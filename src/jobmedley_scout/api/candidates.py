"""実測した2つの応答を、モデルへ写す。**純粋。ネットワークもブラウザも触らない。**

写す先は2つある::

    POST /api/customers/members/search/                     一覧
    POST /api/customers/graphql/MemberOnScoutProfileModalOfDesktop   レジュメ

**キーパスは座標から来る。** ここに直書きしない -- 媒体が形を変えたときに
書き換える場所を1つにしておくためで、それが 6.4 の再発防止の形でもある。

**写さないものがある。** 写せるのに写していないのではなく、**意味を観測して
いないから写していない**。理由はその場に書いてある (原則3)。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

from jobmedley_scout.config.placeholders import is_resolved
from jobmedley_scout.config.site_coordinates import SiteCoordinates
from jobmedley_scout.models.candidate import Candidate, Education, ResumeFacts
from jobmedley_scout.recon.masked import unmask, unmask_all

#: 一覧の応答で候補者の並びが入っているキー。実測 (2026-08-22)。
MEMBERS_KEY = "members"
#: 同じ応答に入っている検索の識別子。送信payloadの ``searchUuid`` に載る。
SEARCH_UUID_KEY = "search_uuid"
#: 次のページの手掛かり。
TOTAL_KEY = "total"
NEXT_CURSOR_KEY = "next_cursor"


def value_at(payload: object, path: str) -> object:
    """Follow a dotted key path. ``None`` if any step is missing. **Pure.**

    **配列は辿らない。** ``a.b`` は辞書の連鎖だけを見る。配列に入るのは
    呼び出し側の仕事で、そうしておかないと「1件目だけ見た」のか
    「全部見た」のかが経路の文字列から読めなくなる。
    """
    node: object = payload
    for step in path.split("."):
        if not isinstance(node, Mapping):
            return None
        node = node.get(step)
    return node


def _texts(node: object, *keys: str) -> tuple[str, ...]:
    """Collect one field from a list of objects, dropping withheld entries."""
    if not isinstance(node, Sequence) or isinstance(node, str | bytes):
        return ()
    found: list[str] = []
    for item in node:
        if not isinstance(item, Mapping):
            continue
        current: object = item
        for key in keys:
            if not isinstance(current, Mapping):
                current = None
                break
            current = current.get(key)
        if isinstance(current, str) and current:
            found.append(current)
    return unmask_all(tuple(found))


def _joined_places(node: object) -> tuple[str, ...]:
    """``[{prefecture:{name}, city:{name}}]`` -> ``("神奈川県川崎市多摩区",)``.

    都道府県だけの希望もあるので、市区町村が無ければ都道府県だけを返す。
    **繋いだ文字列を作るのはここだけ** で、モデルには繋いだ形で渡る。
    """
    if not isinstance(node, Sequence) or isinstance(node, str | bytes):
        return ()
    found: list[str] = []
    for item in node:
        if not isinstance(item, Mapping):
            continue
        prefecture = value_at(item, "prefecture.name")
        city = value_at(item, "city.name")
        parts = [str(part) for part in (prefecture, city) if isinstance(part, str) and part]
        if parts:
            found.append("".join(parts))
    return unmask_all(tuple(found))


def rows_in(list_response: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    """The candidate rows in one page of the list. **Pure.**"""
    rows = list_response.get(MEMBERS_KEY)
    if not isinstance(rows, Sequence) or isinstance(rows, str | bytes):
        return ()
    return tuple(item for item in rows if isinstance(item, Mapping))


def search_uuid_in(list_response: Mapping[str, object]) -> str | None:
    """The search identifier the send payload needs. ``None`` if absent.

    **スキーマ上は省略できる** (``searchUuid: String``) が、媒体自身の
    クライアントは常に送っている。取れたら送る。
    """
    found = list_response.get(SEARCH_UUID_KEY)
    return found if isinstance(found, str) and found else None


#: 一覧の行が居住地を持っているキー。**実測したキー名そのもの**
#: (2026-08-22 observe-api 4回目: ``members[].short_address``)。
RESIDENCE_KEY: Final[str] = "short_address"


#: 一覧の行に載っている、文面の材料になる欄。**キー名は実測済み** (observe-api 4回目)。
#:
#: **値の形は観測していない。** 値を出さない方針で観測したので、分かっているのは
#: キーの名前と「文字列らしい」程度である。唯一 ``qualifications[].name`` だけは
#: 座標ファイルに形が書いてある。
#:
#: だから :func:`_row_texts` は **文字列の配列とオブジェクトの配列の両方を受ける**。
#: そして :func:`describe_row_shapes` が「どちらだったか」を報告する。推測で
#: 決め打ちすると、外したときに黙って0件になる (原則2/原則3)。
ROW_AGE_KEY: Final[str] = "age"
ROW_QUALIFICATION_KEY: Final[str] = "qualifications"
ROW_DESIRED_CITIES_KEY: Final[str] = "desired_cities"
ROW_DESIRED_JOBS_KEY: Final[str] = "member_desired_job_categories"
ROW_CAREER_JOBS_KEY: Final[str] = "member_career_job_categories"

#: オブジェクトの配列だったときに、名前が入っていそうな欄。上から順に試す。
ROW_NAME_KEYS: Final[tuple[str, ...]] = ("name", "label", "title", "job_category_name")

#: 経験年数が入っていそうな欄。**無ければ年数を書かない** -- 書けばそれは創作である。
ROW_YEAR_KEYS: Final[tuple[str, ...]] = ("career_year", "careerYear", "years", "year")


def _row_texts(node: object) -> tuple[str, ...]:
    """Names out of a list-row array, accepting **either shape**.

    文字列の配列でも、``{"name": ...}`` の配列でも読む。形を観測していないので
    決め打ちしない (原則3)。読めた分だけ返し、読めなかったことは
    :func:`describe_row_shapes` が別に報告する。
    """
    if not isinstance(node, Sequence) or isinstance(node, str | bytes):
        return ()
    found: list[str] = []
    for item in node:
        if isinstance(item, str) and item.strip():
            found.append(item.strip())
        elif isinstance(item, Mapping):
            for key in ROW_NAME_KEYS:
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    found.append(value.strip())
                    break
    return unmask_all(tuple(found))


def _row_occupation_years(node: object) -> tuple[str, ...]:
    """``職種: N年`` out of the career array. **年数が無ければ職種名も返さない。**

    年数だけを渡す欄 (``CAREER_YEARS``) に職種名だけを入れると、モデルは欄を
    埋めるために年数を自分で作る。レジュメ側で一度直したのと同じ穴である
    (:func:`_occupation_years` の注記)。
    """
    if not isinstance(node, Sequence) or isinstance(node, str | bytes):
        return ()
    found: list[str] = []
    for item in node:
        if not isinstance(item, Mapping):
            continue
        name = next(
            (
                str(item[key]).strip()
                for key in ROW_NAME_KEYS
                if isinstance(item.get(key), str) and str(item[key]).strip()
            ),
            "",
        )
        years = next(
            (item[key] for key in ROW_YEAR_KEYS if isinstance(item.get(key), int | str)),
            None,
        )
        if name and years is not None and str(years).strip():
            found.append(f"{name}: {years}年")
    return unmask_all(tuple(found))


def _row_age(row: Mapping[str, object]) -> int | None:
    value = row.get(ROW_AGE_KEY)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def list_facts_from_row(row: Mapping[str, object]) -> ResumeFacts:
    """Facts the **list row** already carries. **レジュメが読めなくても在る。**

    実測40回目、レジュメが読めずモデルへ渡った人物の事実は会員番号と市名だけ
    だった。だが一覧の行には年齢も資格も希望勤務地も載っていた -- 読んでいな
    かっただけである。座標ファイルには「使える手掛かり」として書いてあった。
    """
    return ResumeFacts(
        age=_row_age(row),
        qualifications=_row_texts(row.get(ROW_QUALIFICATION_KEY)),
        desired_locations=_row_texts(row.get(ROW_DESIRED_CITIES_KEY)),
        desired_occupations=_row_texts(row.get(ROW_DESIRED_JOBS_KEY)),
        experienced_occupations=_row_texts(row.get(ROW_CAREER_JOBS_KEY)),
        experienced_occupation_years=_row_occupation_years(row.get(ROW_CAREER_JOBS_KEY)),
    )


def describe_row_shapes(row: Mapping[str, object]) -> tuple[str, ...]:
    """What shape each material key actually had. **値は1つも出さない** (13.2)。

    形を観測していない欄を読みに行っているので、**外したことが分かる仕掛けが要る**。
    これが無いと、読めなかった欄は黙って「非公開」になり、原因が一覧の形なのか
    候補者が本当に未記入なのかが区別できない (原則2)。
    """
    out: list[str] = []
    for key in (
        ROW_QUALIFICATION_KEY,
        ROW_DESIRED_CITIES_KEY,
        ROW_DESIRED_JOBS_KEY,
        ROW_CAREER_JOBS_KEY,
    ):
        out.append(f"{key}: {_shape_note(row.get(key), key in row)}")
    age_note = "読めました" if _row_age(row) is not None else _missing(row, ROW_AGE_KEY)
    out.append(f"{ROW_AGE_KEY}: {age_note}")
    return tuple(out)


def _missing(row: Mapping[str, object], key: str) -> str:
    """Why a scalar key produced nothing. **3つを分ける。**

    実測41回目、``age: 読めない形です`` と報告した。だが ``null`` は「読めない
    形」ではない -- **この候補者が年齢を公開していない** という観測である。
    同じ言葉にすると、こちらの形の外しと候補者の未記入が区別できず、直せない
    ものを直そうとする (原則2)。

    **値そのものは出さない。** 出すのは型の名前だけである (13.2)。
    """
    if key not in row:
        return "キーがありません"
    value = row.get(key)
    if value is None:
        return "null (この候補者が公開していません)"
    if isinstance(value, str):
        # **文字列でも中身で意味が違う。** 空なら未記入、数字でないなら別の綴り
        # (「20代」等)。同じ言葉にすると、直せないものを直そうとする (実測45回目)。
        if not value.strip():
            return "空の文字列 (この候補者が公開していません)"
        return "数字ではない文字列です (綴りが想定と違います)"
    return f"読めない形です ({type(value).__name__})"


def _shape_note(node: object, present: bool) -> str:
    if not present:
        return "キーがありません"
    if not isinstance(node, Sequence) or isinstance(node, str | bytes):
        return "配列ではありません"
    if not node:
        return "空の配列 (この候補者に記載が無い)"
    read = len(_row_texts(node))
    kinds = sorted({"文字列" if isinstance(i, str) else type(i).__name__ for i in node})
    return f"{len(node)} 件中 {read} 件読めました ({'/'.join(kinds)} の配列)"


def candidate_from_row(row: Mapping[str, object]) -> Candidate | None:
    """One list row -> a candidate. ``None`` if it carries no usable id.

    **氏名は入れない。** この媒体に氏名は無く、``code`` は会員番号である
    (config/site_coordinates.yaml の注記)。入れれば「氏名: 3323741」として
    モデルへ渡る。
    """
    raw = row.get("id")
    if raw is None:
        return None
    observed = str(raw)
    if not observed.strip():
        return None
    # **会員番号は別に持つ。** 画面に出る番号で、宛名に使う (プロンプト STEP3 (2))。
    code = row.get("code")
    # 居住地。プロンプトの STEP1 が通勤時間の見積もりに使う唯一の材料である。
    # **粒度は観測していない** (models.candidate.Candidate.residence の注記)。
    #
    # **伏せ字を通す。** 媒体は非公開の欄に「（未応募のため非表示）」を入れて
    # 返すことがある (recon.masked)。素通しすると、その文字列が居住地として
    # プロンプトへ渡り、モデルはそれを地名として扱う。
    address = row.get(RESIDENCE_KEY)
    residence = unmask(address) if isinstance(address, str) else None
    return Candidate(
        candidate_id=observed,
        raw_id_observed=observed,
        member_code=str(code) if isinstance(code, str | int) and str(code).strip() else None,
        residence=residence.strip() if residence and residence.strip() else None,
        # **一覧の行が持っている材料を捨てない。** レジュメが読めなくても、
        # 年齢・資格・経験職種・希望勤務地はここに載っている (実測40回目)。
        list_facts=list_facts_from_row(row),
    )


def resume_from_response(
    response: Mapping[str, object], *, keypaths: Mapping[str, str | None]
) -> ResumeFacts:
    """The resume response -> facts. **座標のキーパスだけを使う。**

    ``keypaths`` は ``resume.fields.*`` の写像である。``None`` は「この媒体に
    その軸は無い」で、そのフィールドは空のままにする -- 空なら
    :mod:`generation.facts` が「非公開」として渡し、モデルは言及できない。
    """
    return ResumeFacts(
        age=_age(response, keypaths.get("age")),
        experienced_occupations=_labels(response, keypaths.get("experienced_occupations")),
        experienced_occupation_years=_occupation_years(
            response, keypaths.get("experienced_occupations")
        ),
        desired_occupations=_desired_occupations(response, keypaths.get("desired_occupations")),
        desired_locations=_joined_places(
            value_at(response, _root_of(keypaths.get("desired_occupations"), "workplaces"))
        ),
        desired_features=_texts(
            value_at(response, _root_of(keypaths.get("desired_occupations"), "features")), "name"
        ),
        qualifications=_qualifications(response, scheduled=False),
        qualifications_scheduled=_qualifications(response, scheduled=True),
        self_pr=_self_pr(response),
        educations=_educations(response, keypaths.get("educations")),
        # **職歴は写していない。**
        #
        # キーパスは分かっている (``resume.fields.employments``) が、要素の意味を
        # 観測していない。``position`` が役職なのか職種なのか、``jobContent`` との
        # 関係がどうなのかは、実データを1件も見ていない (観測した候補者は職歴が
        # 空だった)。**勤務先名は未応募の相手には返ってこない** ので、
        # Employment.company も埋まらない。
        #
        # 空にしておけば「非公開」として渡り、モデルは職歴に言及できない。
        # 埋めるのは、値を1件でも見てからである (6.4 の手順3)。
        employments=(),
    )


def _root_of(path: str | None, leaf: str) -> str:
    """Sibling key under the same parent as ``path``. ``""`` if unknown.

    ``...desiredCondition.jobCategories`` と ``...desiredCondition.workplaces`` は
    同じ親に生えている。**片方の座標から兄弟を導く** ことで、座標を増やさずに
    済ませている -- 増やせば運用者が埋める欄が増える。
    """
    if not path or "." not in path:
        return ""
    parent, _, _ = path.rpartition(".")
    return f"{parent}.{leaf}"


def _age(response: Mapping[str, object], path: str | None) -> int | None:
    if not path:
        return None
    found = value_at(response, path)
    # **bool は int の部分型である。** 素通しすると True が 1歳になる。
    if isinstance(found, bool) or not isinstance(found, int):
        return None
    return found


def _labels(response: Mapping[str, object], path: str | None) -> tuple[str, ...]:
    """``careerJobCategories[].label`` -- **経験してきた** 職種 (6.4 の経験側)。"""
    if not path:
        return ()
    return _texts(value_at(response, path), "label")


def _occupation_years(response: Mapping[str, object], path: str | None) -> tuple[str, ...]:
    """``careerJobCategories[]`` -> 「歯科衛生士(3年)」。**画面と同じ形にする。**

    ``label`` だけを写して「経験年数」の欄へ渡すと、**モデルは年数を自分で埋める** --
    観測できている事実 (``careerYear``) を捨てて推測させることになる (原則3)。

    **年数が読めない要素は落とす。** 年数なしで並べると、職種名が経験年数の欄に
    現れて年数のように読まれる。落とせば「非公開」として渡り、モデルは言及できない。
    """
    if not path:
        return ()
    rows = value_at(response, path)
    if not isinstance(rows, Sequence) or isinstance(rows, str | bytes):
        return ()
    out: list[str] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        label = row.get("label")
        years = row.get("careerYear")
        if not isinstance(label, str) or not label.strip():
            continue
        if isinstance(years, bool) or not isinstance(years, int | float):
            continue
        out.append(f"{label.strip()}({years:g}年)")
    return tuple(dict.fromkeys(out))


def _desired_occupations(response: Mapping[str, object], path: str | None) -> tuple[str, ...]:
    """``desiredCondition.jobCategories[].jobCategory.name`` -- **希望する** 職種。

    **経験側 (``appeal.careerJobCategories``) と取り違えないこと。** 包みの名前で
    分かれているので、座標を取り違えなければ混ざらない (6.4)。
    """
    if not path:
        return ()
    return _texts(value_at(response, path), "jobCategory", "name")


def _qualifications(response: Mapping[str, object], *, scheduled: bool) -> tuple[str, ...]:
    """Acquired or scheduled qualifications. **混ぜない。**

    ``isScheduled`` が真なら「取得予定」である。保有と同じ列に入れれば
    「◯◯をお持ちの方へ」がまだ持っていない人へ飛ぶ (6.4 と同じ形)。

    座標を置いていないのは、**この2つが1つの配列を旗で分けたものだから** で、
    キーパスを2本用意しても同じ場所を指すことになる。
    """
    node = value_at(response, "data.memberGet.member.appeal.qualifications")
    if not isinstance(node, Sequence) or isinstance(node, str | bytes):
        return ()
    found: list[str] = []
    for item in node:
        if not isinstance(item, Mapping):
            continue
        # **旗が読めないものは、どちらの列にも入れない。**
        #
        # 最初こう書いて、自分の試験に落ちた::
        #
        #     is_scheduled = flag is True     # bool でない値が「保有」になる
        #
        # ``isScheduled`` が ``"yes"`` のような読めない値だと ``False`` に落ち、
        # **取得予定が保有側へ流れ込む**。「◯◯をお持ちの方へ」がまだ持って
        # いない人へ飛ぶ (13.6)。迷ったら送らない側へ倒す。
        flag = item.get("isScheduled")
        if not isinstance(flag, bool):
            continue
        if flag != scheduled:
            continue
        name = item.get("name")
        if isinstance(name, str) and name:
            found.append(name)
    return unmask_all(tuple(found))


def _self_pr(response: Mapping[str, object]) -> str | None:
    """``appeal.selfPr``. **「未入力」は値ではない。**"""
    return unmask(_as_text(value_at(response, "data.memberGet.member.appeal.selfPr")))


def _educations(response: Mapping[str, object], path: str | None) -> tuple[Education, ...]:
    """``latestEducationBackground`` -> one entry. **単数である。**

    「最終」学歴なので1件しか無い。モデル側がタプルなので、ここで1件の列に包む。

    **学校名は入れていない。** ``schoolName`` は ``AppliedMember`` にしか無く、
    スカウトの相手は未応募なので返ってこない。

    ``major`` を学部・学科に充てている。同じ入れ物に ``department`` もあるが、
    **2つの関係を観測していない** ので、片方だけを使う。
    """
    if not path:
        return ()
    node = value_at(response, path)
    if not isinstance(node, Mapping):
        return ()
    raw_level = unmask(_as_text(node.get("schoolType")))
    faculty = unmask(_as_text(node.get("major")))
    if raw_level is None and faculty is None:
        return ()
    return (Education(school=None, raw_level=raw_level, faculty=faculty),)


def _as_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


#: 必ず在る写像 (``null`` を取らない座標)。
REQUIRED_RESUME_FIELDS: tuple[str, ...] = (
    "age",
    "experienced_occupations",
    "desired_occupations",
    "educations",
    "employments",
)


def resume_keypaths(coordinates: SiteCoordinates) -> dict[str, str | None]:
    """``resume.fields.*`` as a plain mapping. ``None`` means "no such axis here".

    **未確定 (``UNRESOLVED``) も ``None`` に落とす。** 呼び出し側から見れば
    「まだ知らない」も「無い」も同じ -- どちらの場合もその欄は空のままにする、
    が正しい振る舞いだからである。**違いは報告に出す** (下の
    :func:`unresolved_resume_fields`)。
    """
    found: dict[str, str | None] = {}
    for name in REQUIRED_RESUME_FIELDS:
        value = coordinates.json_path(f"resume.fields.{name}")
        found[name] = value if isinstance(value, str) else None
    return found


def unresolved_resume_fields(coordinates: SiteCoordinates) -> tuple[str, ...]:
    """Resume axes that are **not yet known** (as opposed to known-absent).

    **黙って空にしない** (原則2)。未確定のまま取り込めば、その項目は永久に
    「非公開」で渡り続け、誰も気付かない。
    """
    return tuple(
        name
        for name in REQUIRED_RESUME_FIELDS
        if not is_resolved(coordinates.json_path(f"resume.fields.{name}"))
    )


__all__ = [
    "describe_row_shapes",
    "list_facts_from_row",
    "MEMBERS_KEY",
    "NEXT_CURSOR_KEY",
    "SEARCH_UUID_KEY",
    "TOTAL_KEY",
    "candidate_from_row",
    "resume_from_response",
    "REQUIRED_RESUME_FIELDS",
    "resume_keypaths",
    "unresolved_resume_fields",
    "rows_in",
    "search_uuid_in",
    "value_at",
]
