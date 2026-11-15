"""ブラウザでの声の会話でのナレッジ化（T-1802、統合ワークスペースの testing §15）のテスト。

候補を画面で 1 件ずつ「保存する・保存しない・あとで」で答える。偽の認識・偽のブラウザ・偽の抽出（LLM を呼ばない）・
一時の Vault・一時の DB だけを使う。
"""

import json

import pytest

import ckc.extract
import llm
import orchestrator as orc
import skills
from ckc import store
from ckc.models import Candidate, CandidateStatus, Confidence, Evidence, Kind, Speaker, Turn, new_id
from tests.test_voice_web import Fake, make_session, wav
from tests.test_voice_web_speech import Clock, until

R1003 = "repo-1003-jarvis-claude"
JK = "vault-jarvis-knowledge"
HISTORY = [Turn(Speaker.OWNER, "駐車場は 2 台にすると決めた"), Turn(Speaker.AI, "承知しました"),
           Turn(Speaker.OWNER, "朝食は出さない方針にする"), Turn(Speaker.OWNER, "照明は暖色にしたい")]


def _s(desc, permission="read", phrases=(), inputs=None):
    return {"description": desc, "status": "active", "permission": permission,
            "entry": {"repo": R1003, "function": "x"}, "inputs": inputs or {}, "phrases": list(phrases)}


INDEX = {
    "knowledge-answer": _s("普通の質問", inputs={"question": {"type": "string", "required": True}}),
    "knowledge-capture": _s("ナレッジ化", "local_write", ["ナレッジ化して"]),
    "theme-register": _s("テーマ登録", "local_write", ["テーマ登録:"], {"theme": {"type": "string", "required": True}}),
}


@pytest.fixture
def env(db_path, ckc_data_dir, make_vault, tmp_path, monkeypatch):
    jk = make_vault("jarvis-knowledge")
    path = tmp_path / "skills.json"
    path.write_text(json.dumps({"skills": INDEX}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("JARVIS_SKILLS_JSON", str(path))
    skills.reset_cache()

    def no_llm(*a, **k):
        raise AssertionError("LLM を呼ばない")
    monkeypatch.setattr(llm, "complete", no_llm)
    titles = ["駐車場は 2 台", "朝食は出さない", "照明は暖色"]

    def fake_extract(conversation_id):
        quotes = [t.text for t in HISTORY if t.speaker == Speaker.OWNER]
        made = [Candidate(new_id("cand"), conversation_id, Kind.DECISION, title, f"{title}にする。", ["jarvis"], None,
                          Confidence.CONFIRMED, [Evidence(quote, Speaker.OWNER)], resource_id=JK)
                for title, quote in zip(titles, quotes)]
        store.add_candidates(made)
        return ckc.extract.ExtractResult(made, [], [])
    monkeypatch.setattr(ckc.extract, "extract", fake_extract)
    yield jk
    skills.reset_cache()


def real_handle(text, ctx, ask=None):
    return orc.handle(text, ctx, ask=ask, use_llm=False)


def start(clock=None):
    clock = clock or Clock()
    fake = Fake(text="ナレッジ化して")
    s, events, _ = make_session(fake, handle=real_handle, clock=clock)
    s.ctx.history.extend(HISTORY)
    s.submit(wav(), 48000)
    events.wait("approval")
    general = events.of("approval")[0]
    assert "「knowledge-capture」を実行します" in general["text"] and "choices" not in general
    assert s.approve(1, general["id"], "run") == "ok"          # 今までどおりの毎回の確認（会話を LLM に送る）
    return s, events, fake, clock


def card(events, n):
    events.wait("approval", n + 1)
    ev = events.of("approval")[n]
    assert ev["choices"] == "review"
    return ev


def statuses():
    return sorted(c.status.value for c in store.list_candidates())


def notes(jk):
    return sorted(p.name for p in (jk / "_jarvis" / "knowledge").rglob("*.md")) if (jk / "_jarvis").exists() else []


def test_save_reject_later_one_by_one(env):
    s, events, _, _ = start()
    first = card(events, 1)
    assert "知識の候補 1/3: 駐車場は 2 台" in first["text"] and "駐車場は 2 台にする。" in first["text"]
    until(lambda: any("知識の候補が 3 件あります" in x for x in s.speaker.heard(1)))
    assert s.approve(1, first["id"], "run") == "ok"
    second = card(events, 2)
    assert s.approve(1, second["id"], "decline") == "ok"
    third = card(events, 3)
    assert s.approve(1, third["id"], "later") == "ok"
    events.wait("turn_done")
    assert notes(env) == [n for n in notes(env) if "駐車場" in n] and len(notes(env)) == 1
    assert statuses() == ["pending", "published", "rejected"]
    done = events.of("answer_done")[0]["text"]
    assert "1 件を保存しました" in done and "1 件は保存しませんでした" in done and "1 件は保存待ち" in done
    assert [e["result"] for e in events.of("approval_done")] == ["run", "run", "declined", "later"]
    assert s.ctx.history == []                                   # 取り込んだ対話はリセット（今までと同じ）


def test_expired_card_stays_pending(env):
    s, events, _, clock = start()
    card(events, 1)
    clock.t += 61
    second = card(events, 2)
    assert s.approve(1, second["id"], "run") == "ok"
    third = card(events, 3)
    assert s.approve(1, third["id"], "decline") == "ok"
    events.wait("turn_done")
    assert statuses() == ["pending", "published", "rejected"] and len(notes(env)) == 1


@pytest.mark.parametrize("how", ["speak", "end"])
def test_interrupt_speech_or_end_leaves_the_rest_pending(env, how):
    s, events, fake, _ = start()
    first = card(events, 1)
    if how == "speak":
        fake.text = "はい"                                       # 声の「はい」は答えにならない（割り込みで取り消し）
        s.submit(wav(), 48000)
    else:
        s.end()
    until(lambda: statuses() == ["pending", "pending", "pending"] and len(events.of("approval")) == 2)
    assert s.approve(1, first["id"], "run") == "stale" and notes(env) == []


def test_used_or_other_ids_and_later_on_normal_approvals_are_rejected(env):
    s, events, _, _ = start()
    first = card(events, 1)
    assert s.approve(2, first["id"], "run") == "stale"
    assert s.approve(1, first["id"] + "x", "run") == "stale"
    assert s.approve(1, first["id"], "later") == "ok"
    assert s.approve(1, first["id"], "run") == "stale"          # 使用済み
    second = card(events, 2)
    s.approve(1, second["id"], "later")
    third = card(events, 3)
    s.approve(1, third["id"], "later")
    events.wait("turn_done")
    assert statuses() == ["pending", "pending", "pending"]


def test_later_is_not_accepted_for_ordinary_confirmations(env):
    fake = Fake(text="ナレッジ化して")
    s, events, _ = make_session(fake, handle=real_handle, clock=Clock())
    s.ctx.history.extend(HISTORY)
    s.submit(wav(), 48000)
    events.wait("approval")
    general = events.of("approval")[0]
    assert s.approve(1, general["id"], "later") == "stale"      # 一般の確認では「あとで」を受け付けない
    assert s.approve(1, general["id"], "decline") == "ok"
    events.wait("turn_done")
    assert statuses() == []


def test_similar_existing_knowledge_is_shown_and_not_replaced(env):
    import db
    db.insert_node("Knowledge", title="駐車場の台数", content="駐車場は 2 台", node_id="ckc_old_1",
                   url="ckc://knowledge/ckc_old_1", meta={"status": "active"})
    from ckc.review import card_text
    c = Candidate(new_id("cand"), "conv", Kind.DECISION, "駐車場", "本文", ["jarvis"], None, Confidence.CONFIRMED,
                  [Evidence("x", Speaker.OWNER)], resource_id=JK, similar=["ckc_old_1"])
    text = card_text(c, 1, 1)
    assert "似た既存の知識（置き換えは ckc_cli.py review で）: 駐車場の台数" in text


def test_without_cards_browser_still_refuses_and_theme_register_stays_terminal_only(env):
    ctx = orc.Context(terminal=False)
    out = orc.execute(orc.Route("knowledge-capture", {}, "rule"), ctx, "ナレッジ化して", ask=lambda p: "y")
    assert out.error == "needs_terminal"                         # 画面で答えられないとき（評価の画面など）は今までどおり
    ctx.review = lambda text, spoken=None, edit=None: "save"
    out = orc.execute(orc.Route("theme-register", {"theme": "x"}, "rule"), ctx, "テーマ登録: x", ask=lambda p: "y")
    assert out.error == "needs_terminal"


def test_capture_status_values_are_known():
    assert {s.value for s in CandidateStatus} >= {"pending", "published", "rejected"}


def test_capture_holds_the_mic_and_refuses_interrupts_until_cards_end(env):
    """ナレッジ化の実行中（候補を作る間とカードの間）はマイクで割り込ませない（T-2903、上位 testing §28）。"""
    s, events, _, _ = start()
    first = card(events, 1)
    holds = events.of("mic_hold")
    assert [h["on"] for h in holds] == [True] and holds[0]["turn"] == 1
    order = [e["type"] for e in events]
    assert order.index("mic_hold") < len(order) - 1 - order[::-1].index("approval")   # カードより先に知らせる
    assert s.interrupt(1) == "stale"                                           # 割り込みは 409（取り消さない）
    assert s.approve(1, first["id"], "run") == "ok"
    second = card(events, 2)
    assert s.interrupt(1) == "stale"
    s.approve(1, second["id"], "decline")
    s.approve(1, card(events, 3)["id"], "later")
    events.wait("turn_done")
    assert [h["on"] for h in events.of("mic_hold")] == [True, False]
    assert statuses() == ["pending", "published", "rejected"]


def test_mic_hold_is_only_for_knowledge_capture(env):
    from tests.test_voice_web import Fake, make_session, wav
    from tests.test_voice_web_speech import Clock
    s, events, _ = make_session(Fake(text="こんにちは"), handle=real_handle, clock=Clock())
    s.submit(wav(), 48000)
    events.wait("turn_done")
    assert events.of("mic_hold") == []

