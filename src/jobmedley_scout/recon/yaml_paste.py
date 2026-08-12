"""Render a Python value as a YAML scalar the operator can paste verbatim.

段階1 (``observe_login``) と段階2 (``observe_list``) の両方が、観測した値を
``config/site_coordinates.yaml`` へそのまま貼れる形で印字する。**同じ危険を
2箇所で別々に踏まないよう、ここへ集約する。**
"""

from __future__ import annotations

import json


def yaml_scalar(value: object) -> str:
    """Render ``value`` as a YAML scalar.

    **``f'"{value}"'`` で囲んではいけない。** セレクタは
    ``input[name="customer[email]"]`` のように二重引用符を含むのが普通で、
    素朴に囲むと入れ子になって YAML として壊れる。貼り付けた運用者は、原因の
    分からない設定エラーを受け取ることになる。

    YAML 1.2 は JSON の上位互換なので、:func:`json.dumps` の出力はそのまま
    正しい YAML スカラ (およびフロー列) になる。エスケープ規則を自前で書かない。
    """
    return json.dumps(value, ensure_ascii=False)
