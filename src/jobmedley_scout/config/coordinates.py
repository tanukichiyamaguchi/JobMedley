"""The registry of every site-specific coordinate this system needs.

**この一覧が「まだ分かっていないこと」の全量である。** 4章の事前調査と3章の
ラダーは、ここに並んだキーを埋めていく作業だと考えてよい。

各座標は、どのラダー段階で・どうやって確定するかを自分で持っている
(:class:`CoordinateSpec`)。``scout coordinates`` はこの情報をそのまま印字する
ので、未確定の項目に出会った運用者は「次に何をすればよいか」をコードを読まずに
知ることができる。

:data:`REQUIRED_BY_COMMAND` が重要である。コマンドごとに必要な座標を宣言して
あるので、**ラダーの各段でプロジェクトは実際に動く**。全部埋まるまで何もできない、
という状態にはならない。偵察コマンドは段階1/2の座標しか要求しないし、返信同期は
送信系の座標を要求しない。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from jobmedley_scout.config.placeholders import LadderStage


class CoordKind(StrEnum):
    """How a coordinate's raw YAML value is parsed once it is filled in."""

    URL = "url"
    SELECTOR = "selector"
    STATUS_SET = "status_set"
    JSON_PATH = "json_path"
    ENUM_MAP = "enum_map"
    STRING = "string"
    STRING_LIST = "string_list"
    BOOL = "bool"
    INT = "int"


@dataclass(frozen=True)
class CoordinateSpec:
    """One thing we do not know yet, and how to find it out."""

    key: str
    stage: LadderStage
    kind: CoordKind
    how_to_obtain: str
    #: 「確認した結果 存在しなかった」が正当な答えになりうる座標。
    #: 例: この媒体に媒体標準の追客機能が無い、2段階認証が無い。
    #: これらは ``null`` を書けるが、``UNRESOLVED`` のままにはできない。
    nullable: bool = False


_S1 = LadderStage.STAGE_1_LOGIN
_S2 = LadderStage.STAGE_2_PREFLIGHT
_S3 = LadderStage.STAGE_3_RECON
_S4 = LadderStage.STAGE_4_DRYRUN_API
_S6 = LadderStage.STAGE_6_LIVE_SMALL

COORDINATES: tuple[CoordinateSpec, ...] = (
    # ---- 段階1: ログイン ---------------------------------------------------
    CoordinateSpec(
        "auth.login_url",
        _S1,
        CoordKind.URL,
        "https://customers.job-medley.com/customers/sign_in/ を開き、"
        "実際にフォームが描画されるURL (リダイレクト後) をアドレスバーから転記する。",
    ),
    CoordinateSpec(
        "auth.is_spa",
        _S1,
        CoordKind.BOOL,
        "ログイン画面で右クリック→ページのソースを表示。フォーム要素がHTMLに"
        "含まれていれば false (旧来型)、空の div だけなら true (SPA)。"
        "true の場合、送信ボタンのクリックに Enter キーのフォールバックは効かない (5.5)。",
    ),
    CoordinateSpec(
        "auth.email_selector",
        _S1,
        CoordKind.SELECTOR,
        "ログインフォームのメールアドレス入力欄。開発者ツールで要素を検査し、"
        "name/id 属性ベースの安定したセレクタを選ぶ。",
    ),
    CoordinateSpec(
        "auth.password_selector", _S1, CoordKind.SELECTOR, "ログインフォームのパスワード入力欄。"
    ),
    CoordinateSpec(
        "auth.submit_selector",
        _S1,
        CoordKind.SELECTOR,
        "ログインの送信ボタン。SPAの場合は複数のテキスト候補で探す方式にするため、"
        "auth.submit_text_candidates も併せて埋めること (5.5)。",
    ),
    CoordinateSpec(
        "auth.submit_text_candidates",
        _S1,
        CoordKind.STRING_LIST,
        "送信ボタンの表示文字列の候補 (例: ['ログイン', 'サインイン'])。"
        "セレクタが効かなかったときのフォールバック。",
    ),
    CoordinateSpec(
        "auth.success_marker_selector",
        _S1,
        CoordKind.SELECTOR,
        "**ログイン後にのみ存在する要素**のセレクタ (「ログアウト」リンクなど)。"
        "遷移の完了やステータスコードで判定してはならない (5.5)。"
        "手動ログイン後に開発者ツールで探すのが確実。",
    ),
    CoordinateSpec(
        "auth.twofa_kind",
        _S1,
        CoordKind.STRING,
        "2段階認証の種別: none / sms / totp / email_link のいずれか。"
        "email_link の場合、CI側で突破する手段が無いため手動ログイン＋"
        "セッション持ち込みがほぼ必須になる (4章・5.4)。",
    ),
    # ---- 段階2: ログイン後コンテキストと画面遷移 ---------------------------
    CoordinateSpec(
        "context.selection_required",
        _S2,
        CoordKind.BOOL,
        "手動ログイン直後に候補者一覧のURLへ直接遷移してデータが返るかを試す。"
        "返らなければグループ/拠点/求人アカウントの選択ステップが挟まっている (4章)。"
        "その場合 true にし、context.selector を埋める。",
    ),
    CoordinateSpec(
        "context.selector",
        _S2,
        CoordKind.SELECTOR,
        "グループ/拠点の選択コントロール。選択が不要なら null。",
        nullable=True,
    ),
    CoordinateSpec("nav.mypage_url", _S2, CoordKind.URL, "ログイン後のトップ/マイページのURL。"),
    CoordinateSpec(
        "nav.candidate_list_url", _S2, CoordKind.URL, "候補者一覧 (検索結果) の画面URL。"
    ),
    CoordinateSpec(
        "nav.list_ready_selector",
        _S2,
        CoordKind.SELECTOR,
        "候補者一覧の描画完了を表す要素。**「通信の静止」を待ってはならない** (5.3) ので、"
        "この要素の出現を待つ。行そのものではなく行のコンテナを選ぶと空結果でも待てる。",
    ),
    CoordinateSpec(
        "nav.drawer_close_selectors",
        _S2,
        CoordKind.STRING_LIST,
        "候補者ドロワー/モーダルを閉じるコントロールの候補を、**実画面で確認した順**に。"
        "総当たりして駄目ならEscape、それでも消えなければ一覧へ再遷移する (5.7)。",
    ),
    # ---- 段階3: 偵察で確定する内部API -------------------------------------
    CoordinateSpec(
        "api.base_url",
        _S3,
        CoordKind.URL,
        "内部APIのオリジン。`scout recon capture-send` の出力から。",
    ),
    CoordinateSpec(
        "api.candidate_list.url_pattern",
        _S3,
        CoordKind.URL,
        "候補者一覧取得APIのURL (ページング付き)。偵察中の応答インデックスから特定する。",
    ),
    CoordinateSpec("api.resume.url_pattern", _S3, CoordKind.URL, "候補者レジュメ取得APIのURL。"),
    CoordinateSpec(
        "api.precheck.url_pattern",
        _S3,
        CoordKind.URL,
        "送信前チェック (既送信・対象外の除外) API。無ければ null。",
        nullable=True,
    ),
    CoordinateSpec(
        "api.quota.url_pattern",
        _S3,
        CoordKind.URL,
        "送信枠の残数照会API。無ければ null。",
        nullable=True,
    ),
    CoordinateSpec(
        "api.idempotency_header",
        _S3,
        CoordKind.STRING,
        "冪等キーを載せるヘッダ名。媒体に受け口が無ければ null "
        "(その場合は再試行前に送信済み一覧を照会する手順で代替する、9.2)。",
        nullable=True,
    ),
    # 送信エンドポイントは枠ごとに別 (6.1)。成功ステータスも枠ごとに違う (6.2)。
    CoordinateSpec(
        "api.send.paid.url_pattern",
        _S3,
        CoordKind.URL,
        "有料枠の送信API。`scout recon capture-send --slot paid` が記録した" "中断済みPOSTのURL。",
    ),
    CoordinateSpec(
        "api.send.paid.success_statuses",
        _S3,
        CoordKind.STATUS_SET,
        "**成功とみなすHTTPステータスの集合。** エンドポイントごとに違う "
        "(参照実装では 200 と 201 が混在した、6.2)。段階4のdryRun検証で実測する。",
    ),
    CoordinateSpec(
        "api.send.paid.payload_template",
        _S3,
        CoordKind.JSON_PATH,
        "有料枠のpayload形状。IDが配列か単数か、トークンが必要かが枠ごとに違う。"
        "偵察が記録した実リクエストボディをそのまま雛形にする。",
    ),
    CoordinateSpec(
        "api.send.free.url_pattern",
        _S3,
        CoordKind.URL,
        "無料枠の送信API。無ければ null。",
        nullable=True,
    ),
    CoordinateSpec(
        "api.send.free.success_statuses",
        _S3,
        CoordKind.STATUS_SET,
        "無料枠の成功ステータス集合。無料枠が無ければ null。",
        nullable=True,
    ),
    CoordinateSpec(
        "api.send.free.payload_template",
        _S3,
        CoordKind.JSON_PATH,
        "無料枠のpayload形状。無ければ null。",
        nullable=True,
    ),
    CoordinateSpec(
        "api.auth_failure_codes",
        _S3,
        CoordKind.STRING_LIST,
        "**媒体固有の失効応答フォーマット** (6.6)。403 に添えられる認証系エラーコードを"
        "実測して列挙する。汎用の401判定だけでは 403＋独自コードを取り逃す。"
        "セッションを意図的に失効させて1回叩けば分かる。",
    ),
    # ---- レジュメのキー写像 (6.4) -----------------------------------------
    # `scout recon resume-keys` が **キーパスのみ** をログに出す。値は出さない。
    # 確定するまで各項目は UNRESOLVED のままにし、モデル側は空のままにする。
    CoordinateSpec(
        "resume.fields.experienced_industries",
        _S3,
        CoordKind.JSON_PATH,
        "**経験してきた**業界のキーパス。希望条件配下の同名キーと取り違えないこと -- "
        "参照実装の「ご希望の◯◯業界」という虚偽はこの取り違えが原因 (6.4)。",
    ),
    CoordinateSpec(
        "resume.fields.experienced_occupations",
        _S3,
        CoordKind.JSON_PATH,
        "**経験してきた**職種のキーパス。",
    ),
    CoordinateSpec(
        "resume.fields.desired_industries", _S3, CoordKind.JSON_PATH, "**希望する**業界のキーパス。"
    ),
    CoordinateSpec(
        "resume.fields.desired_occupations",
        _S3,
        CoordKind.JSON_PATH,
        "**希望する**職種のキーパス。",
    ),
    CoordinateSpec("resume.fields.employments", _S3, CoordKind.JSON_PATH, "職歴リストのキーパス。"),
    CoordinateSpec("resume.fields.educations", _S3, CoordKind.JSON_PATH, "学歴リストのキーパス。"),
    CoordinateSpec(
        "resume.fields.language_text",
        _S3,
        CoordKind.JSON_PATH,
        "語学欄のキーパス。外国語ネイティブ判定は**この欄にのみ**適用する (7.2)。"
        "範囲を絞る以上、この欄が実際に取れているかの確認が新たな前提になるので、"
        "抽出実装とログ確認をセットで行うこと。",
    ),
    CoordinateSpec("resume.fields.age", _S3, CoordKind.JSON_PATH, "年齢のキーパス。"),
    CoordinateSpec(
        "resume.fields.membership_status", _S3, CoordKind.JSON_PATH, "会員ステータスのキーパス。"
    ),
    CoordinateSpec(
        "resume.fields.specialty", _S3, CoordKind.JSON_PATH, "専門/得意領域のキーパス。"
    ),
    CoordinateSpec("resume.fields.summary", _S3, CoordKind.JSON_PATH, "職務要約のキーパス。"),
    # ---- enum の実値 (6.5) -------------------------------------------------
    CoordinateSpec(
        "enums.education.exact_map",
        _S3,
        CoordKind.ENUM_MAP,
        "学歴の生値→EducationLevel の完全一致マップ。実データで観測した値だけを書く。"
        "網羅は不可能なので、漏れはキーワード推定と UNKNOWN が受ける (3段構え)。",
    ),
    CoordinateSpec(
        "enums.membership.qualifying_values",
        _S3,
        CoordKind.STRING_LIST,
        "対象とする会員ステータスの生値 (参照実装では5種類)。無料枠の経路では適用しない。",
    ),
    # ---- 受信箱 / 返信検知 (10章) -----------------------------------------
    CoordinateSpec(
        "inbox.entry_link_selector",
        _S3,
        CoordKind.SELECTOR,
        "マイページから受信箱へ遷移するリンク。**直接URL遷移では一覧の通信が発火しない** "
        "(10.3) ため、クリックで辿る導線が必要。",
    ),
    CoordinateSpec(
        "inbox.list_response_url_pattern",
        _S3,
        CoordKind.URL,
        "受信箱一覧を返す非同期通信のURLパターン。DOMではなく**この応答本文**に対して"
        "照合する (10.3)。",
    ),
    CoordinateSpec(
        "inbox.subject_json_path",
        _S3,
        CoordKind.JSON_PATH,
        "応答本文の中で件名が入っている位置。返信行は 'Re: 送信した件名' になる (10.2)。",
    ),
    CoordinateSpec(
        "inbox.next_page_control",
        _S3,
        CoordKind.SELECTOR,
        "次ページへ進める操作。**終了判定には使わない** -- 終了は内容の署名で行う (10.5)。"
        "DOMにページャが無い前提で書くこと。",
    ),
    # ---- 追客 (9.8) --------------------------------------------------------
    CoordinateSpec(
        "followup.native_supported",
        _S3,
        CoordKind.BOOL,
        "媒体標準の追客(リマインド)機能があるか。あれば自前再送と二重にならないよう"
        "設定でどちらか一方に決める (9.8)。",
    ),
    CoordinateSpec(
        "followup.param_name",
        _S3,
        CoordKind.STRING,
        "媒体標準の追客を指定する送信payloadのパラメータ名。無ければ null。"
        "**オプショナルなネスト構造なら入口でガードすること** (6.7)。",
        nullable=True,
    ),
    CoordinateSpec(
        "followup.allowed_days",
        _S3,
        CoordKind.STRING_LIST,
        "追客日数として媒体が受け付ける値 (参照実装は3日・5日・10日の3値のみ)。"
        "想定外の値は既定値に丸める。無ければ null。",
        nullable=True,
    ),
    # ---- ID体系の分断 (6.8) -----------------------------------------------
    CoordinateSpec(
        "id.bridge_required",
        _S4,
        CoordKind.BOOL,
        "**画面に出ているIDと、送信APIが要求するIDが同じか** (4章)。違えば true。"
        "歴史のある媒体は新旧サブシステムが同居してID体系が分断されていることがある。",
    ),
    CoordinateSpec(
        "id.bridge_attribute_selector",
        _S6,
        CoordKind.SELECTOR,
        "APIで橋が架からない場合に、UIから正しいIDを拾う要素 (コピー用URL要素など)。"
        "id.bridge_required が false なら null。",
        nullable=True,
    ),
    CoordinateSpec(
        "id.bridge_extract_pattern",
        _S6,
        CoordKind.STRING,
        "上記要素の属性値からIDを抜く正規表現 (捕捉グループ1がID)。不要なら null。",
        nullable=True,
    ),
)

COORDINATES_BY_KEY: dict[str, CoordinateSpec] = {spec.key: spec for spec in COORDINATES}

#: コマンド別の必須座標。**これがラダーを機能させている** -- 各段でプロジェクトが
#: 実際に動き、全部埋まるまで何もできない状態にはならない。
REQUIRED_BY_COMMAND: dict[str, frozenset[str]] = {
    # 偵察は段階1/2の座標だけで走る。送信系を要求したら鶏と卵になる。
    #
    # **段階1は「発見の工程」なので何も要求しない。** ここに auth.* を並べていた
    # 時期があったが、それは循環だった -- 手動ログインにセレクタは要らない
    # (人間が自分で入力する)。auth.email_selector 等は *自動* ログイン用であり、
    # 段階1の **出力** であって入力ではない。要求してしまうと、ラダーの1歩目が
    # 「1歩目の成果物が無い」という理由で始められなくなる。
    "recon-login": frozenset(),
    # 保存セッションで入るので auth.login_url は要らない。到達したい画面
    # (nav.*) だけが本当に必要な前提。
    "recon-capture-send": frozenset(
        {
            "auth.success_marker_selector",
            "nav.candidate_list_url",
            "nav.list_ready_selector",
        }
    ),
    "recon-resume-keys": frozenset(
        {"auth.success_marker_selector", "nav.candidate_list_url", "nav.list_ready_selector"}
    ),
    "ingest": frozenset(
        {
            "auth.success_marker_selector",
            "nav.candidate_list_url",
            "nav.list_ready_selector",
            "api.candidate_list.url_pattern",
            "api.resume.url_pattern",
        }
    ),
    # 生成は媒体座標をほとんど要求しない -- 純粋ロジックとLLMだけで完結する。
    "generate": frozenset(),
    "send": frozenset(
        {
            "auth.success_marker_selector",
            "api.base_url",
            "api.send.paid.url_pattern",
            "api.send.paid.success_statuses",
            "api.send.paid.payload_template",
            "api.auth_failure_codes",
        }
    ),
    "sync-replies": frozenset(
        {
            "auth.success_marker_selector",
            "nav.mypage_url",
            "inbox.entry_link_selector",
            "inbox.list_response_url_pattern",
            "inbox.subject_json_path",
        }
    ),
    "followup": frozenset({"followup.native_supported"}),
    # 分析はDBだけを読むので媒体座標を要求しない。
    "analytics": frozenset(),
    "preflight": frozenset(),
    "coordinates": frozenset(),
}


def specs_for_stage(stage: LadderStage) -> tuple[CoordinateSpec, ...]:
    return tuple(spec for spec in COORDINATES if spec.stage is stage)


def commands_unblocked_by(key: str) -> tuple[str, ...]:
    """Which commands this coordinate is required by.

    ``scout coordinates`` が「このキーを埋めると何ができるようになるか」を
    見せるために使う。
    """
    return tuple(sorted(cmd for cmd, keys in REQUIRED_BY_COMMAND.items() if key in keys))
