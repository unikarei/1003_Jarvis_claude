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
from db import init_db  # noqa: E402
from run_task import record  # noqa: E402

GREETING = """JARVISと対話します（終了: exit / quit / Ctrl+C）
例:「民泊の補助金について知ってることは？」→「その出典は？」「2 番目の出典は？」
   「システムの状態は？」「次にやることは？」「改善すべき点は？」「全体を見せて」
   「ナレッジ化して」（それまでの対話を知識にする）「テーマ登録: <テーマ>」「〜する関数を書いて」
"""


_STREAM_STATE: dict = {}


def _stream(chunk: str) -> None:
    """答えを生成しながら表示する（T-1212）。最初の差分で「JARVIS >」を出す。"""
    started = _STREAM_STATE.get("started")
    if started is not None and not started[0]:
        started[0] = True
        print("\nJARVIS > ", end="", flush=True)
    print(chunk, end="", flush=True)


def main():
    print(GREETING)
    init_db()
    ctx = orchestrator.Context(stream=_stream)

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

        started = [False]
        _STREAM_STATE["started"] = started
        outcome = orchestrator.handle(user_input, ctx)
        if started[0] and ctx.shown and outcome.text.startswith(ctx.shown):
            rest = outcome.text[len(ctx.shown):].strip()          # 答えは表示済み。出典などの残りだけ
            print(("\n\n" + rest if rest else "") + "\n")
        else:
            if started[0]:
                print()
            print(f"\nJARVIS > {outcome.text}\n")
        try:
            record(user_input, outcome)   # 使ったスキルと成否をナレッジグラフに記録する
        except Exception as e:  # 記録の失敗で対話を止めない
            print(f"[記録できませんでした] {type(e).__name__}: {e}\n")
        # ナレッジ化の材料にする対話。Owner の発言は、応答が失敗・時間切れでも残す（決定や好みは Owner の発言にある）。
        # Jarvis の応答は成功したものだけ。ナレッジ化そのものは材料にしない
        if not (outcome.skill == "knowledge-capture" and outcome.method == "rule"):
            ctx.history.append(Turn(Speaker.OWNER, user_input))
            if outcome.ok and outcome.error is None:
                ctx.history.append(Turn(Speaker.AI, outcome.text))


if __name__ == "__main__":
    main()
