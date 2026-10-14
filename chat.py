"""JARVISとの対話型CLI。発言を振り分けて、Jarvis のスキル（知識の検索・出典の追跡・構成の照合・開発ボード・
改善の監査・管理画面・ナレッジ化・テーマ登録・コードの依頼など）を実行する（FR-014、orchestrator.py）。

書き込みのあるスキルは、実行の前に確認する。終了: exit または quit と入力、もしくは Ctrl+C / Ctrl+D。
"""

import sys

# Windowsで標準入出力がリダイレクトされる場合、既定のコンソールコードページ（cp932等）と
# UTF-8が不一致になりUnicodeEncodeErrorになることがあるため、明示的にUTF-8を強制する。
sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")

import orchestrator  # noqa: E402
from ckc.models import Speaker, Turn  # noqa: E402

GREETING = """JARVISと対話します（終了: exit / quit / Ctrl+C）
例:「民泊の補助金について知ってることは？」→「その出典は？」「2 番目の出典は？」
   「システムの状態は？」「次にやることは？」「改善すべき点は？」「全体を見せて」
   「ナレッジ化して」（それまでの対話を知識にする）「テーマ登録: <テーマ>」「〜する関数を書いて」
"""


def main():
    print(GREETING)
    ctx = orchestrator.Context()

    while True:
        try:
            user_input = input("あなた > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n終了します。")
            return

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit"):
            print("終了します。")
            return

        outcome = orchestrator.handle(user_input, ctx)
        print(f"\nJARVIS > {outcome.text}\n")
        # ナレッジ化の材料にする対話（ナレッジ化そのものと、実行しなかった・失敗したものは除く）
        if outcome.ok and outcome.error is None and outcome.skill != "knowledge-capture":
            ctx.history += [Turn(Speaker.OWNER, user_input), Turn(Speaker.AI, outcome.text)]


if __name__ == "__main__":
    main()
