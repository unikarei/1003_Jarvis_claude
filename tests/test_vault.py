"""ローカル Vault への書き込み（T-108）のテスト。仮の Vault（一時フォルダ）だけを使う。"""

import subprocess
from datetime import date

import pytest

import update_obsidian
import vault
from vault import VaultError

JK = "vault-jarvis-knowledge"


@pytest.fixture
def jk(make_vault, ckc_data_dir):
    return make_vault("jarvis-knowledge")


def _git(path, *args):
    return subprocess.run(["git", *args], cwd=path, capture_output=True, text=True, check=True).stdout


def test_resolve_vault(vault_root):
    assert vault.resolve_vault(JK) == vault_root / "jarvis-knowledge"
    with pytest.raises(VaultError, match="未知"):
        vault.resolve_vault("vault-nope")


def test_create_file_writes_content(jk, ckc_data_dir):
    path = vault.create_file(JK, "_jarvis/knowledge/Decision/a.md", "本文\n")
    assert path == jk / "_jarvis/knowledge/Decision/a.md"
    assert path.read_text(encoding="utf-8") == "本文\n"
    assert list((ckc_data_dir / "tmp").iterdir()) == []  # 一時ファイルが残らない


def test_create_file_never_overwrites(jk):
    vault.create_file(JK, "n.md", "最初")
    with pytest.raises(FileExistsError):
        vault.create_file(JK, "n.md", "上書き")
    assert (jk / "n.md").read_text(encoding="utf-8") == "最初"


def test_create_file_numbers_if_exists(jk):
    vault.create_file(JK, "n.md", "1")
    second = vault.create_file(JK, "n.md", "2", number_if_exists=True)
    third = vault.create_file(JK, "n.md", "3", number_if_exists=True)
    assert (second.name, third.name) == ("n_2.md", "n_3.md")
    assert (jk / "n.md").read_text(encoding="utf-8") == "1"


def test_create_file_does_not_overwrite_file_appearing_just_before(jk, monkeypatch):
    """存在確認と作成の間に同名のファイルが現れても上書きしない（os.link による排他作成）。"""
    original = vault._write_temp

    def write_temp_then_race(content):
        tmp = original(content)
        (jk / "race.md").write_text("他の端末", encoding="utf-8")
        return tmp

    monkeypatch.setattr(vault, "_write_temp", write_temp_then_race)
    with pytest.raises(FileExistsError):
        vault.create_file(JK, "race.md", "Jarvis")
    assert (jk / "race.md").read_text(encoding="utf-8") == "他の端末"


@pytest.mark.parametrize("relpath", ["../x.md", "/etc/x.md", ".git/config", ".obsidian/app.json", ""])
def test_rejects_paths_outside_vault(jk, relpath):
    with pytest.raises(VaultError):
        vault.create_file(JK, relpath, "x")


def test_rejects_symlink_escape(jk, tmp_path):
    (jk / "link").symlink_to(tmp_path)
    with pytest.raises(VaultError):
        vault.create_file(JK, "link/x.md", "x")


@pytest.mark.parametrize("marker", ["MERGE_HEAD", "rebase-merge", "rebase-apply", "index.lock"])
def test_refuses_while_git_is_busy(jk, marker):
    target = jk / ".git" / marker
    target.mkdir() if marker.startswith("rebase") else target.write_text("x")
    with pytest.raises(VaultError, match="Git の操作中"):
        vault.create_file(JK, "n.md", "x")
    assert not (jk / "n.md").exists()


def test_refuses_non_git_vault(vault_root, ckc_data_dir):
    (vault_root / "jarvis-knowledge").mkdir()
    with pytest.raises(VaultError, match="Git リポジトリではありません"):
        vault.create_file(JK, "n.md", "x")


def test_modify_file_with_backup(jk, ckc_data_dir):
    content, sha = vault.read_file(JK, "README.md")
    vault.modify_file(JK, "README.md", content + "追記\n", sha)
    assert (jk / "README.md").read_text(encoding="utf-8") == "test vault\n追記\n"
    backups = list((ckc_data_dir / "backups" / JK).iterdir())
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == "test vault\n"


def test_modify_refuses_uncommitted_file(jk):
    (jk / "README.md").write_text("編集中\n", encoding="utf-8")
    content, sha = vault.read_file(JK, "README.md")
    with pytest.raises(VaultError, match="未同期"):
        vault.modify_file(JK, "README.md", content + "x", sha)
    assert (jk / "README.md").read_text(encoding="utf-8") == "編集中\n"


def test_modify_refuses_untracked_file(jk):
    vault.create_file(JK, "new.md", "未コミット\n")
    content, sha = vault.read_file(JK, "new.md")
    with pytest.raises(VaultError, match="未同期"):
        vault.modify_file(JK, "new.md", content + "x", sha)


def test_modify_ignores_uncommitted_changes_in_other_files(jk):
    (jk / "other.md").write_text("Owner が編集中\n", encoding="utf-8")
    content, sha = vault.read_file(JK, "README.md")
    vault.modify_file(JK, "README.md", content + "x", sha)


def test_modify_refuses_when_changed_after_read(jk):
    content, sha = vault.read_file(JK, "README.md")
    (jk / "README.md").write_text("他の端末の変更\n", encoding="utf-8")
    _git(jk, "commit", "-qam", "other device")
    with pytest.raises(VaultError, match="読み込み後に変更"):
        vault.modify_file(JK, "README.md", content + "x", sha)
    assert (jk / "README.md").read_text(encoding="utf-8") == "他の端末の変更\n"


def test_only_read_only_git_commands_are_used(jk, monkeypatch):
    calls = []
    real_run = subprocess.run

    def spy(cmd, *args, **kwargs):
        if cmd and cmd[0] == "git":
            calls.append(cmd)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(vault.subprocess, "run", spy)
    vault.create_file(JK, "a.md", "x")
    content, sha = vault.read_file(JK, "README.md")
    vault.modify_file(JK, "README.md", content + "x", sha)
    assert calls and all(c[:3] == ["git", "--no-optional-locks", "status"] for c in calls)
    assert len(_git(jk, "log", "--oneline").splitlines()) == 1  # コミットされていない


# --- update_obsidian.py（関数名・CLI・追記の書式は従来どおり） ---

@pytest.fixture
def oarai(make_vault, ckc_data_dir):
    return make_vault("oarai-obsidian")


def test_create_note_defaults_to_oarai_vault(oarai):
    path = update_obsidian.create_note("民泊@大洗/新規.md", "内容")
    assert path == oarai / "民泊@大洗/新規.md"
    with pytest.raises(FileExistsError, match="append_note"):
        update_obsidian.create_note("民泊@大洗/新規.md", "別の内容")


def test_append_note_keeps_footer_format(oarai):
    update_obsidian.append_note("README.md", "追記内容")
    assert (oarai / "README.md").read_text(encoding="utf-8") == (
        f"test vault\n\n\n---\n（JARVIS追記: {date.today().isoformat()}）\n追記内容\n")


def test_append_note_missing_file(oarai):
    with pytest.raises(FileNotFoundError, match="create_note"):
        update_obsidian.append_note("none.md", "x")
