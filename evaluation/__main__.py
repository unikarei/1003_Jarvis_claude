"""評価部門の CLI（../docs/spec.md §3.4.10、T-1410）。

    python -m evaluation build [--n 60] [--seed 0] [--version V] [--split 30,15,15] [--db <パス>] [--keep-copy]

親のプロセスは DB のコピーを作って子のプロセスを起動するだけ。実際の処理は子（JARVIS_EVAL_ISOLATED=1）が行う。
"""

import argparse
import sys

import config
from evaluation import isolate, qa


def _split(text: str) -> tuple[int, ...]:
    try:
        parts = tuple(int(p) for p in text.split(","))
    except ValueError:
        raise argparse.ArgumentTypeError(f"30,15,15 の形で指定してください: {text}")
    if len(parts) != len(qa.SPLITS) or any(p < 0 for p in parts):
        raise argparse.ArgumentTypeError(f"dev,val,test の 3 つの数で指定してください: {text}")
    return parts


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m evaluation", description="ナレッジ回答の評価部門")
    sub = p.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="評価セット（疑似 Q&A）を作る（LLM は Ollama だけ）")
    b.add_argument("--n", type=int, default=60)
    b.add_argument("--seed", type=int, default=0)
    b.add_argument("--version", default=None, help="既定は qa-YYYYMMDD-HHMM")
    b.add_argument("--split", type=_split, default=qa.DEFAULT_SPLIT, help="dev,val,test の数（既定 30,15,15）")
    b.add_argument("--db", default=None, help="読む DB（既定は config.DB_PATH）。コピーを読む")
    b.add_argument("--keep-copy", action="store_true", help="終わった後も DB のコピーを残す")
    return p


def _print_summary(path, items: list[dict], info: dict) -> None:
    s = qa.summary(items)
    print(f"評価セット: {path}（{s['count']} 問 / 必要 {info['requested']} 問、試行 {info['attempts']} 回）")
    for label, key in (("データソース別", "by_source"), ("形別", "by_style"), ("組別", "by_split")):
        print(f"- {label}: " + "、".join(f"{k} {v}" for k, v in s[key].items()))
    rejected = info["rejected"]
    reasons = "、".join(f"{qa.REJECT_REASONS.get(k, k)} {v}" for k, v in rejected.items())
    print(f"- 捨てた数: {sum(rejected.values())}" + (f"（{reasons}）" if reasons else ""))


def _check(args) -> str | None:
    """DB をコピーする前に引数を確かめる。問題があれば理由。"""
    if args.command == "build":
        if sum(args.split) != args.n:
            return f"--split の合計（{sum(args.split)}）が --n（{args.n}）と一致しません"
        if args.version:
            try:
                if qa.exists(args.version):
                    return f"評価セットの版 {args.version} はすでにあります（上書きしません）"
            except ValueError as e:
                return str(e)
    return None


def _build(args) -> int:
    version = args.version or qa.default_version()
    result = qa.build(n=args.n, seed=args.seed, split=args.split)
    try:
        path = qa.save(result.items, version, result.info)
    except FileExistsError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    _print_summary(path, result.items, result.info)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parser().parse_args(argv)
    problem = _check(args)
    if problem:
        print(f"エラー: {problem}", file=sys.stderr)
        return 2
    if not isolate.is_isolated():
        try:
            return isolate.run_isolated(argv, args.db or config.DB_PATH, keep_copy=args.keep_copy)
        except isolate.IsolationError as e:
            print(f"エラー: {e}", file=sys.stderr)
            return 1
    try:
        isolate.ensure_isolated()
    except isolate.IsolationError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    if args.command == "build":
        return _build(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
