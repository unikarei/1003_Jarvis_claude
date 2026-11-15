"""会話から呼ぶ書き込みのある処理（ナレッジ化・テーマ登録）。chat.py と orchestrator.py が使う。

chat.py は読み込み時に標準入出力の設定を変えるため、ほかのモジュールから使う処理をここに分けた（FR-014、T-1207）。
"""

from ckc.models import Turn


def run_ckc(history: list[Turn], ask=None) -> bool:
    """対話を CKC に渡す。取り込めたら True（呼び出し側は記録をリセットする）。ask は確認の入力（既定は input。
    声の会話はキーボードの列から読む）。"""
    if not history:
        print("ナレッジ化する対話がまだありません。\n")
        return False
    from ckc.capture import CaptureError
    from ckc.extract import ExtractError
    from ckc_cli import import_turns
    from db import init_db

    init_db()
    try:
        import_turns(history, title=f"JARVIS との対話: {history[0].text.splitlines()[0][:30]}", ask=ask)
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


def run_ckc_with_cards(history: list[Turn], decide, gate=None, on_none=None) -> tuple[str, bool]:
    """画面で 1 件ずつ答えるナレッジ化（ブラウザでの声の会話、T-1802）。（結果の文, 取り込めたか）。
    on_none: この会話から新しい候補が出なかった（0 件・抽出で保存できる候補がない）ときに呼ぶ。保存待ちの候補を出して
    その結果の文を返す（なければ None）。取り込みの失敗（CaptureError）では呼ばない（上位 spec §3.3.13 B、T-2504）。"""
    if not history:
        return "ナレッジ化する対話がまだありません。", False
    from ckc.capture import CaptureError
    from ckc.extract import ExtractError
    from ckc.review import decide_each

    try:
        candidates = capture_candidates(history)
    except CaptureError as e:
        return f"[CKC] {e}", False
    except ExtractError as e:
        message, captured = f"[CKC] {e}", False
    else:
        if candidates:
            return summary_text(decide_each(candidates, decide, gate)), True
        message, captured = "知識の候補はありませんでした。", True
    pending = on_none() if on_none is not None else None
    if pending:
        return f"この会話からは新しい候補がありませんでした。保存待ちの候補: {pending}", captured
    return message, captured


def review_pending_with_cards(decide, gate=None) -> str | None:
    """保存待ちの候補を画面で 1 件ずつ確認する（会話の終わりに保存待ちにしたもの、T-1803）。なければ None。"""
    from ckc import store
    from ckc.models import CandidateStatus
    from ckc.review import decide_each
    from db import init_db

    init_db()
    pending = store.list_candidates(CandidateStatus.PENDING)
    if not pending:
        return None
    return summary_text(decide_each(pending, decide, gate))


# ---------------------------------------------------------------- 会話の終わりの知識化（T-1803、spec §3.3.13 C）

MIN_OWNER_TURNS = 3
END_PROMPT = "この会話から知識の候補を作りますか？ [y/N] > "
END_SPOKEN = "この会話から知識の候補を作りますか。キーボードで答えてください。"


def capture_on_end_enabled() -> bool:
    import os
    return os.environ.get("JARVIS_CAPTURE_ON_END", "1") != "0"


def worth_capturing(history: list[Turn]) -> bool:
    """会話の終わりに知識化を扱うか: 設定で止めておらず、Owner の発言が 3 回以上ある（private の発言・ナレッジ化の後の
    発言は、会話の側が履歴に入れていない・消しているので数えない）。"""
    from ckc.models import Speaker
    return capture_on_end_enabled() and sum(t.speaker == Speaker.OWNER for t in history) >= MIN_OWNER_TURNS


def offer_capture_at_end(history: list[Turn], ask) -> bool:
    """文字の会話・Mac の声の会話の終わり（T-1803）。キーボードで聞き、y のときだけ「ナレッジ化して」と同じ流れに入る。
    取り込んだら True。"""
    if not worth_capturing(history):
        return False
    try:
        reply = ask(END_PROMPT)
    except (EOFError, KeyboardInterrupt):
        return False
    if (reply or "").strip().lower() != "y":
        return False
    return run_ckc(list(history), ask=ask)


def pending_count() -> int:
    from ckc import store
    from ckc.models import CandidateStatus
    from db import init_db

    init_db()
    return len(store.list_candidates(CandidateStatus.PENDING))


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
