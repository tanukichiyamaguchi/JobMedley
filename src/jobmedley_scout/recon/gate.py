"""The reconnaissance send gate.

3章 段階3の中核。**このモジュールは送信路より先に書かれ、送信路が存在しない
段階でテストされている。** それが指示書の要求する順序である。

なぜ必要か:

> **副作用のある操作は、受動的な観測では絶対に取れません。** 送信APIは送信ボタンを
> 押すまで発火せず、押せば本当に送信されてしまいます。

そこで、送信ボタンを押す **直前に** ネットワーク層のブロックを武装し、以降の
送信系POSTを記録してから中断する。

**方針は fail-closed。武装中は「安全と確実に分かるもの」以外すべてを止める。**
センチネルやURLパターンでは絞り込まない。理由:

* 段階3では **送信URLそのものが未知** である。それで絞り込むのは循環参照になる
* payload にセンチネルが載らない送信は素通ししてしまう

武装中に無関係な計測ビーコンのPOSTまで中断されるのは **許容する**。武装窓は
ミリ秒単位であり、``disarm()`` は ``finally`` に置く。センチネルは
**解析時にのみ** 使い、どれが送信APIだったかを切り分ける。

本モジュールは **ブラウザに一切依存しない**。ブラウザ依存部はテストできないので、
判定はそこへ置かない (13.4)。``tests/recon/test_gate.py`` が
「武装前は通す / 武装後は止める / GETは常に通す」を固定している。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import urlsplit

from jobmedley_scout.recon.graphql import is_read_only_graphql

#: 副作用が無いと **確実に分かる** メソッドだけ。武装中でも素通しする。
#: GET を止めると送信画面そのものが描画されず、偵察が成立しない。
#:
#: ここに更新系を足した時点で、このモジュールの存在意義が消える。
#: 中身はテストで固定してある。``OPTIONS`` は仕様上は安全だが入れていない --
#: 「安全そう」ではなく「安全と確実に分かる」ものだけを通す方針のため。
SAFE_METHODS: frozenset[str] = frozenset({"GET", "HEAD"})

#: :data:`GateMode.BLOCK_SEND` が読み取りとみなす REST の経路の末尾。
#:
#: **推測ではない。** 実測23/24回目、一覧を開いた瞬間に媒体のオリジンへ飛んだ
#: 非GETは、計測ビーコンを除くとこの6本だけで、末尾はすべてここに在る::
#:
#:     customer_search_conditions/search_manual/
#:     customer_search_conditions/search_recommend/
#:     received_favorites/search/
#:     scouted_members/search/
#:     customer_search_conditions/label/
#:     members/search/
#:
#: 足すときは「名前がそれらしいから」ではなく、**押さない偵察で実際に飛んだ**
#: ことを根拠にすること。ここに書き込み経路を1つ入れれば、このモードの
#: 意味が消える。
READ_PATH_SEGMENTS: frozenset[str] = frozenset(
    {"search", "search_manual", "search_recommend", "label"}
)


#: **知っていて受け入れる書き込み。** 読み取りではない。
#:
#: :data:`READ_PATH_SEGMENTS` とは別にしてあるのが要点である。あちらは
#: 「副作用が無いと観測できたもの」で、こちらは **「副作用が在ると分かっていて、
#: それでも通すもの」** である。混ぜれば、受け入れた覚えのない書き込みが
#: 読み取りの顔をして増えていく。
#:
#: 呼び出し側が名前で明示したときだけ効く (``SendGate(accepted_writes=...)``)。
#: 既定は空である。
#:
#: 実測25回目に見つかった1つ::
#:
#:     POST /api/customers/members/mark_read/   {"member_ids": [...]}
#:
#: プロフィールを開くと飛ぶ。一覧の ``members[].read_profile`` を立てる書き込み
#: で、**運用者が「プロフィール確認」を押すたびに起きているもの** と同じである。
#: 遮断がこれを止めたら、モーダルはプロフィールの取得まで進まなかった --
#: 止めたことでレジュメが観測できなくなった。
KNOWN_WRITE_MARK_READ = "mark_read"


def is_accepted_write(url: str, accepted: frozenset[str]) -> bool:
    """Whether this URL is a write the caller explicitly agreed to. Pure."""
    if not accepted:
        return False
    segments = [segment for segment in urlsplit(url).path.split("/") if segment]
    if not segments:
        return False
    return segments[-1].lower() in accepted


def is_read_path(url: str) -> bool:
    """Whether a REST URL's path names one of the observed read endpoints. Pure.

    末尾の空の節 (``.../search/`` の末尾スラッシュ) は落としてから見る。
    判定できない形は **すべて False** -- 通さない側へ倒す。
    """
    segments = [segment for segment in urlsplit(url).path.split("/") if segment]
    if not segments:
        return False
    return segments[-1].lower() in READ_PATH_SEGMENTS


def is_own_origin(url: str, own_host: str) -> bool:
    """Whether ``url`` addresses ``own_host`` itself. Pure.

    **部分一致で判定してはいけない。** 実測23回目、``own_host in url`` で
    判定していたせいで、他所への計測ビーコンが :data:`GateMode.BLOCK_THIRD_PARTY`
    を素通りした::

        POST https://www.google-analytics.com/g/collect?...
             &dl=https%3A%2F%2Fcustomers.job-medley.com%2Fcustomers%2Fsearches...

    ビーコンは「どのページから送ったか」を ``dl`` に載せる。そこに媒体のホスト名が
    そのまま入るので、URL全体を見る部分一致は **他所への通信を媒体の通信と
    取り違える**。しかも報告はその取り違えを引き継いで「媒体のオリジンへ飛んだ
    非GET」として並べる -- 止めたつもりのものを通し、通した事実を別の名前で
    報告していた。

    ホスト名だけを取り出し、完全一致か部分ドメインかで判定する。
    ホスト名が取れない URL (``data:`` など) と ``own_host=""`` は
    **通さない側へ倒れる**。部分一致版では ``own_host=""`` が
    「すべて通す」だったので、そこも同時に塞がる。
    """
    host = (urlsplit(url).hostname or "").lower()
    own = own_host.lower().strip().lstrip(".")
    if not host or not own:
        return False
    return host == own or host.endswith("." + own)


class GateDecision(StrEnum):
    PASS = "pass"
    RECORD_AND_ABORT = "record_and_abort"
    #: 記録したうえで、**中断ではなく空の応答を返す**。
    #:
    #: 実測5回目: 中断すると媒体の共通エラー処理が働き、``/customers/network_error/``
    #: へ画面ごと飛ばされて探索が終わった。空の応答なら媒体は「通信は成立したが
    #: 中身が無い」と受け取るので、画面が残り、探索を続けられる。
    #:
    #: **安全性は中断と同じである。** どちらもリクエストは媒体のサーバへ到達しない。
    RECORD_AND_STUB = "record_and_stub"


class GateMode(StrEnum):
    """What the armed gate lets through. **名前が安全上の性質そのものである。**"""

    #: 武装中は :data:`SAFE_METHODS` 以外の **すべて** を止める。既定。
    BLOCK_ALL = "block_all"
    #: 武装中は **書き込みだけ** を止め、GraphQL の読み取り (``query``) は通す。
    #:
    #: 探索コマンド専用の緩和である。媒体は GraphQL の単一ページアプリなので、
    #: 画面を開くための読み取りも POST で来る -- BLOCK_ALL のままでは送信画面へ
    #: 到達できず、段階3が原理的に終わらない (:mod:`recon.graphql` の冒頭)。
    BLOCK_WRITES = "block_writes"
    #: 媒体自身のオリジンへの通信は **全部通し**、他所への通信だけを止める。
    #:
    #: **これは送信に対する保護ではない。** 送信APIも媒体のオリジンにあるので、
    #: このモードで送信ボタンを押せば送信は成立する。
    #:
    #: 使ってよいのは **ボタンを1つも押さないコマンドだけ** である。押さなければ
    #: 飛ぶ通信は「運用者が自分でそのページを開いたとき」と同じものだけになり、
    #: 偵察が新たな副作用を持ち込むことはない。守っているのは遮断ではなく
    #: 「押さないこと」で、それは試験で固定する
    #: (``tests/guardrails/test_observe_only_never_presses.py``)。
    #:
    #: **なぜ要るのか。** 実測22回目、BLOCK_WRITES のまま一覧を開いたら、候補者を
    #: 取ってくる通信そのものが止まった::
    #:
    #:     POST /api/customers/customer_search_conditions/search_manual/
    #:     POST /api/customers/received_favorites/search/
    #:     POST /api/customers/scouted_members/search/
    #:
    #: **この媒体の読み取りは GraphQL ではなく REST の POST である。**
    #: 遮断から見れば書き込みと区別が付かないので、空の応答を返していた。
    #: 観測したかったものを、観測のための仕掛けが止めていた。
    #:
    #: 他所への通信 (計測ビーコン) を止め続けるのは、観測に要らないうえ、
    #: 止めたほうが安全側で、報告も読める量に収まるからである (実測22回目は
    #: 534件のうち529件が計測ビーコンだった)。
    BLOCK_THIRD_PARTY = "block_third_party"
    #: 媒体の **読み取りだけ** を通す。送信は通さない。押してよい。
    #:
    #: :data:`BLOCK_THIRD_PARTY` は媒体のオリジンを丸ごと素通しにするので、
    #: 押せば送信が成立した。だから「押さないこと」が唯一の保護だった。
    #: **このモードは押せる。** 通す条件を許可制にしてあるからである::
    #:
    #:     GraphQL          読み取り (query) だけ通す。mutation は止める
    #:                      -- 送信 (SendSingleScout) は mutation である
    #:     REST の POST     経路の末尾が :data:`READ_PATH_SEGMENTS` のものだけ
    #:     それ以外         止める (他所のオリジンを含む)
    #:
    #: **なぜ許可制なのか。** 「送信のURLだけ止める」は拒否制で、知らない
    #: 送信路に対して素通しになる。許可制なら、知らないものは全部止まる。
    #: 中身は推測ではなく実測である -- 実測23/24回目で一覧を開いたときに飛んだ
    #: REST の POST は、末尾が ``search`` 系か ``label`` のものだけだった。
    #:
    #: **残るリスクを隠さない。** 許可した経路が実は書き込みだった場合は通す。
    #: ``search`` / ``label`` という名前と、それらが「ページを開いただけで
    #: 飛ぶ」という観測が根拠であって、証明ではない。押す対象を
    #: 「プロフィールを開く操作」に限る規律は、このモードでも要る。
    BLOCK_SEND = "block_send"


@dataclass(frozen=True)
class RecordedRequest:
    """One request that was intercepted while the gate was armed."""

    method: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    body: str | None = None
    #: 1始まりの連番。``clear()`` しても戻らない (後述)。
    sequence: int = 0


class SendGate:
    """Decides whether a request may proceed. Browser-independent and pure.

    使い方::

        gate.arm()
        try:
            click_send(page)
        finally:
            gate.disarm()   # 武装窓はミリ秒単位に保つ
        analyse(gate.recorded)
    """

    def __init__(
        self,
        *,
        mode: GateMode = GateMode.BLOCK_ALL,
        own_host: str = "job-medley.com",
        accepted_writes: frozenset[str] = frozenset(),
    ) -> None:
        # 安全メソッドの集合を **注入可能にしていない**。差し替えられる形にすると、
        # ``SendGate(frozenset({"GET", "POST"}))`` の1行で fail-closed が消える --
        # しかもテストは通ったままになる。この集合は :data:`SAFE_METHODS` 固定で、
        # 変更にはモジュールの編集とレビューを要する。
        self._safe_methods = SAFE_METHODS
        # **既定は BLOCK_ALL。** 緩和は呼び出し側が名前で明示したときだけ効く。
        # 引数名も列挙子の名前も「何を止めるか」をそのまま述べているので、
        # 読み違えて緩められない。
        self._mode = mode
        #: :data:`GateMode.BLOCK_THIRD_PARTY` が「自分のオリジン」とみなすホスト。
        #: 既定を媒体に固定してあるので、書き換えるにはモジュールの編集が要る。
        self._own_host = own_host.lower()
        #: **知っていて受け入れる書き込み** (:data:`KNOWN_WRITE_MARK_READ`)。
        #: 既定は空 -- 受け入れるかどうかは呼び出し側が名前で明示する。
        #: :data:`GateMode.BLOCK_SEND` でのみ効く。
        self._accepted_writes = frozenset(item.lower() for item in accepted_writes)
        self._armed = False
        #: 受け入れた書き込みのうち、実際に通したもの。**必ず報告に出すこと。**
        #: 何を書いたか分からないまま偵察が終わるのが一番悪い。
        self._accepted_passed: list[RecordedRequest] = []
        #: 武装中に **通した** 読み取り。通した事実も観測なので残す --
        #: 報告が「何を通したか」を述べられないと、緩和が黙って効く。
        self._passed_reads: list[RecordedRequest] = []
        self._recorded: list[RecordedRequest] = []
        #: 記録の通し番号。``clear()`` で戻さないのは、消す前と後の「1番」が
        #: 解析ログ上で区別できなくなるため。
        self._sequence = 0

    # --- 武装 ---------------------------------------------------------------
    def arm(self) -> None:
        """Start blocking. Call this **immediately before** clicking send.

        **既存の記録は消さない。** 武装のたびに黙って証拠が消えると、複数回の
        試行を突き合わせられなくなる。
        """
        self._armed = True

    def disarm(self) -> None:
        """Stop blocking. Belongs in a ``finally``."""
        self._armed = False

    @property
    def is_armed(self) -> bool:
        return self._armed

    # --- 判定 ---------------------------------------------------------------
    def decide(
        self,
        method: str,
        url: str,
        body: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> GateDecision:
        """Whether this request passes, or is recorded and aborted.

        武装前は何も止めない。武装中は :data:`SAFE_METHODS` に **完全一致** する
        もの以外すべてを止める。

        大文字小文字の正規化を **あえて行わない** のが要点である。``"get"`` を
        ``"GET"`` へ畳んで通すような親切は、正規化の穴をそのまま fail-closed の
        穴にする。想定外の形で来たメソッドは「安全と確実に分かる」に該当しないので
        止める -- 偵察中にGETが1本中断されるのは復旧できるが、更新系を1本
        通してしまうのは復旧できない。
        """
        if not self._armed:
            return GateDecision.PASS
        if method in self._safe_methods:
            return GateDecision.PASS
        # **許可制。** GraphQL は読み取りだけ、REST は観測済みの読み取り経路だけを
        # 通す。送信 (mutation) も、知らない経路も、ここで止まる。
        if (
            self._mode is GateMode.BLOCK_SEND
            and is_own_origin(url, self._own_host)
            and (
                is_read_only_graphql(url, body)
                or is_read_path(url)
                or is_accepted_write(url, self._accepted_writes)
            )
        ):
            if is_accepted_write(url, self._accepted_writes) and not is_read_path(url):
                # **書き込みを通したことは別枠で数える。** 読み取りに混ぜない。
                self._accepted_passed.append(
                    RecordedRequest(method=method, url=url, headers=dict(headers or {}), body=None)
                )
            self._sequence += 1
            self._passed_reads.append(
                RecordedRequest(
                    method=method,
                    url=url,
                    headers=dict(headers or {}),
                    # **本文は残さない** (13.2)。一覧を要求する本文には検索条件が
                    # 載り、そこから個人が絞り込まれうる。
                    body=None,
                    sequence=self._sequence,
                )
            )
            return GateDecision.PASS
        if self._mode is GateMode.BLOCK_THIRD_PARTY and is_own_origin(url, self._own_host):
            # **媒体自身のオリジンは素通し。** 押さないコマンド専用の緩和である
            # (:class:`GateMode` の注記)。通した事実は残す。
            self._sequence += 1
            self._passed_reads.append(
                RecordedRequest(
                    method=method,
                    url=url,
                    headers=dict(headers or {}),
                    # **本文は残さない** (13.2)。一覧の応答を要求する本文には
                    # 検索条件が載り、そこから個人が絞り込まれうる。
                    body=None,
                    sequence=self._sequence,
                )
            )
            return GateDecision.PASS
        if self._mode is GateMode.BLOCK_WRITES and is_read_only_graphql(url, body):
            # **読み取りだけを通す。** 判定は :mod:`recon.graphql` (純粋) が行い、
            # 判定できないものは全て「通さない」側へ倒れる。
            self._sequence += 1
            self._passed_reads.append(
                RecordedRequest(
                    method=method,
                    url=url,
                    headers=dict(headers or {}),
                    # **本文は残さない。** 通した読み取りの本文には媒体の画面に
                    # 出る値が載りうる (13.2)。止めた側と違い解析にも使わない。
                    body=None,
                    sequence=self._sequence,
                )
            )
            return GateDecision.PASS

        self._sequence += 1
        self._recorded.append(
            RecordedRequest(
                method=method,
                url=url,
                # 呼び出し側が使い回す辞書で証拠が書き換わらないよう複製する。
                headers=dict(headers or {}),
                body=body,
                sequence=self._sequence,
            )
        )
        if self._mode in (GateMode.BLOCK_WRITES, GateMode.BLOCK_THIRD_PARTY, GateMode.BLOCK_SEND):
            return GateDecision.RECORD_AND_STUB
        return GateDecision.RECORD_AND_ABORT

    # --- 記録 ---------------------------------------------------------------
    @property
    def mode(self) -> GateMode:
        return self._mode

    @property
    def own_host(self) -> str:
        return self._own_host

    @property
    def recorded(self) -> tuple[RecordedRequest, ...]:
        return tuple(self._recorded)

    @property
    def accepted_writes(self) -> frozenset[str]:
        return self._accepted_writes

    @property
    def accepted_passed(self) -> tuple[RecordedRequest, ...]:
        """Writes that were **knowingly let through**. 報告に必ず出すこと。"""
        return tuple(self._accepted_passed)

    @property
    def passed_reads(self) -> tuple[RecordedRequest, ...]:
        """Reads that were **let through** while armed. 通した事実も観測である。"""
        return tuple(self._passed_reads)

    def clear(self) -> None:
        """Drop the recorded evidence. **The sequence counter keeps going.**"""
        self._recorded.clear()
