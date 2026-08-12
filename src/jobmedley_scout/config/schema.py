"""Typed configuration schema.

7.6 の事故: 設定キーを書き間違えても寛容な読み込み (キーがなければ既定値) の
ため、**エラーも警告も出ないまま年齢上限が消える** 状態だった。

対処として、本モジュールのモデルには **既定値を一切置いていない**。すべての値は
YAML に明記されなければならない。これにより打鍵ミスは2つの独立した失敗になる --
「未知のキーがある」(``extra="forbid"``) と「必須キーが無い」。片方をすり抜けても
もう片方で落ちる。

設定ファイルが「取り消せない外向き操作」の条件を決めているなら、寛容な読み込みは
事故装置である。
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

_STRICT = ConfigDict(extra="forbid", frozen=True)


class UndeterminablePolicy(StrEnum):
    """What a targeting rule does when it cannot decide.

    7.1: **スキップは、黙って合格させるのと同義。** 各ルールがどちらに倒れるかを
    設定に必須記載させることで、非対称な業務判断 (学歴は取りこぼさない側、他は
    除外側) が YAML の diff で見える所に出る。
    """

    INCLUDE = "include"
    EXCLUDE = "exclude"


class FollowupMode(StrEnum):
    """9.8: どちらが真実かを設定で1つに決める。二重に走らせない。"""

    PLATFORM_NATIVE = "platform_native"
    SELF_SCHEDULED = "self_scheduled"
    DISABLED = "disabled"


class AnalyticsSinkKind(StrEnum):
    LOCAL = "local"
    GOOGLE_SHEETS = "google_sheets"


class WaitRange(BaseModel):
    """A randomized wait interval in seconds.

    5.2: 操作の速さそのものがボット判定のシグナルになる。送信間隔と1実行あたりの
    送信上限は、ボット検知が疑われたときに調整する運用ノブでもあるため設定値。
    """

    model_config = _STRICT

    min_seconds: float
    max_seconds: float

    @model_validator(mode="after")
    def _ordered(self) -> WaitRange:
        if self.min_seconds < 0:
            raise ValueError("待機時間に負の値は指定できません")
        if self.min_seconds > self.max_seconds:
            raise ValueError(f"min_seconds({self.min_seconds}) > max_seconds({self.max_seconds})")
        return self


class SafetyConfig(BaseModel):
    """The safety valves. 12.6 の要点はこれらの **実効値を印字すること**。

    「安全弁を作った」と「安全弁が効いている」は別物である。参照実装では状態消失
    ガードが実行基盤の環境変数に渡っておらず、ドキュメントには手順があるのに
    CIでは常に無効だった。
    """

    model_config = _STRICT

    #: 既定で有効。本番送信は明示操作でのみ解除する (9.1)。
    dry_run: bool
    #: 送信履歴が空のまま実送信しようとしたら中断する (9.1・12.1)。
    state_loss_guard: bool
    #: このファイルが存在する間は全送信を停止する。
    kill_switch_path: Path
    #: 取り込み上限。**送信上限とは別物** (9.7)。片方で兼用すると
    #: 「取り込まれなかったから送られない」が静かに起きる。
    ingest_cap: int
    #: 1通あたりのLLMリクエスト上限 (13.1)。リトライ×フォールバック×修正リトライで
    #: 最大12まで膨らみうるので、構造でコストの上振れを止める。
    max_llm_requests_per_message: int


class SendConfig(BaseModel):
    model_config = _STRICT

    #: **枠ごとの上限** (9.7)。無料枠の処理が有料枠の送信上限に食われると、
    #: その日の対象が未処理のまま残る。
    per_run_cap_paid: int
    per_run_cap_free: int
    #: 送信間隔。dry_run 時にも適用する (実ブラウザ操作は発生しているため、5.2)。
    interval: WaitRange


class BrowserConfig(BaseModel):
    model_config = _STRICT

    #: **ハードコードしないこと** (5.1)。メジャーバージョンが古くなりすぎると
    #: かえって不自然になるため、更新できる必要がある。
    user_agent: str
    locale: str
    timezone: str
    viewport_width: int
    viewport_height: int
    accept_language: str
    headless: bool
    #: ページ読み込みの短いタイムアウト。**通信の静止は待たない** (5.3) ので、
    #: これは切り上げるためのものであり、例外は握りつぶす。
    navigation_timeout_ms: int
    selector_timeout_ms: int


class WaitsConfig(BaseModel):
    model_config = _STRICT

    between_actions: WaitRange
    login_form_fields: WaitRange
    list_paging: WaitRange
    after_mypage: WaitRange
    inbox_paging: WaitRange


class TargetingConfig(BaseModel):
    """What the system filters on. **Deliberately almost empty.**

    2026-08-12 に、指示書15章から持ち込んだビズリーチ参照実装の条件 (年齢・学歴・
    勤続年数・転職回数・外国語ネイティブ・海外大学) を全廃した。経緯と、なぜ
    「寛容な閾値で無効化する」形を採らなかったかは
    :mod:`jobmedley_scout.targeting.rules` の冒頭にある。

    対象の定義は媒体側の検索条件 (座標 ``nav.candidate_list_url``) が持つ。
    ``extra="forbid"`` があるので、古い設定を持ち込むと ``min_longest_tenure_years``
    等は **未知のキー** として読込時に落ちる。消えたフィールドが黙って復活する
    経路は無い。
    """

    model_config = _STRICT

    #: **ルールIDごとの判定不能時の方針。既定値は無い** (7.1)。
    #: ここに並ぶ行が、走っているルールの全部である。宣言と実装のどちらかにしか
    #: 無いルールIDがあれば ConfigError になる (registry の両方向検査)。
    undeterminable_policy: dict[str, UndeterminablePolicy]


class MatchingConfig(BaseModel):
    """8.3 対策3: 照合の緩さがそのままハルシネーションになる。"""

    model_config = _STRICT

    #: 既定は順方向のみ。双方向の部分一致は「トヨタ自動車」と
    #: 「トヨタ自動車直系の販売会社」を一致させた。
    bidirectional_substring: bool
    #: 3文字未満の語は後方一致の対象外 ("営業" が12名の "法人営業" に一致した)。
    min_token_length_for_suffix_match: int
    #: マスタに混在する業種の総称。企業名一致の根拠から除外する
    #: ("サービス業" と "株式会社サービスプロダクト" が一致した)。
    industry_generic_terms: tuple[str, ...]
    #: マスタに混在する役職。職種の共通点の根拠から除外する ("課長" で一致した)。
    job_title_stopwords: tuple[str, ...]


class GenerationConfig(BaseModel):
    model_config = _STRICT

    max_introductions: int
    min_introductions: int
    followup_introductions: int
    max_exclamation_marks: int
    #: 8.3 対策4: 共通点がない相手の紹介文にこれらが含まれていたら修正リトライ。
    assertive_terms: tuple[str, ...]
    #: 「近い」「近しい」は断定しないラベルなので許容する。
    allowed_soft_terms: tuple[str, ...]
    #: 8.7: 本文に書いてよいURLのドメイン。バリデータで機械的に検証する。
    url_allowlist: tuple[str, ...]
    matching: MatchingConfig


class ThinkingEffort(StrEnum):
    """Thinking depth. Replaces the retired fixed-token budget.

    **8.2 の事故がまさにこれ**: 「LLM APIの仕様変更で本番が静かに全滅しうる
    (参照実装では拡張思考のパラメータ形式が廃止され、全件が生成失敗しました)」。

    現行モデル (claude-sonnet-5 / claude-opus-5 等) では
    ``thinking={"type": "enabled", "budget_tokens": N}`` は **400 で拒否される**。
    正しくは ``thinking={"type": "adaptive"}`` + ``output_config={"effort": ...}``。
    固定トークン予算という概念自体が無くなったので、設定項目もそれに合わせてある。
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


class LlmConfig(BaseModel):
    """13.1: モデル名・最大トークン数・思考の深さを単一情報源に置き、
    コスト重視のモデルへ即座に切り替えられるようにする。"""

    model_config = _STRICT

    model: str
    #: 非ストリーミングは概ね16000が上限の目安 (それ以上はHTTPタイムアウトの危険)。
    max_tokens: int
    thinking_enabled: bool
    #: 思考の深さ。**budget_tokens は現行モデルで廃止されている** (上記参照)。
    effort: ThinkingEffort
    max_retries: int


class ReplyConfig(BaseModel):
    model_config = _STRICT

    #: 10.2 のガード3点。
    subject_min_length: int
    subject_prefix_length: int
    page_size: int
    max_pages: int
    #: 応答キャプチャの1件あたり上限文字数。
    response_capture_char_cap: int
    #: 9.6: ローテーション付きの再確認バッチ。
    recheck_batch_size: int
    recheck_window_days: int


class FollowupConfig(BaseModel):
    model_config = _STRICT

    mode: FollowupMode
    days_after_first: int
    #: 媒体が受け付ける日数値。想定外の値はここの先頭に丸める (6.7)。
    allowed_days: tuple[int, ...]


class CalendarConfig(BaseModel):
    """12.4: 業務ルールの判定はアプリ側に置き、休止の理由を文字列で返す。"""

    model_config = _STRICT

    skip_weekends: bool
    #: 祝日 (YYYY-MM-DD)。シェルスクリプトでは表現できなかったもの。
    holidays: tuple[str, ...]
    send_window_start_hour_jst: int
    send_window_end_hour_jst: int


class AnalyticsConfig(BaseModel):
    model_config = _STRICT

    sink: AnalyticsSinkKind
    weekly_periods: int
    monthly_periods: int
    trend_weeks: int
    trend_months: int
    trend_min_sample: int
    output_dir: Path
    spreadsheet_id: str | None


class PathsConfig(BaseModel):
    """12.7: 状態と資格情報を **別の永続化単位** に置く。

    参照実装では、ログイン済みセッションが状態ディレクトリ配下にあり、実行基盤の
    キャッシュがそれを丸ごと保存・復元していた。既定ブランチで作られたキャッシュは
    他のブランチからも復元できるため、リポジトリに書き込める者がセッション情報を
    取り出せる状態だった (媒体アカウント乗っ取りの経路)。
    """

    model_config = _STRICT

    #: 重複防止・スケジュールなどの状態。キャッシュ対象にしてよい。
    state_dir: Path
    #: セッション・鍵。**キャッシュ対象から除外し、毎回シークレットから復元する。**
    credentials_dir: Path
    #: 偵察の生ダンプ。保持期間を通常より短くする (13.2)。
    recon_dump_dir: Path
    recon_dump_retention_days: int


class IdConfig(BaseModel):
    """9.3 の2点目: 畳むのは**観測されたパターンのみ**。"""

    model_config = _STRICT

    #: 観測済みの表記ゆれパターン。ジョブメドレーではまだ観測が無いので空。
    #: 推測で足さないこと -- 別人を1件に merge するほうが表記ゆれより深刻。
    observed_patterns: tuple[dict[str, str], ...]


class Config(BaseModel):
    """The whole behavior configuration."""

    model_config = _STRICT

    safety: SafetyConfig
    send: SendConfig
    browser: BrowserConfig
    waits: WaitsConfig
    targeting: TargetingConfig
    generation: GenerationConfig
    llm: LlmConfig
    reply: ReplyConfig
    followup: FollowupConfig
    calendar: CalendarConfig
    analytics: AnalyticsConfig
    paths: PathsConfig
    ids: IdConfig
    #: 4章・13.6 の記録。利用者が確認済みである旨を残す欄であり、
    #: 起動前チェックのゲートにはしない (利用者の判断による)。
    compliance_note: str = Field(min_length=1)
