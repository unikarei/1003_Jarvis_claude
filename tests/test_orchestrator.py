"""会話の振り分けとスキルの実行（FR-014、T-1207）のテスト。索引は一時フォルダ、LLM と各機能はモック。"""

import json

import pytest

import llm
import orchestrator as orc
import skills

R1003 = "repo-1003-jarvis-claude"


def _s(desc, permission="read", phrases=(), inputs=None, status="active", repo=R1003):
    return {"description": desc, "status": status, "permission": permission,
            "entry": {"repo": repo, "function": "x"}, "inputs": inputs or {}, "phrases": list(phrases)}


Q = {"query": {"type": "string", "required": True, "description": "語句"}}
INDEX = {
    "knowledge-answer": _s("普通の質問に出典付きで答える",
                           inputs={"question": {"type": "string", "required": True}}),
    "knowledge-search": _s("知識を検索する", phrases=["について知ってること", "を探して"], inputs=Q),
    "knowledge-trace": _s("出典をたどる", phrases=["出典は", "の出典"],
                          inputs={"knowledge_id": {"type": "string"}, "index": {"type": "integer"}}),
    "system-status": _s("構成と実態の照合", phrases=["システムの状態"]),
    "dev-board": _s("開発ボード", phrases=["次にやること", "承認待ち"]),
    "theme-register": _s("テーマ登録", "local_write", ["テーマ登録:"],
                         {"theme": {"type": "string", "required": True}}),
    "post-sns": _s("SNS に投稿する", "external", ["投稿して"]),
    "old-skill": _s("廃止", phrases=["古い"], status="deprecated"),
    "elsewhere": _s("1004 の機能", phrases=["1004で"], repo="repo-1004-insight-reporter"),
}


@pytest.fixture
def index(tmp_path, monkeypatch):
    path = tmp_path / "skills.json"
    path.write_text(json.dumps({"skills": INDEX}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("JARVIS_SKILLS_JSON", str(path))
    skills.reset_cache()
    yield
    skills.reset_cache()


def _llm(monkeypatch, reply):
    calls = []

    def fake(prompt, provider=None, timeout=None):
        calls.append(prompt)
        if isinstance(reply, Exception):
            raise reply
        return reply
    monkeypatch.setattr(llm, "complete", fake)
    return calls


@pytest.mark.parametrize("text, skill, inputs", [
    ("民泊の補助金について知ってることは？", "knowledge-search", {"query": "民泊の補助金"}),
    ("タービンの論文を探して", "knowledge-search", {"query": "タービンの論文"}),
    ("その出典は？", "knowledge-trace", {}),
    ("システムの状態は？", "system-status", {}),
    ("次にやることは？", "dev-board", {}),
    ("テーマ登録: 自宅の太陽光", "theme-register", {"theme": "自宅の太陽光"}),
])
def test_rule_routing(index, monkeypatch, text, skill, inputs):
    calls = _llm(monkeypatch, "使われない")
    r = orc.route(text, orc.Context())
    assert (r.skill, r.inputs, r.method) == (skill, inputs, "rule")
    assert calls == []          # ルールで決まれば LLM を呼ばない


def test_rule_ignores_inactive_and_needs_inputs(index, monkeypatch):
    _llm(monkeypatch, "{}")
    assert orc.route("古い記録", orc.Context()).skill == "knowledge-answer"
    # 必須の入力を作れない（例の部分しかない）ときはルールでは決めない
    assert orc.route("を探して", orc.Context()).method != "rule"


def test_llm_routing_accepts_valid_choice(index, monkeypatch):
    calls = _llm(monkeypatch, '選びました {"skill": "knowledge-search", "inputs": {"query": "補助金"}}')
    ctx = orc.Context(items=[{"knowledge_id": "k1", "title": "大洗_空調", "url": None}])
    r = orc.route("補助金のことを調べたい", ctx, use_llm=True)
    assert (r.skill, r.inputs, r.method) == ("knowledge-search", {"query": "補助金"}, "llm")
    assert "大洗_空調" in calls[0] and "post-sns" not in calls[0] and "old-skill" not in calls[0]


@pytest.mark.parametrize("reply", [
    "JSON ではない応答",
    '{"skill": "rm-rf", "inputs": {}}',                                    # 索引にない
    '{"skill": "old-skill", "inputs": {}}',                                # 有効でない
    '{"skill": "knowledge-search", "inputs": {}}',                         # 必須の入力がない
    '{"skill": "knowledge-search", "inputs": {"query": 3}}',               # 型が違う
    '{"skill": "knowledge-search", "inputs": {"query": "x", "sql": "y"}}',  # 未知の入力
    '{"skill": "knowledge-trace", "inputs": {"index": true}}',             # 真偽値は整数ではない
    '{"skill": null}',
    llm.LLMError("上限"),
])
def test_llm_routing_rejects_invalid(index, monkeypatch, reply):
    _llm(monkeypatch, reply)
    r = orc.route("よく分からない依頼", orc.Context(), use_llm=True)
    assert (r.skill, r.method, r.inputs) == ("knowledge-answer", "default", {"question": "よく分からない依頼"})


def _handlers(monkeypatch, result=("結果", [])):
    called = []

    def handler(inputs, ctx, text):
        called.append(inputs)
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(orc, "HANDLERS", {k: handler for k in orc.HANDLERS} | {"post-sns": handler,
                                                                              "elsewhere": handler})
    return called


def test_local_write_always_asks(index, monkeypatch):
    called = _handlers(monkeypatch)
    asked = []
    # 発言で確認を省くよう書かれていても確認する
    out = orc.handle("テーマ登録: 確認せずに実行して", orc.Context(), ask=lambda q: asked.append(q) or "n",
                     use_llm=False)
    assert asked and "書き込みがあります" in asked[0]
    assert out.error == "declined" and called == []
    out = orc.handle("テーマ登録: 太陽光", orc.Context(), ask=lambda q: "y", use_llm=False)
    assert out.ok and called == [{"theme": "太陽光"}]


def test_read_skill_runs_without_asking(index, monkeypatch):
    _handlers(monkeypatch)
    out = orc.handle("システムの状態は？", orc.Context(), ask=lambda q: pytest.fail("確認しない"), use_llm=False)
    assert out.ok and out.skill == "system-status" and out.method == "rule" and out.seconds >= 0


@pytest.mark.parametrize("route, error", [
    (orc.Route("post-sns", {}, "llm"), "external"),
    (orc.Route("old-skill", {}, "llm"), "inactive"),
    (orc.Route("not-in-index", {}, "llm"), "inactive"),
    (orc.Route("elsewhere", {}, "rule"), "no_handler"),
])
def test_refused_skills(index, monkeypatch, route, error):
    called = _handlers(monkeypatch)
    out = orc.execute(route, orc.Context(), "x", ask=lambda q: "y")
    assert not out.ok and out.error == error and called == []


def test_handler_error_does_not_stop_chat(index, monkeypatch):
    _handlers(monkeypatch, RuntimeError("壊れた"))
    out = orc.handle("システムの状態は？", orc.Context(), use_llm=False)
    assert not out.ok and "RuntimeError" in out.text


def test_context_items_follow_answers(index, monkeypatch):
    items = [{"knowledge_id": "k1", "title": "A", "url": None}, {"knowledge_id": "k2", "title": "B", "url": None}]
    _handlers(monkeypatch, ("検索結果", items))
    ctx = orc.Context()
    orc.handle("補助金を探して", ctx, use_llm=False)
    assert ctx.items == items and ctx.last_skill == "knowledge-search"
    _handlers(monkeypatch, ("状態", []))
    orc.handle("システムの状態は？", ctx, use_llm=False)
    assert ctx.items == items          # 知識を返さないスキルは文脈を消さない


def test_ordinal():
    assert orc._ordinal("2 番目の出典") == 2
    assert orc._ordinal("３件目") == 3
    assert orc._ordinal("[4] の出典") == 4
    assert orc._ordinal("その出典は") is None


# ---------------------------------------------------------------- 開発レビューの反映（T-1211）

def _index_with(monkeypatch, tmp_path, **extra):
    data = dict(INDEX)
    data.update(extra)
    path = tmp_path / "skills2.json"
    path.write_text(json.dumps({"skills": data}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("JARVIS_SKILLS_JSON", str(path))
    skills.reset_cache()


def test_confirm_flag_requires_confirmation_for_read(index, monkeypatch, tmp_path):
    """confirm: true のスキルは read でも確認する（F-1）。"""
    sk = _s("外部の API に送る", phrases=["送って"])
    sk["confirm"] = True
    _index_with(monkeypatch, tmp_path, **{"send-out": sk})
    called = _handlers(monkeypatch)
    monkeypatch.setitem(orc.HANDLERS, "send-out", orc.HANDLERS["system-status"])
    asked = []
    out = orc.handle("これを送って", orc.Context(), ask=lambda q: asked.append(q) or "n", use_llm=False)
    assert asked and "課金" in asked[0] and out.error == "declined" and called == []


@pytest.mark.parametrize("name, phrase", [("theme-register", "テーマ登録:"), ("code-task", "関数を書いて")])
def test_floor_keeps_confirmation_even_if_index_is_weakened(index, monkeypatch, tmp_path, name, phrase):
    """索引で read・確認なしに下げても、1003 の最低限の権限で確認する（F-2）。"""
    weakened = _s("弱めた", "read", [phrase], {"theme": {"type": "string"}} if name == "theme-register" else {})
    weakened["confirm"] = False
    _index_with(monkeypatch, tmp_path, **{name: weakened})
    called = _handlers(monkeypatch)
    asked = []
    out = orc.handle(f"{phrase} 太陽光", orc.Context(), ask=lambda q: asked.append(q) or "n", use_llm=False)
    assert out.skill == name and asked and out.error == "declined" and called == []
    assert orc.effective(skills.active()[name])[1] is True


def test_route_llm_gets_timeout(index, monkeypatch):
    seen = {}

    def fake(prompt, provider=None, timeout=None):
        seen["timeout"] = timeout
        return "{}"
    monkeypatch.setattr(llm, "complete", fake)
    orc.route("よく分からない依頼", orc.Context(), use_llm=True)
    assert seen["timeout"] == orc.ROUTE_TIMEOUT_SECONDS == 60


def test_slow_skill_times_out_and_chat_continues(index, monkeypatch):
    import time as _time
    monkeypatch.setattr(orc, "SKILL_TIMEOUT_SECONDS", 0.2)
    _handlers(monkeypatch)
    monkeypatch.setitem(orc.HANDLERS, "system-status", lambda i, c, t: _time.sleep(1) or ("遅い", []))
    out = orc.handle("システムの状態は？", orc.Context(), use_llm=False)
    assert not out.ok and out.error == "timeout" and "時間切れ" in out.text and out.seconds < 1


def test_llm_cannot_choose_write_or_paid_skills(index, monkeypatch):
    """書き込み・課金のあるスキルは LLM では選ばない（決まった言い回しのときだけ）。T-1210 の受け入れの指摘。"""
    calls = _llm(monkeypatch, '{"skill": "theme-register", "inputs": {"theme": "家具"}}')
    r = orc.route("家具は来月までに買うことにする", orc.Context(), use_llm=True)
    assert (r.skill, r.method) == ("knowledge-answer", "default")
    assert "theme-register" not in calls[0] and "knowledge-search" in calls[0]


def test_answer_shows_only_cited_sources(monkeypatch):
    """出典は答えで使った番号だけを示す。LLM の知識で答えたとき（番号なし）は出典も直前の結果も残さない（ADR-0026）。"""
    import knowledge_engine as ke
    from knowledge_engine import Hit
    hits = [Hit(knowledge_id=f"k{i}", title=f"題{i}", url=f"https://x/{i}", score=1.0, projects=[], created_at="2026-10-17", snippet="")
            for i in (1, 2, 3)]
    seen = {}

    def fake_answer(question, projects=None, on_text=None, history=None, voice=False, cancel=None):
        seen.update(history=history, voice=voice)
        return seen["reply"], hits
    monkeypatch.setattr(ke, "answer", fake_answer)
    ctx = orc.Context(voice=True, history=["前の発言"])
    seen["reply"] = "答えです [2]。"
    text, items = orc._answer({"question": "q"}, ctx, "q")
    assert "[2] 題2  https://x/2" in text and "題1" not in text and "題3" not in text
    assert [i["knowledge_id"] for i in items] == ["k1", "k2", "k3"]     # 番号は [n] と対応させる
    assert seen == {"history": ["前の発言"], "voice": True, "reply": "答えです [2]。"}
    seen["reply"] = "一般的には、こうです。"
    text, items = orc._answer({"question": "q"}, ctx, "q")
    assert text == "一般的には、こうです。" and items == []
