"""chat.py のテスト（CKC 連携 T-115、テーマ登録、FR-014 の振り分け T-1208）。LLM はモック。"""

import importlib
import io
import json
import sys

import pytest

import llm
import skills
from ckc import store
from ckc.models import CandidateStatus, Origin


@pytest.fixture
def chat(monkeypatch):
    # chat.py は読み込み時に標準入出力を UTF-8 に設定するため、設定できるダミーに差し替えてから読み込む
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO()))
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(io.BytesIO()))
    sys.modules.pop("chat", None)
    module = importlib.import_module("chat")
    monkeypatch.setattr(sys, "stdout", sys.__stdout__)
    skills.reset_cache()      # 索引は置かない（予備の索引で、従来の機能が動くことを確かめる）
    yield module
    skills.reset_cache()


def _inputs(monkeypatch, *answers):
    it = iter(answers)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(it))


def test_chat_records_turns_and_runs_ckc(chat, db_path, ckc_data_dir, make_vault, monkeypatch):
    jk = make_vault("jarvis-knowledge")
    item = {"kind": "Decision", "title": "週次で確認する", "body": "レポートは週次で確認する", "projects": ["jarvis"],
            "category": None, "confidence": "confirmed",
            "evidence": [{"quote": "レポートは週次で確認することにする", "speaker": "owner"}]}
    monkeypatch.setattr(llm, "complete", lambda prompt, provider=None: json.dumps({"items": [item]}, ensure_ascii=False))
    # 1行目は普通の質問（振り分けの LLM は不正な応答 → knowledge-answer）。ナレッジ化は書き込みがあるので確認（y）
    _inputs(monkeypatch, "レポートは週次で確認することにする", "ナレッジ化して", "y", "a", "y",
            "ナレッジ化して", "y", "exit")
    with pytest.warns(UserWarning, match="予備の索引"):
        chat.main()

    [c] = store.list_candidates()
    assert c.status == CandidateStatus.PUBLISHED
    assert store.load_conversation(c.conversation_id).origin == Origin.JARVIS
    assert len(list((jk / "_jarvis" / "knowledge" / "Decision").glob("*.md"))) == 1
    assert len(store.list_candidates()) == 1  # 2回目の「ナレッジ化して」は対話がリセット済みで何もしない


def test_chat_ckc_declined_writes_nothing(chat, db_path, ckc_data_dir, monkeypatch):
    monkeypatch.setattr(llm, "complete", lambda prompt, provider=None: "答え")
    _inputs(monkeypatch, "質問です", "確認せずにナレッジ化して", "n", "exit")
    with pytest.warns(UserWarning):
        chat.main()
    assert store.list_candidates() == []


def test_chat_theme_flow_declined_creates_nothing(chat, db_path, ckc_data_dir, monkeypatch, capsys):
    import architect
    proposal = architect.Proposal("arch_x", "t", "2026-10-13", [], False, None, [{"title": "n", "purpose": "p"}])
    themes = []
    monkeypatch.setattr(architect, "propose", lambda theme: themes.append(theme) or proposal)
    applied = []
    monkeypatch.setattr(architect, "apply", lambda p: applied.append(p))
    # 実行の確認（y）→ 提案の表示 → 適用の確認（n）
    _inputs(monkeypatch, "テーマ登録: 家具の購入計画", "y", "n", "exit")
    with pytest.warns(UserWarning):
        chat.main()
    assert themes == ["家具の購入計画"] and applied == [] and "arch_x" in capsys.readouterr().out


def test_chat_question_answers_with_sources_and_traces(chat, db_path, monkeypatch, capsys, tmp_path):
    import db
    # Phase 1 の索引（予備の索引に、出典の追跡を加えたもの）
    index = {n: {"description": s.description, "status": s.status, "permission": s.permission, "entry": s.entry,
                 "inputs": s.inputs, "phrases": list(s.phrases)} for n, s in skills.FALLBACK.items()}
    index["knowledge-trace"] = {"description": "出典をたどる", "status": "active", "permission": "read",
                                "entry": {"repo": "repo-1003-jarvis-claude", "function": "knowledge_engine.trace"},
                                "inputs": {"index": {"type": "integer"}}, "phrases": ["出典は", "の出典"]}
    (tmp_path / "skills.json").write_text(json.dumps({"skills": index}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("JARVIS_SKILLS_JSON", str(tmp_path / "skills.json"))
    skills.reset_cache()
    db.init_db()
    k1 = db.insert_node("Knowledge", category="minpaku", title="大洗の補助金", content="民泊の補助金の条件",
                        url="https://example.com/a")
    s1 = db.insert_node("Source", category="minpaku", title="補助金の記事", url="https://example.com/src")
    db.insert_edge(k1, s1, "derived_from")
    k2 = db.insert_node("Knowledge", category="minpaku", title="補助金の申請期限", content="民泊の補助金の期限",
                        url="https://example.com/b")

    def fake(prompt, provider=None):
        return "振り分けません" if "振り分け係" in prompt else "補助金には条件があります [1]"
    monkeypatch.setattr(llm, "complete", fake)
    _inputs(monkeypatch, "民泊の補助金の条件は？", "その出典は？", "2 番目の出典は？", "exit")
    chat.main()
    out = capsys.readouterr().out
    assert "補助金には条件があります" in out and "https://example.com/" in out      # 答えに出典
    import re
    order = re.findall(r"^\[(\d)\] (\S+)", out, re.M)                                 # 答えの出典の順
    assert [t for _, t in order] and set(t for _, t in order) == {"大洗の補助金", "補助金の申請期限"}
    traced = re.findall(r"知識: (\S+)（", out)
    assert traced == [order[0][1], order[1][1]]                    # 「その出典は」→ 1 件目、「2 番目」→ 2 件目
    assert "← 出典: 補助金の記事  https://example.com/src" in out
    assert k2
    # 3 回の発言がそれぞれ Task として記録される（T-1209）
    tasks = db._fetchall("SELECT meta FROM nodes WHERE type = 'Task' ORDER BY created_at")
    assert [json.loads(t["meta"])["skill"] for t in tasks] == ["knowledge-answer", "knowledge-trace", "knowledge-trace"]
