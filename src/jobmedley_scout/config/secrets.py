"""Credentials, read from the environment only.

12.7: 資格情報と状態を同じ永続化単位に置かない。ここが「資格情報は設定ファイルにも
状態ディレクトリにも決して入らない」を守る唯一の入口である。

* 認証情報は環境変数からのみ読む
* 保存セッションは毎回シークレットから一時領域 (``paths.credentials_dir``) へ
  復元し、キャッシュの対象から除外する
* 設定ファイルに認証情報の欄は **存在しない** (schema.py を参照)
"""

from __future__ import annotations

import base64
import binascii
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from jobmedley_scout.errors import ConfigError

ENV_ANTHROPIC_KEY = "ANTHROPIC_API_KEY"
ENV_PLATFORM_EMAIL = "JOBMEDLEY_EMAIL"
ENV_PLATFORM_PASSWORD = "JOBMEDLEY_PASSWORD"
ENV_STORAGE_STATE_B64 = "JOBMEDLEY_STORAGE_STATE_B64"
#: 普通のブラウザの「Copy as cURL」をそのまま入れる欄
#: (:mod:`handover.curl_session`)。ローカルにCLIを置けない運用のための経路。
ENV_SESSION_CURL = "JOBMEDLEY_SESSION_CURL"


@dataclass(frozen=True)
class Secrets:
    """Credentials for this run. Never logged, never persisted to the state dir."""

    anthropic_api_key: str | None
    platform_email: str | None
    platform_password: str | None
    storage_state_b64: str | None
    session_curl: str | None

    def has_password_login(self) -> bool:
        return bool(self.platform_email) and bool(self.platform_password)

    def has_saved_session(self) -> bool:
        """**両方の持ち込み経路を見る。**

        片方だけを見ていると、cURL 経路だけを設定した利用者に対して起動前チェックが
        「認証経路がありません」と言い、実行は普通に通る、という食い違いが起きる。
        点検が実態と違うことを言い出したら、点検は嘘をつく仕組みになる (12.6)。
        """
        return bool(self.storage_state_b64) or bool(self.session_curl)

    def require_anthropic_key(self) -> str:
        if not self.anthropic_api_key:
            raise ConfigError(f"{ENV_ANTHROPIC_KEY} が設定されていません。文面生成には必須です。")
        return self.anthropic_api_key

    def require_password_login(self) -> tuple[str, str]:
        """5.4 経路2: 認証情報が未設定なら即座にエラーで停止する。"""
        if not self.platform_email or not self.platform_password:
            raise ConfigError(
                f"保存セッションが無く、{ENV_PLATFORM_EMAIL} / {ENV_PLATFORM_PASSWORD} も"
                f"設定されていません。認証経路がありません。"
            )
        return self.platform_email, self.platform_password


def load_secrets(env: Mapping[str, str] | None = None) -> Secrets:
    source = os.environ if env is None else env

    def get(name: str) -> str | None:
        value = source.get(name, "").strip()
        return value or None

    return Secrets(
        anthropic_api_key=get(ENV_ANTHROPIC_KEY),
        platform_email=get(ENV_PLATFORM_EMAIL),
        platform_password=get(ENV_PLATFORM_PASSWORD),
        storage_state_b64=get(ENV_STORAGE_STATE_B64),
        session_curl=get(ENV_SESSION_CURL),
    )


def restore_storage_state(secrets: Secrets, destination: Path) -> Path | None:
    """Materialize the saved browser session from whichever secret is set.

    5.4 経路1: 人が一度2段階認証を突破した結果を持ち込む。**データセンターの
    IPアドレスからの自動ログインは媒体の2段階認証やボット検知で失敗しやすく**、
    CI側で突破する手段がないため、これ以外に方法がない。

    持ち込みの形は2つある。優先順位には理由がある:

    1. ``JOBMEDLEY_STORAGE_STATE_B64`` -- Playwright が書いた本物の
       ``storage_state``。localStorage もクッキーの属性も欠けていない
    2. ``JOBMEDLEY_SESSION_CURL`` -- 普通のブラウザの Copy as cURL から組み立てた
       もの。**Cookie ヘッダから分かることしか入っていない**

    1 が上なのは情報量の差である。両方あるときに 2 を採ると、より完全な材料が
    あるのに不完全な方で走ることになる。
    """
    if secrets.storage_state_b64:
        try:
            decoded = base64.b64decode(secrets.storage_state_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ConfigError(
                f"{ENV_STORAGE_STATE_B64} をbase64として復号できません: {exc}"
            ) from exc
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(decoded)
        # 資格情報なので所有者のみ読み書き可能にする。
        destination.chmod(0o600)
        return destination

    if secrets.session_curl:
        # import はここで行う。handover 側は errors にしか依存しない純粋な変換で、
        # secrets を先に読み込む必要がない。
        from jobmedley_scout.handover.curl_session import (
            storage_state_from_curl,
            write_storage_state,
        )

        write_storage_state(storage_state_from_curl(secrets.session_curl), destination)
        return destination

    return None
