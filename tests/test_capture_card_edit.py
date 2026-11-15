"""ブラウザの知識の候補のカードで、題名・本文をその場で直して保存する（T-2900、統合ワークスペースの testing §25）。

偽の認識・偽のブラウザ・偽の抽出・一時の Vault・一時の DB だけを使う（test_capture_cards と同じ準備）。
"""

import pytest

from ckc import store
from tests.test_capture_cards import card, env, notes, start, statuses  # noqa: F401  （env は fixture）
from voice.web.session import APPROVAL_SECONDS, EDIT_SECONDS, MAX_EDIT_BODY, MAX_EDIT_TITLE


def _published():
    return [c for c in store.list_candidates() if c.status.value == "published"]


def test_card_carries_current_title_and_body_for_editing(env):
    s, events, _, _ = start()
    first = card(events, 1)
    assert first["edit"] == {"title": "駐車場は 2 台", "body": "駐車場は 2 台にする。"}
    s.approve(1, first["id"], "later")
    for n in (2, 3):
        ev = card(events, n)
        assert ev["edit"]["title"] and ev["edit"]["body"]
        s.approve(1, ev["id"], "later")
    events.wait("turn_done")


def test_edit_and_save_publishes_edited_text_keeping_kind_projects_evidence(env):
    s, events, _, _ = start()
    first = card(events, 1)
    assert s.approve(1, first["id"], "edit", title="  駐車場は 3 台  ", body="駐車場は 3 台にする（来月決定）。") == "ok"
    second = card(events, 2)
    s.approve(1, second["id"], "later")
    third = card(events, 3)
    s.approve(1, third["id"], "later")
    events.wait("turn_done")
    [c] = _published()
    assert c.title == "駐車場は 3 台" and c.body == "駐車場は 3 台にする（来月決定）。"
    assert c.kind.value == "Decision" and c.projects == ["jarvis"]
    assert c.evidence[0].quote == "駐車場は 2 台にすると決めた"       # 根拠は会話の発言のまま
    [name] = notes(env)
    text = next((env / "_jarvis" / "knowledge").rglob(name)).read_text(encoding="utf-8")
    assert "駐車場は 3 台にする（来月決定）。" in text and "# 駐車場は 3 台" in text
    assert [e["result"] for e in events.of("approval_done")][1] == "edited"
    assert "1 件を保存しました" in events.of("answer_done")[0]["text"]


@pytest.mark.parametrize("title,body", [("", "本文"), ("題名", "  "), ("x" * (MAX_EDIT_TITLE + 1), "本文"),
                                        ("題名", "y" * (MAX_EDIT_BODY + 1)), (None, "本文"), ("題名", 3)])
def test_bad_edit_is_rejected_and_card_still_waits(env, title, body):
    s, events, _, _ = start()
    first = card(events, 1)
    assert s.approve(1, first["id"], "edit", title=title, body=body) == "bad"
    assert s.approve(1, first["id"], "run") == "ok"                 # 何も決めていないので、そのまま答えられる
    for n in (2, 3):
        s.approve(1, card(events, n)["id"], "later")
    events.wait("turn_done")
    assert [c.title for c in _published()] == ["駐車場は 2 台"]


def test_secret_in_edit_is_not_saved_and_candidate_stays_unchanged(env):
    s, events, _, _ = start()
    first = card(events, 1)
    assert s.approve(1, first["id"], "edit", title="鍵", body="キーは sk-ant-api03-" + "a" * 90 + " です") == "ok"
    for n in (2, 3):
        s.approve(1, card(events, n)["id"], "later")
    events.wait("turn_done")
    assert statuses() == ["pending", "pending", "pending"] and notes(env) == []
    assert "駐車場は 2 台" in [c.title for c in store.list_candidates()]
    assert "1 件は保存できませんでした" in events.of("answer_done")[0]["text"]


def test_hold_extends_deadline_once_only_for_review_cards(env):
    s, events, _, clock = start()
    first = card(events, 1)
    clock.t += APPROVAL_SECONDS - 5
    assert s.hold(1, first["id"]) == EDIT_SECONDS
    assert s.hold(1, first["id"]) is None                          # 1 回だけ
    clock.t += EDIT_SECONDS - 10                                   # 元の期限は過ぎたが、延ばした期限の中
    assert s.approve(1, first["id"], "edit", title="駐車場は 1 台", body="駐車場は 1 台にする。") == "ok"
    for n in (2, 3):
        s.approve(1, card(events, n)["id"], "later")
    events.wait("turn_done")
    assert [c.title for c in _published()] == ["駐車場は 1 台"]


def test_hold_and_edit_are_refused_on_wrong_id_turn_or_expired(env):
    s, events, _, clock = start()
    first = card(events, 1)
    assert s.hold(2, first["id"]) is None and s.hold(1, first["id"] + "x") is None
    assert s.approve(1, first["id"] + "x", "edit", title="a", body="b") == "stale"
    clock.t += APPROVAL_SECONDS + 1
    assert s.hold(1, first["id"]) is None
    for n in (2, 3):                                               # 期限切れの 1 件目は保存待ち。残りも答えて終える
        s.approve(1, card(events, n)["id"], "later")
    events.wait("turn_done")
    assert statuses() == ["pending", "pending", "pending"]


def test_edit_and_hold_are_not_accepted_for_ordinary_confirmations(env):
    from tests.test_capture_cards import HISTORY, real_handle
    from tests.test_voice_web import Fake, make_session, wav
    from tests.test_voice_web_speech import Clock
    fake = Fake(text="ナレッジ化して")
    s, events, _ = make_session(fake, handle=real_handle, clock=Clock())
    s.ctx.history.extend(HISTORY)
    s.submit(wav(), 48000)
    events.wait("approval")
    general = events.of("approval")[0]
    assert "edit" not in general
    assert s.hold(1, general["id"]) is None
    assert s.approve(1, general["id"], "edit", title="a", body="b") == "stale"
    assert s.approve(1, general["id"], "decline") == "ok"
    events.wait("turn_done")


@pytest.mark.parametrize("how", ["speak", "end"])
def test_cut_after_edit_before_save_does_not_save(env, how):
    import threading
    from tests.test_voice_web import wav
    from tests.test_voice_web_speech import until
    s, events, fake, _ = start()
    first = card(events, 1)
    reached, go = threading.Event(), threading.Event()
    original = s._before_run

    def paused(turn):                                               # 保存の直前で止め、その間に割り込む・終える
        reached.set()
        go.wait(5)
        return original(turn)
    s._before_run = paused
    assert s.approve(1, first["id"], "edit", title="駐車場は 9 台", body="駐車場は 9 台にする。") == "ok"
    assert reached.wait(5)
    if how == "speak":                                              # 新しい発話（ナレッジ化の実行中も受け付ける）
        fake.text = "はい"
        threading.Thread(target=s.submit, args=(wav(), 48000), daemon=True).start()
        until(lambda: 1 in s._cut)
    else:
        s.end()
    go.set()
    until(lambda: len(events.of("approval_done")) >= 2)
    assert notes(env) == [] and "駐車場は 9 台" not in [c.title for c in store.list_candidates()]
    assert events.of("approval_done")[1]["result"] == "cancelled"
