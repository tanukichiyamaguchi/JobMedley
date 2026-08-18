"""段階3の別経路: **配信ファイルを読むだけ** で送信APIの形を確定する。

なぜこの経路を足したのか (経緯と根拠は :mod:`recon.bundle` の冒頭)。

**このコマンドが行う通信は GET だけである。** ボタンを1つも押さない。だから
送信は「遮断したから起きない」のではなく **起こす操作が存在しない**。それでも
遮断は仕掛けて武装しておく -- 万一どこかが非GETを出したら、それは想定外なので
止めて記録するのが正しい (fail-closed の考え方はここでも同じ)。

ブラウザ依存部はここに閉じ込め、判断は :mod:`recon.bundle` (純粋) に置く (13.4)。
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from jobmedley_scout.browser import session_store
from jobmedley_scout.browser.context import browser_context
from jobmedley_scout.config.placeholders import UNRESOLVED_TOKEN, Coord, require
from jobmedley_scout.config.schema import BrowserConfig
from jobmedley_scout.recon.bundle import (
    GraphQLOperation,
    merge_operations,
    operations_in,
    rank_send_operations,
    script_urls,
    send_url_pattern,
)
from jobmedley_scout.recon.capture_send import install_gate
from jobmedley_scout.recon.gate import SendGate
from jobmedley_scout.recon.yaml_paste import yaml_scalar as _scalar

#: このコマンドが埋めうる座標キー。
READ_BUNDLE_KEYS: tuple[str, ...] = ("api.send.paid.url_pattern",)

#: 読みに行く配信ファイルの上限。**黙って打ち切らない** -- 超えた分は報告に出す。
MAX_SCRIPTS = 40

#: 1ファイルあたりに読む最大文字数。巨大なバンドルで実行が止まらないように。
#: 超えた場合は「切り詰めた」ことを報告する (静かに取り逃がさない)。
MAX_SOURCE_CHARS = 8_000_000


@dataclass(frozen=True)
class BundleObservation:
    """The whole run, in the shape the report needs."""

    requested_url: str
    origin: str = ""
    session_present: bool = True
    #: 素のHTMLを取れたか。**False なら以降は何も読めていない。**
    html_read: bool = False
    #: HTML から見つけた script の数。
    scripts_found: int = 0
    #: 実際に読めた script の数。
    scripts_read: int = 0
    #: 上限で読まなかった数。**0 でなければ報告に出す** (静かな取りこぼしを作らない)。
    scripts_skipped: int = 0
    #: 大きすぎて切り詰めたファイルの数。
    scripts_truncated: int = 0
    operations: tuple[GraphQLOperation, ...] = ()
    #: 武装中に遮断された非GET。**GETしかしない設計なので、ここは空が正常。**
    blocked_non_get: int = 0
    note: str = ""

    def mutations(self) -> tuple[GraphQLOperation, ...]:
        return tuple(op for op in self.operations if op.is_mutation)

    def send_candidates(self) -> tuple[GraphQLOperation, ...]:
        """送信を名乗る mutation。**名前と種別の両方で絞る** (bundle の docstring)。"""
        return tuple(op for op in rank_send_operations(self.operations) if op.looks_like_send())

    def render(self) -> str:
        lines = ["段階3: 配信ファイルから読んだ GraphQL の操作", ""]

        if not self.session_present:
            lines.append("  保存セッションがありません。シークレットを設定してください。")
            return "\n".join(lines)
        if not self.html_read:
            lines.append("  **HTMLを取得できなかったため、配信ファイルを1つも読んでいません。**")
            lines.append(f"  {self.note or '理由は記録されていません。'}")
            return "\n".join(lines)

        lines.append(f"  script: {self.scripts_found} 個中 {self.scripts_read} 個を読みました")
        if self.scripts_skipped:
            lines.append(
                f"    **上限で読まなかった: {self.scripts_skipped} 個** (見落としの可能性)"
            )
        if self.scripts_truncated:
            lines.append(f"    大きすぎて切り詰めた: {self.scripts_truncated} 個")
        mutation_count = len(self.mutations())
        lines.append(
            f"  見つかった操作: {len(self.operations)} 個 (うち mutation {mutation_count} 個)"
        )
        if self.blocked_non_get:
            # GETしかしない設計なので、ここが0でないこと自体が観測である。
            lines.append(
                f"  **想定外: 非GETを {self.blocked_non_get} 件遮断しました** (GETのみの設計)"
            )
        lines.append("")

        candidates = self.send_candidates()
        if candidates:
            lines.append("  送信を名乗る mutation (有力な順):")
            for operation in candidates[:8]:
                lines.append(f"    {operation.signature()}")
        else:
            lines.append("  送信を名乗る mutation は見つかりませんでした。")
            others = [op for op in rank_send_operations(self.operations) if op.is_mutation]
            if others:
                lines.append("  参考: 見つかった mutation (名前順):")
                for operation in others[:12]:
                    lines.append(f"    {operation.signature()}")
        lines.append("")
        lines.extend(self._coordinate_lines(candidates))
        lines.append("")
        lines.append("**このコマンドは GET しか行っていません。** ボタンを1つも押さないので、")
        lines.append("送信は起こす操作そのものが存在しません。印字したのは操作の種別・名前・")
        lines.append("変数の型だけで、配信ファイルの原文は出していません (13.2)。")
        return "\n".join(lines)

    def _coordinate_lines(self, candidates: tuple[GraphQLOperation, ...]) -> list[str]:
        out = ["config/site_coordinates.yaml の該当行:", ""]
        if len(candidates) == 1 and self.origin:
            url = send_url_pattern(self.origin, candidates[0])
            out.append(f"  api.send.paid.url_pattern: {_scalar(url)}")
            out.append("    # 配信ファイルの中で、送信を名乗る mutation はこれ1つでした。")
            out.append(f"    # 変数の形: {candidates[0].signature()}")
            return out

        out.append(f"  api.send.paid.url_pattern: {UNRESOLVED_TOKEN}")
        if not self.origin:
            out.append("    # オリジンを取れていません。")
        elif not candidates:
            # **「無かった」で終わらせない。** 取りこぼしの可能性を述べる。
            out.append("    # 送信を名乗る mutation が見つかりませんでした。")
            out.append("    # 素の GraphQL 文がバンドルに残っていない作り")
            out.append("    # (persisted query / 事前コンパイル) の可能性があります。")
        else:
            # **1つに絞れないなら埋めない** (原則3)。候補は候補として並べる。
            out.append(f"    # 候補が {len(candidates)} 個あり、観測だけでは1つに絞れません。")
            for operation in candidates[:8]:
                out.append(f"    #   候補: {send_url_pattern(self.origin, operation)}")
                out.append(f"    #     {operation.signature()}")
        return out


def _origin_of(url: str) -> str:
    scheme, _, rest = url.partition("://")
    if not rest:
        return ""
    return f"{scheme}://{rest.partition('/')[0]}"


def read_bundle(
    config: BrowserConfig,
    credentials_dir: Path,
    candidate_list_url: Coord[str],
) -> BundleObservation:
    """Fetch the served JavaScript and read the GraphQL operations out of it.

    **通信は GET だけである。** 認証済みのセッションを使うのは、HTML が
    ログイン後の画面のものである必要があるためで、書き込みは一切行わない。
    """
    requested_url = require(candidate_list_url, used_by="recon.read_bundle.read_bundle")
    origin = _origin_of(requested_url)

    session = session_store.session_path(credentials_dir)
    if not session.exists():
        return BundleObservation(requested_url=requested_url, session_present=False)

    gate = SendGate()
    with browser_context(config, storage_state=session) as (context, page):
        # **GET しかしない設計だが、遮断は仕掛けて武装しておく。** 想定外の非GETは
        # 止めて記録するのが正しい (fail-closed)。ページ操作をしないので、
        # 武装したままでも読み込みが妨げられることは無い。
        install_gate(page, gate)
        gate.arm()
        try:
            html = ""
            try:
                html = context.request.get(requested_url).text()
            except Exception:
                return BundleObservation(
                    requested_url=requested_url,
                    origin=origin,
                    note="HTMLの取得に失敗しました (セッション切れ / 到達不能)。",
                )

            urls = script_urls(html, requested_url)
            targets = urls[:MAX_SCRIPTS]
            per_file: list[tuple[GraphQLOperation, ...]] = []
            read = 0
            truncated = 0
            for url in targets:
                source = ""
                with suppress(Exception):
                    source = context.request.get(url).text()
                if not source:
                    continue
                read += 1
                if len(source) > MAX_SOURCE_CHARS:
                    source = source[:MAX_SOURCE_CHARS]
                    truncated += 1
                per_file.append(operations_in(source))

            return BundleObservation(
                requested_url=requested_url,
                origin=origin,
                html_read=True,
                scripts_found=len(urls),
                scripts_read=read,
                scripts_skipped=max(0, len(urls) - len(targets)),
                scripts_truncated=truncated,
                operations=merge_operations(per_file),
                blocked_non_get=len(gate.recorded),
            )
        finally:
            gate.disarm()
