"""Discovery / Architecture Guardian の実行（MAP Phase 3、../docs/architecture.md §11）。

使い方:
    python discover.py --dry-run     # 調べて結果を表示するだけ（何も書かない）
    python discover.py               # Registry リポジトリの discovered/・generated/ に書く
    python discover.py --commit      # 書いたうえで scripts/commit_registry_outputs.sh でコミット・push

調べるだけで、何も変更しない（git は読み取りだけ、fetch もしない）。Registry の YAML は書き換えない。
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from discovery import settings
from discovery.collect import collect
from discovery.guardian import evaluate
from discovery.output import render_outputs, write_outputs

COMMIT_SCRIPT = Path(__file__).resolve().parent / "scripts" / "commit_registry_outputs.sh"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Registry と実態の差分を調べる")
    parser.add_argument("--registry-dir", type=Path, default=settings.REGISTRY_DIR)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args(argv)

    registry_json = args.registry_dir / "generated" / "registry.json"
    try:
        registry = json.loads(registry_json.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"Registry を読めません: {registry_json}: {e}", file=sys.stderr)
        return 2

    snapshot = collect(registry)
    report = evaluate(registry, snapshot)
    texts = render_outputs(registry, snapshot, report)
    if args.dry_run:
        print(texts["generated/guardian-report.md"])
        return 0
    for path in write_outputs(args.registry_dir, texts):
        print(f"書きました: {path}")
    if args.commit:
        env = {**os.environ, "REGISTRY_REPO": str(args.registry_dir)}
        return subprocess.run(["/bin/bash", str(COMMIT_SCRIPT)], env=env).returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
