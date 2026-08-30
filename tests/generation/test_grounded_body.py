"""**書ける立場と、書ける材料。** 実測40回目で運用者が指摘した4種類を固定する。

1通目の下見に、この段落が出た（会員番号と居住地は伏せてある）::

    ご登録いただき、ご自身のキャリアと向き合っていらっしゃること自体に敬意を
    持ってご連絡しています。歯科衛生士は、患者様の小さな変化に気づき、長い時間を
    かけて信頼を積み上げる仕事です。日々そうした積み重ねをされている方だからこそ、
    次の環境選びも慎重になられているのではと想像しています。詳しいご経歴までは
    拝見できておりませんが、だからこそ一度お話しして、これまで大事にしてこられた
    ことを伺いたいと思いました。

運用者の指摘は4つで、どれも正しい。

1. **「ご登録いただき」は立場が違う。** 求職者が登録したのはジョブメドレーで
   あって当院ではない。当院は他の求人企業と同じ一利用者であり、礼を述べる立場が
   無い。丁寧に見えるが事実として誤りである
2. **「キャリアと向き合っていらっしゃる」は事実か。** 記載は無い。創作である
3. **大げさな表現をやめる。** 職種一般への賛辞は誰にでも当てはまり、その人宛ではない
4. **「詳しいご経歴までは拝見できておりません」は蛇足。** こちらの都合であって
   相手の関心事ではない

**原因は禁止の不足ではなかった。** プロンプトの絶対禁止事項には既に「プロフィール
に記載のない事柄の推測や創作」と「ガヤ (一般論の水増し)」が書いてあった。それでも
出たのは、同じプロンプトの実行手順が「具体的に想像し」「共感を示す」と **書けと
命じていた** からである。レジュメが読めず材料がゼロの状態で書けと言われれば、
創作しか残らない (ラダーの撤退条件が言っていたとおり)。

だから手順を直し (材料が無ければ段落ごと省く)、ここはその裏取りである。
**検査だけでは直らないし、プロンプトだけでは確かめられない。**
"""

from __future__ import annotations

import pytest

from jobmedley_scout.generation.scout_body import BodyViolationKind, validate_body

#: 検査用。**実在の会員番号ではない。**
MEMBER = "00000000"
CLINIC_ADDRESS = "〒214-0001 神奈川県川崎市多摩区菅４丁目３−３２ ２階"


def _kinds(text: str) -> set[str]:
    """The violation kinds this text trips, ignoring whole-message structure."""
    structural = {
        BodyViolationKind.TOO_SHORT.value,
        BodyViolationKind.TOO_LONG.value,
        BodyViolationKind.MISSING_HEADLINE.value,
        BodyViolationKind.MISSING_APPLY_BUTTON.value,
        BodyViolationKind.MISSING_SALUTATION.value,
    }
    found = validate_body(text, member_code=MEMBER, clinic_address=CLINIC_ADDRESS)
    return {v.kind.value for v in found} - structural


# ---------------------------------------------------------------------------
# 1. 立場が違う -- 当院は媒体の運営者ではない
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "ご登録いただき、ありがとうございます。",
        "ご登録くださりありがとうございます。",
        "ジョブメドレーをご利用いただきありがとうございます。",
    ],
)
def test_thanking_for_registering_is_refused(text: str) -> None:
    """**求職者が登録したのは媒体であって当院ではない。**

    丁寧に見えるが事実として誤りで、読み手には「この医院はこの媒体を自分のものだと
    思っている」と映る。
    """
    assert BodyViolationKind.PLATFORM_THANKS.value in _kinds(text)


def test_a_neutral_mention_of_the_profile_is_not_refused() -> None:
    """**礼の構文だけを拾う。** 中立な言及まで落とすと正常な文が書けなくなる。"""
    assert BodyViolationKind.PLATFORM_THANKS.value not in _kinds(
        "プロフィールに歯科衛生士の資格をご記載でしたので、ご連絡しました。"
    )


# ---------------------------------------------------------------------------
# 2. 材料が無いのに内心を書く
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "ご自身のキャリアと向き合っていらっしゃること自体に敬意を持っています。",
        "次の環境選びも慎重になられているのではと想像しています。",
        "日々そうした積み重ねをされている方だからこそ、と感じています。",
        "これまで努力されている方だからこそ、お伝えしたいことがあります。",
        "働き方に迷われているのではないでしょうか。",
    ],
)
def test_an_invented_inner_state_is_refused(text: str) -> None:
    """**分からないことは書けない。** 丁寧な言葉で包んでも事実にはならない。"""
    assert BodyViolationKind.UNGROUNDED_CLAIM.value in _kinds(text)


def test_the_commute_hedge_the_prompt_requires_is_not_refused() -> None:
    """**「かと思います」は落とさない。**

    通勤時間の断定を避ける表現で、プロンプトが STEP1-4 で要求している。
    ここを落とすと、要求と検査が矛盾して書き直しが永久に終わらない。
    """
    text = (
        "お車でおよそ30分から40分前後かと思います。"
        "実際には道の混み具合で前後するかもしれませんが、"
        "もし気になるようでしたら通勤手段も含めて一緒に確認させていただきます。"
    )
    assert _kinds(text) == set()


# ---------------------------------------------------------------------------
# 3. 職種一般への賛辞
# ---------------------------------------------------------------------------


def test_generic_praise_for_the_occupation_is_refused() -> None:
    """誰にでも当てはまる文は、その人宛ではない。"""
    text = "歯科衛生士は、患者様の小さな変化に気づき、長い時間をかけて信頼を積み上げる仕事です。"
    assert BodyViolationKind.GENERIC_PRAISE.value in _kinds(text)


def test_talking_about_this_clinic_s_own_work_is_not_refused() -> None:
    """**医院の話は落とさない。** 落とすのは職種一般の定義文だけである。"""
    assert BodyViolationKind.GENERIC_PRAISE.value not in _kinds(
        "当院ではおひとり45分の時間を確保し、担当制で診療にあたっています。"
    )


# ---------------------------------------------------------------------------
# 4. こちらの都合を書く
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "詳しいご経歴までは拝見できておりませんが、一度お話ししたいと思いました。",
        "ご経歴を拝見できていないのですが、お話を伺えればと思います。",
        "詳細までは把握できておりません。",
    ],
)
def test_talking_about_what_we_cannot_see_is_refused(text: str) -> None:
    """読み手には関係が無い。**書かなければ済む。**"""
    assert BodyViolationKind.SYSTEM_META.value in _kinds(text)


def test_the_member_code_apology_the_prompt_requires_is_not_refused() -> None:
    """**例外はここだけ。** 相手への非礼を詫びるものなので必要である (STEP3 (2))。"""
    text = (
        f"{MEMBER}様（システム上お名前が表示されず、会員番号でのご挨拶となる失礼をお許しください）"
    )
    assert BodyViolationKind.SYSTEM_META.value not in _kinds(text)


def test_reading_the_profile_in_the_positive_is_not_refused() -> None:
    """肯定形は正常な文である。**否定形だけを拾う。**"""
    assert BodyViolationKind.SYSTEM_META.value not in _kinds(
        "プロフィールを拝見しました。ご経歴を拝見して、ぜひご連絡したいと思いました。"
    )


# ---------------------------------------------------------------------------
# 運用者が「問題ない」とした部分を落とさないこと
#
# **ここが一番大事な検査である。** 検査が広すぎると、良い文まで書き直させ、
# 書き直しの上限を使い切って生成が失敗する。運用者は実測40回目で、問題の段落
# 以外は「問題なく」と明言した。その部分が素通りすることを固定する。
# ---------------------------------------------------------------------------

APPROVED_PARAGRAPHS: tuple[str, ...] = (
    "○○市からですと、お車でおよそ1時間前後、もしくはもう少しかかるかと思います。"
    "実際には道の混み具合で前後するかもしれませんが、もし気になるようでしたら"
    "通勤手段も含めて一緒に確認させていただきますので、遠慮なくお知らせください。"
    "少し距離がありますので、そのあたりも含めてご相談いただければと思います。",
    "じっくり患者様と向き合える環境をお探しでしたら、当院ではおひとり45分の時間を確保し、"
    "担当制で診療にあたっています。技術に不安がある時期でも、オリジナルの研修で本人の"
    "ペースに合わせて指導しますので、経験の浅さを気にされている方にも力を発揮して"
    "いただけます。木曜と日曜が休診で残業もほとんどなく、これまでの働き方と両立しやすい"
    "環境です。ライフイベントで働き方が変わることも医院として理解し、スタッフ全員で"
    "応援しています。",
    "まずはお話だけでも、見学だけでも大歓迎です。当日はスタッフ全員でお迎えします。"
    "ご興味をお持ちいただけましたら、【このスカウトに応募する】ボタンを押してください。"
    "お会いできたら嬉しいです。",
    "ヤガサキ歯科医院 院長 矢ケ崎 隆信",
)


@pytest.mark.parametrize("paragraph", APPROVED_PARAGRAPHS)
def test_the_paragraphs_the_operator_approved_are_left_alone(paragraph: str) -> None:
    """**誤検知ゼロ。** 良い文を落とす検査は、生成そのものを止める。"""
    assert _kinds(paragraph) == set(), f"運用者が問題ないとした文を落としています: {paragraph[:30]}"
