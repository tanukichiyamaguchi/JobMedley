"""Saved browser session (storage state).

12.7 の事故:

> 参照実装では、ログイン済みセッションが状態ディレクトリ配下にあり、実行基盤の
> キャッシュがそれを丸ごと保存・復元しています。既定ブランチで作られたキャッシュは
> **他のブランチからも復元できる** ため、リポジトリに書き込める者がセッション情報を
> 取り出せる状態でした (媒体アカウント乗っ取りの経路になります)。

したがって:

* セッションは ``paths.credentials_dir`` (資格情報の永続化単位) にのみ置く
* ``paths.state_dir`` (重複防止・スケジュール) とは **別のディレクトリ**
* CI では毎回シークレットから一時領域へ復元し、**キャッシュの対象から除外する**

5.4: **セッションの保存タイミングは、終了時だけでなくログイン成功直後にも。**
実行が途中で落ちても、人間が突破した2段階認証の結果を失わないため。
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

SESSION_FILENAME = "storage_state.json"


def session_path(credentials_dir: Path) -> Path:
    """Where the saved session lives. **Never under state_dir** (12.7)."""
    return credentials_dir / SESSION_FILENAME


def save(context: Any, credentials_dir: Path) -> Path:
    """Persist the browser session.

    ログイン成功直後にも呼ぶこと (5.4)。終了時だけに任せると、途中で落ちたときに
    手動ログインをやり直す羽目になる。
    """
    destination = session_path(credentials_dir)
    destination.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(destination))
    destination.chmod(0o600)
    return destination


def exists(credentials_dir: Path) -> bool:
    return session_path(credentials_dir).exists()


def to_base64(credentials_dir: Path) -> str:
    """Encode the session for storage as a CI secret.

    5.4 経路1 の運用: ローカルでヘッドフル起動して人間がログインし、この出力を
    シークレットに登録する。**データセンターのIPからの自動ログインは媒体の
    2段階認証やボット検知で失敗しやすく、CI側で突破する手段がない** ため、
    人が一度突破した結果を持ち込む以外に方法がない。
    """
    path = session_path(credentials_dir)
    if not path.exists():
        raise FileNotFoundError(
            f"保存セッションがありません: {path}\n"
            f"`scout recon login` をヘッドフルで実行し、手動でログインしてください。"
        )
    return base64.b64encode(path.read_bytes()).decode("ascii")
