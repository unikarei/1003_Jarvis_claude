"""Architecture Registry の読み込み（T-310）のテスト。registry.json はテスト用に作る。"""

import json

import pytest

import config
import registry
import vault
from ckc.route import default_category, route_vault


def _write_registry(path, **override):
    data = {
        "projects": {
            "jarvis": {"project_id": "jarvis", "default_vault": "vault-jarvis-knowledge", "jarvis_category": None},
            "oarai-minpaku": {"project_id": "oarai-minpaku", "default_vault": "vault-oarai-obsidian",
                              "jarvis_category": "minpaku"},
            "ai-news": {"project_id": "ai-news", "default_vault": "vault-jarvis-knowledge", "jarvis_category": "news"},
        },
        "resources": {
            "vault-jarvis-knowledge": {"resource_type": "vault",
                                       "locator": {"host-mac-mini": "~/somewhere/jarvis-knowledge",
                                                   "host-windows-pc": "unknown"}},
            "vault-oarai-obsidian": {"resource_type": "vault", "locator": "/abs/oarai-obsidian"},
            "vault-only-on-phone": {"resource_type": "vault", "locator": {"host-pixel-6": "Documents/x"}},
            "repo-x": {"resource_type": "repository", "locator": "~/repo-x"},
        },
    }
    data.update(override)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def registry_json(tmp_path, monkeypatch):
    path = tmp_path / "registry.json"
    _write_registry(path)
    monkeypatch.setattr(config, "REGISTRY_JSON", path)
    return path


def test_projects_come_from_registry(registry_json):
    assert registry.projects() == {"jarvis": ("vault-jarvis-knowledge", None),
                                   "oarai-minpaku": ("vault-oarai-obsidian", "minpaku"),
                                   "ai-news": ("vault-jarvis-knowledge", "news")}
    assert default_category(["ai-news"]) == "news"
    assert route_vault(["oarai-minpaku"]) == "vault-oarai-obsidian"


def test_vault_paths_use_this_hosts_locator(registry_json, monkeypatch, tmp_path):
    monkeypatch.delenv("JARVIS_VAULT_ROOT", raising=False)
    paths = registry.vault_paths()
    assert paths["vault-jarvis-knowledge"].as_posix().endswith("/somewhere/jarvis-knowledge")
    assert not str(paths["vault-jarvis-knowledge"]).startswith("~")
    assert paths["vault-oarai-obsidian"].as_posix() == "/abs/oarai-obsidian"
    assert "vault-only-on-phone" not in paths and "repo-x" not in paths


def test_vault_root_env_keeps_dev_and_tests_isolated(registry_json, vault_root):
    """JARVIS_VAULT_ROOT があれば、Registry の場所ではなく VAULT_ROOT の下を使う（本番の Vault に触れない）。"""
    assert vault.resolve_vault("vault-jarvis-knowledge") == vault_root / "jarvis-knowledge"


def test_missing_registry_falls_back_with_warning(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REGISTRY_JSON", tmp_path / "none.json")
    with pytest.warns(UserWarning, match="予備の設定値"):
        assert registry.projects() == config.FALLBACK_PROJECTS
    assert set(registry.vault_paths()) == set(config.FALLBACK_VAULTS)


@pytest.mark.parametrize("content", ["{壊れた", json.dumps({"projects": []})])
def test_broken_registry_falls_back(tmp_path, monkeypatch, content):
    path = tmp_path / "registry.json"
    path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(config, "REGISTRY_JSON", path)
    with pytest.warns(UserWarning):
        assert registry.projects() == config.FALLBACK_PROJECTS


def test_fallback_matches_registry_projects():
    """予備の値が Registry（2026-10-12 時点）と食い違っていないこと。"""
    assert set(config.FALLBACK_PROJECTS) == {"jarvis", "oarai-minpaku", "piano", "turbine-research", "physical-ai",
                                            "ai-news", "trading", "realestate", "personal"}
