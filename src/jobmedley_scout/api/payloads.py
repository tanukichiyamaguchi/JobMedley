"""Request payload construction, with entrance guards.

6.7 の事故:

> 参照実装では、追客の設定を「nullまたはオブジェクト」で渡す仕様のところに
> **文字列を渡してしまい、型不一致で送信が全滅しました。**

> APIクライアントの入口に「想定型でなければ安全なデフォルト (null) に落とす」
> 処理を入れてください。**上位のバグが送信全滅に育つのを防げます。**

payload の形状そのものは段階3の偵察で確定する座標 (``api.send.*.payload_template``)
なので、ここでは「雛形に値を差し込む」ことと「危険な型を入口で潰す」ことだけを行う。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from jobmedley_scout.config.placeholders import Coord, require
from jobmedley_scout.errors import ConfigError

#: 追客日数として媒体が受け付ける値。想定外は既定値に丸める (6.7)。
#: 実値は座標 ``followup.allowed_days`` で確定するまで暫定。
DEFAULT_FOLLOWUP_DAYS = 5


def guard_optional_object(name: str, value: object) -> Mapping[str, object] | None:
    """Coerce a ``null | object`` parameter, refusing anything else.

    **文字列を渡されたら None に落とす。** 上位のバグで文字列が来たとき、送信を
    全滅させるより「追客なしで送る」ほうが被害が小さい。落としたことは呼び出し側で
    警告として記録すること。
    """
    if value is None:
        return None
    if isinstance(value, Mapping):
        return dict(value)
    # ここに来るのは上位のバグ。安全側 (None) に落とす。
    return None


def coerce_followup_days(value: object, allowed: tuple[int, ...]) -> int:
    """Round an arbitrary value onto one of the platform's accepted day counts.

    参照実装の追客日数は自由な数値ではなく3日・5日・10日の3値のみだった。
    想定外の値は既定値に丸める (6.7)。
    """
    if not allowed:
        return DEFAULT_FOLLOWUP_DAYS
    if isinstance(value, bool) or not isinstance(value, int):
        return allowed[0]
    return value if value in allowed else allowed[0]


def parse_payload_template(template: Coord[str | None], *, used_by: str) -> dict[str, Any]:
    """Parse a payload template coordinate into a mutable dict.

    雛形は偵察が記録した **実リクエストボディそのもの** を想定している。
    JSONとして読めなければ設定エラーにする -- 推測で補わない。
    """
    raw = require(template, used_by=used_by)
    if raw is None:
        raise ConfigError(f"{used_by}: payload雛形が null です。この送信枠は使用できません。")
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise ConfigError(
            f"{used_by}: payload雛形をJSONとして解釈できません。"
            f"偵察が記録した実リクエストボディをそのまま貼り付けてください: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise ConfigError(f"{used_by}: payload雛形のトップレベルはオブジェクトである必要があります")
    return parsed


#: 雛形の中でこの記法が現れたら、実際の値に差し替える。
#: 偵察が記録した実ボディの該当箇所を、手でこの記法に書き換えて使う。
PLACEHOLDER_CANDIDATE_ID = "{{CANDIDATE_ID}}"
PLACEHOLDER_SUBJECT = "{{SUBJECT}}"
PLACEHOLDER_BODY = "{{BODY}}"
PLACEHOLDER_FOLLOWUP_DAYS = "{{FOLLOWUP_DAYS}}"


def _substitute(node: object, values: Mapping[str, object]) -> object:
    if isinstance(node, str):
        # 値そのものが丸ごとプレースホルダなら、型を保ったまま差し替える
        # (IDが数値の枠と文字列の枠が両方ありうるため)。
        if node in values:
            return values[node]
        return node
    if isinstance(node, dict):
        return {key: _substitute(value, values) for key, value in node.items()}
    if isinstance(node, list):
        return [_substitute(item, values) for item in node]
    return node


def build_send_payload(
    template: Coord[str | None],
    *,
    candidate_id: str | int,
    subject: str,
    body: str,
    followup_days: int | None,
    used_by: str,
) -> dict[str, Any]:
    """Fill a send payload template.

    6.2: payload の形状は枠によって違う (IDが配列か単数か、トークンが必要か)。
    だからこそ雛形を座標にしてあり、コードは差し込みしかしない。
    """
    parsed = parse_payload_template(template, used_by=used_by)
    values: dict[str, object] = {
        PLACEHOLDER_CANDIDATE_ID: candidate_id,
        PLACEHOLDER_SUBJECT: subject,
        PLACEHOLDER_BODY: body,
        PLACEHOLDER_FOLLOWUP_DAYS: followup_days,
    }
    result = _substitute(parsed, values)
    if not isinstance(result, dict):  # pragma: no cover - parse_payload_template guarantees dict
        raise ConfigError(f"{used_by}: payload の組み立て結果がオブジェクトになりませんでした")
    return result
