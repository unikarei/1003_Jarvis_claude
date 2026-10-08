"""JARVISとの対話型CLI。質問・依頼を入力すると、ナレッジ検索またはコード実行サブエージェントに
振り分けて実行する（run_task.pyと同じ分類ロジックで、Task/Skill/Agentグラフにも記録される）。

終了: exit または quit と入力、もしくは Ctrl+C / Ctrl+D。
"""

import sys

# Windowsで標準入出力がリダイレクトされる場合、既定のコンソールコードページ（cp932等）と
# UTF-8が不一致になりUnicodeEncodeErrorになることがあるため、明示的にUTF-8を強制する。
sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")

from run_task import run_task  # noqa: E402


def main():
    print("JARVISと対話します（終了: exit / quit / Ctrl+C）")
    print("例:「大洗の物件の所在地は？」「OpenAIに関する最近のニュースは？」「Pythonで○○する関数を書いて」\n")

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

        try:
            response = run_task(user_input)
        except Exception as e:
            print(f"[エラー] {type(e).__name__}: {e}\n")
            continue

        print(f"\nJARVIS > {response}\n")


if __name__ == "__main__":
    main()
