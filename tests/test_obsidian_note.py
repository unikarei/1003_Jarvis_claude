"""会話からの Obsidian のノートの編集 `obsidian-note`（T-1702、統合ワークスペースの testing §14）のテスト。

一時の Vault（git init したフォルダ）・一時の DB・偽の LLM（Ollama の代わり）・偽の確認（ask）だけを使う。
本物の LLM・本番の Vault・本番の DB は使わない。
"""

import json
import subprocess
from pathlib import Path

import pytest

import db
import ingest_obsidian
import llm
import obsidian_note as on
import orchestrator as orc
import skills
import vault
from ckc.models import Confidence, Evidence, Kind, KnowledgeNote, NoteStatus, Origin, SourceRef, Speaker
from ckc.render import parse_note, render_note

R1003 = "repo-1003-jarvis-claude"
OA, JK = "vault-oarai-obsidian", "vault-jarvis-knowledge"
MEMO = "民泊@大洗/01_物件/大洗メモ.md"


def _s(desc, permission="read", phrases=(), inputs=None, status="active", **extra):
    return {"description": desc, "status": status, "permission": permission,
            "entry": {"repo": R1003, "function": "x"}, "inputs": inputs or {}, "phrases": list(phrases), **extra}


INDEX = {
    "knowledge-answer": _s("ナレッジの質問", inputs={"question": {"type": "string", "required": True}}),
    "general-chat": _s("通常の会話", inputs={"question": {"type": "string", "required": True}}),
    "code-task": _s("コード", phrases=["関数を書いて"], confirm=True),
    "obsidian-note": _s("Obsidian のノートを編集する", "local_write", ["Obsidian", "オブシディアン"],
                        requires_any=list(on.OP_WORDS)),
}


def _git(path, *args):
    subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)


def put(vault_dir: Path, rel: str, content: str, commit: bool = True) -> Path:
    path = vault_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if commit:
        _git(vault_dir, "add", "-A")
        _git(vault_dir, "commit", "-q", "-m", "x")
    return path


class LLM:
    """偽の LLM（Ollama の代わり）。reply を JSON にして返す。呼ばれた provider と prompt を残す。"""

    def __init__(self, reply=None):
        self.reply, self.calls = reply, []

    def __call__(self, prompt, provider=None, timeout=None):
        self.calls.append((provider, prompt))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply if isinstance(self.reply, str) else json.dumps(self.reply or {}, ensure_ascii=False)


class Ask:
    """偽の確認。answers を順に返し、prompt・spoken・expect を残す。"""

    def __init__(self, *answers, hook=None):
        self.answers, self.calls, self.hook = list(answers), [], hook

    def __call__(self, prompt, spoken=None, expect=None):
        self.calls.append({"prompt": prompt, "spoken": spoken, "expect": expect})
        if self.hook:
            self.hook(len(self.calls))
        return self.answers.pop(0) if self.answers else "n"


@pytest.fixture
def env(db_path, ckc_data_dir, make_vault, tmp_path, monkeypatch):
    oarai, jk = make_vault("oarai-obsidian"), make_vault("jarvis-knowledge")
    put(oarai, MEMO, "# 大洗メモ\n\n## 物件\n\n駅から近い。\n\n## 予算\n\n500 万円。\n")
    path = tmp_path / "skills.json"
    path.write_text(json.dumps({"skills": INDEX}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("JARVIS_SKILLS_JSON", str(path))
    skills.reset_cache()
    fake = LLM()
    monkeypatch.setattr(llm, "complete", fake)
    yield {"oarai": oarai, "jk": jk, "llm": fake}
    skills.reset_cache()


def say(text, ctx, ask=None):
    return orc.handle(text, ctx, ask=ask, use_llm=False)


def read(vault_dir, rel):
    return (vault_dir / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------- 振り分け

@pytest.mark.parametrize("text", ["Obsidian の大洗メモに駐車場ありを追加して", "オブシディアンで日記ってノートを作って",
                                  "obsidian の古いメモを消して", "Obsidian を編集して", "Obsidian のメモを更新して",
                                  "Obsidian のメモに『関数を書いて』と追加して", "ＯＢＳＩＤＩＡＮ に 追記 して"])
def test_routes_to_obsidian_note(env, text):
    assert orc.route(text, orc.Context(), use_llm=False).skill == "obsidian-note"


@pytest.mark.parametrize("text", ["Obsidian って何？", "メモに追加して", "データベースの設計について教えて",
                                  "Obsidian に大洗のメモはある？"])
def test_other_utterances_are_routed_as_before(env, text):
    assert orc.route(text, orc.Context(), use_llm=False).skill != "obsidian-note"


def test_requires_any_needs_both_and_wins_over_phrase_only(env, tmp_path, monkeypatch):
    index = dict(INDEX, **{"memo-skill": _s("メモ", "local_write", ["ノート"], requires_any=["保存"])})
    path = tmp_path / "skills2.json"
    path.write_text(json.dumps({"skills": index}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("JARVIS_SKILLS_JSON", str(path))
    skills.reset_cache()
    active = skills.active()
    assert active["memo-skill"].requires_any == ("保存",) and active["code-task"].requires_any == ()
    assert orc.route_by_rule("ノートを見せて", active) is None
    assert orc.route_by_rule("この関数を書いてノートに保存", active).skill == "memo-skill"   # phrases だけの一致より優先
    assert orc.route_by_rule("関数を書いて", active).skill == "code-task"                    # requires_any なしは今まで通り


@pytest.mark.parametrize("bad", [["保存", ""], [1], "保存"])
def test_invalid_requires_any_is_a_load_error(bad):
    with pytest.raises(ValueError, match="requires_any"):
        skills._parse("x", _s("x", requires_any=bad))


def test_edit_without_items_asks_for_the_operation(env):
    ctx = orc.Context()
    out = say("Obsidian を編集して", ctx, Ask())
    assert out.skill == "obsidian-note" and "追加・作成・書き直し・削除" in out.text and out.private
    assert ctx.pending is not None and ctx.pending.state["item"] == "op"


@pytest.mark.parametrize("index", ["missing", "inactive", "broken"])
def test_without_the_declaration_nothing_reaches_other_skills(env, tmp_path, monkeypatch, index):
    data = {k: v for k, v in INDEX.items() if k != "obsidian-note"}
    if index == "inactive":
        data["obsidian-note"] = dict(INDEX["obsidian-note"], status="candidate")
    path = tmp_path / "skills3.json"
    path.write_text("{" if index == "broken" else json.dumps({"skills": data}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("JARVIS_SKILLS_JSON", str(path))
    skills.reset_cache()

    def no_llm(*a, **k):
        raise AssertionError("LLM に渡さない")
    monkeypatch.setattr(llm, "complete", no_llm)
    monkeypatch.setattr(llm, "complete_stream", no_llm)
    import warnings
    with warnings.catch_warnings():                 # 壊れた索引の警告（予備の索引で動く）は関係しない
        warnings.simplefilter("ignore", UserWarning)
        out = say("Obsidian のノートに秘密の本文を追加して", orc.Context(), Ask("y"))
        assert out.text == on.UNAVAILABLE_TEXT and out.private and out.skill == "obsidian-note"
        assert "秘密の本文" not in json.dumps(out.inputs, ensure_ascii=False)
        assert orc.route("Obsidian に大洗のメモはある？", orc.Context(), use_llm=False).skill != "obsidian-note"


# ---------------------------------------------------------------- 聞き返し

def test_append_asks_note_then_choose_then_content_then_writes(env):
    put(env["oarai"], "民泊@大洗/大洗メモ2.md", "二つ目\n")
    put(env["oarai"], "民泊@大洗/90_資料/大洗メモ_旧.md", "古い\n")
    ctx = orc.Context()
    q1 = say("Obsidian に追加して", ctx, Ask())
    assert "どのノート" in q1.text
    q2 = say("大洗", ctx, Ask())                                     # 部分一致が 3 件 → 番号で選ぶ
    assert "番号で選んでください" in q2.text and q2.text.count("大洗メモ") == 3
    number = [i for i, c in enumerate(ctx.pending.state["candidates"], 1) if c[1] == MEMO][0]
    q3 = say(str(number), ctx, Ask())
    assert "何を追加しますか" in q3.text
    ask = Ask("y")
    out = say("駐車場は 2 台", ctx, ask)
    assert "追記しました" in out.text and ctx.pending is None
    assert "駐車場は 2 台" in ask.calls[0]["prompt"] and MEMO in ask.calls[0]["prompt"]   # 確認に書く内容とパス
    assert len(ask.calls) == 1                                       # 一般の確認（confirm_text）は出ない
    text = read(env["oarai"], MEMO)
    assert text.endswith("駐車場は 2 台\n") and "（JARVIS追記: " in text


def test_append_under_heading_goes_to_the_end_of_that_section(env):
    env["llm"].reply = {"note": "大洗メモ", "heading": "物件", "content": "南向き"}
    out = say("Obsidian の大洗メモの物件に南向きを追加して", orc.Context(), Ask("y"))
    assert "追記しました" in out.text
    text = read(env["oarai"], MEMO)
    assert text.index("南向き") < text.index("## 予算") and text.index("駅から近い") < text.index("南向き")


def test_create_asks_vault_folder_new_folder_name_and_content(env):
    ctx = orc.Context()
    assert "どの Vault" in say("Obsidian で新しいノートを作って", ctx, Ask()).text
    q = say("汎用", ctx, Ask())
    assert "どのフォルダ" in q.text and on.TOP_FOLDER in q.text
    assert "新しいフォルダ「日記」" in say("日記", ctx, Ask()).text
    assert "ノートの名前" in say("はい", ctx, Ask()).text
    assert "何を書きますか" in say("テスト", ctx, Ask()).text
    ask = Ask("y")
    out = say("今日は晴れ", ctx, ask)
    assert "作りました" in out.text
    assert "日記/テスト.md" in ask.calls[0]["prompt"]                # 決まった相対パスを確認に出す
    assert read(env["jk"], "日記/テスト.md") == "今日は晴れ"


def test_not_found_offers_to_create(env):
    env["llm"].reply = {"note": "存在しないノート", "content": "メモ"}
    ctx = orc.Context()
    assert "新しく作りますか" in say("Obsidian の存在しないノートにメモを追加して", ctx, Ask()).text
    assert "どの Vault" in say("はい", ctx, Ask()).text
    assert ctx.pending.state["op"] == "create" and ctx.pending.state["name"] == "存在しないノート"


def test_r_returns_to_the_content_question_and_keeps_the_pending_request(env):
    env["llm"].reply = {"note": "大洗メモ", "content": "最初の内容"}
    ctx = orc.Context()
    out = say("Obsidian の大洗メモに追加して", ctx, Ask("r"))
    assert "何を追加しますか" in out.text and ctx.pending is not None and out.private
    out = say("言い直した内容", ctx, Ask("y"))
    assert "追記しました" in out.text
    text = read(env["oarai"], MEMO)
    assert "言い直した内容" in text and "最初の内容" not in text
    assert ctx.history == []


@pytest.mark.parametrize("op, reply, answers", [
    ("append", {"note": "大洗メモ", "content": "追加"}, ("y",)),
    ("replace", {"note": "大洗メモ", "old": "駅から近い。", "new": "駅から遠い。"}, ("y",)),
    ("delete", {"note": "大洗メモ"}, ("y", "削除")),
])
def test_note_changed_while_waiting_for_confirmation_is_not_written(env, op, reply, answers):
    env["llm"].reply = reply
    words = {"append": "追加して", "replace": "書き直して", "delete": "削除して"}

    def change(n):
        if n == 1:
            put(env["oarai"], MEMO, "ほかの端末で変えた\n")
    out = say(f"Obsidian の大洗メモを{words[op]}", orc.Context(), Ask(*answers, hook=change))
    assert "確認の間にノートが変わった" in out.text
    assert read(env["oarai"], MEMO) == "ほかの端末で変えた\n"


@pytest.mark.parametrize("old, hint", [("ない文", "見つかりません"), ("。", "か所あります")])
def test_replace_asks_again_when_the_old_text_is_not_unique(env, old, hint):
    env["llm"].reply = {"note": "大洗メモ", "old": old, "new": "x"}
    ctx = orc.Context()
    out = say("Obsidian の大洗メモを書き直して", ctx, Ask())
    assert hint in out.text and ctx.pending.state["item"] == "old"


# ---------------------------------------------------------------- 中止

def test_cancel_stops_without_writing(env):
    ctx = orc.Context()
    say("Obsidian の大洗メモに追加して", ctx, Ask())
    before = read(env["oarai"], MEMO)
    out = say("キャンセル", ctx, Ask("y"))
    assert out.text == on.CANCELLED_TEXT and out.private and ctx.pending is None
    assert read(env["oarai"], MEMO) == before


def test_expired_pending_request_is_not_routed_and_next_utterance_is(env, monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(orc, "clock", lambda: t[0])
    ctx = orc.Context()
    say("Obsidian を編集して", ctx, Ask())
    t[0] += on.PENDING_SECONDS + 1
    calls = len(env["llm"].calls)
    out = say("大洗の補助金について教えて", ctx, Ask())                # 書く内容かもしれないので、どこにも渡さない
    assert out.text == on.TIMEOUT_TEXT and out.private and ctx.pending is None
    assert len(env["llm"].calls) == calls
    assert say("Obsidian って何？", ctx, Ask()).skill != "obsidian-note"   # 次の発言は普通に振り分ける


def test_three_unclear_answers_give_up(env):
    ctx = orc.Context()
    say("Obsidian を編集して", ctx, Ask())
    say("うーん", ctx, Ask())
    say("わからない", ctx, Ask())
    out = say("どうしよう", ctx, Ask())
    assert out.text == on.GAVE_UP_TEXT and ctx.pending is None


# ---------------------------------------------------------------- 確認

@pytest.mark.parametrize("op, reply, prompt_has", [
    ("append", {"note": "大洗メモ", "content": "書く文"}, "書く文"),
    ("create", {"note": "新規", "vault": "jarvis-knowledge", "content": "新しい本文"}, "新しい本文"),
    ("replace", {"note": "大洗メモ", "old": "駅から近い。", "new": "駅から遠い。"}, "駅から遠い。"),
    ("rewrite", {"note": "大洗メモ", "content": "全部新しい本文"}, "全部新しい本文"),
    ("delete", {"note": "大洗メモ"}, "先頭の 5 行"),
])
def test_first_confirmation_contains_the_preview(env, op, reply, prompt_has):
    env["llm"].reply = reply
    words = {"append": "追加して", "create": "作って", "replace": "書き直して", "rewrite": "全体を書き直して",
             "delete": "削除して"}
    ctx = orc.Context()
    ask = Ask("n")
    out = say(f"Obsidian の大洗メモを{words[op]}", ctx, ask)
    if op == "create":                                              # フォルダを聞かれる
        say("一番上", ctx, ask)
    assert ask.calls and prompt_has in ask.calls[0]["prompt"], out.text
    if op == "rewrite":
        assert "置き換え後の全文" in ask.calls[0]["prompt"]


def test_one_step_writes_only_with_y(env):
    env["llm"].reply = {"note": "大洗メモ", "content": "追加"}
    before = read(env["oarai"], MEMO)
    for reply in ("n", "Y ", "", "はい"):
        out = say("Obsidian の大洗メモに追加して", orc.Context(), Ask(reply))
        if reply.strip().lower() == "y":
            assert "追記しました" in out.text
            return
        assert out.text == on.CANCELLED_TEXT and read(env["oarai"], MEMO) == before


@pytest.mark.parametrize("second, deleted", [("削除", True), (" 削除 ", True), ("y", False), ("はい", False),
                                             ("", False), ("置き換え", False)])
def test_delete_needs_y_then_the_word(env, second, deleted):
    env["llm"].reply = {"note": "大洗メモ"}
    ask = Ask("y", second)
    out = say("Obsidian の大洗メモを削除して", orc.Context(), ask)
    assert (env["oarai"] / MEMO).exists() is not deleted, out.text
    assert ask.calls[1]["expect"] == "削除" and "『削除』と入力" in ask.calls[1]["prompt"]
    assert "削除します" in ask.calls[0]["spoken"] and "キーボード" in ask.calls[1]["spoken"]


def test_delete_first_step_no_stops_before_the_second(env):
    env["llm"].reply = {"note": "大洗メモ"}
    ask = Ask("n", "削除")
    say("Obsidian の大洗メモを削除して", orc.Context(), ask)
    assert len(ask.calls) == 1 and (env["oarai"] / MEMO).exists()


@pytest.mark.parametrize("second, done", [("置き換え", True), ("削除", False), ("y", False)])
def test_rewrite_needs_the_word_replace(env, second, done):
    env["llm"].reply = {"note": "大洗メモ", "content": "新しい全文"}
    out = say("Obsidian の大洗メモの全体を書き直して", orc.Context(), Ask("y", second))
    assert (read(env["oarai"], MEMO) == "新しい全文") is done, out.text


def test_no_ask_means_no_write(env):
    env["llm"].reply = {"note": "大洗メモ", "content": "追加"}
    before = read(env["oarai"], MEMO)
    out = say("Obsidian の大洗メモに追加して", orc.Context(), None)
    assert "書きませんでした" in out.text and read(env["oarai"], MEMO) == before


def test_ask_without_spoken_or_expect_still_works(env):
    env["llm"].reply = {"note": "大洗メモ", "content": "追加"}
    prompts = []
    out = say("Obsidian の大洗メモに追加して", orc.Context(), lambda prompt: prompts.append(prompt) or "y")
    assert "追記しました" in out.text and len(prompts) == 1


# ---------------------------------------------------------------- 守り

@pytest.mark.parametrize("rel", ["notes/.obsidian/x.md", "a/.trash/b.md", ".OBSIDIAN/x.md", "x/.Trash/y.md",
                                 ".git/x.md", "../x.md", "/etc/x.md", "note.txt", "note"])
def test_protected_paths_and_non_markdown_are_refused(env, rel):
    with pytest.raises(vault.VaultError):
        vault.create_file(JK, rel, "x")


def test_symlink_into_protected_place_is_refused(env):
    (env["jk"] / ".obsidian").mkdir()
    (env["jk"] / "link").symlink_to(env["jk"] / ".obsidian")
    with pytest.raises(vault.VaultError):
        vault.create_file(JK, "link/x.md", "x")
    put(env["jk"], ".obsidian/hidden.md", "x", commit=False)
    assert "link/hidden.md" not in vault.list_notes(JK) and ".obsidian/hidden.md" not in vault.list_notes(JK)


def test_list_notes_skips_protected_places_at_any_level(env):
    put(env["jk"], "a/.trash/old.md", "x", commit=False)
    put(env["jk"], "a/ok.md", "x", commit=False)
    notes = vault.list_notes(JK)
    assert "a/ok.md" in notes and not any(".trash" in n for n in notes)


def test_git_busy_uncommitted_and_changed_notes_are_not_touched(env):
    content, digest = vault.read_file(OA, MEMO)
    (env["oarai"] / ".git" / "MERGE_HEAD").write_text("x")
    with pytest.raises(vault.VaultError, match="Git の操作中"):
        vault.delete_file(OA, MEMO, digest)
    (env["oarai"] / ".git" / "MERGE_HEAD").unlink()
    put(env["oarai"], MEMO, content + "未同期\n", commit=False)
    with pytest.raises(vault.VaultError, match="未同期"):
        vault.delete_file(OA, MEMO, vault.digest(content + "未同期\n"))
    _git(env["oarai"], "commit", "-qam", "sync")
    with pytest.raises(vault.VaultError, match="読み込み後"):
        vault.delete_file(OA, MEMO, digest)
    assert (env["oarai"] / MEMO).exists()


def test_rewrite_and_delete_keep_backups(env, ckc_data_dir):
    env["llm"].reply = {"note": "大洗メモ", "content": "新しい全文"}
    say("Obsidian の大洗メモの全体を書き直して", orc.Context(), Ask("y", "置き換え"))
    _git(env["oarai"], "commit", "-qam", "sync")
    env["llm"].reply = {"note": "大洗メモ"}
    say("Obsidian の大洗メモを削除して", orc.Context(), Ask("y", "削除"))
    backups = sorted(p.read_text(encoding="utf-8") for p in (ckc_data_dir / "backups" / OA).glob("*_大洗メモ.md"))
    assert "新しい全文" in backups and any("駅から近い" in b for b in backups)


# ---------------------------------------------------------------- 削除と知識

def _ingested(env):
    source = ingest_obsidian.SOURCES[0]
    ingest_obsidian.ingest_vault(source)
    return db.find_knowledge_by_url(ingest_obsidian.blob_url(source, MEMO))


def test_delete_removes_the_knowledge_source_and_edges(env, ckc_data_dir):
    import knowledge_engine as ke
    put(env["oarai"], "民泊@大洗/別のメモ.md", "駅から近い別の話\n")
    k = _ingested(env)
    assert k is not None and any(h.knowledge_id == k["id"] for h in ke.search("駅から近い"))
    env["llm"].reply = {"note": "大洗メモ"}
    out = say("Obsidian の大洗メモを削除して", orc.Context(), Ask("y", "削除"))
    assert "削除しました" in out.text and "2 件消しました" in out.text            # Knowledge と Source
    assert db.get_node(k["id"]) is None and not (env["oarai"] / MEMO).exists()
    assert not db._fetchall("SELECT * FROM edges WHERE src_id = ? OR dst_id = ?", (k["id"], k["id"]))
    assert all(h.knowledge_id != k["id"] for h in ke.search("駅から近い"))
    assert any(h.title == "別のメモ" for h in ke.search("駅から近い"))           # ほかのノートの知識は消えない
    saved = list((ckc_data_dir / "backups" / OA).glob("*.nodes.json"))
    assert saved and k["id"] in saved[0].read_text(encoding="utf-8")


def test_failed_db_delete_restores_the_note(env, monkeypatch):
    k = _ingested(env)
    before = read(env["oarai"], MEMO)

    def boom(ids):
        raise RuntimeError("db")
    monkeypatch.setattr(db, "delete_nodes", boom)
    env["llm"].reply = {"note": "大洗メモ"}
    out = say("Obsidian の大洗メモを削除して", orc.Context(), Ask("y", "削除"))
    assert "削除できませんでした（ノートは元に戻しました）" in out.text
    assert read(env["oarai"], MEMO) == before and db.get_node(k["id"]) is not None


def test_failed_db_delete_does_not_overwrite_a_new_file(env, monkeypatch):
    _ingested(env)

    def boom(ids):
        put(env["oarai"], MEMO, "ほかの端末が作った\n", commit=False)
        raise RuntimeError("db")
    monkeypatch.setattr(db, "delete_nodes", boom)
    env["llm"].reply = {"note": "大洗メモ"}
    out = say("Obsidian の大洗メモを削除して", orc.Context(), Ask("y", "削除"))
    assert "戻していません" in out.text and "バックアップは" in out.text
    assert read(env["oarai"], MEMO) == "ほかの端末が作った\n"


def test_failed_json_backup_deletes_nothing(env, monkeypatch):
    k = _ingested(env)
    original = Path.write_text

    def fail(self, *a, **kw):
        if self.name.endswith(".nodes.json"):
            raise OSError("disk")
        return original(self, *a, **kw)
    monkeypatch.setattr(Path, "write_text", fail)
    env["llm"].reply = {"note": "大洗メモ"}
    out = say("Obsidian の大洗メモを削除して", orc.Context(), Ask("y", "削除"))
    assert "何も消していません" in out.text
    assert (env["oarai"] / MEMO).exists() and db.get_node(k["id"]) is not None


# ---------------------------------------------------------------- CKC のノート

def _ckc_note(env, body="最初の本文", conversation="conv-1"):
    note = KnowledgeNote(ckc_id="ckc_test_1", kind=Kind.DECISION, status=NoteStatus.ACTIVE, title="方針",
                         body=body, projects=["oarai"], category="minpaku", confidence=Confidence.CONFIRMED,
                         source=SourceRef(Origin.JARVIS, conversation, "会話", "2026-11-08T00:00:00", None),
                         evidence=[Evidence("根拠の引用", Speaker.OWNER)], created="2026-11-08T00:00:00",
                         approved="2026-11-08T00:00:00")
    rel = "_jarvis/knowledge/Decision/2026-11-08_方針.md"
    put(env["jk"], rel, render_note(note))
    src = db.insert_node("Source", title="会話", url=f"ckc://conversation/{conversation}")
    db.insert_node("Knowledge", title="方針", content=body, node_id=note.ckc_id, url=f"ckc://knowledge/{note.ckc_id}",
                   meta={"resource_id": JK, "note_relpath": rel})
    db.insert_edge(note.ckc_id, src, "derived_from")
    return note, rel, src


def test_ckc_append_goes_before_evidence_and_updates_the_knowledge(env):
    note, rel, _ = _ckc_note(env)
    env["llm"].reply = {"note": "2026-11-08_方針", "content": "追記した決定"}
    out = say("Obsidian の 2026-11-08_方針 に追記して", orc.Context(), Ask("y"))
    assert "追記しました" in out.text
    parsed = parse_note(read(env["jk"], rel))
    assert "追記した決定" in parsed.body and parsed.evidence == note.evidence
    assert "追記した決定" in db.get_node(note.ckc_id)["content"]
    assert "ckc_id" not in db.get_node(note.ckc_id)["content"]


def test_ckc_rewrite_replaces_only_the_body(env):
    note, rel, _ = _ckc_note(env)
    env["llm"].reply = {"note": "2026-11-08_方針", "content": "まったく新しい本文"}
    ask = Ask("y", "置き換え")
    say("Obsidian の 2026-11-08_方針 の全体を書き直して", orc.Context(), ask)
    assert "本文だけを置き換えます" in ask.calls[0]["prompt"]
    parsed = parse_note(read(env["jk"], rel))
    assert parsed.body == "まったく新しい本文"
    assert dataclasses_equal_except_body(parsed, note)
    assert db.get_node(note.ckc_id)["content"] == "まったく新しい本文"


def dataclasses_equal_except_body(a, b):
    import dataclasses
    return dataclasses.replace(a, body="") == dataclasses.replace(b, body="")


def test_ckc_replace_of_text_outside_the_body_asks_again(env):
    _ckc_note(env)
    env["llm"].reply = {"note": "2026-11-08_方針", "old": "根拠の引用", "new": "x"}
    ctx = orc.Context()
    out = say("Obsidian の 2026-11-08_方針 を書き直して", ctx, Ask("y"))
    assert "本文の中に見つかりませんでした" in out.text and ctx.pending.state["item"] == "old"


def test_ckc_knowledge_update_failure_restores_the_note(env, monkeypatch):
    note, rel, _ = _ckc_note(env)
    before = read(env["jk"], rel)

    def boom(node_id, content):
        raise RuntimeError("db")
    monkeypatch.setattr(db, "update_node_content", boom)
    env["llm"].reply = {"note": "2026-11-08_方針", "content": "追記"}
    out = say("Obsidian の 2026-11-08_方針 に追記して", orc.Context(), Ask("y"))
    assert "ノートは元に戻しました" in out.text and read(env["jk"], rel) == before
    assert db.get_node(note.ckc_id)["content"] == "最初の本文"


def test_ckc_delete_keeps_the_shared_conversation_source(env):
    note, rel, src = _ckc_note(env)
    other = db.insert_node("Knowledge", title="別の知識", content="x", url="ckc://knowledge/other")
    db.insert_edge(other, src, "derived_from")
    env["llm"].reply = {"note": "2026-11-08_方針"}
    out = say("Obsidian の 2026-11-08_方針 を削除して", orc.Context(), Ask("y", "削除"))
    assert "1 件消しました" in out.text
    assert db.get_node(note.ckc_id) is None and db.get_node(src) is not None and db.get_node(other) is not None


# ---------------------------------------------------------------- 書いた後

def test_reingest_after_writing_and_messages(env, monkeypatch):
    k = _ingested(env)
    env["llm"].reply = {"note": "大洗メモ", "content": "新しい追記"}
    out = say("Obsidian の大洗メモに追加して", orc.Context(), Ask("y"))
    assert out.text.endswith("追記しました。")
    assert "新しい追記" in db.get_node(k["id"])["content"]                  # 取り込み直しで検索に反映
    _git(env["oarai"], "commit", "-qam", "sync")

    def boom(source):
        raise RuntimeError("x")
    monkeypatch.setattr(ingest_obsidian, "ingest_vault", boom)
    out = say("Obsidian の大洗メモに追加して", orc.Context(), Ask("y"))
    assert "追記しました" in out.text and "次の取り込みまで" in out.text


def test_writing_outside_the_ingest_scope_says_so(env):
    put(env["oarai"], "外のメモ.md", "x\n")
    env["llm"].reply = {"note": "外のメモ", "content": "追記"}
    out = say("Obsidian の外のメモに追加して", orc.Context(), Ask("y"))
    assert "追記しました" in out.text and "検索には反映されない場所です" in out.text


# ---------------------------------------------------------------- LLM

def test_default_extraction_uses_ollama_and_falls_back_to_rules(env):
    env["llm"].reply = "JSON ではない返事"
    items = on.extract("Obsidian の大洗メモを削除して")
    assert env["llm"].calls[-1][0] == "ollama" and items["op"] == "delete"
    env["llm"].reply = RuntimeError("ollama down")
    assert on.extract("Obsidian に追加して")["op"] == "append"


# ---------------------------------------------------------------- 本文を外に出さない

def test_private_outcome_and_record_keep_no_text(env):
    from run_task import record
    env["llm"].reply = {"note": "大洗メモ", "content": "秘密の追記"}
    text = "Obsidian の大洗メモに秘密の追記を追加して"
    out = say(text, orc.Context(), Ask("y"))
    assert out.private and out.inputs == {"op": "append", "vault": OA, "path": MEMO, "result": out.inputs["result"]}
    task = db.get_node(record(text, out))
    blob = json.dumps(dict(task), ensure_ascii=False)
    assert task["title"] == "obsidian-note: append" and "秘密の追記" not in blob
    assert task["content"].endswith("追記しました。")


def test_exception_in_the_skill_is_private_and_has_no_message(env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("パス /secret/本文")
    monkeypatch.setattr(on, "run", boom)
    out = say("Obsidian に追加して", orc.Context(), Ask())
    assert out.private and out.text == "[エラー] obsidian-note: RuntimeError" and "本文" not in out.text


def test_chat_keeps_obsidian_turns_out_of_history(env, monkeypatch):
    import builtins
    import io
    import sys
    for name in ("stdin", "stdout"):                 # chat.py は読み込み時に標準入出力を UTF-8 にする
        monkeypatch.setattr(sys, name, io.TextIOWrapper(io.BytesIO(), encoding="utf-8"))
    import chat
    env["llm"].reply = {"note": "大洗メモ", "content": "秘密の追記"}
    contexts = []
    original = orc.Context
    monkeypatch.setattr(orc, "Context", lambda **kw: contexts.append(original(**kw)) or contexts[-1])
    inputs = iter(["Obsidian の大洗メモに追加して", "y", "exit"])
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(inputs))
    monkeypatch.setattr(chat, "init_db", lambda: None)
    monkeypatch.setattr(orc, "start_warm_up", lambda **kwargs: None)
    chat.main()
    assert "秘密の追記" in read(env["oarai"], MEMO) and contexts[0].history == []
