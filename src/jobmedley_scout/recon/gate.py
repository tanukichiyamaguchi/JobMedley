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

#: 副作用が無いと **確実に分かる** メソッドだけ。武装中でも素通しする。
#: GET を止めると送信画面そのものが描画されず、偵察が成立しない。
#:
#: ここに更新系を足した時点で、このモジュールの存在意義が消える。
#: 中身はテストで固定してある。``OPTIONS`` は仕様上は安全だが入れていない --
#: 「安全そう」ではなく「安全と確実に分かる」ものだけを通す方針のため。
SAFE_METHODS: frozenset[str] = frozenset({"GET", "HEAD"})


class GateDecision(StrEnum):
    PASS = "pass"
    RECORD_AND_ABORT = "record_and_abort"


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

    def __init__(self) -> None:
        # 安全メソッドの集合を **注入可能にしていない**。差し替えられる形にすると、
        # ``SendGate(frozenset({"GET", "POST"}))`` の1行で fail-closed が消える --
        # しかもテストは通ったままになる。この集合は :data:`SAFE_METHODS` 固定で、
        # 変更にはモジュールの編集とレビューを要する。
        self._safe_methods = SAFE_METHODS
        self._armed = False
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
        return GateDecision.RECORD_AND_ABORT

    # --- 記録 ---------------------------------------------------------------
    @property
    def recorded(self) -> tuple[RecordedRequest, ...]:
        return tuple(self._recorded)

    def clear(self) -> None:
        """Drop the recorded evidence. **The sequence counter keeps going.**"""
        self._recorded.clear()
