"""Mechanical checks on the message that will actually be sent.

8.5 と 8.7 の要点は検査対象である。**検証は LLM の出力ではなく、コードが
組み立て終わった最終本文に掛ける。** 中核要素だけを見て通すと、システム自身が
足した定型文・署名・フッターが検証されないまま外に出る。

そして 8.5 のもう一つの教訓: 参照実装は自前のフッターに含まれる ◎ を絵文字と
判定し、**全メッセージで修正リトライを走らせていた。** 自分の装飾で自分の
検査を踏むのは、検査が緩いより質が悪い -- 毎回コストと遅延を払ったうえで、
本物の違反が「いつもの1件」に埋もれる。除外する記号は
:data:`generation.assemble.SYSTEM_GLYPHS` から取り、検査側で列挙し直さない。

修正リトライは :data:`MAX_CORRECTION_RETRIES` 回だけ。呼び出し側ごとに
「もう1回だけ」を足していくと、事実上の無限ループとコスト事故になる。
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict

from jobmedley_scout.config.schema import GenerationConfig
from jobmedley_scout.generation.assemble import SYSTEM_GLYPHS
from jobmedley_scout.generation.subject import (
    MIN_SUBJECT_NORM_LENGTH,
    validate_subject_format,
)
from jobmedley_scout.models.message import AssembledMessage
from jobmedley_scout.models.text_norm import fold_width, normalize_subject

_STRICT = ConfigDict(extra="forbid", frozen=True)

#: 修正リトライの上限。呼び出し側はこの回数だけ再生成してよい (8.5)。
MAX_CORRECTION_RETRIES: Final[int] = 1

#: 前後に付ける抜粋の文字数。違反箇所を人間が読める形でレポートに出すため。
_EXCERPT_RADIUS: Final[int] = 15

#: 絵文字として扱う符号位置の範囲。
#:
#: 装飾記号のブロック (矢印・罫線・幾何学模様) まで含めて広めに取っている。
#: メーラーによってはこれらを絵文字として色付きで描画するため、ビジネス文面
#: としては同じ問題になるからである。代償として **システム自身の装飾記号も
#: 引っかかる** ので、:data:`generation.assemble.SYSTEM_GLYPHS` を明示的に
#: 除外する (8.5 の自爆リトライ対策)。
#:
#: 丸数字 (①) や 〒・※ は意図的に範囲外にしてある -- 日本語のビジネス文面で
#: 正当に使われる記号を弾くと、運用者が検査自体を切りたくなる。
_SYMBOL_RANGES: Final[tuple[tuple[int, int], ...]] = (
    (0x2190, 0x21FF),  # 矢印
    (0x2300, 0x23FF),  # 技術記号 (⌚ ⏰ など)
    (0x2500, 0x25FF),  # 罫線・幾何学模様 (─ ◎ ■ ▼)
    (0x2600, 0x27BF),  # その他の記号・装飾記号 (☀ ★ ✅ ❤)
    (0x2B00, 0x2BFF),  # 追加の矢印・星
    (0xFE0F, 0xFE0F),  # 異体字セレクタ-16 (直前の文字を絵文字表示にする)
    (0x1F000, 0x1FAFF),  # 絵文字本体
)

#: スキームか ``www.`` が付いたものだけを URL と見なす。**素のドメイン表記を
#: 拾いに行かないこと** -- 署名のメールアドレス (scout@example.co.jp) が URL と
#: して誤検知され、全メッセージが違反になる。自分の署名で自分の検査を踏むのは
#: 8.5 のフッター絵文字と同じ自爆であり、そちらの方が実害が大きい。
_URL_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:https?://|www\.)[\w\-.~:/?#\[\]@!$&'()*+,;=%]+",
    re.IGNORECASE,
)


class ViolationKind(StrEnum):
    """What kind of rule the final message broke."""

    #: 共通点が無いのに断定語を使った (8.3 対策4)。
    ASSERTIVE_WITHOUT_COMMONALITY = "assertive_without_commonality"
    TOO_MANY_EXCLAMATIONS = "too_many_exclamations"
    EMOJI = "emoji"
    #: 許可リストに無いURL (8.7)。
    URL_NOT_ALLOWED = "url_not_allowed"
    SUBJECT_FORMAT = "subject_format"
    SUBJECT_TOO_SHORT = "subject_too_short"
    #: 保存される突合キーが件名と一致しない (10.2)。返信が永久に検知不能になる。
    SUBJECT_KEY_MISMATCH = "subject_key_mismatch"


class Violation(BaseModel):
    """One rule break, with enough context for a human to judge it."""

    model_config = _STRICT

    kind: ViolationKind
    #: 運用者向けの説明 (日本語)。
    detail: str
    #: 実際に引っかかった文字列。
    evidence: str


def _excerpt(text: str, index: int, length: int) -> str:
    """The offending fragment with a little context around it."""
    start = max(0, index - _EXCERPT_RADIUS)
    end = min(len(text), index + length + _EXCERPT_RADIUS)
    fragment = text[start:end].replace("\n", " ")
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{fragment}{suffix}"


def _is_symbol(char: str) -> bool:
    code = ord(char)
    return any(start <= code <= end for start, end in _SYMBOL_RANGES)


def _mask_allowed_terms(text: str, allowed: tuple[str, ...]) -> str:
    """Blank out the sanctioned soft terms before scanning for assertive ones.

    「近い」「近しい」は断定しないラベルとして許容されている。断定語がその
    内側に現れる語を将来足したときに誤検知しないよう、先に伏せておく。
    長さを保った伏せ字にしているのは、抜粋の位置がずれないようにするため。
    """
    masked = text
    for term in allowed:
        if term:
            masked = masked.replace(term, "〇" * len(term))
    return masked


def _host_of(url: str) -> str:
    without_scheme = re.sub(r"\Ahttps?://", "", url, flags=re.IGNORECASE)
    host = re.split(r"[/?#]", without_scheme, maxsplit=1)[0]
    host = host.rpartition("@")[2]
    host = host.split(":")[0]
    return host.casefold().removeprefix("www.")


def _url_allowed(url: str, allowlist: tuple[str, ...]) -> bool:
    host = _host_of(url)
    if not host:
        return False
    for entry in allowlist:
        allowed = entry.casefold().strip().removeprefix("www.")
        if not allowed:
            continue
        # サブドメインは許可する (customers.job-medley.com)。末尾に "." を
        # 付けて比較しているのは job-medley.com.evil.example を通さないため。
        if host == allowed or host.endswith(f".{allowed}"):
            return True
    return False


def _check_assertive_terms(
    text: str, cfg: GenerationConfig, had_commonality: bool
) -> list[Violation]:
    """8.3 対策4: 共通点が無い相手に断定語を使わせない。

    ``had_commonality`` には **断定できる共通点があるか** を渡すこと
    (:func:`generation.matching.has_confident_commonality`)。緩い一致しか
    無いのに True を渡すと、この検査は素通りする。
    """
    if had_commonality:
        return []
    masked = _mask_allowed_terms(text, cfg.allowed_soft_terms)
    violations: list[Violation] = []
    for term in cfg.assertive_terms:
        if not term:
            continue
        index = masked.find(term)
        if index < 0:
            continue
        violations.append(
            Violation(
                kind=ViolationKind.ASSERTIVE_WITHOUT_COMMONALITY,
                detail=(
                    f"共通点が無い相手に断定語「{term}」を使っています。"
                    "断定しない語 (「近い」など) に置き換えてください。"
                ),
                evidence=_excerpt(text, index, len(term)),
            )
        )
    return violations


def _check_exclamations(text: str, cfg: GenerationConfig) -> list[Violation]:
    # 全角の「！」も数える。fold_width で半角に畳んでから数えれば取りこぼさない。
    count = fold_width(text).count("!")
    if count <= cfg.max_exclamation_marks:
        return []
    return [
        Violation(
            kind=ViolationKind.TOO_MANY_EXCLAMATIONS,
            detail=(
                f"感嘆符が {count} 個あります (上限 {cfg.max_exclamation_marks} 個)。"
                "ビジネス文面として過剰です。"
            ),
            evidence=f"! × {count}",
        )
    ]


def _check_emoji(text: str) -> list[Violation]:
    """Flag pictographs, but never the decorations this system adds itself.

    ``SYSTEM_GLYPHS`` の除外を外すと、フッターの ◎ と罫線と矢印で全メッセージが
    違反になり、毎回修正リトライが走る (8.5 で実際に起きた)。
    """
    violations: list[Violation] = []
    seen: set[str] = set()
    for index, char in enumerate(text):
        if char in SYSTEM_GLYPHS or char in seen:
            continue
        if not _is_symbol(char):
            continue
        seen.add(char)
        violations.append(
            Violation(
                kind=ViolationKind.EMOJI,
                detail=(
                    f"絵文字・装飾記号 {char!r} (U+{ord(char):04X}) が含まれています。"
                    "システムが付ける装飾記号以外は使えません。"
                ),
                evidence=_excerpt(text, index, 1),
            )
        )
    return violations


def _check_urls(text: str, cfg: GenerationConfig) -> list[Violation]:
    violations: list[Violation] = []
    for match in _URL_RE.finditer(text):
        url = match.group(0).rstrip(".,;:)）」】")
        if _url_allowed(url, cfg.url_allowlist):
            continue
        violations.append(
            Violation(
                kind=ViolationKind.URL_NOT_ALLOWED,
                detail=(
                    f"許可リストに無いURL ({_host_of(url) or url}) が本文にあります。"
                    f"許可: {', '.join(cfg.url_allowlist) or '(なし)'}"
                ),
                evidence=url,
            )
        )
    return violations


def _check_subject(message: AssembledMessage) -> list[Violation]:
    violations: list[Violation] = []
    if not validate_subject_format(message.subject):
        violations.append(
            Violation(
                kind=ViolationKind.SUBJECT_FORMAT,
                detail="件名が想定の形式 (<氏名>様｜<件名>｜<月>/<日>) ではありません。",
                evidence=message.subject,
            )
        )
    recomputed = normalize_subject(message.subject)
    if len(recomputed) < MIN_SUBJECT_NORM_LENGTH:
        violations.append(
            Violation(
                kind=ViolationKind.SUBJECT_TOO_SHORT,
                detail=(
                    f"件名が正規化後 {len(recomputed)} 文字しかありません "
                    f"(下限 {MIN_SUBJECT_NORM_LENGTH} 文字)。返信の突合キーとして"
                    "使えません (10.2)。"
                ),
                evidence=message.subject,
            )
        )
    # 保存される突合キーを件名から作り直して突き合わせる。ここがずれたまま
    # 送信すると、その対象の返信は恒久的に検知できなくなる (10.2, 13.3)。
    if recomputed != message.subject_norm:
        violations.append(
            Violation(
                kind=ViolationKind.SUBJECT_KEY_MISMATCH,
                detail=(
                    "保存予定の subject_norm が件名から再計算した値と一致しません。"
                    "この状態で送ると返信を検知できません。"
                ),
                evidence=f"{message.subject_norm!r} != {recomputed!r}",
            )
        )
    elif not recomputed.startswith(message.subject_prefix35):
        violations.append(
            Violation(
                kind=ViolationKind.SUBJECT_KEY_MISMATCH,
                detail="保存予定の subject_prefix35 が件名の前方一致キーになっていません。",
                evidence=f"{message.subject_prefix35!r} ⊄ {recomputed!r}",
            )
        )
    return violations


def validate(
    message: AssembledMessage, cfg: GenerationConfig, had_commonality: bool
) -> tuple[Violation, ...]:
    """Check the assembled message. Empty result means it may be sent.

    件名と本文の両方を検査対象にする。件名は LLM が書いた文字列を含むので、
    本文だけを見ると断定語や絵文字を見逃す。
    """
    text = f"{message.subject}\n{message.body}"
    violations: list[Violation] = []
    violations.extend(_check_assertive_terms(text, cfg, had_commonality))
    violations.extend(_check_exclamations(text, cfg))
    violations.extend(_check_emoji(text))
    violations.extend(_check_urls(text, cfg))
    violations.extend(_check_subject(message))
    return tuple(violations)
