"""Published values. **Not coordinates.**

座標との違いを明確にしておく。座標は「観測しないと知りようがない値」であり、
推測で埋めてはいけない。ここに置くのは **媒体が公開している値** で、観測ではなく
参照によって得られるものである。

サインインURLは公開ページなので、これを座標にすると「公開されている事実を
UNRESOLVED と書く」ことになり、かえって不正確になる。一方 ``auth.login_url``
(リダイレクト後に実際にフォームが描画されるURL) は観測しないと分からないので、
そちらは座標のままにしてある。
"""

from __future__ import annotations

#: 顧客(採用企業)向けのサインインページ。公開URL。
#: ここから始めて、リダイレクト後に実際に落ち着いた先が座標 ``auth.login_url``。
PUBLIC_SIGN_IN_URL = "https://customers.job-medley.com/customers/sign_in/"

#: 媒体の登録ドメイン。これも公開値であって観測値ではない。
PLATFORM_DOMAIN = "job-medley.com"


def is_platform_host(host: str) -> bool:
    """Whether ``host`` belongs to the platform.

    セッションの持ち込み (:mod:`handover.curl_session`) で、**計測タグ宛ての通信を
    選んでしまう事故** を構造で塞ぐために使う。

    実際に起きた: 開発者ツールのフィルタに ``job-medley`` と入力すると、宛先が
    ``www.google-analytics.com`` の通信まで残る。計測ビーコンは **閲覧中のページURLを
    自分のリクエストに載せて送る** ので、URL部分一致のフィルタに引っかかるのである。
    手順書で注意を促すだけでは、読み飛ばした人が同じ所で落ちる。

    サブドメインは許す (``customers.`` 以外に認証が置かれている可能性を、観測せずに
    否定できないため)。**末尾一致ではなくラベル境界で判定する** -- ``endswith`` だけだと
    ``job-medley.com.example.invalid`` が通ってしまい、塞いだつもりの穴が残る。
    """
    lowered = host.lower().rstrip(".")
    return lowered == PLATFORM_DOMAIN or lowered.endswith(f".{PLATFORM_DOMAIN}")


#: ログアウト系リンクの手掛かり。**座標 auth.success_marker_selector の代用ではない。**
#: 段階1で「マーカーの候補を人間に見せる」ためだけに使う探索語であり、
#: 本番の判定には使わない -- 本番はあくまで確定した座標で判定する。
LOGOUT_TEXT_HINTS: tuple[str, ...] = (
    "ログアウト",
    "サインアウト",
    "Logout",
    "Sign out",
)
