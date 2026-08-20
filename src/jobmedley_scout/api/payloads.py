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
import re
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
#:
#: **本文の記法は偵察の出力と揃えてある** (:data:`recon.payload_shape.BODY_MARKER`)。
#: 揃っていないと、貼った雛形の本文が差し替わらないまま送信される。
PLACEHOLDER_CANDIDATE_ID = "{{CANDIDATE_ID}}"
PLACEHOLDER_SUBJECT = "{{SUBJECT}}"
PLACEHOLDER_BODY = "{{BODY}}"
PLACEHOLDER_FOLLOWUP_DAYS = "{{FOLLOWUP_DAYS}}"

#: 実測20回目に観測した送信 payload には、この3つが載っていた。
#: **どれも候補者の属性ではない** ので、上の4つの語彙では表せなかった。
#:
#: - 求人ID / 求人の給与ID: どの求人へのスカウトかを指す。運用者が決める値
#: - 検索UUID: **どの検索から辿り着いた候補者か**。一覧APIの応答から持ち出す
#:
#: 語彙を増やしただけでは値は埋まらない。埋め方が決まるまでは
#: :func:`assert_fully_filled` が送信を止める。
PLACEHOLDER_JOB_OFFER_ID = "{{JOB_OFFER_ID}}"
PLACEHOLDER_JOB_OFFER_SALARY_ID = "{{JOB_OFFER_SALARY_ID}}"
PLACEHOLDER_SEARCH_UUID = "{{SEARCH_UUID}}"

#: 偵察が「値が決まっていない」と印した箇所の記法 (``<string>`` / ``<number>`` 等)。
UNFILLED_PATTERN = re.compile(r"^<[^<>]+>$")


def unfilled_slots(payload: object, prefix: str = "") -> tuple[str, ...]:
    """Key paths still holding a placeholder or a kind marker. **Pure.**

    2種類ある。どちらも「まだ値が決まっていない」という同じ事実である。

    - ``{{...}}`` -- 差し込むつもりだったのに差し込まれなかった
    - ``<...>``   -- 偵察が種別だけを記録した箇所。人間がまだ埋めていない
    """
    found: list[str] = []
    if isinstance(payload, str):
        if UNFILLED_PATTERN.match(payload) or (payload.startswith("{{") and payload.endswith("}}")):
            found.append(prefix or "(トップレベル)")
    elif isinstance(payload, Mapping):
        for key, value in payload.items():
            found.extend(unfilled_slots(value, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            found.extend(unfilled_slots(value, f"{prefix}[{index}]"))
    return tuple(found)


def assert_fully_filled(payload: Mapping[str, Any], *, used_by: str) -> None:
    """Refuse a payload that still has unfilled slots. **送信は取り消せない。**

    これが無いと、埋め忘れは **失敗としてではなく成功として** 現れる。
    ``scoutMessage`` に ``"<string>"`` の入ったスカウトが実在の候補者へ飛び、
    月次の送信枠を1通消費し、相手の受信箱に残る。取り消す手段は無い (13.6)。

    「送れなかった」は次の実行でやり直せる。「間違ったものを送った」はやり直せない。
    **だから、迷ったら送らない側に倒す。**
    """
    if remaining := unfilled_slots(payload):
        raise ConfigError(
            f"{used_by}: payload に値の決まっていない箇所が残っています: "
            f"{', '.join(remaining)}。"
            f"座標 api.send.*.payload_template の該当箇所を実際の値、または "
            f"差し込み用の記法 ({PLACEHOLDER_CANDIDATE_ID} 等) に書き換えてください。"
            f"**このまま送ると、記法がそのまま本文や項目として媒体へ渡ります。**"
        )


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
    extra: Mapping[str, object] | None = None,
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
    values.update(extra or {})
    result = _substitute(parsed, values)
    if not isinstance(result, dict):  # pragma: no cover - parse_payload_template guarantees dict
        raise ConfigError(f"{used_by}: payload の組み立て結果がオブジェクトになりませんでした")
    # **差し込み漏れは、ここで止める。** 通してしまうと記法そのものが媒体へ渡り、
    # 取り消せない送信になる (13.6)。
    assert_fully_filled(result, used_by=used_by)
    return result
