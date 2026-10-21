"""評価部門の評価セット（疑似 Q&A、T-1410、ADR-0030）のテスト。DB・Registry・保存先は一時フォルダ、LLM は偽物。"""

import json
import os
import re
import sqlite3
import subprocess
import sys
from collections import Counter

import pytest

import config
import db
import evaluation
import knowledge_engine as ke
import llm
from evaluation import __main__ as cli
from evaluation import isolate, qa

WEB = "https://example.com/a"
OBSIDIAN = "https://github.com/unikarei/oarai-obsidian/blob/main/n"
JK = "https://github.com/unikarei/jarvis-knowledge/blob/main/n"
FILLER = "これは評価のための説明の文章です。" * 6    # 80 字以上にする


@pytest.fixture(autouse=True)
def eval_dir(tmp_path, monkeypatch):
    path = tmp_path / "eval"
    monkeypatch.setattr(evaluation, "EVAL_DIR", path)
    monkeypatch.setenv("JARVIS_EVAL_DIR", str(path))   # 子のプロセスにも渡す
    for name in (isolate.ISOLATED_ENV, isolate.SOURCE_DB_ENV, isolate.COPIED_AT_ENV):
        monkeypatch.delenv(name, raising=False)
    return path


_seq = iter(range(1, 10 ** 6))


def add(title, content, url):
    n = next(_seq)   # 短縮 ID（4 桁）の衝突を避けるため ID を決めて入れる
    kid = db.insert_node("Knowledge", category="news", title=title, content=content, node_id=f"knowledge_news_{n:06d}")
    sid = db.insert_node("Source", category="news", title=title, url=url, node_id=f"source_news_{n:06d}")
    db.insert_edge(kid, sid, "derived_from")
    return kid


def body(*facts):
    return FILLER + "".join(f"答えは{f}です。" for f in facts)


def populate(counts):
    """{url の先頭: 件数} で、要点 KEY<番号> を 1 つずつ持つ知識を作る。"""
    ids, i = [], 0
    for prefix, count in counts.items():
        for _ in range(count):
            i += 1
            ids.append(add(f"題名{i}", body(f"KEY{i:03d}"), f"{prefix}{i}.md"))
    return ids


def fake_llm(prompt):
    """プロンプトの本文にある KEY<番号> をすべて要点にする。"""
    content = prompt.split("# 知識の本文", 1)[1]
    facts = sorted(set(re.findall(r"KEY\d+", content)))
    return json.dumps({"question": "あの件はどうなっていたっけ", "key_facts": facts}, ensure_ascii=False)


def echo_answer(question, content):
    return content


# --- データソースの判定の共有 ---

def test_source_by_knowledge_matches_stats(db_path):
    populate({WEB: 3, OBSIDIAN: 2, JK: 1})
    db.insert_node("Knowledge", title="出所なし", content="x")
    counts = Counter(ke.source_by_knowledge().values())
    assert counts == {"web": 3, "vault-oarai-obsidian": 2, "vault-jarvis-knowledge": 1, "unknown": 1}
    assert {s["source_id"]: s["count"] for s in ke.stats()["by_source"]} == counts


# --- 選び方 ---

def test_select_is_stratified_excludes_short_and_reproducible(db_path):
    populate({WEB: 10, OBSIDIAN: 4, JK: 10})
    short = add("短い", "短い本文 KEY999", WEB + "short")
    picked = qa.select_knowledge(12, seed=1)
    assert Counter(k["source"] for k in picked) == {"web": 4, "vault-oarai-obsidian": 4, "vault-jarvis-knowledge": 4}
    picked = qa.select_knowledge(18, seed=1)
    assert Counter(k["source"] for k in picked) == {"web": 7, "vault-oarai-obsidian": 4, "vault-jarvis-knowledge": 7}
    assert short not in {k["id"] for k in qa.select_knowledge(100, seed=1)}
    assert [k["id"] for k in qa.select_knowledge(12, seed=1)] == [k["id"] for k in qa.select_knowledge(12, seed=1)]


# --- 作問 ---

def test_generate_from_fake_llm_with_style_instruction():
    prompts = []

    def fake(prompt):
        prompts.append(prompt)
        return '```json\n{"question": "大洗の物件ってどこだっけ", "key_facts": ["茨城県東茨城郡大洗町"]}\n```'

    k = {"id": "knowledge_minpaku_1", "title": "大洗_物件概要", "content": "所在地: 茨城県東茨城郡大洗町" + "あ" * 3000,
         "source": "vault-oarai-obsidian"}
    item = qa.generate(k, fake, seed=0)
    assert item["question"] == "大洗の物件ってどこだっけ"
    assert item["key_facts"] == ["茨城県東茨城郡大洗町"]
    assert (item["knowledge_id"], item["source"]) == ("knowledge_minpaku_1", "vault-oarai-obsidian")
    assert item["style"] in qa.STYLES
    assert qa.STYLES[item["style"]] in prompts[0]
    assert "大洗_物件概要" in prompts[0] and "あ" * (qa.CONTENT_CHARS - 20) in prompts[0]
    assert "あ" * qa.CONTENT_CHARS not in prompts[0]                  # 本文は先頭 2,000 字まで
    assert qa.generate(k, fake, seed=0)["style"] == item["style"]       # 同じ種で同じ形
    styles = {qa.pick_style(f"k{i}", 0) for i in range(40)}
    assert styles == set(qa.STYLES)                                     # 形が混ざる


def test_generate_unreadable_json_is_rejected():
    item = qa.generate({"id": "k", "title": "t", "content": "c"}, lambda p: "すみません、作れません", 0)
    assert item["question"] is None
    assert qa.validate(item, "c", echo_answer) == "invalid_json"


@pytest.mark.parametrize("facts, reason", [
    (["茨城県東茨城郡大洗町", 1, None, "木造"], "key_facts_count"),     # 元の配列は 4 個
    (["茨城県東茨城郡大洗町", 1], "key_facts_invalid"),               # 文字列でない要素
    ("茨城県東茨城郡大洗町", "key_facts_invalid"),                    # 配列でない
])
def test_generate_keeps_malformed_key_facts_for_rejection(facts, reason):
    text = json.dumps({"question": "大洗の物件の住所は？", "key_facts": facts}, ensure_ascii=False)
    it = qa.generate({"id": "k", "title": "t", "content": CONTENT}, lambda p: text, 0)
    assert qa.validate(it, CONTENT, echo_answer) == reason


# --- 検証 ---

CONTENT = "大洗_物件概要\n所在地: 茨城県東茨城郡大洗町 磯浜町１２３\n構造: 木造２階建て"


def item(question="大洗の物件の住所は？", facts=("茨城県東茨城郡大洗町",)):
    return {"question": question, "key_facts": list(facts)}


@pytest.mark.parametrize("it, reason", [
    (item(facts=("千葉県",)), "fact_not_in_content"),
    (item(question="茨城県東茨城郡大洗町の物件の住所は？"), "fact_in_question"),
    ({"question": None, "key_facts": None}, "invalid_json"),
    (item(question=""), "question_empty"),
    (item(question="あ" * 201), "question_too_long"),
    (item(facts=()), "key_facts_count"),
    (item(facts=("大洗町", "木造", "２階建て", "所在地")), "key_facts_count"),
])
def test_validate_rejects(it, reason):
    assert qa.validate(it, CONTENT, echo_answer) == reason


def test_validate_normalizes_width_space_punctuation():
    # 本文は全角の「１２３」と空白、要点は半角・空白なし・句読点つき
    assert qa.validate(item(facts=("大洗町磯浜町123。",)), CONTENT, echo_answer) is None
    assert qa.validate(item(question="ＫＥＹ１の物件は？", facts=("key1",)), CONTENT + " KEY1", echo_answer) \
        == "fact_in_question"


def test_validate_answer_check_and_ambiguity():
    asked = []

    def unsure(question, content):
        asked.append((question, content))
        return "不明"

    assert qa.validate(item(), CONTENT, unsure) == "not_answerable"
    assert asked == [("大洗の物件の住所は？", CONTENT)]
    others = [qa.normalize(f"別の知識 {i} 茨城県東茨城郡大洗町") for i in range(qa.AMBIGUOUS_SOURCES)]
    assert qa.validate(item(), CONTENT, echo_answer, others) == "ambiguous"
    assert qa.validate(item(), CONTENT, echo_answer, others[:-1]) is None
    # 要点の 1 つでもほかに少なければ残す
    assert qa.validate(item(facts=("茨城県東茨城郡大洗町", "木造")), CONTENT, echo_answer, others) is None
    # 要点ごとに別の知識が多くても、すべてを含む知識が少なければ残す
    split_others = others + [qa.normalize(f"別の知識 木造 {i}") for i in range(qa.AMBIGUOUS_SOURCES)]
    assert qa.validate(item(facts=("茨城県東茨城郡大洗町", "木造")), CONTENT, echo_answer, split_others) is None
    both = [qa.normalize(f"別の知識 {i} 茨城県東茨城郡大洗町 木造") for i in range(qa.AMBIGUOUS_SOURCES)]
    assert qa.validate(item(facts=("茨城県東茨城郡大洗町", "木造")), CONTENT, echo_answer, both) == "ambiguous"


def test_build_counts_reject_reasons(db_path):
    ids = populate({WEB: 6})
    bad = {ids[0]: "壊れた応答", ids[1]: '{"question": "KEY002 は？", "key_facts": ["KEY002"]}',
           ids[2]: '{"question": "何？", "key_facts": ["存在しない語"]}'}

    def fake(prompt):
        for kid, text in bad.items():
            if f"題名{ids.index(kid) + 1}\n" in prompt:
                return text
        if "題名4\n" in prompt:
            raise llm.LLMError("落ちた")
        return fake_llm(prompt)

    def answer(question, content):
        return "不明" if "KEY005" in content else content

    result = qa.build(n=6, seed=0, split=(2, 2, 2), llm_complete=fake, answer_check=answer)
    assert result.info["rejected"] == {"invalid_json": 1, "fact_in_question": 1, "fact_not_in_content": 1,
                                       "llm_error": 1, "not_answerable": 1}
    assert [i["knowledge_id"] for i in result.items] == [ids[5]]
    assert result.info["attempts"] == 6


def test_build_rejects_ambiguous_facts(db_path):
    for i in range(qa.AMBIGUOUS_SOURCES + 1):
        add(f"共通{i}", body("KEY900"), f"{WEB}{i}")
    result = qa.build(n=3, seed=0, split=(1, 1, 1), llm_complete=fake_llm, answer_check=echo_answer)
    assert result.items == []
    assert result.info["rejected"] == {"ambiguous": qa.AMBIGUOUS_SOURCES + 1}


# --- build ---

def test_build_reaches_n_with_split_ratio_and_fields(db_path):
    populate({WEB: 30, OBSIDIAN: 30, JK: 30})
    result = qa.build(n=60, seed=0, llm_complete=fake_llm, answer_check=echo_answer)
    items = result.items
    assert len(items) == 60 and result.info["attempts"] == 60
    assert Counter(i["split"] for i in items) == {"dev": 30, "val": 15, "test": 15}
    assert Counter(i["source"] for i in items) == {"web": 20, "vault-oarai-obsidian": 20, "vault-jarvis-knowledge": 20}
    for source in ("web", "vault-oarai-obsidian", "vault-jarvis-knowledge"):   # データソースの割合を保つ
        assert Counter(i["split"] for i in items if i["source"] == source) == {"dev": 10, "val": 5, "test": 5}
    assert set(items[0]) == {"id", "question", "key_facts", "knowledge_id", "title", "source", "style", "group",
                             "split"}
    assert len({i["id"] for i in items}) == 60 and len({i["group"] for i in items}) == 60
    again = qa.build(n=60, seed=0, llm_complete=fake_llm, answer_check=echo_answer)
    assert again.items == items                                                 # 同じ種で同じ評価セット
    other = qa.build(n=60, seed=1, llm_complete=fake_llm, answer_check=echo_answer)
    assert [i["split"] for i in other.items] != [i["split"] for i in items] or \
        [i["knowledge_id"] for i in other.items] != [i["knowledge_id"] for i in items]


def test_build_stops_at_attempt_limit(db_path):
    populate({WEB: 20})
    calls = []

    def broken(prompt):
        calls.append(prompt)
        return "not json"

    result = qa.build(n=5, seed=0, split=(3, 1, 1), llm_complete=broken, answer_check=echo_answer)
    assert result.items == [] and len(calls) == 15 == result.info["attempts"]
    assert result.info["rejected"] == {"invalid_json": 15}


def test_build_keeps_same_fact_groups_in_one_split(db_path):
    populate({WEB: 5, OBSIDIAN: 5})
    same_a = add("大洗_物件概要", body("KEY101"), OBSIDIAN + "a.md")
    same_b = add("大洗 物件概要", body("KEY102"), JK + "b.md")                  # 題名の正規化が同じ
    over_a = add("物件の価格", body("KEY201"), WEB + "p")
    over_b = add("物件の価格の記録", body("KEY201", "KEY202"), JK + "q.md")      # 要点が重なる
    for seed in range(5):
        result = qa.build(n=14, seed=seed, split=(8, 3, 3), llm_complete=fake_llm, answer_check=echo_answer)
        by_k = {i["knowledge_id"]: i for i in result.items}
        assert len(result.items) == 14
        for a, b in ((same_a, same_b), (over_a, over_b)):
            assert by_k[a]["group"] == by_k[b]["group"]
            assert by_k[a]["split"] == by_k[b]["split"]
        assert by_k[same_a]["group"] != by_k[over_a]["group"]
        splits = {}
        for i in result.items:
            splits.setdefault(i["group"], set()).add(i["split"])
        assert all(len(s) == 1 for s in splits.values())
        assert Counter(i["split"] for i in result.items) == {"dev": 8, "val": 3, "test": 3}


@pytest.mark.parametrize("sizes, split", [
    ((4, 4, 3, 2, 2), (7, 4, 4)),            # 大きい順の貪欲では 8・4・3 になる例
    ((5, 3, 3, 2, 2), (5, 5, 5)),
    ((3, 3, 3, 3, 1, 1, 1), (9, 3, 3)),
])
def test_assign_splits_matches_counts_when_possible(sizes, split):
    items = []
    for g, size in enumerate(sizes):
        for m in range(size):
            items.append({"knowledge_id": f"k{g}_{m}", "title": f"群{g}", "key_facts": [f"F{g}_{m}"],
                          "source": "src"})
    contents = {i["knowledge_id"]: "無関係な本文" for i in items}
    for seed in range(10):
        for i in items:
            i.pop("split", None)
        qa.assign_splits(items, contents, split, seed)
        assert Counter(i["split"] for i in items) == dict(zip(qa.SPLITS, split))
        by_group = {}
        for i in items:
            by_group.setdefault(i["group"], set()).add(i["split"])
        assert len(by_group) == len(sizes) and all(len(s) == 1 for s in by_group.values())


def test_build_rejects_split_not_matching_n(db_path):
    with pytest.raises(ValueError):
        qa.build(n=10, split=(5, 2, 2), llm_complete=fake_llm, answer_check=echo_answer)


# --- 保存と読み込み ---

def test_save_load_meta_and_no_overwrite(db_path, eval_dir, monkeypatch):
    populate({WEB: 3, OBSIDIAN: 3})
    monkeypatch.setenv(isolate.SOURCE_DB_ENV, "/somewhere/jarvis.db")
    monkeypatch.setenv(isolate.COPIED_AT_ENV, "2026-10-22T10:00:00")
    result = qa.build(n=6, seed=3, split=(2, 2, 2), llm_complete=fake_llm, answer_check=echo_answer)
    path = qa.save(result.items, "qa-test", result.info)
    assert path == eval_dir / "qa" / "qa-test.jsonl"
    assert qa.load("qa-test") == result.items
    meta = qa.load_meta("qa-test")
    assert (meta["version"], meta["seed"], meta["count"]) == ("qa-test", 3, 6)
    assert meta["by_source"] == {"vault-oarai-obsidian": 3, "web": 3}
    assert meta["by_split"] == {"dev": 2, "val": 2, "test": 2}
    assert sum(meta["by_style"].values()) == 6
    assert meta["rejected"] == {"total": 0, "reasons": {}}
    assert meta["llm"] == {"generate": "fake_llm", "answer_check": "echo_answer"}
    assert (meta["source_db"], meta["copied_at"]) == ("/somewhere/jarvis.db", "2026-10-22T10:00:00")
    assert meta["created_at"]
    with pytest.raises(FileExistsError):
        qa.save(result.items, "qa-test", result.info)
    with pytest.raises(ValueError):
        qa.save(result.items, "../escape")
    assert re.fullmatch(r"qa-\d{8}-\d{4}", qa.default_version())


def test_cli_refuses_existing_version_before_copy(db_path, eval_dir, capsys):
    qa.save([], "qa-dup", {})
    assert cli.main(["build", "--version", "qa-dup", "--db", str(db_path)]) == 2
    assert "すでにあります" in capsys.readouterr().err
    assert not (eval_dir / "tmp").exists()
    assert cli.main(["build", "--n", "10", "--db", str(db_path)]) == 2       # --split の合計が違う


# --- 隔離 ---

CHILD = """
import os, sqlite3, config, db
from evaluation import isolate
isolate.ensure_isolated()
n = sqlite3.connect(db.DB_PATH).execute("SELECT count(*) FROM nodes WHERE type='Knowledge'").fetchone()[0]
print(os.environ["JARVIS_DB_PATH"], config.DB_PATH, db.DB_PATH, n, sep="|")
"""


def _snapshot(path):
    return path.read_bytes(), os.stat(path).st_mtime_ns


def test_run_isolated_child_reads_backup_copy(db_path, eval_dir, capfd):
    populate({WEB: 2})
    before = _snapshot(db_path)
    code = isolate.run_isolated([], db_path, command=[sys.executable, "-c", CHILD])
    assert code == 0
    env_path, config_path, db_module_path, count = capfd.readouterr().out.strip().split("|")
    assert env_path == config_path == db_module_path
    assert os.path.dirname(env_path) == str((eval_dir / "tmp"))
    assert env_path != str(db_path) and count == "2"
    assert not os.path.exists(env_path)                     # 終わったらコピーを消す
    assert _snapshot(db_path) == before                     # 元の DB は変わらない


def test_run_isolated_keep_copy(db_path, eval_dir):
    seen = {}

    def runner(cmd, env, cwd):
        seen.update(env)
        return subprocess.CompletedProcess(cmd, 0)

    isolate.run_isolated(["build"], db_path, keep_copy=True, runner=runner)
    copy = seen["JARVIS_DB_PATH"]
    assert os.path.exists(copy)
    assert (seen[isolate.ISOLATED_ENV], seen[isolate.SOURCE_DB_ENV]) == ("1", str(db_path.resolve()))
    assert seen[isolate.COPIED_AT_ENV]


def test_copy_db_includes_wal_contents(tmp_path):
    src = tmp_path / "wal.db"
    writer = sqlite3.connect(src)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE t (x)")
    writer.execute("INSERT INTO t VALUES (1)")
    writer.commit()                                         # チェックポイント前（WAL にだけある）
    copy = isolate.copy_db(src, tmp_path / "copies")
    writer.close()
    assert sqlite3.connect(copy).execute("SELECT x FROM t").fetchall() == [(1,)]


def test_copy_db_missing_source_is_error(tmp_path):
    with pytest.raises(isolate.IsolationError):
        isolate.copy_db(tmp_path / "none.db", tmp_path / "copies")
    assert not (tmp_path / "none.db").exists()


def test_child_refuses_production_db(db_path, monkeypatch, capsys):
    with pytest.raises(isolate.IsolationError):
        isolate.ensure_isolated()                           # 隔離されていない
    monkeypatch.setenv(isolate.ISOLATED_ENV, "1")
    monkeypatch.setenv(isolate.SOURCE_DB_ENV, str(db_path))
    with pytest.raises(isolate.IsolationError, match="本番"):
        isolate.ensure_isolated()                           # コピーの元の DB を指している
    monkeypatch.setattr(config, "DB_PATH", isolate.PRODUCTION_DB)
    monkeypatch.delenv(isolate.SOURCE_DB_ENV)
    with pytest.raises(isolate.IsolationError, match="本番"):
        isolate.ensure_isolated()                           # 本番の既定の場所
    called = []
    monkeypatch.setattr(qa, "build", lambda *a, **k: called.append(1))
    assert cli.main(["build"]) == 1
    assert called == [] and "本番" in capsys.readouterr().err


def test_cli_build_end_to_end_in_child(db_path, eval_dir, capfd):
    """CLI → コピー → 子のプロセスで build。LLM は接続できない Ollama（conftest）なのですべて llm_error で捨てる。"""
    populate({WEB: 2, OBSIDIAN: 1})
    before = _snapshot(db_path)
    code = cli.main(["build", "--n", "3", "--split", "1,1,1", "--version", "qa-e2e", "--db", str(db_path)])
    out = capfd.readouterr()
    assert code == 0, out.err
    assert "捨てた数: 3" in out.out
    meta = qa.load_meta("qa-e2e")
    assert meta["count"] == 0 and meta["rejected"] == {"total": 3, "reasons": {"llm_error": 3}}
    assert meta["source_db"] == str(db_path.resolve()) and meta["copied_at"]
    assert meta["llm"]["generate"].startswith("ollama:")
    assert list((eval_dir / "tmp").iterdir()) == []         # コピーは消えた
    assert _snapshot(db_path) == before
