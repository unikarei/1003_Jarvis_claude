"""テスト共通の設定。本番の jarvis.db・Vault・data/ を使わないよう、config を読み込む前に
パスを一時フォルダへ差し替え、本番のパスを指していたらテストを始めずに止める（../docs/spec.md §3.3.12）。
"""

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="jarvis-test-"))
os.environ["JARVIS_DB_PATH"] = str(_TMP_ROOT / "jarvis.db")
os.environ["JARVIS_VAULT_ROOT"] = str(_TMP_ROOT / "vaults")
os.environ["JARVIS_CKC_DATA_DIR"] = str(_TMP_ROOT / "ckc")

import config  # noqa: E402

PRODUCTION_ROOTS = [
    Path.home() / "work" / "obsidian",
    Path.home() / "work" / "0002_Jarvis2" / "1003_Jarvis_claude",
]


def production_paths_in_use(paths: list[Path]) -> list[Path]:
    """本番の場所の下にあるパスを返す。"""
    hits = []
    for path in paths:
        resolved = Path(path).expanduser().resolve()
        if any(resolved == root or root in resolved.parents for root in PRODUCTION_ROOTS):
            hits.append(resolved)
    return hits


def pytest_sessionstart(session):
    hits = production_paths_in_use([config.DB_PATH, config.VAULT_ROOT, config.CKC_DATA_DIR])
    if hits:
        pytest.exit(f"本番のパスを指しているためテストを中止します: {hits}", returncode=2)


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """テストごとに空の DB を作る。"""
    import db

    path = tmp_path / "jarvis.db"
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    return path


@pytest.fixture
def vault_root(tmp_path, monkeypatch):
    """Vault 群の親フォルダを一時フォルダにする。"""
    root = tmp_path / "vaults"
    root.mkdir()
    monkeypatch.setattr(config, "VAULT_ROOT", root)
    return root


@pytest.fixture
def make_vault(vault_root):
    """git init 済みの仮の Vault を作る。make_vault("jarvis-knowledge") のように使う。"""

    def _make(name: str) -> Path:
        path = vault_root / name
        path.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=path, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
        (path / "README.md").write_text("test vault\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=path, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
        return path

    return _make


@pytest.fixture
def ckc_data_dir(tmp_path, monkeypatch):
    path = tmp_path / "ckc"
    monkeypatch.setattr(config, "CKC_DATA_DIR", path)
    return path
