"""会話から呼ぶ書き込みのある処理（ナレッジ化・テーマ登録）。chat.py と orchestrator.py が使う。

chat.py は読み込み時に標準入出力の設定を変えるため、ほかのモジュールから使う処理をここに分けた（FR-014、T-1207）。
"""

from ckc.models import Turn


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
