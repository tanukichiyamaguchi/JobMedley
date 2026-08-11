"""Turn a browser's "Copy as cURL" into a saved session, with no local install.

5.4 経路1 は「ローカルでヘッドフル起動して人間がログインする」ことを前提に
書かれている。**その前提が成り立たない運用がある。** 開発も実行もクラウドで
行う場合、人間の目の前にあるブラウザと、コードが動く環境が別の場所にある。

しかし段階1が本当に必要としているのは「ローカルでCLIを動かすこと」ではなく、
**人間が一度2段階認証を突破した結果** である。それは普通のブラウザから取り出せる:

1. 媒体に普通にログインする (ただのウェブ操作。何もインストールしない)
2. 開発者ツール → Network → 認証済みのリクエストを右クリック → Copy as cURL
3. その文字列をシークレットに登録する

本モジュールはその文字列を Playwright の ``storage_state`` へ変換する。
**ブラウザに一切依存しない純粋な変換** なので、認証情報なしで完全にテストできる
(13.4)。この環境で検証できない部分を最小にするための分割である。

なぜ ``document.cookie`` を読ませないのか
-----------------------------------------

コンソールに JavaScript を貼らせる案は使わない。``document.cookie`` は
**HttpOnly のクッキーを見ることができない** ので、セッションクッキーだけが
欠けた「一見それらしい」出力ができあがる。取り込みは成功し、ログインだけが
復元されない -- 原則2の静かな失敗そのものになる。Copy as cURL が送信する
``Cookie`` ヘッダには HttpOnly も含まれるので、こちらを唯一の経路にする。

推測しないこと
--------------

* クッキーのドメインは **観測したリクエストのホストに限定する**。
  ``.job-medley.com`` のように広げれば復元される確率は上がるかもしれないが、
  それは「観測していないホストへセッションを送る」ことであり、推測である
* localStorage はこの経路では取れない。**取れないものを取れたことにしない** ので、
  別入力として受け取り、無ければ空のまま返す。認証が localStorage 側にある媒体では
  この経路は成立せず、それは ``scout recon verify-session`` が判定する
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from jobmedley_scout.errors import ConfigError
from jobmedley_scout.recon.known import is_platform_host

#: 行継続の記法。bash の ``\`` は :func:`shlex.split` が解釈するので、
#: ここで潰すのは cmd.exe の ``^`` と PowerShell の バッククォート のみ。
_CONTINUATIONS = re.compile(r"[`^]\r?\n")
_URL_LIKE = re.compile(r"^https?://", re.IGNORECASE)

#: 値を持つフラグ。次のトークンを引数として食う。取りこぼすとURLの誤検出になる
#: (例: ``--data 'https://...'`` の中身をURLと取り違える)。
_FLAGS_WITH_VALUE = frozenset(
    {
        "-H", "--header",
        "-b", "--cookie",
        "-d", "--data", "--data-raw", "--data-binary", "--data-urlencode",
        "-X", "--request",
        "-A", "--user-agent",
        "-e", "--referer",
        "-u", "--user",
        "--url",
        "-o", "--output",
        "--connect-timeout", "--max-time", "--retry",
        "--proxy", "-x",
        "--cert", "--key", "--cacert",
        "--form", "-F",
    }
)  # fmt: skip


@dataclass(frozen=True)
class Cookie:
    """One cookie, as Playwright's ``storage_state`` wants it."""

    name: str
    value: str
    domain: str
    path: str
    secure: bool

    def to_playwright(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "domain": self.domain,
            "path": self.path,
            # -1 は「セッションクッキー」。有効期限は Cookie ヘッダに現れないので
            # **知らないものを知っている形で書かない**。
            "expires": -1,
            # HttpOnly かどうかもヘッダからは分からない。復元の可否には影響しない
            # (ブラウザへ入れる側なので) ため、保守的に false を書く。
            "httpOnly": False,
            "secure": self.secure,
            "sameSite": "Lax",
        }


@dataclass(frozen=True)
class CurlObservation:
    """What we could read out of the pasted command. Nothing inferred."""

    url: str
    cookie_header: str
    user_agent: str | None


def _tokenize(text: str) -> list[str]:
    normalized = _CONTINUATIONS.sub(" ", text.strip())
    try:
        tokens = shlex.split(normalized)
    except ValueError as exc:
        raise ConfigError(
            f"cURL コマンドとして解釈できませんでした ({exc})。\n"
            f"  開発者ツールの Network タブで、認証済みのリクエストを右クリックし\n"
            f"  「Copy as cURL」(Windows では「Copy as cURL (bash)」) を選んでください。"
        ) from exc
    if not tokens:
        raise ConfigError("入力が空です。Copy as cURL の結果を貼り付けてください。")
    if tokens[0].lower() not in {"curl", "curl.exe"}:
        raise ConfigError(
            f"先頭が 'curl' ではありません: {tokens[0]!r}\n"
            f"  コマンド全体を、途中で切らずに貼り付けてください。"
        )
    return tokens


def parse_curl(text: str) -> CurlObservation:
    """Read the URL, the ``Cookie`` header and the User-Agent out of a paste.

    見つからないものは黙って補わない。特に ``Cookie`` が無い場合は
    **「クッキーの無いセッション」を作らずに停止する** -- それを作ってしまうと、
    取り込みだけ成功してログインだけ復元されない状態になる。
    """
    tokens = _tokenize(text)

    url: str | None = None
    cookie_header: str | None = None
    user_agent: str | None = None

    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in _FLAGS_WITH_VALUE:
            if index + 1 >= len(tokens):
                raise ConfigError(f"{token} に値がありません。貼り付けが途中で切れていませんか。")
            value = tokens[index + 1]
            index += 2
            if token in {"-H", "--header"}:
                name, _, header_value = value.partition(":")
                lowered = name.strip().lower()
                if lowered == "cookie" and cookie_header is None:
                    cookie_header = header_value.strip()
                elif lowered == "user-agent" and user_agent is None:
                    user_agent = header_value.strip()
            elif token in {"-b", "--cookie"} and cookie_header is None:
                cookie_header = value.strip()
            elif token in {"-A", "--user-agent"} and user_agent is None:
                user_agent = value.strip()
            elif token == "--url" and url is None:
                url = value
            continue
        if url is None and _URL_LIKE.match(token):
            url = token
        index += 1

    if url is None:
        raise ConfigError("URL が見つかりませんでした。コマンド全体を貼り付けてください。")
    if not cookie_header:
        raise ConfigError(
            "Cookie ヘッダが見つかりませんでした。\n"
            "  そのリクエストは認証済みではない可能性があります。ログイン後の画面で\n"
            "  発生したリクエスト (媒体のドメイン宛て) を選び直してください。\n"
            "  クッキーの無いセッションを作っても、取り込みが成功してログインだけが\n"
            "  復元されない状態になるため、ここで停止します。"
        )
    return CurlObservation(url=url, cookie_header=cookie_header, user_agent=user_agent)


def cookies_from_header(cookie_header: str, *, url: str) -> tuple[Cookie, ...]:
    """Split a ``Cookie`` header into cookies scoped to ``url``'s host.

    ドメインは **観測したホストそのもの** にする。親ドメインへ広げれば復元される
    確率は上がるかもしれないが、それは観測していないホストへセッションを送ることを
    意味する。復元できなかったかどうかは ``scout recon verify-session`` が判定するので、
    確率を上げるために推測を混ぜる必要はない。

    同名のクッキーが複数あった場合は **最初のものを採る**。ブラウザはより限定的な
    スコープのものを先に送るため (RFC 6265)、どちらか選ぶならこちらである。
    """
    parts = urlsplit(url)
    if not parts.hostname:
        raise ConfigError(f"URL からホスト名を取り出せません: {url!r}")
    if not is_platform_host(parts.hostname):
        # **実際に起きた間違いを構造で塞ぐ。** 開発者ツールのフィルタは URL 全体への
        # 部分一致なので、``customers.job-medley.com`` と入力しても計測ビーコンが残る --
        # ビーコンは閲覧中のページURLを自分のクエリに載せて送るからである。
        # 手順書の注意書きだけでは、読み飛ばした人が同じ所で落ちる。
        raise ConfigError(
            f"ジョブメドレー宛ての通信ではありません: {parts.hostname}\n"
            f"  計測タグ (Google アナリティクス等) のリクエストを選んでいませんか。\n"
            f"  開発者ツールのフィルタは URL 全体への部分一致なので、ドメイン名を\n"
            f"  入力しても計測ビーコンが残ります (ビーコンは閲覧中のページURLを\n"
            f"  自分のクエリに載せて送るため)。\n"
            f"  Network タブで **Doc** ボタンを押し、F5 で再読み込みしてから、\n"
            f"  Request URL が https://customers.job-medley.com/ で始まる行を\n"
            f"  選び直してください。"
        )
    domain = parts.hostname
    secure = parts.scheme.lower() == "https"

    cookies: list[Cookie] = []
    seen: set[str] = set()
    for chunk in cookie_header.split(";"):
        name, separator, value = chunk.strip().partition("=")
        name = name.strip()
        if not separator or not name or name in seen:
            continue
        seen.add(name)
        cookies.append(
            Cookie(name=name, value=value.strip(), domain=domain, path="/", secure=secure)
        )

    if not cookies:
        raise ConfigError("Cookie ヘッダを解釈できませんでした: 名前=値 の対が1つもありません。")
    return tuple(cookies)


def storage_state_from_curl(
    text: str, *, local_storage: dict[str, str] | None = None
) -> dict[str, Any]:
    """Build a Playwright ``storage_state`` from a pasted cURL command.

    ``local_storage`` は任意。開発者ツールの Application → Local Storage から
    書き写してもらう場合のみ渡す。**渡されなければ空のまま返す** -- 取れていない
    ものを、それらしい形で埋めない。
    """
    observation = parse_curl(text)
    cookies = cookies_from_header(observation.cookie_header, url=observation.url)

    parts = urlsplit(observation.url)
    origin = f"{parts.scheme}://{parts.netloc}"
    origins: list[dict[str, Any]] = []
    if local_storage:
        origins.append(
            {
                "origin": origin,
                "localStorage": [
                    {"name": key, "value": value} for key, value in local_storage.items()
                ],
            }
        )

    return {"cookies": [cookie.to_playwright() for cookie in cookies], "origins": origins}


def summarize(storage_state: dict[str, Any], user_agent: str | None = None) -> str:
    """A report that names what was imported but **never prints a value**.

    13.2 の方針をそのまま適用する。セッションクッキーの値は、このシステムが扱う
    中で最も危険な文字列である -- 1本あれば媒体アカウントに入れる。ログに出れば
    実行ログを見られる全員に渡ることになるので、名前と件数だけを出す。
    """
    cookies = storage_state.get("cookies", [])
    origins = storage_state.get("origins", [])
    names = sorted(str(cookie.get("name", "")) for cookie in cookies)
    domains = sorted({str(cookie.get("domain", "")) for cookie in cookies})

    lines = [
        "セッションを取り込みました。",
        "",
        f"  クッキー   : {len(cookies)}件",
        f"  名前       : {', '.join(names)}",
        f"  ドメイン   : {', '.join(domains)}",
        "  値         : 表示しません (13.2)",
    ]

    stored = sum(len(entry.get("localStorage", [])) for entry in origins)
    if stored:
        lines.append(f"  localStorage: {stored}件")
    else:
        lines.extend(
            [
                "  localStorage: なし",
                "    ^ この経路では取得できません。媒体が認証を localStorage に置いている",
                "      場合、クッキーだけでは復元されません。次の確認で判明します。",
            ]
        )

    if user_agent:
        lines.extend(
            [
                "",
                "取り込み元ブラウザの User-Agent:",
                f"  {user_agent}",
                "  ^ config/config.yaml の browser.user_agent をこれに合わせると、",
                "    セッションを作ったブラウザと実行時のブラウザの申告が一致します (5.1)。",
            ]
        )

    lines.extend(["", "次: `scout recon verify-session` で復元できるか確認してください。"])
    return "\n".join(lines)


def write_storage_state(storage_state: dict[str, Any], destination: Any) -> None:
    """Write the session to disk with owner-only permissions.

    ``destination`` は :class:`pathlib.Path`。12.7 により、置いてよいのは
    ``paths.credentials_dir`` の下だけである (状態ディレクトリではない)。
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(storage_state, ensure_ascii=False), encoding="utf-8")
    destination.chmod(0o600)
