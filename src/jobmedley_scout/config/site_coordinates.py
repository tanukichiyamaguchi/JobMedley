"""Typed access to site coordinates.

アクセサが種別ごとに分かれているのは、``Coord[T]`` の ``T`` を具体化して
mypy strict に仕事をさせるため。``coords.url(...)`` は ``Coord[str]`` を返すので、
``str`` を要求する引数へそのまま渡すと **型検査で落ちる**。値を取り出す唯一の
手段は :func:`config.placeholders.require` である。

nullable な座標 (「確認した結果 存在しなかった」が正当な答えになりうるもの) は
``optional_*`` 系のアクセサを使い、``Coord[str | None]`` を返す。``None`` は
確定した答えであって未確定ではない -- この区別が本システムの前提である。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import NoReturn

from jobmedley_scout.config.coordinates import COORDINATES_BY_KEY, CoordKind
from jobmedley_scout.config.placeholders import Coord, Unresolved
from jobmedley_scout.errors import ConfigError


class SiteCoordinates:
    """All site coordinates, each either a parsed value or :class:`Unresolved`."""

    def __init__(self, values: Mapping[str, object]) -> None:
        self._values = dict(values)

    def _fetch(self, key: str, expected: CoordKind) -> object:
        spec = COORDINATES_BY_KEY.get(key)
        if spec is None:
            raise ConfigError(f"未登録の座標キー '{key}' が参照されました (coordinates.py に無い)")
        if spec.kind is not expected:
            raise ConfigError(
                f"座標 '{key}' の種別は {spec.kind} ですが {expected} として読まれました"
            )
        if key not in self._values:
            raise ConfigError(f"座標 '{key}' が読み込まれていません")
        return self._values[key]

    # --- 必須の座標 (null を取らない) ------------------------------------
    def url(self, key: str) -> Coord[str]:
        value = self._fetch(key, CoordKind.URL)
        return value if isinstance(value, Unresolved | str) else self._bad(key, value)

    def selector(self, key: str) -> Coord[str]:
        value = self._fetch(key, CoordKind.SELECTOR)
        return value if isinstance(value, Unresolved | str) else self._bad(key, value)

    def string(self, key: str) -> Coord[str]:
        value = self._fetch(key, CoordKind.STRING)
        return value if isinstance(value, Unresolved | str) else self._bad(key, value)

    def json_path(self, key: str) -> Coord[str]:
        value = self._fetch(key, CoordKind.JSON_PATH)
        return value if isinstance(value, Unresolved | str) else self._bad(key, value)

    def boolean(self, key: str) -> Coord[bool]:
        value = self._fetch(key, CoordKind.BOOL)
        return value if isinstance(value, Unresolved | bool) else self._bad(key, value)

    def integer(self, key: str) -> Coord[int]:
        value = self._fetch(key, CoordKind.INT)
        return value if isinstance(value, Unresolved | int) else self._bad(key, value)

    def status_set(self, key: str) -> Coord[frozenset[int]]:
        value = self._fetch(key, CoordKind.STATUS_SET)
        return value if isinstance(value, Unresolved | frozenset) else self._bad(key, value)

    def string_list(self, key: str) -> Coord[tuple[str, ...]]:
        value = self._fetch(key, CoordKind.STRING_LIST)
        return value if isinstance(value, Unresolved | tuple) else self._bad(key, value)

    def enum_map(self, key: str) -> Coord[dict[str, str]]:
        value = self._fetch(key, CoordKind.ENUM_MAP)
        return value if isinstance(value, Unresolved | dict) else self._bad(key, value)

    # --- nullable な座標 (None は「確認した結果 存在しない」) --------------
    def optional_url(self, key: str) -> Coord[str | None]:
        value = self._fetch(key, CoordKind.URL)
        return (
            value if value is None or isinstance(value, Unresolved | str) else self._bad(key, value)
        )

    def optional_selector(self, key: str) -> Coord[str | None]:
        value = self._fetch(key, CoordKind.SELECTOR)
        return (
            value if value is None or isinstance(value, Unresolved | str) else self._bad(key, value)
        )

    def optional_string(self, key: str) -> Coord[str | None]:
        value = self._fetch(key, CoordKind.STRING)
        return (
            value if value is None or isinstance(value, Unresolved | str) else self._bad(key, value)
        )

    def optional_json_path(self, key: str) -> Coord[str | None]:
        value = self._fetch(key, CoordKind.JSON_PATH)
        return (
            value if value is None or isinstance(value, Unresolved | str) else self._bad(key, value)
        )

    def optional_status_set(self, key: str) -> Coord[frozenset[int] | None]:
        value = self._fetch(key, CoordKind.STATUS_SET)
        if value is None or isinstance(value, Unresolved | frozenset):
            return value
        return self._bad(key, value)

    def optional_string_list(self, key: str) -> Coord[tuple[str, ...] | None]:
        value = self._fetch(key, CoordKind.STRING_LIST)
        if value is None or isinstance(value, Unresolved | tuple):
            return value
        return self._bad(key, value)

    # --- 監査 --------------------------------------------------------------
    def is_unresolved(self, key: str) -> bool:
        return isinstance(self._values.get(key), Unresolved)

    def unresolved_keys(self) -> tuple[str, ...]:
        return tuple(sorted(k for k, v in self._values.items() if isinstance(v, Unresolved)))

    def resolved_keys(self) -> tuple[str, ...]:
        return tuple(sorted(k for k, v in self._values.items() if not isinstance(v, Unresolved)))

    def raw_items(self) -> Mapping[str, object]:
        return dict(self._values)

    @staticmethod
    def _bad(key: str, value: object) -> NoReturn:  # pragma: no cover - defensive
        raise ConfigError(f"座標 '{key}' の値の型が不正です: {type(value).__name__}")
