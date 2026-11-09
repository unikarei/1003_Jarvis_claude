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


def capture_candidates(history: list[Turn]) -> list:
    """対話を CKC に取り込み、知識の候補を返す（ブラウザ・会話の終わり用、T-1802・T-1803）。取り込み済みの会話なら、
    その会話の保存待ちの候補。CaptureError・ExtractError はそのまま上げる。"""
    from ckc import store
    from ckc.capture import build_conversation, capture
    from ckc.extract import extract
    from ckc.models import CandidateStatus, Origin
    from db import init_db

    init_db()
    result = capture(build_conversation(list(history), Origin.JARVIS,
                                        f"JARVIS との対話: {history[0].text.splitlines()[0][:30]}"))
    if not result.is_new:
        return store.list_candidates(CandidateStatus.PENDING, conversation_id=result.conversation_id)
    return extract(result.conversation_id).candidates


def summary_text(counts: dict) -> str:
    parts = [f"{counts['saved']} 件を保存しました"] if counts["saved"] else ["保存した知識はありません"]
    if counts["rejected"]:
        parts.append(f"{counts['rejected']} 件は保存しませんでした")
    if counts["later"]:
        parts.append(f"{counts['later']} 件は保存待ちにしました（ckc_cli.py review で確認できます）")
    if counts["failed"]:
        parts.append(f"{counts['failed']} 件は保存できませんでした（ckc_cli.py publish でやり直せます）")
    return "。".join(parts) + "。"


def run_ckc_with_cards(history: list[Turn], decide, gate=None) -> tuple[str, bool]:
    """画面で 1 件ずつ答えるナレッジ化（ブラウザでの声の会話、T-1802）。（結果の文, 取り込めたか）。"""
    if not history:
        return "ナレッジ化する対話がまだありません。", False
    from ckc.capture import CaptureError
    from ckc.extract import ExtractError
    from ckc.review import decide_each

    try:
        candidates = capture_candidates(history)
    except (CaptureError, ExtractError) as e:
        return f"[CKC] {e}", False
    if not candidates:
        return "知識の候補はありませんでした。", True
    return summary_text(decide_each(candidates, decide, gate)), True


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
