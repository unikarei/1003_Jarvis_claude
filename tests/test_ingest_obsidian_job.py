"""Obsidian の毎日の取り込み（T-1801、統合ワークスペースの testing §15）のテスト。一時の Vault・DB・偽の通知だけを使う。"""

import plistlib
from pathlib import Path

import db
import ingest_obsidian as ing
from tests.test_ingest_obsidian import vaults  # noqa: F401  （fixture）

JK = "vault-jarvis-knowledge"


class Notify(list):
    def __call__(self, job, detail):
        self.append((job, detail))


def _run(**kw):
    lines, notify = [], Notify()
    code = ing.run_scheduled(out=lines.append, notify=notify, **kw)
    return code, lines, notify


def test_creates_new_updates_changed_and_leaves_unchanged(vaults):
    oarai, jk = vaults
    code, lines, notify = _run()
    assert code == 0 and notify == []
    assert any("vault-oarai-obsidian 新規 1 件・更新 0 件・消えたノート 0 件" in line for line in lines)
    assert any(f"{JK} 新規 2 件・更新 0 件・消えたノート 0 件" in line for line in lines)
    before = {r["id"]: r["content"] for r in db._fetchall("SELECT id, content FROM nodes")}
    (jk / "研究" / "タービン.md").write_text("翼の設計（改訂）", encoding="utf-8")
    code, lines, _ = _run()
    assert code == 0 and any(f"{JK} 新規 0 件・更新 1 件" in line for line in lines)
    after = {r["id"]: r["content"] for r in db._fetchall("SELECT id, content FROM nodes")}
    changed = {k for k in before if before[k] != after.get(k)}
    assert len(changed) == 2 and all(after[k] == "翼の設計（改訂）" for k in changed)   # Source と Knowledge だけ


def test_missing_notes_are_reported_but_knowledge_is_kept(vaults):
    oarai, jk = vaults
    _run()
    count = len(db._fetchall("SELECT id FROM nodes"))
    (jk / "研究" / "タービン.md").unlink()
    code, lines, _ = _run()
    assert code == 0
    assert any(f"{JK} 新規 0 件・更新 0 件・消えたノート 1 件" in line for line in lines)
    assert any("消えたノート（知識は残しています）: 研究/タービン.md" in line for line in lines)
    assert len(db._fetchall("SELECT id FROM nodes")) == count


def test_log_lines_have_no_note_bodies(vaults):
    oarai, jk = vaults
    (jk / "秘密.md").write_text("本文にしかない語ABC", encoding="utf-8")
    _, lines, _ = _run()
    assert not any("本文にしかない語ABC" in line or "翼の設計" in line for line in lines)


def test_failed_vault_is_skipped_others_continue_and_notified(vaults, monkeypatch):
    original = ing.ingest_vault

    def flaky(source):
        if source.resource_id == JK:
            raise RuntimeError("読めない")
        return original(source)
    monkeypatch.setattr(ing, "ingest_vault", flaky)
    code, lines, notify = _run()
    assert code == 1
    assert any("vault-oarai-obsidian 新規 1 件" in line for line in lines)
    assert any(f"{JK} 失敗: RuntimeError" in line for line in lines)
    assert notify == [(ing.JOB_NAME, f"{JK}: RuntimeError")]


def test_missing_vault_folder_is_a_failure(db_path, ckc_data_dir, make_vault):
    make_vault("oarai-obsidian")                                   # jarvis-knowledge はない
    code, lines, notify = _run()
    assert code == 1 and len(notify) == 1 and JK in notify[0][1]
    assert any("vault-oarai-obsidian 新規 0 件" in line for line in lines)


def test_launchd_plist_runs_scheduled_at_0545():
    path = Path(__file__).resolve().parents[1] / "scripts" / "launchd" / "com.ohide.jarvis.ingest-obsidian.plist"
    data = plistlib.loads(path.read_bytes())
    assert data["Label"] == "com.ohide.jarvis.ingest-obsidian"
    assert data["ProgramArguments"][1:] == ["/Users/ohide/work/0002_Jarvis2/1003_Jarvis_claude/ingest_obsidian.py",
                                            "--scheduled"]
    assert data["StartCalendarInterval"] == {"Hour": 5, "Minute": 45}
    assert data["StandardOutPath"].endswith("/Library/Logs/jarvis/ingest-obsidian.log")
