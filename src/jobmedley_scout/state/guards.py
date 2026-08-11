"""Safety valves, as pure assertions over already-observed facts (9.1 / 12.1 / 12.6).

本モジュールはファイルもDBも触らない。「キルスイッチのファイルが存在するか」
「送信履歴が何件あるか」は **呼び出し側が観測して渡す**。理由は 12.6:

> 参照実装では、状態消失ガードが実行基盤の環境変数に渡っておらず、ドキュメントには
> 手順があるのに **CIでは常に無効** だった。「安全弁を作った」と「安全弁が効いて
> いる」は別物である。

安全弁の判定を純粋関数にしておけば、**「有効なら止まる」ことを単体テストで表明
できる** ようになり、配線 (実効値がどこから来たか) は :mod:`config.effective` と
起動前チェックが印字して受け持つ、という分担になる。

例外の使い分け:

* :class:`StateIntegrityError` -- 異常。実行を非0で落とす。
* :class:`KillSwitchEngaged` -- **意図した正常停止**。エラーではないので終了コードは
  0 系で扱うこと (:mod:`jobmedley_scout.errors` の注記)。
"""

from __future__ import annotations

from jobmedley_scout.errors import KillSwitchEngaged, StateIntegrityError


def assert_send_history_present(sent_count: int, *, enabled: bool) -> None:
    """Refuse to send for real when the send history is empty.

    12.1 の事故: 送信完了後に実行が中断され (1回は失敗、3回はタイムアウトによる
    キャンセル)、**送信記録56件が巻き戻った。** 巻き戻ると二重送信の危険がある
    だけでなく、消えた記録の **件名が復元不能** なので、その対象の返信は恒久的に
    検知できなくなる (13.3/10.2)。

    したがって「実送信をしようとしているのに送信履歴が0件」は、初回実行と
    区別がつかないとしても **止める側** に倒す。誤検知の代償は「初回だけ人が
    明示的にガードを外す」ことだけであり、見逃しの代償は復元不能な損失である。

    ``enabled=False`` のときは何もしない。無効化できること自体は仕様 (初回投入)
    だが、無効のまま本番送信に進む組み合わせは起動前チェックが FAIL にする (12.6)。
    """
    if not enabled:
        # 7.1: 黙って合格させるのと無効化は同義。だから無効化の可視化 (実効値の印字) は
        # 起動前チェック側の必須項目にしてある。ここでは判定しないことだけを明示する。
        return
    if sent_count < 0:
        raise ValueError(f"送信履歴の件数が負の値です: {sent_count}")
    if sent_count == 0:
        raise StateIntegrityError(
            "送信履歴が0件のまま実送信に進もうとしました (状態消失ガード)。\n"
            "12.1: 参照実装では実行の中断で送信記録56件が巻き戻り、"
            "二重送信の危険に加えて、失われた件名は復元できないため "
            "その対象の返信は恒久的に検知不能になりました。\n"
            "本当に初回実行なら、状態消失ガードを明示的に無効化してから再実行してください。"
        )


def assert_dry_run_immutable(dry_run: bool, mutation_description: str) -> None:
    """Refuse any state mutation while ``dry_run`` is in effect.

    9.1/9.2: **dry_run時は状態を一切動かさない。** dry run が送信記録に触れると、
    「送ったことになっているのに送っていない」対象が生まれ、本番に切り替えた日に
    その対象だけが永久に送られない。しかもエラーは出ない。

    dry run の記録先は専用テーブルだけ (:mod:`state.dryrun_log`)。この表明は
    その構造上の分離をすり抜ける経路が後から生えたときに落ちるための保険である。
    """
    if dry_run:
        raise StateIntegrityError(
            f"dry_run 中に状態を変更しようとしました: {mutation_description}\n"
            "9.1: dry_run時は状態を一切動かしません。dry run の痕跡は "
            "dry_run_log テーブルにのみ書いてください。"
        )


def check_kill_switch(path_exists: bool) -> None:
    """Stop cleanly when the kill-switch file is present.

    これは **異常ではなく意図した停止**。:class:`KillSwitchEngaged` を
    :class:`PermanentError` の下に置いていないのはそのため -- 異常終了と混同すると、
    運用者が意図的に止めたことが監視上「障害」として鳴り続ける。
    """
    if path_exists:
        raise KillSwitchEngaged(
            "キルスイッチのファイルが存在するため、送信を停止しました (正常停止)。"
            "再開するにはそのファイルを削除してください。"
        )
