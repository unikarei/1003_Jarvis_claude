"""ナレッジ回答の評価部門（Evaluation Unit、../docs/spec.md §3.4.10、ADR-0030）。

- qa: 評価セット（疑似 Q&A）の作成・検証・保存（T-1410）
- isolate: 本番の DB のコピーを読む子のプロセスで動かす仕組み

成果物は EVAL_DIR（既定 data/eval/、Git の対象外）の下に置く。
"""

import os
from pathlib import Path

import config

# CKC・遅延の記録と同じ data/ の下に置く。テストで JARVIS_CKC_DATA_DIR を一時フォルダにしたときは一緒にそちらへ移る
EVAL_DIR = Path(os.environ.get("JARVIS_EVAL_DIR", config.CKC_DATA_DIR.parent / "eval"))
