"""Post-login context establishment.

5.6 の事故:

> 参照実装の媒体では、1アカウントが複数の採用グループに紐づいており、**グループを
> 選択しないと検索もスカウトもできませんでした。**
>
> 保存セッションで入った場合も選択が外れている可能性があるため、**ログインの有無に
> かかわらず毎回実行してください。** 選択肢がない場合は「選択済みまたは不要」として
> 続行し、例外で止めないでください。

ジョブメドレーに同種のステップがあるかは未確定 (座標 ``context.selection_required``)。
4章の判定方法: 手動ログイン直後に目的画面へ直接URL遷移してデータが返るか。返らなければ
選択ステップが挟まっている。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from jobmedley_scout.browser.waits import pause
from jobmedley_scout.config.placeholders import Coord, is_resolved, require
from jobmedley_scout.config.schema import WaitsConfig


class ContextOutcome(StrEnum):
    SELECTED = "selected"
    ALREADY_SET_OR_UNNECESSARY = "already_set_or_unnecessary"
    NOT_REQUIRED = "not_required"
    UNKNOWN_COORDINATE = "unknown_coordinate"


@dataclass(frozen=True)
class ContextResult:
    outcome: ContextOutcome
    detail: str


def establish_context(
    page: Any,
    *,
    selection_required: Coord[bool],
    selector: Coord[str | None],
    waits: WaitsConfig,
) -> ContextResult:
    """Establish any post-login selection. **Runs every time, never raises.**

    保存セッションで入った場合も選択が外れている可能性があるので、ログインの有無に
    かかわらず毎回呼ぶ。失敗しても例外にしない -- 選択が不要な媒体で止まってしまう。
    """
    if not is_resolved(selection_required):
        # 座標未確定。何もせず続行する。ここで例外にすると、まだ調査していない
        # という理由だけで偵察コマンドすら走らなくなる。
        return ContextResult(
            ContextOutcome.UNKNOWN_COORDINATE,
            "context.selection_required が未確定のため、追加コンテキストの確立は行わなかった",
        )

    if not require(selection_required, used_by="browser.context_setup.establish_context"):
        return ContextResult(ContextOutcome.NOT_REQUIRED, "この媒体では選択ステップは不要")

    if not is_resolved(selector):
        return ContextResult(
            ContextOutcome.UNKNOWN_COORDINATE,
            "選択が必要だが context.selector が未確定",
        )
    control = require(selector, used_by="browser.context_setup.establish_context")
    if control is None:
        return ContextResult(ContextOutcome.NOT_REQUIRED, "選択コントロールは存在しないと確認済み")

    try:
        element = page.query_selector(control)
        if element is None:
            # 選択肢がない場合は「選択済みまたは不要」として続行する (5.6)。
            return ContextResult(
                ContextOutcome.ALREADY_SET_OR_UNNECESSARY,
                "選択コントロールが見つからない (選択済みか、この画面では不要)",
            )
        element.click()
        pause(waits.between_actions)
    except Exception as exc:
        # 例外で止めない (5.6)。確立できなかった事実だけ返す。
        return ContextResult(
            ContextOutcome.ALREADY_SET_OR_UNNECESSARY,
            f"選択を試みたが完了を確認できなかった: {exc}",
        )
    return ContextResult(ContextOutcome.SELECTED, "追加コンテキストを選択した")
