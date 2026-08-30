"""**送る引数を、入っているSDKが受け取れることを固定する。**

実測38回目 (preview 2回目) の事故::

    結果: llm_failed
    書き直し: 1 回 / API呼び出し: 2 回
    トークン: in=0 / out=0
    **思考オフのフォールバックが発火しました** (API仕様変更の疑い)
    失敗: StructuredCallError(TypeError)

``in=0 / out=0`` が答えを持っていた。**課金が1トークンも起きていない** --
モデルにもAPIにも届いていない。落ちていたのは SDK の入口で、
``Messages.create() got an unexpected keyword argument 'output_config'`` だった。

pyproject が ``anthropic>=0.40,<0.50`` を指定しており、0.49 には
``output_config`` (構造化出力 + effort) が無い。生成コードは正しい形を書いて
いたのに、**SDK がそこまで運んでくれなかった**。

8.2 が想定していたのは「LLM APIの仕様変更で本番が静かに全滅する」ことだった。
実際に起きたのは **その逆で、SDK がAPIに追いついていない** ほうである。
向きが逆なだけで、結果 (全件生成失敗) は同じである。

**この検査は媒体にもモデルにも接続しない。** 見るのは、入っている SDK の
``messages.create`` が、こちらの送る引数名を知っているかどうかだけである。
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import anthropic
import pytest

LLM_CLIENT = Path("src/jobmedley_scout/generation/llm_client.py")


def _sdk_parameters() -> frozenset[str]:
    """The keyword names the installed SDK's ``messages.create`` accepts."""
    # 鍵は要らない。**送らないので、ダミーで構わない。**
    client = anthropic.Anthropic(api_key="not-used-no-request-is-made")
    return frozenset(inspect.signature(client.messages.create).parameters)


def _kwargs_we_send() -> frozenset[str]:
    """Every keyword ``call_structured`` passes, read out of the source.

    **手で並べない。** 並べると、コードに引数を足したときにこの検査だけが
    古いままになり、守っているつもりで守らない状態になる。
    """
    tree = ast.parse(LLM_CLIENT.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        # client.messages.create(...) の呼び出し
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "create"
        ):
            names.update(kw.arg for kw in node.keywords if kw.arg is not None)
        # **attempts に積んでいる辞書のキーも引数になる** (``**extra``)。
        if isinstance(node, ast.Dict):
            for key in node.keys:
                if (
                    isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                    and key.value in ("thinking", "output_config")
                ):
                    names.add(key.value)
    return frozenset(names)


def test_the_installed_sdk_accepts_every_keyword_we_send() -> None:
    """**入っている SDK が引数を知っていること。** 知らなければ TypeError になる。

    落ちたときに考えるべきは「引数をやめるか」ではなく、
    **「pyproject の anthropic のピンが古すぎないか」** である。
    """
    unknown = sorted(_kwargs_we_send() - _sdk_parameters())
    assert not unknown, (
        f"入っている anthropic {anthropic.__version__} の messages.create が "
        f"{unknown} を知りません。SDK が古すぎます。\n"
        f"生成は output_config (構造化出力 + effort) と "
        f'thinking={{"type": "adaptive"}} を使います。\n'
        f"pyproject.toml の anthropic のピンを上げてください "
        f"(実測38回目: 上限 <0.50 で全件生成失敗)。"
    )


def test_the_structured_output_keyword_is_the_current_one() -> None:
    """``output_format`` は廃止された。**現行は ``output_config.format`` である。**

    どちらも「構造化出力」だが、廃止された側を送っても TypeError にはならず
    **黙って無視されうる** -- 構造化されていない応答が返り、生成が失敗として
    数えられる。名前を固定しておく。
    """
    assert "output_config" in _sdk_parameters()
    source = LLM_CLIENT.read_text(encoding="utf-8")
    assert "output_format" not in source, "廃止された output_format を使っています"


def test_the_thinking_shape_is_adaptive_rather_than_a_token_budget() -> None:
    """8.2 の事故そのもの。**``budget_tokens`` は現行モデルで 400 になる。**

    参照実装はこれで全件失敗した。同じものを書き戻さないよう固定する。
    """
    source = LLM_CLIENT.read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith(("#", '"', "'"))
    )
    assert "budget_tokens" not in code, (
        "拡張思考に budget_tokens を使っています。現行モデルでは 400 で拒否されます "
        '(8.2 の事故そのもの)。thinking={"type": "adaptive"} を使ってください。'
    )


@pytest.mark.parametrize("name", ["model", "max_tokens", "system", "messages"])
def test_the_basic_keywords_are_still_the_ones_the_sdk_takes(name: str) -> None:
    """基本の4つ。**上の検査が「引数を1つも読めていない」状態で通るのを防ぐ。**"""
    assert name in _kwargs_we_send()
    assert name in _sdk_parameters()
