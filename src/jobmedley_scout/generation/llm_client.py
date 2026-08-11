"""The Anthropic client wrapper.

8.2 の要点と、それが現行APIでどう解決されるか:

> **LLM APIの仕様変更で本番が静かに全滅しうることを想定してください** (参照実装では
> 拡張思考のパラメータ形式が廃止され、全件が生成失敗しました)。生成失敗はログでは
> なく **集計値として出力** してください。

この事故は現行APIで実際に起きている。``thinking={"type": "enabled",
"budget_tokens": N}`` は claude-sonnet-5 / claude-opus-5 などで **400 で拒否される**。
正しくは ``thinking={"type": "adaptive"}`` + ``output_config={"effort": ...}``。
設定側も ``effort`` を持つ形にしてある (:class:`config.schema.ThinkingEffort`)。

> **拡張思考モードと強制ツール選択は併用できない場合があります。** ツールが呼ばれず
> テキストだけが返ることがあるため、その場合は「思考オフ＋強制ツール選択」で1回だけ
> 取り直すヘルパーを用意してください。**そのヘルパーは、初回生成と修正リトライの
> 両方に通してください。**

構造化出力は ``output_config.format`` (JSONスキーマ) で行う。これは拡張思考と
併用でき、強制ツール選択そのものが不要になるため、上記の非互換は原理的に起きない。
それでも :func:`call_structured` は「構造化出力が得られなかったら思考オフで1回だけ
取り直す」フォールバックを持つ -- API仕様は再び変わりうるし、参照実装が踏んだのは
まさに「変わった」ことだからである。そしてこのヘルパーは **初回生成と修正リトライの
両方が必ず通る唯一の入口** になっている (参照実装は片方にしか入れておらず、2回目も
テキストだけだとクラッシュする潜在バグがあった)。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from jobmedley_scout.config.schema import LlmConfig
from jobmedley_scout.errors import GenerationError

#: 13.1: 1通あたりの最大リクエスト数を明示し、超えたら生成失敗として扱う。
#: リトライとフォールバックと修正リトライの掛け算で最大12まで膨らみ得るため、
#: コストの上振れを構造で止める。
DEFAULT_MAX_REQUESTS_PER_MESSAGE = 6


@dataclass(frozen=True)
class TokenUsage:
    """Token accounting for one request.

    13.1: 「レスポンスの使用トークン数を記録し、**1送信あたりの実コスト** を週次で
    観測できるようにする (参照実装では完全に不可視です)」。
    """

    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    @property
    def billable_input(self) -> int:
        return self.input_tokens + self.cache_creation_input_tokens


@dataclass(frozen=True)
class StructuredResult:
    """A validated structured response, plus how it was obtained."""

    data: Mapping[str, Any]
    usage: TokenUsage
    model: str
    stop_reason: str | None
    #: 思考オフのフォールバックで取り直したか。集計してレポートに出す --
    #: 常時フォールバックしているなら API 側の仕様が変わった合図。
    used_fallback: bool
    request_count: int


class AnthropicLike(Protocol):
    """The slice of the Anthropic SDK this module uses.

    13.4: 「LLMクライアントはモック注入を前提にした設計にし、テストで実APIを
    呼ばない」。Protocol にしてあるので、テストは軽量なフェイクを渡せる。
    """

    @property
    def messages(self) -> Any: ...


def _extract_usage(response: Any) -> TokenUsage:
    usage = getattr(response, "usage", None)
    if usage is None:
        return TokenUsage(0, 0)
    return TokenUsage(
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        cache_read_input_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        cache_creation_input_tokens=int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
    )


def _first_json_object(response: Any) -> Mapping[str, Any] | None:
    """Pull the structured payload out of a response, or ``None``.

    ``None`` は「構造化出力が得られなかった」の合図であり、フォールバックの発火条件。
    例外にしないのは、それが正常な分岐だから。
    """
    content = getattr(response, "content", None) or ()
    for block in content:
        if getattr(block, "type", None) != "text":
            continue
        text = getattr(block, "text", "") or ""
        if not text.strip():
            continue
        try:
            parsed = json.loads(text)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def call_structured(
    client: AnthropicLike,
    *,
    config: LlmConfig,
    system: str,
    messages: Sequence[Mapping[str, Any]],
    schema: Mapping[str, Any],
    max_requests: int = DEFAULT_MAX_REQUESTS_PER_MESSAGE,
) -> StructuredResult:
    """Get a schema-validated object back, retrying once with thinking off.

    **初回生成も修正リトライも、必ずこの関数を通すこと。** 片方だけに入れると、
    2回目がテキストだけを返したときにクラッシュする (参照実装の潜在バグ)。

    ``max_requests`` は 13.1 のコスト上限。超えたら :class:`GenerationError` に
    落とし、集計値として数える。
    """
    if max_requests < 1:
        raise GenerationError(f"max_requests は1以上である必要があります: {max_requests}")

    request_count = 0
    last_usage = TokenUsage(0, 0)

    # --- 第1経路: 拡張思考あり + 構造化出力 -------------------------------
    # output_config.format は拡張思考と併用できるので、強制ツール選択は使わない。
    # 8.2 の非互換はこの設計では原理的に起きない。
    attempts: list[dict[str, Any]] = []
    if config.thinking_enabled:
        attempts.append(
            {
                "thinking": {"type": "adaptive"},
                "output_config": {"effort": str(config.effort), "format": _json_format(schema)},
            }
        )
    # --- 第2経路 (フォールバック): 思考オフで1回だけ取り直す ---------------
    # 「思考オフ＋構造化出力」。API仕様が再び変わって第1経路が構造化出力を
    # 返さなくなっても、ここで拾える。
    attempts.append({"output_config": {"format": _json_format(schema)}})

    for index, extra in enumerate(attempts):
        if request_count >= max_requests:
            break
        request_count += 1
        try:
            response = client.messages.create(
                model=config.model,
                max_tokens=config.max_tokens,
                system=system,
                messages=list(messages),
                **extra,
            )
        except Exception as exc:
            # 最後の経路まで失敗したら生成失敗として集計する。途中なら次の経路へ。
            if index == len(attempts) - 1:
                raise GenerationError(f"LLM呼び出しに失敗しました: {exc}") from exc
            continue

        last_usage = _extract_usage(response)
        stop_reason = getattr(response, "stop_reason", None)

        # 安全性による拒否。content を読む前に必ず確認する。
        if stop_reason == "refusal":
            raise GenerationError(
                "モデルが安全性の理由で応答を拒否しました。候補者データに不適切な"
                "内容が含まれていないか確認してください。"
            )

        data = _first_json_object(response)
        if data is not None:
            return StructuredResult(
                data=data,
                usage=last_usage,
                model=str(getattr(response, "model", config.model)),
                stop_reason=stop_reason,
                used_fallback=index > 0,
                request_count=request_count,
            )
        # 構造化出力が得られなかった -> 次の経路 (思考オフ) で1回だけ取り直す。

    raise GenerationError(
        f"構造化出力を取得できませんでした ({request_count}回試行、"
        f"最終トークン: in={last_usage.input_tokens}/out={last_usage.output_tokens})。"
        f"LLM APIの仕様変更の可能性があります -- 8.2: 本番が静かに全滅しうる経路です。"
    )


def _json_format(schema: Mapping[str, Any]) -> dict[str, Any]:
    return {"type": "json_schema", "schema": dict(schema)}
