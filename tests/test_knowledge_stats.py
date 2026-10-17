"""集計のスキル knowledge-stats（T-1401、ADR-0027）のテスト。DB・Registry・索引は一時フォルダ、LLM は呼ばない。"""

import json
import os
import sqlite3

import pytest

import config
import db
import knowledge_cli
import knowledge_engine as ke
import llm
import orchestrator as orc
import skills
from voice.speak import speech_summary

R1003 = "repo-1003-jarvis-claude"
PROJECT_OS = os.path.expanduser("~/work/0002_Jarvis2/oarai-project-os-since2026")
REGISTRY = {
    "projects": {},
    "resources": {
        "vault-oarai-obsidian": {"resource_type": "vault", "git_remote": "https://github.com/unikarei/oarai-obsidian.git",
                                 "locator": {"host-mac-mini": "~/work/obsidian/oarai-obsidian",
                                             "host-windows-pc": "D:\\obsidian\\oarai-obsidian"}},
        "vault-jarvis-knowledge": {"resource_type": "vault",
                                   "git_remote": "https://github.com/unikarei/jarvis-knowledge.git",
                                   "locator": {"host-mac-mini": "~/work/obsidian/jarvis-knowledge"}},
        "repo-oarai-project-os": {"resource_type": "repository", "locator": "~/work/0002_Jarvis2/oarai-project-os-since2026",
                                  "git_remote": "https://github.com/unikarei/oarai-project-os-since2026.git"},
        "repo-workspace": {"resource_type": "repository", "locator": "~/work/0002_Jarvis2",
                           "git_remote": "https://github.com/unikarei/jarvis-workspace.git"},
        "data-insight-reporter-db": {"resource_type": "data_store", "locator": "~/work/0002_Jarvis2/1004/db"},
    },
}


@pytest.fixture
def temp_registry(tmp_path, monkeypatch):
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(REGISTRY, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config, "REGISTRY_JSON", path)
    return path


def _add(url, category, created_at, source=True):
    kid = db.insert_node("Knowledge", category=category, title="t", content="c", url=None if source else url)
    db._execute("UPDATE nodes SET created_at = ? WHERE id = ?", (created_at, kid))
    if source:
        sid = db.insert_node("Source", category=category, title="t", url=url)
        db.insert_edge(kid, sid, "derived_from")
    return kid


@pytest.fixture
def knowledge(db_path, temp_registry):
    for i in range(5):
        _add(f"https://techcrunch.com/a{i}", "news", f"2026-10-1{i}T06:00:00")
    _add("https://example.com/b", "trading", "2026-10-16T07:30:00", source=False)   # Source なし → Knowledge の url
    for i in range(3):
        _add(f"https://github.com/unikarei/oarai-obsidian/blob/main/n{i}.md", "minpaku", f"2026-10-0{i + 1}T09:00:00")
    _add(f"{PROJECT_OS}/data/tasks.json", "minpaku", "2026-10-05T10:00:00")
    _add("https://github.com/unikarei/oarai-project-os-since2026/blob/main/docs/a.md", "minpaku", "2026-10-07T10:00:00")
    _add("insight-reporter://piano_growth/2026-10-12", "piano", "2026-10-12T08:00:00")
    _add(None, None, "2026-09-01T00:00:00")
    _add("https://github.com/unikarei/oarai-obsidian-other/x.md", "news", "2026-10-02T00:00:00")   # 別のリポジトリ


def test_stats_counts_order_names(knowledge):
    s = ke.stats()
    assert s["total"] == 14
    assert [(x["source_id"], x["name"], x["count"]) for x in s["by_source"]] == [
        ("web", "Web の記事（RSS など）", 7),
        ("vault-oarai-obsidian", "Obsidian の Vault「oarai-obsidian」", 3),
        ("repo-oarai-project-os", "リポジトリ「oarai-project-os-since2026」", 2),
        ("data-insight-reporter-db", "Insight Reporter", 1),
        ("unknown", "出所の記録なし", 1),
    ]
    by_id = {x["source_id"]: x for x in s["by_source"]}
    assert by_id["web"]["last_updated"] == "2026-10-16T07:30:00"
    assert by_id["vault-oarai-obsidian"]["last_updated"] == "2026-10-03T09:00:00"
    assert by_id["repo-oarai-project-os"]["last_updated"] == "2026-10-07T10:00:00"
    assert all(x["target"] == "jarvis.db の Knowledge" for x in s["by_source"])
    assert "repo-workspace" not in by_id      # 長く一致する資源を優先する
    assert [(x["category"], x["count"]) for x in s["by_category"]] == [
        ("news", 6), ("minpaku", 5), ("(なし)", 1), ("piano", 1), ("trading", 1)]
    assert s["by_category"][0]["last_updated"] == "2026-10-14T06:00:00"
    assert "registry_unavailable" not in s


def test_unavailable_vault_not_zero(knowledge):
    s = ke.stats()
    assert s["unavailable"] == [{"source_id": "vault-jarvis-knowledge", "name": "Obsidian の Vault「jarvis-knowledge」",
                                 "reason": "jarvis.db に取り込まれていない（件数は未取得）"}]
    assert all(x["count"] > 0 for x in s["by_source"])
    assert "vault-jarvis-knowledge" not in {x["source_id"] for x in s["by_source"]}


def test_registry_unavailable(db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REGISTRY_JSON", tmp_path / "missing.json")
    _add("https://github.com/unikarei/oarai-obsidian/blob/main/n.md", "minpaku", "2026-10-01T09:00:00")
    _add("insight-reporter://piano_growth/x", "piano", "2026-10-02T09:00:00")
    with pytest.warns(UserWarning):
        s = ke.stats()
    assert s["registry_unavailable"] is True and s["unavailable"] == []
    assert {x["source_id"]: x["count"] for x in s["by_source"]} == {"web": 1, "data-insight-reporter-db": 1}


def test_stats_empty_db(db_path, temp_registry):
    s = ke.stats()
    assert s["total"] == 0 and s["by_source"] == [] and s["by_category"] == []
    assert len(s["unavailable"]) == 2


def test_stats_does_not_write(knowledge, temp_registry):
    before = (config.DB_PATH.read_bytes(), temp_registry.read_bytes())
    ke.stats()
    assert (config.DB_PATH.read_bytes(), temp_registry.read_bytes()) == before


def test_stats_missing_db_not_created(tmp_path, monkeypatch, temp_registry, capsys):
    missing = tmp_path / "none" / "jarvis.db"
    monkeypatch.setattr(config, "DB_PATH", missing)
    monkeypatch.setattr(db, "DB_PATH", missing)
    with pytest.raises(sqlite3.OperationalError):
        ke.stats()
    assert knowledge_cli.main(["stats"]) == 1
    assert capsys.readouterr().err.startswith("エラー: ")
    assert not missing.exists()
    missing.parent.mkdir()
    with pytest.raises(sqlite3.OperationalError):
        ke.stats()
    assert not missing.exists()


# ---------------------------------------------------------------- CLI

def test_cli_json(knowledge, capsys):
    assert knowledge_cli.main(["stats", "--json"]) == 0
    out = capsys.readouterr().out
    assert out == json.dumps(ke.stats(), ensure_ascii=False, indent=2) + "\n"


def test_cli_text(knowledge, capsys):
    assert knowledge_cli.main(["stats"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[:4] == [
        "jarvis.db の Knowledge 14 件",
        "データソース別:",
        "順位\t名前\t件数\t最終更新",
        "1\tWeb の記事（RSS など）\t7\t2026-10-16 07:30",
    ]
    assert "未取得: Obsidian の Vault「jarvis-knowledge」" in out
    assert "分野別: news 6 件, minpaku 5 件" in out


@pytest.mark.parametrize("argv", [["stats"], ["stats", "--json"]])
def test_cli_db_error(monkeypatch, db_path, capsys, argv):
    def broken(*a, **k):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(ke, "stats", broken)
    assert knowledge_cli.main(argv) == 1
    out, err = capsys.readouterr()
    assert out == "" and err == "エラー: unable to open database file\n"


# ---------------------------------------------------------------- 会話・声

def _s(desc, phrases=(), inputs=None):
    return {"description": desc, "status": "active", "permission": "read", "entry": {"repo": R1003, "function": "x"},
            "inputs": inputs or {}, "phrases": list(phrases)}


@pytest.fixture
def index(tmp_path, monkeypatch):
    path = tmp_path / "skills.json"
    path.write_text(json.dumps({"skills": {
        "knowledge-answer": _s("普通の質問に答える", inputs={"question": {"type": "string", "required": True}}),
        "knowledge-search": _s("知識を検索する", ["について知ってること", "を探して"],
                               {"query": {"type": "string", "required": True}}),
        "knowledge-stats": _s("ナレッジの件数をデータソース別・分野別に集計する",
                              ["件数", "何件", "多い順", "データソース", "上位"],
                              {"limit": {"type": "integer"}, "by": {"type": "string"}}),
    }}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("JARVIS_SKILLS_JSON", str(path))
    skills.reset_cache()

    def refuse(*a, **k):
        raise AssertionError("LLM を呼ばない")
    monkeypatch.setattr(llm, "complete", refuse)
    monkeypatch.setattr(llm, "complete_stream", refuse)
    yield
    skills.reset_cache()


QUESTION = "Jarvisにつながっているナレッジデータベースを、登録件数の多い順に3つ教えて"


def test_conversation_top3(knowledge, index):
    out = orc.handle(QUESTION, orc.Context(), ask=lambda q: pytest.fail("確認しない"), use_llm=True)
    assert (out.skill, out.method, out.ok) == ("knowledge-stats", "rule", True)
    assert out.text.splitlines() == [
        "jarvis.db の Knowledge 14 件のうち、データソース別の上位 3 つ:",
        "1. Web の記事（RSS など）: 7 件（最終更新 2026-10-16 07:30）",
        "2. Obsidian の Vault「oarai-obsidian」: 3 件（最終更新 2026-10-03 09:00）",
        "3. リポジトリ「oarai-project-os-since2026」: 2 件（最終更新 2026-10-07 10:00）",
        "未取得: Obsidian の Vault「jarvis-knowledge」（jarvis.db に取り込まれていない）",
    ]


def test_conversation_by_category(knowledge, index):
    out = orc.handle("分野別の件数は？", orc.Context(), use_llm=True)
    assert out.skill == "knowledge-stats"
    lines = out.text.splitlines()
    assert lines[0] == "jarvis.db の Knowledge 14 件のうち、分野別の件数:"
    assert lines[1] == "1. news: 6 件（最終更新 2026-10-14 06:00）"
    assert len(lines) == 6 and not any("未取得" in line for line in lines)


def test_conversation_explicit_inputs(knowledge, index):
    msg, items = orc._stats({"limit": 2, "by": "category"}, orc.Context(), "集計して")
    assert msg.splitlines()[0] == "jarvis.db の Knowledge 14 件のうち、分野別の上位 2 つ:"
    assert len(msg.splitlines()) == 3 and items == []


@pytest.mark.parametrize("text, n", [
    ("多い順に3つ", 3), ("三つ教えて", 3), ("上位5のデータソース", 5), ("トップ３", 3), ("上位十", 10),
    ("件数を全部", None), ("何件ある？", None),
])
def test_stats_limit(text, n):
    assert orc._stats_limit(text) == n


def test_speech_summary_top3(knowledge, index):
    out = orc.handle(QUESTION, orc.Context(), use_llm=True)
    assert speech_summary("knowledge-stats", out.text) == (
        "上位は、Web の記事、7 件。Obsidian の Vault oarai-obsidian、3 件。"
        "リポジトリ oarai-project-os-since2026、2 件です。")


def test_speech_summary_reads_only_three():
    text = ("jarvis.db の Knowledge 1000 件のうち、データソース別の件数:\n"
            "1. Web の記事（RSS など）: 966 件（最終更新 2026-10-17 06:00）\n"
            "2. Obsidian の Vault「oarai-obsidian」: 31 件（最終更新 2026-10-16 09:00）\n"
            "3. リポジトリ「oarai-project-os-since2026」: 25 件（最終更新 2026-10-15 10:00）\n"
            "4. Insight Reporter: 3 件（最終更新 2026-10-12 08:00）\n"
            "未取得: Obsidian の Vault「jarvis-knowledge」（jarvis.db に取り込まれていない）")
    assert speech_summary("knowledge-stats", text) == (
        "上位は、Web の記事、966 件。Obsidian の Vault oarai-obsidian、31 件。"
        "リポジトリ oarai-project-os-since2026、25 件です。")
