"""Provenance: what a value was based on, not who wrote it.

11.2 の要点: 参照実装では、誤検知の取り消し可否を「自動／手動」の区分で判断して
いたため、シート経由で手動扱いになった値を自動側から直せなくなった。

**由来のほうが、後から自己修復に使える。** 「自動が書いた」ではなく
「件名一致で書いた」と記録しておけば、件名一致のロジックに欠陥が見つかったとき、
その根拠で書かれた行だけを一括で取り消せる。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 由来の接頭辞。新しい根拠を足すときは、ここに定数として足してから使うこと。
#: 文字列リテラルを直接書くと、一括取り消しの対象から漏れる。
AUTO_SUBJECT_MATCH = "auto/subject-match"
AUTO_SUBJECT_PREFIX = "auto/subject-prefix35"
AUTO_PLATFORM_FLAG = "auto/platform-flag"
AUTO_PIPELINE = "auto/pipeline"
AUTO_MIGRATION = "auto/migration"
MANUAL_SHEET = "manual/sheet"
MANUAL_CLI = "manual/cli"

ALL_PROVENANCES: tuple[str, ...] = (
    AUTO_SUBJECT_MATCH,
    AUTO_SUBJECT_PREFIX,
    AUTO_PLATFORM_FLAG,
    AUTO_PIPELINE,
    AUTO_MIGRATION,
    MANUAL_SHEET,
    MANUAL_CLI,
)


@dataclass(frozen=True)
class Provenance:
    """A provenance marker plus optional free-text detail."""

    prefix: str
    detail: str = ""

    def render(self) -> str:
        return f"{self.prefix}:{self.detail}" if self.detail else self.prefix

    @staticmethod
    def parse(value: str) -> Provenance:
        prefix, _, detail = value.partition(":")
        return Provenance(prefix=prefix, detail=detail)

    def is_automatic(self) -> bool:
        return self.prefix.startswith("auto/")


def matches_origin(stored: str, prefix: str) -> bool:
    """Whether a stored provenance string came from ``prefix``.

    出自別の一括取り消し (:mod:`jobmedley_scout.analytics.retraction`) が使う。
    """
    return Provenance.parse(stored).prefix == prefix
