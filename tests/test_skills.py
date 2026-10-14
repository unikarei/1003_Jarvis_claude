"""スキルの索引の読み込み（FR-014、T-1206）のテスト。索引は一時フォルダに作る。"""

import json
import warnings

import pytest

import config
import registry
import skills


def _skill(**over):
    raw = {"description": "検索する", "status": "active", "permission": "read",
           "entry": {"repo": "repo-1003-jarvis-claude", "function": "knowledge_engine.search"},
           "inputs": {"query": {"type": "string", "required": True, "description": "語句"}},
           "phrases": ["を探して"], "verify": "出典がある", "path": "skills/x/SKILL.md"}
    raw.update(over)
    return raw


@pytest.fixture
def index(tmp_path, monkeypatch):
    path = tmp_path / "skills.json"
    monkeypatch.setenv("JARVIS_SKILLS_JSON", str(path))
    skills.reset_cache()
    yield path
    skills.reset_cache()


def _write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_load_and_active(index):
    _write(index, {"skills": {
        "knowledge-search": _skill(),
        "theme-register": _skill(permission="local_write", phrases=["テーマ登録:"]),
        "old": _skill(status="deprecated", phrases=[]),
        "idea": _skill(status="candidate", phrases=[]),
    }})
    loaded = skills.load()
    assert set(loaded) == {"knowledge-search", "theme-register", "old", "idea"}
    assert set(skills.active()) == {"knowledge-search", "theme-register"}
    s = loaded["knowledge-search"]
    assert s.permission == "read" and s.entry["function"] == "knowledge_engine.search"
    assert s.phrases == ("を探して",) and s.required_inputs() == ["query"]


@pytest.mark.parametrize("content", [None, "{壊れた JSON", json.dumps({"other": {}}), json.dumps([1, 2])])
def test_unreadable_index_falls_back_with_one_warning(index, content):
    if content is not None:
        index.write_text(content, encoding="utf-8")
    with pytest.warns(UserWarning, match="予備の索引"):
        first = skills.load()
    assert set(first) == {"knowledge-answer", "knowledge-capture", "theme-register", "code-task"}
    assert first["knowledge-capture"].permission == "local_write" and "ナレッジ化して" in first["knowledge-capture"].phrases
    assert first["knowledge-answer"].entry["function"] == "knowledge_engine.answer"
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        skills.load()          # 2 回目は警告を出さない


@pytest.mark.parametrize("bad", [
    {"status": "enabled"}, {"permission": "admin"}, {"entry": None}, {"entry": {"function": "x"}},
    {"inputs": ["query"]}, {"phrases": "を探して"}, {"phrases": [""]},
])
def test_invalid_skill_is_skipped(index, bad):
    _write(index, {"skills": {"good": _skill(phrases=["良い"]), "bad": _skill(**bad)}})
    with pytest.warns(UserWarning, match="スキル bad を読み飛ばします"):
        loaded = skills.load()
    assert set(loaded) == {"good"}


def test_index_path_order(tmp_path, monkeypatch):
    monkeypatch.delenv("JARVIS_SKILLS_JSON", raising=False)
    reg = tmp_path / "registry.json"
    monkeypatch.setattr(config, "REGISTRY_JSON", reg)
    # Registry に repo-jarvis-skills がなければ config の既定
    _write(reg, {"projects": {}, "resources": {}})
    registry.reset_cache()
    assert skills.index_path() == config.SKILLS_JSON
    # Registry にあれば、その locator の generated/skills.json
    _write(reg, {"projects": {}, "resources": {"repo-jarvis-skills": {
        "resource_type": "repository", "locator": str(tmp_path / "jarvis-skills")}}})
    registry.reset_cache()
    assert skills.index_path() == tmp_path / "jarvis-skills" / "generated" / "skills.json"
    # 環境変数が最優先
    monkeypatch.setenv("JARVIS_SKILLS_JSON", str(tmp_path / "x.json"))
    assert skills.index_path() == tmp_path / "x.json"
    registry.reset_cache()
