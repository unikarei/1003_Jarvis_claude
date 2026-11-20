"""会話の始めの概要（T-3200、ワークスペースの testing §31）。一時 DB・一時 Vault で動かす。"""
import sqlite3

import pytest

import briefing
import config
import orchestrator as orc
import prompts
import registry
import vault


@pytest.fixture
def vaults(tmp_path, monkeypatch):
    oarai, general = tmp_path / "oarai", tmp_path / "general"
    for path in (oarai / "民泊@大洗" / "00_管理", oarai / "_jarvis", general / "_jarvis" / "knowledge"):
        path.mkdir(parents=True)
    (oarai / "民泊@大洗" / "00_管理" / "大洗_最優先ToDo.md").write_text("# ToDo\n", encoding="utf-8")
    (oarai / "_jarvis" / "auto.md").write_text("x", encoding="utf-8")
    (general / "メモ.md").write_text("x", encoding="utf-8")
    paths = {"vault-oarai-obsidian": oarai, "vault-jarvis-knowledge": general}
    monkeypatch.setattr(registry, "vault_paths", lambda: paths)
    monkeypatch.setattr(vault, "resolve_vault", lambda rid: paths[rid])
    return paths


def test_briefing_lists_knowledge_vault_notes_memory_and_capabilities(vaults):
    text = briefing.build()
    assert "## ナレッジ" in text and "保存待ちの知識の候補" in text
    assert "大洗（oarai-obsidian）: ノート 2 件" in text and "民泊@大洗/00_管理/大洗_最優先ToDo" in text
    assert "自動の知識（_jarvis/）1 件" in text and "_jarvis/auto" not in text
    assert "汎用（jarvis-knowledge）: ノート 1 件" in text and "メモ" in text
    assert "## 会話の記憶" in text
    assert "ToDo・予定・リマインダーの専用の機能はない" in text
    assert "「Codex にして」" in text
    assert len(text) <= briefing.MAX_CHARS


def test_briefing_shortens_long_note_lists(vaults):
    for i in range(200):
        (vaults["vault-oarai-obsidian"] / f"とても長い名前のノートの例_{i:03d}_大洗の調査の記録.md").write_text("x", encoding="utf-8")
    text = briefing.build()
    assert len(text) <= briefing.MAX_CHARS and "ほか" in text


def test_briefing_shows_unknown_when_db_is_missing(vaults, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "none.db")
    text = briefing.build()
    assert "保存待ちの知識の候補: 未確認 件" in text
    assert not (tmp_path / "none.db").exists()            # 読むだけで作らない


def test_briefing_does_not_write(vaults):
    before = {p: p.stat().st_mtime_ns for root in vaults.values() for p in root.rglob("*")}
    db_before = config.DB_PATH.stat().st_mtime_ns if config.DB_PATH.exists() else None
    briefing.build()
    assert before == {p: p.stat().st_mtime_ns for root in vaults.values() for p in root.rglob("*")}
    assert db_before == (config.DB_PATH.stat().st_mtime_ns if config.DB_PATH.exists() else None)


def test_for_ctx_builds_once_and_survives_renew(monkeypatch):
    calls = []
    monkeypatch.setattr(briefing, "build", lambda: calls.append(1) or "概要")
    ctx = orc.Context()
    assert briefing.for_ctx(ctx) == "概要" and briefing.for_ctx(ctx) == "概要" and calls == [1]
    assert briefing.for_ctx(orc.renew_context(ctx, voice=True, terminal=False)) == "概要" and calls == [1]


def test_for_ctx_failure_keeps_conversation(monkeypatch):
    def boom():
        raise RuntimeError("x")
    monkeypatch.setattr(briefing, "build", boom)
    assert briefing.for_ctx(orc.Context()) == ""


def test_briefing_goes_into_rules_and_one_shot():
    text = "# 今あなた（JARVIS）が使えるもの\n- {波かっこ} を含む"
    assert text in prompts.rules(tools=True, briefing=text)
    for kind in ("chat", "answer"):
        prompt = prompts.one_shot(kind, "質問", "資料", "", "", False, briefing=text)
        assert text in prompt and "質問" in prompt
    assert "# 今あなた（JARVIS）が使えるもの" not in prompts.rules(tools=False)   # 概要がなければ入れない


def test_rules_guide_obsidian_editing_instead_of_refusing():
    for tools in (True, False):
        r = prompts.rules(tools=tools)
        assert "Obsidian の大洗_最優先ToDo に〇〇を追加して" in r


def test_harness_session_start_uses_briefing(monkeypatch):
    import harness_session
    import llm
    seen = {}
    monkeypatch.setattr(briefing, "build", lambda: "# 今あなた（JARVIS）が使えるもの（試験）")
    monkeypatch.setattr(llm, "ClaudeSession", lambda rules: seen.setdefault("rules", rules))
    harness_session._start(orc.Context(), "claude")
    assert "使えるもの（試験）" in seen["rules"]
