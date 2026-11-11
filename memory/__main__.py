"""会話の記憶を確認・管理する CLI。"""

import argparse

from memory import store
from memory.summarize import summarize_pending


def main(argv=None):
    parser = argparse.ArgumentParser(description="会話の記憶を確認・管理します")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("list", "summarize", "rebuild", "stats"):
        commands.add_parser(name)
    for name in ("show", "forget"):
        commands.add_parser(name).add_argument("id")
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            for row in store.list_sessions():
                print(f"{row['session_id']} {row['channel']} 発言 {row['owner_turns']} 回 要約: {row['summary_status']}")
        elif args.command == "show":
            row = store.get_session(args.id)
            if row is None:
                print("会話が見つかりません。")
            else:
                print(f"会話: {args.id}（{row['channel']}）")
                for turn in row["turns"]:
                    print(f"{turn['created_at']} {'Owner' if turn['speaker'] == 'owner' else 'JARVIS'}: {turn['text']}")
                print("要約: " + (row["summary"] or "なし"))
        elif args.command == "summarize":
            print(f"{summarize_pending()} 件を要約しました。")
        elif args.command in ("stats", "rebuild"):
            result = store.rebuild() if args.command == "rebuild" else store.stats()
            print(f"会話 {result['sessions']} 件・発言 {result['turns']} 件・要約 {result['summaries']} 件")
        elif input("この会話の記録を消しますか？ [y/N] ").strip().lower() == "y":
            print("削除しました。" if store.forget(args.id) else "会話が見つかりません。")
        else:
            print("削除しませんでした。")
    except Exception as error:
        store.warn("操作", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
