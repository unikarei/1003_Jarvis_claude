"""Obsidian Vault のローカル取り込み（T-202）のテスト。仮の Vault と一時 DB だけを使う。"""

import subprocess
import unicodedata

import pytest

import db
import ingest_obsidian as ing


@pytest.fixture
def vaults(db_path, ckc_data_dir, make_vault):
    oarai = make_vault("oarai-obsidian")
    jk = make_vault("jarvis-knowledge")
    (oarai / "民泊@大洗" / "01_物件").mkdir(parents=True)
    (oarai / "民泊@大洗" / "01_物件" / "大洗_物件概要.md").write_text("物件の概要", encoding="utf-8")
    (oarai / "民泊@大洗" / "99_テンプレート").mkdir()
    (oarai / "民泊@大洗" / "99_テンプレート" / "大洗_調査ノート雛形.md").write_text("", encoding="utf-8")
    (oarai / "日記.md").write_text("民泊@大洗 の外", encoding="utf-8")
    (oarai / "_jarvis").mkdir()
    (oarai / "_jarvis" / "x.md").write_text("Jarvis のフォルダ", encoding="utf-8")
    (jk / "研究").mkdir()
    (jk / "研究" / "タービン.md").write_text("翼の設計", encoding="utf-8")
    (jk / "_jarvis" / "knowledge" / "Decision").mkdir(parents=True)
    (jk / "_jarvis" / "knowledge" / "Decision" / "d.md").write_text("CKC の知識", encoding="utf-8")
    (jk / ".obsidian").mkdir()
    (jk / ".obsidian" / "x.md").write_text("設定", encoding="utf-8")
    return oarai, jk


def _urls(node_type):
    return sorted(r["url"] for r in db.get_connection().execute("SELECT url FROM nodes WHERE type = ?", (node_type,)))


def test_ingests_target_notes_with_github_style_urls(vaults):
    result = ing.ingest_all()
    assert len(result["created"]) == 3  # jarvis-knowledge の README.md（make_vault が作る）も通常のノート
    assert _urls("Knowledge") == [
        "https://github.com/unikarei/jarvis-knowledge/blob/main/README.md",
        "https://github.com/unikarei/jarvis-knowledge/blob/main/研究/タービン.md",
        "https://github.com/unikarei/oarai-obsidian/blob/main/民泊@大洗/01_物件/大洗_物件概要.md",
    ]


def test_rerun_does_not_duplicate_and_updates_changed_content(vaults):
    oarai, _ = vaults
    ing.ingest_all()
    (oarai / "民泊@大洗" / "01_物件" / "大洗_物件概要.md").write_text("物件の概要（更新）", encoding="utf-8")
    result = ing.ingest_all()
    assert result["created"] == [] and len(result["updated"]) == 1
    assert len(_urls("Knowledge")) == 3


def test_existing_github_api_nodes_are_reused(vaults):
    """GitHub API 時代に作られたノード（同じ URL）があれば、新しいノードを作らない。"""
    url = "https://github.com/unikarei/oarai-obsidian/blob/main/民泊@大洗/01_物件/大洗_物件概要.md"
    db.insert_node("Source", category="minpaku", title="大洗_物件概要", content="物件の概要", url=url)
    result = ing.ingest_vault(ing.SOURCES[0])
    assert result == {"created": [], "updated": []}


def test_decomposed_file_names_are_normalized(vaults):
    oarai, _ = vaults
    name = unicodedata.normalize("NFD", "ガイド.md")
    (oarai / "民泊@大洗" / name).write_text("x", encoding="utf-8")
    assert "民泊@大洗/ガイド.md" in ing.list_notes(ing.SOURCES[0], oarai)


def test_missing_vault_is_skipped(db_path, ckc_data_dir, make_vault, capsys):
    make_vault("oarai-obsidian")
    result = ing.ingest_all()
    assert result == {"created": [], "updated": []}
    assert "[skip] vault-jarvis-knowledge" in capsys.readouterr().out


def test_does_not_run_git(vaults, monkeypatch):
    calls = []
    real = subprocess.run
    monkeypatch.setattr(subprocess, "run", lambda cmd, *a, **k: calls.append(cmd) or real(cmd, *a, **k))
    ing.ingest_all()
    assert calls == []
