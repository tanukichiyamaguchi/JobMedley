"""Employer de-duplication for the job-change count.

7.5 の事故: 転職回数が実際より **1回多く** 数えられていた。原因は、現職の勤務先が
「現在の勤務先」欄と「職歴一覧」の両方に現れており、素朴に合算していたこと。
回数は年齢帯ごとの閾値と比較されるので、1回のずれがそのまま誤除外になる。

対処は単純で、**数える前に正規化して集合にする**。正規化は
:mod:`jobmedley_scout.models.text_norm` の共有関数を通す (8.6: 生成側と参照側で
別の正規化を使うと静かに不一致する)。

**残差の向き (重要)。** 法人格や表記ゆれ (「株式会社A」と「A」) は
**畳まない**。畳めば「株式会社サービス」と「サービス株式会社」のような別法人まで
同一視しかねないためである。結果として、同一企業を別表記で書いている候補者は
転職回数が **多め** に出る。すなわち残差の誤りは **「送らない」側** に倒れる
(取りこぼす)。逆向き -- 別企業を同一視して回数を過小評価し、本来除外すべき相手に
**送ってしまう** 誤り -- は選ばない。この非対称は意図的なので、
「表記ゆれも吸収しよう」と正規化を強めるときは、この段落ごと更新すること。
"""

from __future__ import annotations

from collections.abc import Iterable

from jobmedley_scout.models.text_norm import normalize_name, normalize_ws


def dedupe_employers(current: str | None, past: Iterable[str | None]) -> tuple[str, ...]:
    """Distinct employer names, current first, in first-seen order.

    Returns the display spelling (width-folded, whitespace-collapsed) so that a
    caller may present exactly the value the decision was made on -- 8.3 対策2。
    比較には正規化キーを使い、返す値は表示用に留める。
    """
    seen: set[str] = set()
    employers: list[str] = []
    for raw in (current, *past):
        if raw is None:
            continue
        display = normalize_ws(raw)
        if not display:
            continue
        key = normalize_name(raw)
        if not key or key in seen:
            continue
        seen.add(key)
        employers.append(display)
    return tuple(employers)


def count_job_changes(current: str | None, past: Iterable[str | None]) -> int:
    """Number of job changes implied by the distinct employers.

    n社の経験は n-1 回の転職である。0社・1社なら0回。**現職が職歴一覧にも
    入っている前提で数える** -- それが 7.5 の事故そのものだったため、
    重複排除を通さない経路を残していない。

    社名が空の職歴は同一性を判定できないので数から落ちる。回数は少なめに出るので、
    このルール単独では「送る」側に倒れる。判定材料が欠けていることの扱いは
    :mod:`jobmedley_scout.targeting.rules` が UNDETERMINABLE で受け持つ
    (7.1: ここで黙って0扱いにしない)。
    """
    return max(0, len(dedupe_employers(current, past)) - 1)
