"""Source-scanning guardrails.

各テストは、**指示書で「1箇所に集約せよ」と指示された規約** を、人間の規律ではなく
ソース走査で守らせる。規約を破るコードが入ったら CI が落ちる。

なぜテストでやるか: これらの規約はどれも「破っても動いてしまう」種類のものである。
``networkidle`` を1箇所足しても動く (毎回30秒捨てるだけ)。``status == 200`` を
1箇所書いても動く (その枠が200を返す間は)。動いてしまうからこそ、レビューで見つける
のではなく機械で止める必要がある。
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "jobmedley_scout"
SELF = Path(__file__).name


def _python_files(*, exclude: set[str] | None = None) -> list[Path]:
    excluded = exclude or set()
    return [
        path
        for path in SRC.rglob("*.py")
        if path.name not in excluded and "migrations" not in path.parts
    ]


def _code_only(path: Path) -> list[str]:
    """The file's lines with comments and string literals blanked out.

    docstring とコメントは **規約そのものを説明するために** 禁止語を含む
    (「networkidle を待ってはならない」と書くには networkidle と書くしかない)。
    素朴な grep だと、規約を説明している行が規約違反として検出されるという
    倒錯が起きるので、字句解析でコードだけを見る。

    位置を保つために、文字列/コメントの範囲を空白で潰す方式にしてある
    (行番号と桁がずれないので、エラーメッセージがそのまま使える)。
    """
    source = path.read_text(encoding="utf-8")
    grid = [list(line) for line in source.splitlines()]
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type not in (tokenize.COMMENT, tokenize.STRING):
                continue
            (start_row, start_col), (end_row, end_col) = token.start, token.end
            for row in range(start_row, end_row + 1):
                if row - 1 >= len(grid):
                    continue
                line = grid[row - 1]
                first = start_col if row == start_row else 0
                last = end_col if row == end_row else len(line)
                for column in range(first, min(last, len(line))):
                    line[column] = " "
    except tokenize.TokenError:  # pragma: no cover - malformed source fails elsewhere
        pass
    return ["".join(row) for row in grid]


def _offenders(pattern: re.Pattern[str], allowed: set[str]) -> list[str]:
    hits: list[str] = []
    for path in _python_files():
        relative = str(path.relative_to(SRC))
        if relative in allowed:
            continue
        for lineno, line in enumerate(_code_only(path), 1):
            if pattern.search(line):
                hits.append(f"{relative}:{lineno}: {line.strip()}")
    return hits


def test_no_networkidle_anywhere() -> None:
    """5.3: 通信の静止を待ってはならない。

    参照実装は計測タグとロングポーリングのせいでアイドルに到達せず、**毎回30秒の
    タイムアウトを捨てていた**。求人媒体は例外なく計測タグ・チャットウィジェット・
    通知ポーリングを積んでいる。ネットワークアイドルは最初から当てにしない。
    """
    hits = _offenders(re.compile(r"networkidle"), allowed=set())
    assert not hits, (
        "networkidle を待つコードが入りました。5.3: 通信の静止は永遠に来ません。\n"
        "目的の要素の出現を待ってください (browser/navigation.py):\n  " + "\n  ".join(hits)
    )


def test_wall_clock_only_in_clock_module() -> None:
    """時刻の取得は clock.py だけ。

    営業日判定 (12.4)・コホート帰属 (11.3)・冪等/ローテーション (9.2, 9.6) は
    すべて「今」から判断する。注入可能でなければ、このシステムのテスト可能性の
    議論 (13.4) が丸ごと成立しない。
    """
    pattern = re.compile(r"datetime\.now\(|time\.time\(|date\.today\(")
    hits = _offenders(pattern, allowed={"clock.py"})
    assert not hits, (
        "clock.py 以外で実時刻を読んでいます。Clock を注入してください:\n  " + "\n  ".join(hits)
    )


def test_sleep_only_in_waits_module() -> None:
    """実際に眠るのは browser/waits.py だけ (5.2)。

    間隔の抽選は純粋関数として分離してあり、テストできる。眠る処理が散らばると
    テストが実時間ぶん遅くなり、やがて誰も回さなくなる。
    """
    pattern = re.compile(r"time\.sleep\(|wait_for_timeout\(")
    hits = _offenders(pattern, allowed={"browser/waits.py"})
    assert not hits, "browser/waits.py 以外で sleep しています:\n  " + "\n  ".join(hits)


def test_status_code_comparison_only_in_success_module() -> None:
    """6.2: 成功ステータスはエンドポイントごとに違う。判定は1箇所に集約する。

    参照実装では通常送信が200、プラチナ送信とピックアップ送信が201だった。
    200のみを成功とみなす実装では、**成功しているのに失敗扱いになる**。
    """
    pattern = re.compile(
        r"(status|status_code)\s*(==|!=|<|>|<=|>=)\s*\d|"
        r"(status|status_code)\s+in\s+[\(\[\{]\s*\d"
    )
    hits = _offenders(pattern, allowed={"api/success.py", "api/client.py"})
    assert not hits, (
        "api/success.py 以外でHTTPステータスを数値と比較しています。\n"
        "6.2: エンドポイントごとに成功ステータスが違うため、判定は is_success() に"
        "集約してください:\n  " + "\n  ".join(hits)
    )


def test_safety_critical_config_fields_have_no_defaults() -> None:
    """7.6: 安全critical な設定項目に既定値を置かない。

    参照実装では設定キーを書き間違えても寛容な読み込みのため、**エラーも警告も
    出ないまま年齢上限が消える** 状態だった。既定値が無ければ、打鍵ミスは
    「未知のキーがある」と「必須キーが無い」の2つの独立した失敗になる。
    """
    schema = (SRC / "config" / "schema.py").read_text(encoding="utf-8")
    tree = ast.parse(schema)

    safety_critical = {
        "dry_run",
        "state_loss_guard",
        "kill_switch_path",
        "ingest_cap",
        "per_run_cap_paid",
        "per_run_cap_free",
        "max_llm_requests_per_message",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for statement in node.body:
            if not isinstance(statement, ast.AnnAssign) or statement.value is None:
                continue
            target = statement.target
            if isinstance(target, ast.Name) and target.id in safety_critical:
                offenders.append(f"{node.name}.{target.id}")

    assert not offenders, (
        "安全critical な設定項目に既定値が付いています。7.6: 設定ファイルが"
        "「取り消せない外向き操作」の条件を決めているなら、寛容な読み込みは事故装置です:\n  "
        + "\n  ".join(offenders)
    )


def test_config_models_forbid_unknown_keys() -> None:
    """全設定モデルが ``extra="forbid"`` であること (7.6)。

    1つでも寛容なモデルがあると、その配下のタイポが無言で通る。
    """
    schema_path = SRC / "config" / "schema.py"
    source = schema_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    lenient: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        bases = {b.id for b in node.bases if isinstance(b, ast.Name)}
        if "BaseModel" not in bases:
            continue
        assigns = [
            s
            for s in node.body
            if isinstance(s, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "model_config" for t in s.targets)
        ]
        if not assigns:
            lenient.append(node.name)

    assert not lenient, (
        "model_config を宣言していない設定モデルがあります "
        "(extra='forbid' が効きません):\n  " + "\n  ".join(lenient)
    )


def test_send_api_retry_is_documented_as_deliberate() -> None:
    """12.5: 送信APIに自動リトライを掛けない。**意図的であるとコメントに残す。**

    「親切にリトライを足す」と二重送信事故に直結する。理由が書かれていないと、
    後から善意で足される。
    """
    send_module = SRC / "api" / "send.py"
    source = send_module.read_text(encoding="utf-8")
    assert "リトライ" in source, (
        "api/send.py にリトライを掛けない旨の記述がありません。"
        "12.5: 意図的にリトライしていないことをコメントに残してください。"
    )
    assert "retry" not in source.lower().replace("リトライ", ""), (
        "api/send.py にリトライ実装が入った可能性があります。"
        "送信APIへのリトライは二重送信に直結します (12.5)。"
    )
