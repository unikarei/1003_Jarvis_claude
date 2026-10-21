"""本番の DB を読まない・書かない仕組み（../docs/spec.md §3.4.10、T-1410）。

親のプロセスは、読む DB を sqlite3 のバックアップの機能で data/eval/tmp/<日時>.db に写し、同じ CLI を
子のプロセスとして JARVIS_DB_PATH をコピーにして起動し直す（db.py などが import のときに読む場所もコピーになる）。
子は JARVIS_EVAL_ISOLATED=1 で区別し、処理の前に ensure_isolated() で本番の場所を指していないか確かめる。
"""

import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import config
import evaluation

ISOLATED_ENV = "JARVIS_EVAL_ISOLATED"
SOURCE_DB_ENV = "JARVIS_EVAL_SOURCE_DB"    # 子に渡す: 元の DB のパス（メタ情報と本番の判定に使う）
COPIED_AT_ENV = "JARVIS_EVAL_COPIED_AT"    # 子に渡す: コピーの日時
PRODUCTION_DB = config.BASE_DIR / "jarvis.db"


class IsolationError(RuntimeError):
    """コピーを作れない、または子のプロセスが本番の DB を指している。"""


def is_isolated() -> bool:
    return os.environ.get(ISOLATED_ENV) == "1"


def tmp_dir() -> Path:
    return evaluation.EVAL_DIR / "tmp"


def copy_db(src: Path, dst_dir: Path | None = None) -> Path:
    """src をバックアップの機能で dst_dir/<日時>.db に写す（WAL の内容も含めて整合したコピー）。src は読み取り専用で開く。"""
    src = Path(src).expanduser().resolve()
    if not src.is_file():
        raise IsolationError(f"DB がありません: {src}")
    dst_dir = Path(dst_dir or tmp_dir())
    dst_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    dst = dst_dir / f"{stamp}.db"
    source = sqlite3.connect(src.as_uri() + "?mode=ro", uri=True)
    try:
        target = sqlite3.connect(dst)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    return dst


def run_isolated(args: list[str], src: Path, keep_copy: bool = False, command: list[str] | None = None,
                 runner=subprocess.run) -> int:
    """src のコピーを作り、JARVIS_DB_PATH をコピーにした子のプロセスで `python -m evaluation <args>` を動かす。
    終わったらコピーを消す（keep_copy で残す）。子の終了コードを返す。command はテストで差し替える。"""
    src = Path(src).expanduser().resolve()
    copy = copy_db(src)
    env = dict(os.environ)
    env.update({"JARVIS_DB_PATH": str(copy), ISOLATED_ENV: "1", SOURCE_DB_ENV: str(src),
                COPIED_AT_ENV: datetime.now().isoformat(timespec="seconds")})
    try:
        cmd = command or [sys.executable, "-m", "evaluation", *args]
        return runner(cmd, env=env, cwd=str(config.BASE_DIR)).returncode
    finally:
        if keep_copy:
            print(f"DB のコピーを残しました: {copy}", file=sys.stderr)
        else:
            copy.unlink(missing_ok=True)


def production_paths() -> set[Path]:
    """本番とみなす DB の場所（本番の既定の場所と、コピーの元の DB）。"""
    paths = {PRODUCTION_DB.resolve()}
    if os.environ.get(SOURCE_DB_ENV):
        paths.add(Path(os.environ[SOURCE_DB_ENV]).expanduser().resolve())
    return paths


def ensure_isolated() -> None:
    """子のプロセスで、処理を始める前に呼ぶ。隔離されていない・本番の DB を指しているならエラー。"""
    if not is_isolated():
        raise IsolationError(f"{ISOLATED_ENV}=1 の子のプロセスではありません")
    current = Path(config.DB_PATH).expanduser().resolve()
    if current in production_paths():
        raise IsolationError(f"本番の DB を指しているため中止します: {current}")


def source_info() -> dict:
    """メタ情報に書く元の DB のパスとコピーの日時。"""
    return {"source_db": os.environ.get(SOURCE_DB_ENV) or str(config.DB_PATH),
            "copied_at": os.environ.get(COPIED_AT_ENV)}
