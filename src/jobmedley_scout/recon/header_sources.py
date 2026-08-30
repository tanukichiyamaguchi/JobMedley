"""ブラウザが付ける要求ヘッダの **出所** を、値を出さずに探す。純粋。

実測42回目で、レジュメが読めない理由が確定した。ブラウザは4つのヘッダを付けて
いるが、こちらは ``Content-Type`` しか付けていない::

    x-csrf-token:           (値は伏せています)
    x-customer-user-id:     (値は伏せています)
    x-customer-user-email:  (値は伏せています)
    x-experiment-data:      (値は伏せています)

``x-csrf-token`` が無ければ POST は弾かれ、ログイン画面へ転送される。5万字の
HTML の正体はこれである。

**だが値の出所は分からない。** meta タグかもしれない。埋め込みの JSON かも
しれない。storage かもしれない。**当てにいける。だが当てても、当たったことを
確かめる手段が無い** (原則3)。

だからここは「どこに在りそうか」を **名前だけ** で報告する。値は1文字も出さない
-- ``x-customer-user-email`` は運用者のメールアドレスであり、``x-csrf-token`` は
それだけで POST を通せる鍵である (12.7/13.2)。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

#: 探しているヘッダ。**実測42回目にブラウザが送っていたもののうち、
#: こちらが付けていない4つ。**
WANTED_HEADERS: Final[tuple[str, ...]] = (
    "x-csrf-token",
    "x-customer-user-id",
    "x-customer-user-email",
    "x-experiment-data",
)


#: ヘッダ名から、出所の名前に現れそうな語を作るための対応。
#:
#: **``x-`` を落として区切りを緩める。** ``x-csrf-token`` の出所が
#: ``csrf-token`` (meta) や ``csrfToken`` (JS) と綴られることがあるため。
def _parts(header: str) -> tuple[str, ...]:
    """The header name's segments, without the ``x-`` prefix. **Pure.**"""
    bare = header[2:] if header.lower().startswith("x-") else header
    return tuple(part for part in bare.replace("_", "-").split("-") if part)


def search_terms(header: str) -> tuple[str, ...]:
    """Name fragments that a source for this header might contain. **Pure.**"""
    parts = _parts(header)
    bare = "-".join(parts)
    return tuple(dict.fromkeys((bare, "".join(parts), *parts)))


def looks_like_a_source(name: str, header: str) -> bool:
    """Whether this ``name`` plausibly holds the value for ``header``. **Pure.**

    **最後の部品を必須にする。** 名前の最後がそのものを指すからである --
    ``x-customer-user-id`` と ``x-customer-user-email`` は ``customer`` と
    ``user`` を共有していて、そこだけで当てると互いの候補に混ざる。実際に
    混ざった (``customer_user_email`` が ``...-id`` の候補に出た)。

    そのうえで、前の部品も1つ以上要求する。``id`` や ``token`` はどんなページにも
    在る語なので、最後の部品だけで拾うと報告が候補で埋まる。

    **ここは候補を並べるだけで、決めるのは人間である** (原則3)。
    """
    lowered = name.lower()
    parts = [part.lower() for part in _parts(header)]
    if not parts:
        return False
    if parts[-1] not in lowered:
        return False
    return len(parts) == 1 or any(part in lowered for part in parts[:-1])


@dataclass(frozen=True)
class SourceCandidates:
    """Where each wanted header's value might come from. **名前だけ。**"""

    #: ヘッダ名 -> 出所らしい名前の一覧 (``meta[name=...]`` / storage のキー)。
    by_header: Mapping[str, tuple[str, ...]]
    #: 見た名前の総数。**0件でも書く** -- 探したことと見つからなかったことは違う。
    seen: int

    def unresolved(self) -> tuple[str, ...]:
        """Headers with no candidate at all. **黙って諦めない。**"""
        return tuple(name for name in WANTED_HEADERS if not self.by_header.get(name))


def find_sources(names: Sequence[str]) -> SourceCandidates:
    """Match observed names against the wanted headers. **値は受け取らない。**

    引数が名前だけなので、**この関数は値に触れようがない**。呼び出し側が値を
    渡せない形にしてあるのが、13.2 に対する一番強い保証である。
    """
    by_header = {
        header: tuple(name for name in names if looks_like_a_source(name, header))
        for header in WANTED_HEADERS
    }
    return SourceCandidates(by_header=by_header, seen=len(names))


__all__ = [
    "WANTED_HEADERS",
    "SourceCandidates",
    "find_sources",
    "looks_like_a_source",
    "search_terms",
]
