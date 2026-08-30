"""運用者のプロンプトが定めた **出力の決まり** を、検査で固定する。純粋。

2026-08-23 に運用者からスカウト文のプロンプトを受け取った。そこには
「絶対禁止事項」と「出力前の自己チェック」が明記されている。

**モデルの自己チェックを信用しない。** プロンプトに「確認し、違反があれば
書き直してから出力する」と書いてあっても、書いてあることは守られることでは
ない。守らせるのは検査である (8.5 と同じ考え方)。

**このモジュールは :mod:`generation.validators` を置き換えない。** あちらは
16章の仕様が定めた共通の検査 (断定語・絵文字・URL・件名) で、こちらは
**この1つのプロンプトが定めた固有の決まり** である。分けてあるのは、
プロンプトを差し替えたときに何を捨てるべきかが分かるようにするためである。

**既存の8.1の分割とは形が違う。** 16章の設計では LLM が書くのは中核5要素と
件名だけで、宛名・署名・定型文はコードが付ける。運用者のプロンプトは
**本文を丸ごと1本で書かせる** -- 宛名も署名も応募ボタンの案内もモデルが書く。
どちらが正しいという話ではなく、運用者が選んだのは後者である。だから
:mod:`generation.assemble` はこの経路では使わず、代わりにここが「必要な部品が
本当に入っているか」を確かめる。
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict

_STRICT = ConfigDict(extra="forbid", frozen=True)

#: 1行目に必ず出す表示。**プロンプトの STEP3 (1)。**
HEADLINE: Final[str] = "【このスカウトメールはテンプレートではありません】"

#: 応募の案内。**墨付き括弧でなければならない** (STEP3 (9) と STEP4)。
#:
#: カギ括弧「」で書かれると、媒体の画面上のボタン名と一致しなくなる。
APPLY_BUTTON: Final[str] = "【このスカウトに応募する】"

#: 正社員のみの募集なので触れてはいけない語 (絶対禁止事項)。
#:
#: **「時給」を含める。** 待遇の話でなくても、時給という語が出た時点で
#: 「パートの募集かもしれない」と読まれる。
FORBIDDEN_TERMS: Final[tuple[str, ...]] = ("パート", "アルバイト", "時給")

#: 媒体への登録・利用に礼を述べる表現。**当院は媒体の運営者ではない。**
#:
#: 実測40回目、1通目の下見にこう書かれていた::
#:
#:     ご登録いただき、ご自身のキャリアと向き合っていらっしゃること自体に
#:     敬意を持ってご連絡しています。
#:
#: 求職者が登録したのはジョブメドレーであって当院ではない。当院は他の求人企業と
#: 同じ一利用者であり、**礼を述べる立場が無い**。丁寧に見えるが事実として誤りで、
#: 読み手には「この医院はこの媒体を自分のものだと思っている」と映る。
#:
#: **礼の構文だけを拾う。** 「ご登録の際に」のような中立な言及まで落とさない。
PLATFORM_THANKS: Final[tuple[str, ...]] = (
    "ご登録いただ",
    "ご登録くださ",
    "ご登録ありがと",
    "登録してくださ",
    "ご利用いただ",
    "ご利用ありがと",
)

#: こちらから見えている情報の範囲への言及。**否定形だけを拾う。**
#:
#: 「詳しいご経歴までは拝見できておりません」は読み手に何の意味も持たない。
#: こちらの都合であって相手の関心事ではない。
#:
#: **「プロフィールを拝見しました」は落とさない。** 肯定形は正常な文である。
#: **「一緒に確認させていただきます」も落とさない。** これはプロンプトが要求する
#: 表現で、通勤の相談を持ちかけるものである (STEP1)。
_SYSTEM_META: Final[re.Pattern[str]] = re.compile(
    r"(拝見|把握)(でき|して)(て)?(おり|い)?(ませ|ず|ない)"
)

#: 記載が無いのに相手の内心・姿勢・日々の行動を述べる言い回し。
#:
#: **一つ一つが実測40回目の本文から取ってある。** 一般化した正規表現にすると
#: 正常な文まで落ちて書き直しが空回りするので、実際に出た形だけを持つ。
#:
#: 「〜かと思います」は落とさない -- 通勤時間の断定を避ける表現で、プロンプトが
#: 要求している (STEP1-4)。
_UNGROUNDED: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"キャリア(と|に)向き合"),
    re.compile(r"想像(し|いたし)(て|ます|ており)"),
    re.compile(r"日々[^。]{0,24}(積み重ね|重ねられ|努力|研鑽)"),
    re.compile(r"(され|されて|してこられ)ている(方|お方)だからこそ"),
    re.compile(r"慎重に(なられ|なって)"),
    re.compile(r"のではないでしょうか"),
)

#: 職種一般への賛辞。**「歯科衛生士は〜な仕事です」という定義文だけを拾う。**
#:
#: 誰にでも当てはまる文は、その人宛ではない。プロンプトの「ガヤ (一般論の水増し)」
#: がまさにこれで、禁止と書いてあったのに実際に出た。
_GENERIC_PRAISE: Final[re.Pattern[str]] = re.compile(
    r"歯科衛生士(は|とは)[^。]{0,60}(仕事|お仕事)です"
)

#: 箇条書きの記号。行頭に現れたら違反 (絶対禁止事項)。
#:
#: **行頭に限る。** 「・」は文中の並列にも使われるので、どこでも禁止にすると
#: 正常な文が落ちる。プロンプトが禁じているのは箇条書きという **書式** である。
BULLET_PREFIXES: Final[tuple[str, ...]] = ("・", "-", "*", "＊", "●", "◎", "☑", "✅", "✔")

#: 行頭の番号付き箇条書き。``1.`` ``1)`` ``１．`` を拾う。
#:
#: **半角と全角で条件を変えてある。**
#:
#: 半角の ``.`` は後ろに空白を要求する -- 要求しないと「1.5倍」が箇条書きに
#: 見える。全角の ``．`` は空白を要求しない -- 日本語の番号付き箇条書きは
#: 「１．当院の」のように詰めて書くのが普通で、空白を要求すると素通りする
#: (自分の試験がそれで落ちた)。かわりに全角側は **直後が数字でないこと** を
#: 求めて、全角の小数「１．５倍」を巻き込まないようにしてある。
NUMBERED_BULLET: Final[re.Pattern[str]] = re.compile(
    r"^\s*[0-9０-９]+\s*(?:[.)]\s+|[．）](?![0-9０-９]))"
)

#: 太字の記法。プロンプトは「太字を使わない」と明記している。
BOLD_MARKERS: Final[tuple[str, ...]] = ("**", "__", "<b>", "<strong>")

#: 本文の長さ。プロンプトは「600字から700字程度」と言う。
#:
#: **「程度」を数字にするのはこちらの仕事である。** 上下に幅を取ってあるのは、
#: 1文字はみ出しただけで送れなくなるのが目的ではないからで、それでも
#: 大きく外れたもの (200字の手抜き、1500字の長文) は止める。
MIN_CHARS: Final[int] = 500
MAX_CHARS: Final[int] = 850


class BodyViolationKind(StrEnum):
    """このプロンプトが定めた決まりの、破られ方。"""

    MISSING_HEADLINE = "missing_headline"
    MISSING_APPLY_BUTTON = "missing_apply_button"
    #: 墨付き括弧ではない書き方で応募を案内した。
    APPLY_BUTTON_WRONG_BRACKETS = "apply_button_wrong_brackets"
    FORBIDDEN_TERM = "forbidden_term"
    BULLET_LIST = "bullet_list"
    BOLD = "bold"
    ASTERISK = "asterisk"
    TOO_SHORT = "too_short"
    TOO_LONG = "too_long"
    #: 会員番号での呼びかけが無い (STEP3 (2))。
    MISSING_SALUTATION = "missing_salutation"
    #: 差し込みの目印が本文に残っている。**そのまま候補者へ届く。**
    UNFILLED_SLOT = "unfilled_slot"
    #: 医院の住所 (郵便番号・番地) が本文に出た。運用者が「必要ない」とした欄。
    STREET_ADDRESS_LEAKED = "street_address_leaked"
    #: 媒体への登録・利用に礼を述べた。**当院は媒体の運営者ではない** (実測40回目)。
    PLATFORM_THANKS = "platform_thanks"
    #: こちらから見えている情報の範囲に触れた。読み手には関係が無く、蛇足である。
    SYSTEM_META = "system_meta"
    #: 記載が無いのに相手の内心・姿勢・日々の行動を述べた。**創作である。**
    UNGROUNDED_CLAIM = "ungrounded_claim"
    #: 職種一般への賛辞で字数を埋めた。誰にでも当てはまる文はその人宛ではない。
    GENERIC_PRAISE = "generic_praise"
    #: 本文にURLが出た。**このプロンプトはURLを求めていない。**
    URL_PRESENT = "url_present"


class BodyViolation(BaseModel):
    """One rule break, with enough context for a human to judge it."""

    model_config = _STRICT

    kind: BodyViolationKind
    #: 運用者向けの説明 (日本語)。
    detail: str
    #: 実際に引っかかった文字列。**本文そのものは入れない** (13.2)。
    evidence: str


#: 応募案内を **間違った括弧で** 書いたときの形。
_WRONG_BRACKET_APPLY: Final[re.Pattern[str]] = re.compile(
    r"[「『\[［(（]このスカウトに応募する[」』\]］)）]"
)

#: 差し込みの目印。``{{...}}`` が残っていれば埋め忘れである。
_UNFILLED: Final[re.Pattern[str]] = re.compile(r"\{\{[^{}]+\}\}")

#: URL。``http(s)://`` と、裸のドメインらしき並びの両方を拾う。
_URL: Final[re.Pattern[str]] = re.compile(
    r"https?://\S+|\bwww\.\S+|\b[\w-]+\.(?:com|net|jp|co\.jp|org)\b", re.IGNORECASE
)


#: 全角と半角を揃えるための対応表。住所は媒体でもモデルでも表記が揺れる。
_WIDTH_MAP: Final[dict[int, str]] = {
    **{ord("０") + i: str(i) for i in range(10)},
    ord("－"): "-",
    ord("−"): "-",
    ord("‐"): "-",
    ord("―"): "-",
    ord("ー"): "-",
    ord("〜"): "-",
}

#: 郵便番号。``〒`` の有無とハイフンの有無を吸収する。
_POSTAL: Final[re.Pattern[str]] = re.compile(r"(\d{3})-?(\d{4})")

#: 番地。``4丁目3-32`` / ``3-32`` / ``4番地3`` のいずれの書き方も拾う。
_STREET: Final[re.Pattern[str]] = re.compile(r"\d+丁目[\d-]*|\d+番地?[\d-]*|\d+-\d+-\d+")


def _fold(text: str) -> str:
    """Fold width and dash variants. **空白は残す。** Pure."""
    return text.translate(_WIDTH_MAP)


def _squeeze(text: str) -> str:
    """Drop every space, so a body split by spaces still matches. Pure."""
    return _fold(text).replace(" ", "").replace("\u3000", "")


def address_markers(address: str) -> tuple[str, ...]:
    r"""The parts of the clinic address that must **not** appear in the body.

    **市区町村は含めない。** 通勤の話 (STEP1) では「多摩区からですと」のように
    自然に出るし、運用者が要らないと言ったのは番地まで書き込むことである。
    ここが拾うのは郵便番号と番地だけで、そこが出たら書きすぎである。

    **住所からの抽出では空白を残す。** 自分の試験がここで落ちた --
    先に空白を潰すと ``菅4丁目3-32 2階`` が ``菅4丁目3-322階`` になり、
    ``[\d-]*`` が階数の ``2`` まで飲んで ``4丁目3-322`` という印になる。
    実在しない印なので **本文の ``4丁目3-32`` と一致せず、漏れが素通りする**。
    壊れる向きが「見逃す側」なので、静かに効かなくなる種類の間違いだった。
    空白を潰すのは **本文と印を突き合わせる直前** だけでよい。
    """
    folded = _fold(address)
    markers: list[str] = []
    if postal := _POSTAL.search(folded):
        markers.append(f"{postal.group(1)}-{postal.group(2)}")
        markers.append(f"{postal.group(1)}{postal.group(2)}")
    # **郵便番号を先に取り除く。** 取り除かないと ``214-0001`` が番地に見える。
    without_postal = _POSTAL.sub("", folded)
    markers.extend(match.group(0).replace(" ", "") for match in _STREET.finditer(without_postal))
    return tuple(dict.fromkeys(marker for marker in markers if marker))


def validate_body(
    body: str, *, member_code: str, clinic_address: str = ""
) -> tuple[BodyViolation, ...]:
    """Check one generated scout body. Empty result means it may be sent.

    **``member_code`` を渡させるのは、宛名の検査に要るからである。**
    このプロンプトは会員番号で呼びかけると決めている (STEP3 (2))。氏名が
    取れない媒体で、宛名を空にも「様」だけにもしないための唯一の手掛かりが
    会員番号である。

    **``clinic_address`` は「渡したものが出ていないか」を見るために要る。**
    運用者は住所を「本文には必要ない」と言ったが、プロンプトの STEP1 は通勤時間
    の計算に所在地を要求する。だから住所は **渡す**。渡す以上、書かれない保証は
    プロンプトの文言ではなく検査が持つ (8.5 と同じ考え方)。空文字なら検査しない。
    """
    violations: list[BodyViolation] = []
    violations.extend(_check_required_parts(body, member_code))
    violations.extend(_check_address(body, clinic_address))
    violations.extend(_check_forbidden(body))
    violations.extend(_check_grounding(body))
    violations.extend(_check_urls(body))
    violations.extend(_check_formatting(body))
    violations.extend(_check_length(body))
    return tuple(violations)


def _check_required_parts(body: str, member_code: str) -> list[BodyViolation]:
    out: list[BodyViolation] = []
    if HEADLINE not in body:
        out.append(
            BodyViolation(
                kind=BodyViolationKind.MISSING_HEADLINE,
                detail=f"1行目の表示がありません。プロンプトは {HEADLINE} を求めています。",
                # **1行目を引用しない。** 表示が無いときの1行目は、たいてい
                # 会員番号の宛名である。証拠として引くと、それが報告へ乗る (13.2)。
                evidence="(1行目が表示ではありません)" if body.strip() else "(空)",
            )
        )
    if APPLY_BUTTON not in body:
        # **墨付き括弧かどうかを先に見る。** 「無い」と「書き方が違う」は別の
        # 直し方になるので、まとめて1つの違反にしない。
        if wrong := _WRONG_BRACKET_APPLY.search(body):
            out.append(
                BodyViolation(
                    kind=BodyViolationKind.APPLY_BUTTON_WRONG_BRACKETS,
                    detail=(
                        f"応募の案内が墨付き括弧ではありません。"
                        f"{APPLY_BUTTON} と書いてください (媒体の画面の表記と揃えるため)。"
                    ),
                    evidence=wrong.group(0),
                )
            )
        else:
            out.append(
                BodyViolation(
                    kind=BodyViolationKind.MISSING_APPLY_BUTTON,
                    detail=f"応募の案内がありません。{APPLY_BUTTON} を含めてください。",
                    evidence="(見つかりません)",
                )
            )
    if member_code and member_code not in body:
        out.append(
            BodyViolation(
                kind=BodyViolationKind.MISSING_SALUTATION,
                detail=(
                    "会員番号での呼びかけがありません。この媒体では氏名が取れないので、"
                    "宛名は会員番号で書くと決めてあります (プロンプト STEP3 (2))。"
                ),
                evidence="(会員番号が本文にありません)",
            )
        )
    if left := _UNFILLED.search(body):
        out.append(
            BodyViolation(
                kind=BodyViolationKind.UNFILLED_SLOT,
                detail=(
                    "差し込みの目印が本文に残っています。"
                    "**このまま送ると、記法がそのまま候補者へ届きます。**"
                ),
                evidence=left.group(0),
            )
        )
    return out


def _check_address(body: str, clinic_address: str) -> list[BodyViolation]:
    """**渡した住所が本文に出ていないこと。**

    運用者の指示は「住所・アクセスは必要ないです」だった。渡さなければ守られる
    が、渡さないと STEP1 の通勤時間が出せない。渡したうえで守らせる。
    """
    if not clinic_address:
        return []
    folded_body = _squeeze(body)
    for marker in address_markers(clinic_address):
        if marker in folded_body:
            return [
                BodyViolation(
                    kind=BodyViolationKind.STREET_ADDRESS_LEAKED,
                    detail=(
                        "医院の住所 (郵便番号または番地) が本文に出ています。"
                        "運用者は本文に住所は必要ないと決めています。"
                        "所在地は通勤時間の見立て (STEP1) にだけ使ってください。"
                    ),
                    evidence=marker,
                )
            ]
    return []


def _check_forbidden(body: str) -> list[BodyViolation]:
    return [
        BodyViolation(
            kind=BodyViolationKind.FORBIDDEN_TERM,
            detail=(
                f"「{term}」に触れています。この求人は正社員のみの募集なので、"
                f"プロンプトが禁じています。"
            ),
            evidence=term,
        )
        for term in FORBIDDEN_TERMS
        if term in body
    ]


def _check_grounding(body: str) -> list[BodyViolation]:
    """Claims the sender has no standing or no facts to make. **実測40回目。**

    1通目の下見にこの段落が出た::

        ご登録いただき、ご自身のキャリアと向き合っていらっしゃること自体に
        敬意を持ってご連絡しています。歯科衛生士は、患者様の小さな変化に気づき、
        長い時間をかけて信頼を積み上げる仕事です。日々そうした積み重ねを
        されている方だからこそ、次の環境選びも慎重になられているのではと
        想像しています。詳しいご経歴までは拝見できておりませんが、……

    4種類の誤りが1段落に同居していた。礼を述べる立場の誤り、相手の内心の創作、
    職種一般の賛辞、こちらの都合の開示である。

    **原因は禁止の不足ではなかった。** プロンプトの絶対禁止事項には既に
    「プロフィールに記載のない事柄の推測や創作」と「ガヤ (一般論の水増し)」が
    書いてあった。それでも出たのは、同じプロンプトの実行手順が
    「具体的に想像し」「共感を示す」と **書けと命じていた** からである。
    レジュメが読めず材料がゼロの状態で書けと言われれば、創作しか残らない。

    だから直したのは手順のほうで (材料が無ければ段落ごと省く)、ここはその裏取りである。
    **検査だけでは直らないし、プロンプトだけでは確かめられない。**
    """
    out: list[BodyViolation] = []
    for phrase in PLATFORM_THANKS:
        if phrase in body:
            out.append(
                BodyViolation(
                    kind=BodyViolationKind.PLATFORM_THANKS,
                    detail=(
                        "媒体への登録・利用に礼を述べています。"
                        "**当院はこの媒体の運営者ではありません。** 求職者が登録したのは"
                        "媒体であって当院ではなく、礼を述べる立場がありません。"
                    ),
                    evidence=phrase,
                )
            )
    if match := _SYSTEM_META.search(body):
        out.append(
            BodyViolation(
                kind=BodyViolationKind.SYSTEM_META,
                detail=(
                    "こちらから見えている情報の範囲に触れています。"
                    "読み手には関係が無く、蛇足です。書かなければ済みます。"
                ),
                evidence=match.group(0),
            )
        )
    for pattern in _UNGROUNDED:
        if match := pattern.search(body):
            out.append(
                BodyViolation(
                    kind=BodyViolationKind.UNGROUNDED_CLAIM,
                    detail=(
                        "プロフィールに記載が無いのに、相手の内心・姿勢・日々の行動を"
                        "述べています。**分からないことは書けません。**"
                        "材料が無いなら、その段落ごと省いてください。"
                    ),
                    evidence=match.group(0),
                )
            )
    if match := _GENERIC_PRAISE.search(body):
        out.append(
            BodyViolation(
                kind=BodyViolationKind.GENERIC_PRAISE,
                detail=(
                    "職種一般への賛辞で字数を埋めています。"
                    "誰にでも当てはまる文は、その人宛ではありません。"
                ),
                evidence=match.group(0),
            )
        )
    return out


def _check_formatting(body: str) -> list[BodyViolation]:
    out: list[BodyViolation] = []
    for marker in BOLD_MARKERS:
        if marker in body:
            out.append(
                BodyViolation(
                    kind=BodyViolationKind.BOLD,
                    detail="太字の記法が入っています。プロンプトが禁じています。",
                    evidence=marker,
                )
            )
    if "*" in body or "＊" in body:
        out.append(
            BodyViolation(
                kind=BodyViolationKind.ASTERISK,
                detail="アスタリスクが入っています。プロンプトが禁じています。",
                evidence="*" if "*" in body else "＊",
            )
        )
    for line in body.splitlines():
        stripped = line.lstrip()
        if not stripped:
            continue
        # **見出しの表示は箇条書きではない。** 除外しないと1行目で必ず落ちる。
        if stripped.startswith(HEADLINE):
            continue
        if any(stripped.startswith(prefix) for prefix in BULLET_PREFIXES) or NUMBERED_BULLET.match(
            line
        ):
            out.append(
                BodyViolation(
                    kind=BodyViolationKind.BULLET_LIST,
                    detail="箇条書きになっています。プロンプトが禁じています。",
                    # **行を引用しない。** 箇条書きの行には居住地でも会員番号でも
                    # 何でも入りうる。違反の判定に要るのは記号だけである (13.2)。
                    evidence=_bullet_marker(stripped, line),
                )
            )
            break
    return out


def _bullet_marker(stripped: str, line: str) -> str:
    """The bullet symbol that triggered the violation. **行は返さない** (13.2)."""
    for prefix in BULLET_PREFIXES:
        if stripped.startswith(prefix):
            return prefix
    if match := NUMBERED_BULLET.match(line):
        return match.group(0).strip()
    return "(行頭の箇条書き)"


def _check_urls(body: str) -> list[BodyViolation]:
    """**URLは1つも書かせない。**

    :mod:`generation.validators` は許可ドメインの一覧で URL を検査するが、あちらは
    16章の組み立て経路のための検査で、この経路は通らない (module docstring)。
    そして残りの検査 (「！」の上限・絵文字) は **運用者のプロンプトと衝突する** --
    プロンプトは「『！』を適度に使い、親しみやすさを表現する」と明記している。
    だから一括では通せない。

    **URLだけは別である。** このプロンプトは URL を1つも要求していない。にも
    かかわらず本文に出たなら、それはモデルが作ったものである。実在しないURLが
    候補者へ届き、取り消せない (13.6)。応募の導線は媒体の
    ``【このスカウトに応募する】`` ボタンで足りている。
    """
    if found := _URL.search(body):
        return [
            BodyViolation(
                kind=BodyViolationKind.URL_PRESENT,
                detail=(
                    "本文にURLがあります。このプロンプトはURLを求めていないので、"
                    "モデルが作ったものです。応募の導線は"
                    f"{APPLY_BUTTON}で足ります。"
                ),
                evidence=found.group(0)[:40],
            )
        ]
    return []


def _check_length(body: str) -> list[BodyViolation]:
    """Count characters the way a reader would -- **空白と改行は数えない。**

    「600字から700字程度」は読んだときの分量の話である。改行を数えに入れると、
    段落を増やしただけで長さの判定が動く。
    """
    length = len(re.sub(r"\s", "", body))
    if length < MIN_CHARS:
        return [
            BodyViolation(
                kind=BodyViolationKind.TOO_SHORT,
                detail=(
                    f"本文が短すぎます ({length} 字)。プロンプトは600字から700字程度を"
                    f"求めています ({MIN_CHARS} 字未満は止めます)。"
                ),
                evidence=f"{length}字",
            )
        ]
    if length > MAX_CHARS:
        return [
            BodyViolation(
                kind=BodyViolationKind.TOO_LONG,
                detail=(
                    f"本文が長すぎます ({length} 字)。プロンプトは600字から700字程度を"
                    f"求めています ({MAX_CHARS} 字超は止めます)。"
                ),
                evidence=f"{length}字",
            )
        ]
    return []


def render_violations(violations: tuple[BodyViolation, ...]) -> str:
    """A human-readable report. **本文は載せない** (13.2)。"""
    if not violations:
        return "  規則違反はありません。"
    lines = [f"  **{len(violations)} 件の規則違反**:"]
    for violation in violations:
        lines.append(f"    [{violation.kind.value}] {violation.detail}")
        lines.append(f"      引っかかった箇所: {violation.evidence}")
    return "\n".join(lines)


__all__ = [
    "APPLY_BUTTON",
    "BOLD_MARKERS",
    "BULLET_PREFIXES",
    "FORBIDDEN_TERMS",
    "PLATFORM_THANKS",
    "HEADLINE",
    "MAX_CHARS",
    "MIN_CHARS",
    "BodyViolation",
    "BodyViolationKind",
    "render_violations",
    "validate_body",
]
