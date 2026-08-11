"""Candidate ID normalization.

参照実装の事故 (9.3): 同一候補者が取得経路によって2種類の表記で返り、
**分母の水増し・二重送信・照合漏れの3つを同時に** 引き起こした。

対処は4点セットで、どれか1つでも欠けると再発する:

1. **モデル層** に正規化関数を置き、バリデータで全取得経路に強制適用する
   -- 本モジュール。``CandidateId`` は ``Annotated`` 型なので、pydantic を
   通る限りどの取り込み経路も正規化を迂回できない。実装の書き忘れを
   コードレビューではなく型で排除する。
2. 畳むのは **観測されたパターンのみ** -- :data:`BASE_NORMALIZATION_ONLY` を
   参照。既定では表記のみの差 (NFKC・空白) しか畳まない。
3. 既存データには **起動時マイグレーション** -- ``state/migrations/m0002``。
4. 外部と照合する箇所は **両表記を試す** -- :func:`id_representations`。

ジョブメドレーではまだどの表記ゆれも観測していないため、既定のパターン一覧は
空である。**これは未完成ではなく、方針そのもの** -- 観測していないパターンを
推測で畳むと、本来別人である2名を1件に merge するという、表記ゆれより深刻な
事故になる。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator

from jobmedley_scout.errors import ConfigError
from jobmedley_scout.models.text_norm import fold_width, strip_all_ws


class IdPatternKind(StrEnum):
    """Kinds of representation drift that have been *observed* on a platform."""

    STRIP_PREFIX = "strip_prefix"
    STRIP_SUFFIX = "strip_suffix"
    STRIP_SEPARATORS = "strip_separators"
    STRIP_LEADING_ZEROS = "strip_leading_zeros"
    CASEFOLD = "casefold"


@dataclass(frozen=True)
class IdPattern:
    """One observed representation difference, declared in configuration.

    設定ファイルに置くのは、パターンを足すたびにデプロイが要るのを避けるため
    (7.4 の許可リストと同じ理由)。
    """

    name: str
    kind: IdPatternKind
    argument: str = ""


#: 表記のみの差であって実体の差ではない、と断言できる変換だけを常に適用する。
#: これ以外は設定で観測済みと宣言されたものに限る。
BASE_NORMALIZATION_ONLY = "NFKC + 全空白除去のみ (観測済みパターンは設定から注入)"

_ACTIVE_PATTERNS: tuple[IdPattern, ...] = ()


def configure_id_patterns(patterns: tuple[IdPattern, ...]) -> None:
    """Install the observed-drift patterns for this process.

    起動時に設定から一度だけ呼ぶ。プロセスグローバルにしているのは、pydantic の
    バリデータが引数を取れないため -- そしてバリデータに載せることこそが、
    「取り込み経路の書き忘れ」を構造的に排除する唯一の手段だからである。
    """
    global _ACTIVE_PATTERNS
    _ACTIVE_PATTERNS = patterns


def active_id_patterns() -> tuple[IdPattern, ...]:
    return _ACTIVE_PATTERNS


def _apply_pattern(value: str, pattern: IdPattern) -> str:
    if pattern.kind is IdPatternKind.STRIP_PREFIX:
        return value[len(pattern.argument) :] if value.startswith(pattern.argument) else value
    if pattern.kind is IdPatternKind.STRIP_SUFFIX:
        return value[: -len(pattern.argument)] if value.endswith(pattern.argument) else value
    if pattern.kind is IdPatternKind.STRIP_SEPARATORS:
        for separator in pattern.argument:
            value = value.replace(separator, "")
        return value
    if pattern.kind is IdPatternKind.STRIP_LEADING_ZEROS:
        stripped = value.lstrip("0")
        # 全部ゼロだった場合に空文字にしない。
        return stripped if stripped else "0"
    if pattern.kind is IdPatternKind.CASEFOLD:
        return value.casefold()
    raise ConfigError(f"未知のID正規化パターン種別: {pattern.kind!r}")


def normalize_candidate_id(raw: str) -> str:
    """Fold a raw platform ID into its canonical form.

    基底の正規化 (NFKC + 空白除去) は表記のみの差なので常に安全。それ以外は
    :func:`configure_id_patterns` で宣言された観測済みパターンのみを適用する。
    """
    value = strip_all_ws(fold_width(raw))
    if not value:
        raise ValueError("候補者IDが空です。取り込み経路の抽出が失敗しています。")
    for pattern in _ACTIVE_PATTERNS:
        value = _apply_pattern(value, pattern)
    if not value:
        raise ValueError(
            f"候補者ID {raw!r} が正規化の結果 空になりました。"
            f"適用中のパターン: {[p.name for p in _ACTIVE_PATTERNS]}"
        )
    return value


#: Every candidate ID field in every model uses this type. Because the validator
#: is attached to the type rather than called by each ingest path, there is no
#: path that can forget it (9.3 の1点目)。
CandidateId = Annotated[str, AfterValidator(normalize_candidate_id)]


def id_representations(canonical: str) -> tuple[str, ...]:
    """Every representation this ID might appear as on the platform.

    9.3 の4点目: DBは正準形でも、画面は別表記で表示される。外部と照合する箇所は
    逆に **両表記を試す** 必要がある。

    可逆なパターン (接頭辞・接尾辞の除去) だけを復元する。casefold や
    先頭ゼロ除去は情報が落ちていて復元できないため、そちらは ``id_aliases``
    テーブルに実際に観測した表記を蓄積して引き当てる。
    """
    representations = [canonical]
    for pattern in _ACTIVE_PATTERNS:
        if pattern.kind is IdPatternKind.STRIP_PREFIX:
            representations.append(f"{pattern.argument}{canonical}")
        elif pattern.kind is IdPatternKind.STRIP_SUFFIX:
            representations.append(f"{canonical}{pattern.argument}")
    seen: dict[str, None] = {}
    for representation in representations:
        seen.setdefault(representation, None)
    return tuple(seen)
