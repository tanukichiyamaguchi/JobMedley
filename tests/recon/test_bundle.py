"""配信ファイルから GraphQL の操作を読む判断を固定する。

この経路の値打ちは **1回の GET で境界がつく** ことにある。だから守るのは
2点である。

1. **送信を名乗る mutation を、読み取りと取り違えないこと。**
   ``MemberOnScoutProfileModalOfDesktop`` のように読み取りの名前にも ``Scout``
   は入る。名前だけで断ずると、送信でないものを送信APIとして座標に書く
2. **1つに絞れないなら埋めないこと** (原則3)
"""

from __future__ import annotations

from jobmedley_scout.recon.bundle import (
    GraphQLOperation,
    merge_operations,
    operations_in,
    rank_send_operations,
    script_urls,
    send_url_pattern,
)
from jobmedley_scout.recon.read_bundle import BundleObservation

LIST_URL = "https://customers.job-medley.com/customers/searches?age[from]=0"
ORIGIN = "https://customers.job-medley.com"


# --- script の取り出し ----------------------------------------------------------


def test_script_sources_are_resolved_to_absolute_urls() -> None:
    html = """
    <html><head>
      <script src="/assets/app-1a2b.js"></script>
      <script src="https://cdn.example.com/vendor.js"></script>
      <script src="//cdn.example.com/scheme-relative.js"></script>
      <script>inline();</script>
    </head></html>
    """
    assert script_urls(html, LIST_URL) == (
        "https://customers.job-medley.com/assets/app-1a2b.js",
        "https://cdn.example.com/vendor.js",
        "https://cdn.example.com/scheme-relative.js",
    )


def test_inline_and_data_scripts_are_not_fetched() -> None:
    html = '<script>x=1</script><script src="data:text/javascript,x=1"></script>'
    assert script_urls(html, LIST_URL) == ()


def test_the_same_script_is_listed_once() -> None:
    html = '<script src="/a.js"></script><script src="/a.js"></script>'
    assert script_urls(html, LIST_URL) == ("https://customers.job-medley.com/a.js",)


# --- 操作の取り出し -------------------------------------------------------------


def test_a_mutation_and_its_variables_are_read() -> None:
    source = 'var d="mutation SendScout($input: ScoutSendInput!, $n: Int) { sendScout { id } }";'
    (operation,) = operations_in(source)

    assert operation.kind == "mutation"
    assert operation.name == "SendScout"
    assert operation.variables == (("input", "ScoutSendInput!"), ("n", "Int"))
    assert operation.signature() == "mutation SendScout($input: ScoutSendInput!, $n: Int)"


def test_a_query_is_read_but_is_not_a_send() -> None:
    source = "query MemberOnScoutProfileModalOfDesktop($id: ID!) { member { id } }"
    (operation,) = operations_in(source)

    assert operation.kind == "query"
    # **名前に scout が入っていても読み取りは送信ではない。**
    assert not operation.looks_like_send()


def test_a_mutation_without_send_wording_is_not_a_send_candidate() -> None:
    """既読にする等の無関係な更新を送信と取り違えない。"""
    (operation,) = operations_in("mutation MarkRead($id: ID!) { markRead { ok } }")
    assert operation.is_mutation
    assert not operation.looks_like_send()


def test_repeated_definitions_are_folded() -> None:
    source = "mutation SendScout($a: A!) { x } ... mutation SendScout($a: A!) { x }"
    assert len(operations_in(source)) == 1


def test_same_name_with_different_variables_is_kept_apart() -> None:
    """名前だけで畳むと、変数の違う定義が黙って消える。"""
    source = "mutation SendScout($a: A!) { x } mutation SendScout($a: A!, $b: B) { x }"
    assert len(operations_in(source)) == 2


def test_an_operation_without_variables_is_read() -> None:
    (operation,) = operations_in("mutation SendScout { sendScout { id } }")
    assert operation.variables == ()
    assert operation.signature() == "mutation SendScout"


def test_nothing_is_invented_from_a_file_without_graphql() -> None:
    assert operations_in("function send(a,b){return a+b}") == ()


def test_findings_from_several_files_are_merged_without_duplicates() -> None:
    first = operations_in("mutation SendScout($a: A!) { x }")
    second = operations_in("mutation SendScout($a: A!) { x } query Q { y }")
    merged = merge_operations([first, second])
    assert [op.name for op in merged] == ["SendScout", "Q"]


# --- 順位付けと座標 -------------------------------------------------------------


def test_send_mutations_rank_above_other_mutations_and_reads() -> None:
    operations = (
        GraphQLOperation(kind="query", name="ScoutProfile"),
        GraphQLOperation(kind="mutation", name="MarkRead"),
        GraphQLOperation(kind="mutation", name="SendScout"),
    )
    assert [op.name for op in rank_send_operations(operations)] == [
        "SendScout",
        "MarkRead",
        "ScoutProfile",
    ]


def test_the_url_pattern_is_the_observed_shape_with_the_observed_name() -> None:
    operation = GraphQLOperation(kind="mutation", name="SendScout")
    assert send_url_pattern(ORIGIN, operation) == (
        "https://customers.job-medley.com/api/customers/graphql/SendScout"
    )


def _observation(*operations: GraphQLOperation) -> BundleObservation:
    return BundleObservation(
        requested_url=LIST_URL,
        origin=ORIGIN,
        html_read=True,
        scripts_found=3,
        scripts_read=3,
        operations=operations,
    )


def test_one_send_candidate_becomes_the_coordinate() -> None:
    report = _observation(
        GraphQLOperation(kind="query", name="ScoutProfileModal"),
        GraphQLOperation(kind="mutation", name="SendScoutMessage", variables=(("input", "I!"),)),
    ).render()

    assert "api/customers/graphql/SendScoutMessage" in report
    assert "UNRESOLVED" not in report
    assert "mutation SendScoutMessage($input: I!)" in report


def test_two_send_candidates_stay_unresolved() -> None:
    """**1つに絞れないなら埋めない** (原則3)。候補は候補として並べる。"""
    report = _observation(
        GraphQLOperation(kind="mutation", name="SendScoutMessage"),
        GraphQLOperation(kind="mutation", name="CreateScoutDraft"),
    ).render()

    assert "api.send.paid.url_pattern: UNRESOLVED" in report
    assert "候補が 2 個" in report
    assert "SendScoutMessage" in report
    assert "CreateScoutDraft" in report


def test_a_run_that_read_almost_nothing_says_so_instead_of_blaming_the_bundle() -> None:
    """**読んでいない場所について述べない。**

    実測1回目は script を2個しか読めていないのに「素の GraphQL 文がバンドルに
    残っていない (persisted query) 可能性」と述べた。読んでいない範囲についての
    推測であり、運用者を媒体の作りの議論へ誘導する -- 実際の原因はこちらの
    読み方 (素のHTMLの script タグしか見ていなかった) だった。
    """
    report = _observation(GraphQLOperation(kind="query", name="Whatever")).render()

    assert "api.send.paid.url_pattern: UNRESOLVED" in report
    assert "読めていない" in report
    assert "persisted query" not in report, "読んでいない場所について推測している"


def test_a_run_that_read_enough_may_name_the_possibilities() -> None:
    """十分読んだうえで無ければ、**どちらかに決められない** と述べる。"""
    report = BundleObservation(
        requested_url=LIST_URL,
        origin=ORIGIN,
        html_read=True,
        scripts_found=30,
        scripts_read=30,
        scripts_from_page=28,
        operations=(GraphQLOperation(kind="query", name="Whatever"),),
    ).render()

    assert "api.send.paid.url_pattern: UNRESOLVED" in report
    assert "persisted query" in report
    # 押下0回のコマンドの視野の限界も述べる。
    assert "ドロワーを開いたときにだけ" in report
    assert "この観測だけでは決まりません" in report


def test_a_run_that_could_not_read_the_html_says_it_read_nothing() -> None:
    report = BundleObservation(
        requested_url=LIST_URL, origin=ORIGIN, note="HTMLの取得に失敗しました。"
    ).render()

    assert "配信ファイルを1つも読んでいません" in report
    assert "HTMLの取得に失敗しました。" in report


def test_scripts_skipped_by_the_cap_are_reported() -> None:
    """**黙って打ち切らない。** 上限で読まなかった分は見落としでありうる。"""
    report = BundleObservation(
        requested_url=LIST_URL,
        origin=ORIGIN,
        html_read=True,
        scripts_found=50,
        scripts_read=40,
        scripts_skipped=10,
    ).render()

    assert "上限で読まなかった: 10 個" in report


def test_an_unexpected_non_get_is_reported() -> None:
    """GETしかしない設計なので、遮断が発生したこと自体が観測である。"""
    report = BundleObservation(
        requested_url=LIST_URL,
        origin=ORIGIN,
        html_read=True,
        scripts_found=1,
        scripts_read=1,
        blocked_non_get=2,
    ).render()

    assert "想定外: 非GETを 2 件遮断しました" in report
