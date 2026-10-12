"""テストの基盤（T-102）: 本番のパスを使わないことの確認。"""

import subprocess
from pathlib import Path

import config
from tests.conftest import PRODUCTION_ROOTS, production_paths_in_use


def test_paths_are_not_production():
    assert production_paths_in_use([config.DB_PATH, config.VAULT_ROOT, config.CKC_DATA_DIR]) == []


def test_production_paths_are_detected():
    prod_db = PRODUCTION_ROOTS[1] / "jarvis.db"
    prod_vault = PRODUCTION_ROOTS[0] / "jarvis-knowledge"
    assert production_paths_in_use([prod_db, prod_vault, Path("/tmp/x")]) == [prod_db.resolve(), prod_vault.resolve()]


def test_db_fixture_creates_fresh_db(db_path):
    import db

    assert db_path.exists()
    assert db.get_connection().execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 0


def test_make_vault_creates_git_repository(make_vault):
    vault = make_vault("jarvis-knowledge")
    out = subprocess.run(["git", "status", "--porcelain"], cwd=vault, capture_output=True, text=True, check=True)
    assert out.stdout == ""
    assert vault.parent == config.VAULT_ROOT
