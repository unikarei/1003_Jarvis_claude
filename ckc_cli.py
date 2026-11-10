"""CKC（Conversation Knowledge Collector）の CLI。会話を取り込み、知識の候補を確認・承認して Obsidian に保存する。

使い方:
    python ckc_cli.py import 会話.txt            # 取り込み → 抽出 → 1件ずつ確認・承認 → 保存
    pbpaste | python ckc_cli.py import -        # 貼り付け（標準入力）から
    python ckc_cli.py list [--status pending]   # 候補の一覧
    python ckc_cli.py show <ID>
    python ckc_cli.py review                    # 未確認の候補を1件ずつ確認
    python ckc_cli.py approve <ID> [--replace <ckc_id>] / reject <ID>
    python ckc_cli.py edit <ID> [--title ..] [--body ..] [--kind ..] [--projects a,b]
    python ckc_cli.py publish [<ID> ...]        # 承認済みを保存（ID 省略で全件）
    python ckc_cli.py supersede <古い ckc_id> <新しい ckc_id>   # 置き換えの状態更新の再実行

会話の書式: 行頭の「Owner:」「AI:」などで話者を分ける（../docs/tasks.md §1.3）。ID は一覧の [ ] 内の短い ID でよい。
"""

import argparse
import sys
from pathlib import Path

from ckc import review, store
from ckc.capture import CaptureError, build_conversation, capture, capture_file, capture_text
from ckc.extract import ExtractError, extract
from ckc.models import CandidateStatus, Origin
from ckc.publish import PublishError, mark_superseded
from db import init_db
from vault import VaultError


def import_turns(turns, title: str | None = None, ask=None) -> int:
    """Jarvis の対話（chat.py）の発言を取り込み、確認・承認・保存まで行う。ask は確認の入力（既定は input）。"""
    return import_and_review(capture(build_conversation(turns, Origin.JARVIS, title)), ask=ask)


def import_and_review(result, interactive: bool = True, ask=None) -> int:
    if not result.is_new:
        print(f"この会話は取り込み済みです（{result.conversation_id}）。")
        pending = store.list_candidates(CandidateStatus.PENDING, conversation_id=result.conversation_id)
        if not pending:
            return 0
    else:
        print(f"取り込みました（{result.conversation_id}）。知識を抽出しています…")
        extracted = extract(result.conversation_id)
        for index, reasons in extracted.rejected:
            print(f"  検証で除外した候補 #{index}: {', '.join(reasons)}")
        for w in extracted.warnings:
            print(f"  注意: {w}")
        pending = extracted.candidates
    print(f"\n候補 {len(pending)} 件")
    if interactive:
        review.interactive_review(pending, ask=ask)
    else:
        for c in pending:
            print(review.format_candidate(c))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CKC: 会話から知識を抽出して Obsidian に保存する")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("import")
    p.add_argument("file", help="会話のテキストファイル（- で標準入力）")
    p.add_argument("--title")
    p.add_argument("--no-review", action="store_true", help="確認せずに候補の一覧だけ表示する")
    p = sub.add_parser("list")
    p.add_argument("--status", choices=[s.value for s in CandidateStatus])
    sub.add_parser("review")
    for name in ("show", "reject"):
        sub.add_parser(name).add_argument("id")
    p = sub.add_parser("approve")
    p.add_argument("id")
    p.add_argument("--replace", help="置き換える既存の知識の ckc_id")
    p = sub.add_parser("edit")
    p.add_argument("id")
    p.add_argument("--title")
    p.add_argument("--body")
    p.add_argument("--kind")
    p.add_argument("--projects", help="カンマ区切り。- で空")
    p = sub.add_parser("publish")
    p.add_argument("ids", nargs="*")
    p = sub.add_parser("supersede")
    p.add_argument("old")
    p.add_argument("new")
    args = parser.parse_args(argv)

    init_db()
    try:
        if args.cmd == "import":
            if args.file == "-":
                result = capture_text(sys.stdin.read(), title=args.title)
            else:
                result = capture_file(Path(args.file), title=args.title)
            return import_and_review(result, interactive=not args.no_review)
        if args.cmd == "list":
            status = CandidateStatus(args.status) if args.status else None
            for c in store.list_candidates(status):
                print(review.format_candidate(c))
            return 0
        if args.cmd == "review":
            review.interactive_review(store.list_candidates(CandidateStatus.PENDING))
            return 0
        if args.cmd == "show":
            print(review.format_candidate(review.resolve(args.id), detail=True))
            return 0
        if args.cmd == "approve":
            review.approve(review.resolve(args.id), args.replace)
            print("承認しました（`publish` で保存します）。")
            return 0
        if args.cmd == "reject":
            review.reject(review.resolve(args.id))
            print("却下しました。")
            return 0
        if args.cmd == "edit":
            c = review.edit(review.resolve(args.id), title=args.title, body=args.body, kind=args.kind,
                            projects=review._projects(args.projects or ""))
            print(review.format_candidate(c, detail=True))
            return 0
        if args.cmd == "publish":
            targets = ([review.resolve(i) for i in args.ids] if args.ids
                       else store.list_candidates(CandidateStatus.APPROVED))
            not_approved = [c for c in targets if c.status != CandidateStatus.APPROVED]
            if not_approved:
                print("承認されていない候補は保存しません: " + ", ".join(review.short_id(c.candidate_id)
                                                                  for c in not_approved))
                return 1
            done = review.publish_approved(targets)
            return 0 if done == len(targets) else 1
        if args.cmd == "supersede":
            mark_superseded(args.old, args.new)
            print("古い知識の状態を superseded にしました。")
            return 0
    except (CaptureError, ExtractError, PublishError, review.ReviewError, store.StoreError, VaultError,
            FileNotFoundError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
