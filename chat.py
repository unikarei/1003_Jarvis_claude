"""JARVISとの対話型CLI。質問・依頼を入力すると、ナレッジ検索またはコード実行サブエージェントに
振り分けて実行する（run_task.pyと同じ分類ロジックで、Task/Skill/Agentグラフにも記録される）。

終了: exit または quit と入力、もしくは Ctrl+C / Ctrl+D。
"""

import sys

# Windowsで標準入出力がリダイレクトされる場合、既定のコンソールコードページ（cp932等）と
# UTF-8が不一致になりUnicodeEncodeErrorになることがあるため、明示的にUTF-8を強制する。
sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")

from ckc.models import Speaker, Turn  # noqa: E402
from run_task import run_task  # noqa: E402

# 「ナレッジ化して」などで、それまでの対話を CKC で知識にする（../docs/spec.md §3.3、CKC Phase 1）
CKC_TRIGGERS = ("/ckc", "ナレッジ化して", "ナレッジ化する")


def is_ckc_request(text: str) -> bool:
    return text.strip().lower() == "/ckc" or any(t in text for t in CKC_TRIGGERS[1:])


def run_ckc(history: list[Turn]) -> bool:
    """対話を CKC に渡す。取り込めたら True（呼び出し側は記録をリセットする）。"""
    if not history:
        print("ナレッジ化する対話がまだありません。\n")
        return False
    from ckc.capture import CaptureError
    from ckc.extract import ExtractError
    from ckc_cli import import_turns
    from db import init_db

    init_db()
    try:
        import_turns(history, title=f"JARVIS との対話: {history[0].text.splitlines()[0][:30]}")
    except (CaptureError, ExtractError) as e:
        print(f"[CKC] {e}\n")
        return False
    print()
    return True


THEME_PREFIXES = ("テーマ登録:", "テーマ登録：", "/theme ")


def theme_request(text: str) -> str | None:
    """「テーマ登録: …」なら、テーマの本文を返す（Knowledge Architect、MAP Phase 8）。"""
    for prefix in THEME_PREFIXES:
        if text.startswith(prefix):
            return text[len(prefix):].strip() or None
    return None


def run_architect(theme: str) -> None:
    import architect
    from vault import VaultError

    try:
        proposal = architect.propose(theme)
        print("\n" + architect.format_proposal(proposal))
        if input("\nこの内容でノートの雛形と（必要なら）Registry の PR を作りますか？ [y/N] > ").strip().lower() != "y":
            print(f"適用しませんでした（あとで python architect.py apply {proposal.proposal_id} で適用できます）。\n")
            return
        applied = architect.apply(proposal)
        for path in applied.applied.get("notes", []):
            print(f"ノートを作成: {path}")
        if applied.applied.get("registry_pr"):
            print(f"Registry の PR: {applied.applied['registry_pr']}（Owner がレビューしてマージ）")
        print()
    except (architect.ArchitectError, VaultError) as e:
        print(f"[Architect] {e}\n")


def main():
    print("JARVISと対話します（終了: exit / quit / Ctrl+C）")
    print("例:「大洗の物件の所在地は？」「OpenAIに関する最近のニュースは？」「Pythonで○○する関数を書いて」")
    print("「ナレッジ化して」で、それまでの対話から知識を抽出して Obsidian に保存します。")
    print("「テーマ登録: <テーマ>」で、既存資産の再利用と新しいテーマの構成を提案します。\n")
    history: list[Turn] = []

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
        theme = theme_request(user_input)
        if theme:
            run_architect(theme)
            continue
        if is_ckc_request(user_input):
            if run_ckc(history):
                history = []
            continue

        try:
            response = run_task(user_input)
        except Exception as e:
            print(f"[エラー] {type(e).__name__}: {e}\n")
            continue

        history += [Turn(Speaker.OWNER, user_input), Turn(Speaker.AI, response)]
        print(f"\nJARVIS > {response}\n")


if __name__ == "__main__":
    main()
