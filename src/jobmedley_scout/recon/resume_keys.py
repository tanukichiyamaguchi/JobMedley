"""Resume key-path discovery.

6.4: **同名キーが階層違いで別の意味を持つのが最大の罠。**

> 参照実装では、レジュメのトップレベルにある「業界」「職種」が **経験してきた**
> 業界・職種であり、希望条件のオブジェクト配下にある同名キーが **希望する**
> 業界・職種でした。これを取り違えて「ご希望の◯◯業界」と書き、運用者から
> 「嘘が多い」と指摘されました。

確定の手順:

1. 対象オブジェクトの **キー一覧だけを、初回1回だけログに出す** (値は出さない。
   個人情報を残さないため -- 13.2)
2. 本番を1巡させ、ログから実際のキー名を確定する
3. **確定するまで、そのフィールドは空のままにする**

3が重要。空ならプロンプトに出ず、モデルは言及できない。

本モジュールは 1 を行う。**値は決して出力しない。**
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class KeyPath:
    """One discovered key path, with the *shape* of its value -- never the value."""

    path: str
    value_kind: str
    #: 配列やオブジェクトなら要素数。スカラなら None。値そのものは持たない。
    size: int | None = None

    def render(self) -> str:
        size = f" [{self.size}]" if self.size is not None else ""
        return f"{self.path}: <{self.value_kind}>{size}"


def _kind_of(value: object) -> tuple[str, int | None]:
    if value is None:
        return "null", None
    if isinstance(value, bool):
        return "bool", None
    if isinstance(value, int | float):
        return "number", None
    if isinstance(value, str):
        # **長さすら出さない。** 氏名の文字数だけでも十分に識別情報になりうる。
        return "string", None
    if isinstance(value, Mapping):
        return "object", len(value)
    if isinstance(value, Sequence):
        return "array", len(value)
    return type(value).__name__, None


def discover_key_paths(payload: object, *, max_depth: int = 4) -> tuple[KeyPath, ...]:
    """Walk a resume payload and return its key paths, **values excluded**.

    深さ制限があるのは、深くまで辿ると経路が爆発して読めなくなるため。
    「経験」と「希望」の階層差 (6.4) は浅い層に現れるので4段あれば足りる。
    """
    found: list[KeyPath] = []

    def _walk(node: object, prefix: str, depth: int) -> None:
        if depth > max_depth:
            return
        if isinstance(node, Mapping):
            for key in node:
                path = f"{prefix}.{key}" if prefix else str(key)
                value = node[key]
                kind, size = _kind_of(value)
                found.append(KeyPath(path=path, value_kind=kind, size=size))
                _walk(value, path, depth + 1)
        # 配列は先頭要素の形だけ見る。全要素を辿っても同じ形が並ぶだけ。
        elif isinstance(node, Sequence) and not isinstance(node, str | bytes) and node:
            _walk(node[0], f"{prefix}[]", depth + 1)

    _walk(payload, "", 0)
    return tuple(found)


def render_report(paths: tuple[KeyPath, ...]) -> str:
    """A copy-paste-ready report keyed to the coordinate names.

    出力は座標キー名に紐づけてある (:mod:`recon.report` と同じ方針) ので、
    運用者は config/site_coordinates.yaml へ転記するだけで済む。
    """
    lines = [
        "レジュメのキーパス一覧 (**値は含まれていません** -- 13.2)",
        "",
        "6.4 の注意: 同名キーが階層違いで別の意味を持つことがあります。",
        "トップレベルの「業界/職種」が *経験* で、希望条件オブジェクト配下の同名キーが",
        "*希望* だった、というのが参照実装で虚偽文面を生んだ原因です。",
        "**どちらか判断できない項目は UNRESOLVED のままにしてください。**",
        "空ならプロンプトに出ず、モデルは言及できません。",
        "",
    ]
    lines.extend(f"  {path.render()}" for path in paths)
    lines.extend(
        [
            "",
            "転記先の座標 (config/site_coordinates.yaml):",
            "  resume.fields.experienced_industries   <- **経験してきた** 業界",
            "  resume.fields.experienced_occupations  <- **経験してきた** 職種",
            "  resume.fields.desired_industries       <- **希望する** 業界",
            "  resume.fields.desired_occupations      <- **希望する** 職種",
            "  resume.fields.employments / educations / language_text / age /",
            "  resume.fields.membership_status / specialty / summary",
        ]
    )
    return "\n".join(lines)
