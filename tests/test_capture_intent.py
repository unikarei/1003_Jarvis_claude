"""Save mentions must not bypass an interview or other prerequisite conversation."""
import json
import pytest

import capture_intent as intent
import orchestrator as orc
import prompts
import skills
from ckc.models import Speaker, Turn


@pytest.mark.parametrize('text', [
    'インタビューして、その内容をナレッジ化して',
    'まずヒアリングしてからナレッジ化して',
    '私に質問して、その答えをナレッジ化してください',
    '調べてからナレッジ化して',
    'まだナレッジ化しないで', 'あとでナレッジ化して',
    'インタビューが終わったらナレッジ化して',
    '何でもすぐにナレッジ化するのをやめて',
])
def test_clear_prerequisite_or_denial_never_calls_model(text, monkeypatch):
    monkeypatch.setattr(intent.llm, 'complete', lambda *a, **kw: pytest.fail('unnecessary inference'))
    assert not intent.immediate(text, [])


@pytest.mark.parametrize('text', [
    'ナレッジ化して', 'ナレッジ化してくれ', '今の会話をナレッジ化して',
    'ここまでの会話をナレッジ化してください。', 'ここまでをナレッジ化して', '/ckc',
    'この話を残して', 'この内容を保存してください',
    'Obsidian に知識として保存して', '大洗のObsidianにナレッジを入れて',
])
def test_explicit_current_commands_keep_fast_route(text, monkeypatch):
    monkeypatch.setattr(intent.llm,'complete',lambda *a,**kw: pytest.fail('unnecessary inference'))
    assert intent.immediate(text, [])


@pytest.mark.parametrize('raw', [
    '{"decision":"prepare"}', '{"decision":"conversation"}', '{}', 'not json',
    '```json\n{"decision":"capture_now"}\n```',
    '{"decision":"capture_now","execute":true}', '{"decision":true}', '[]', 'x'*257,
])
def test_non_capture_and_invalid_outputs_fail_closed(raw, monkeypatch):
    monkeypatch.setattr(intent.llm,'complete',lambda *a,**kw:raw)
    assert not intent.immediate('今の話も合わせてナレッジ化してほしい', [])


def test_context_is_bounded_and_decision_can_change_with_history(monkeypatch):
    prompts_seen=[]
    def classify(prompt, **kw):
        assert kw == {'provider':'ollama','timeout':8}
        data=json.loads(prompt[len(intent.PROMPT):]); prompts_seen.append(data)
        unfinished=data['history'][-1]['text'] == 'まだ次の質問があります'
        return json.dumps({'decision':'prepare' if unfinished else 'capture_now'})
    monkeypatch.setattr(intent.llm,'complete',classify)
    text='この内容も合わせてナレッジ化してほしい'
    history=[Turn(Speaker.OWNER,'x'*1000)]*15
    history.append(Turn(Speaker.AI,'まだ次の質問があります'))
    assert not intent.immediate(text,history)
    history[-1]=Turn(Speaker.AI,'インタビューは終了しました')
    assert intent.immediate(text,history)
    assert len(prompts_seen[0]['history']) == 12
    assert len(prompts_seen[0]['history'][0]['text']) == 600
    assert prompts_seen[0]['current'] == text


@pytest.mark.parametrize('error',[intent.llm.LLMError('offline'), TimeoutError('timeout'), RuntimeError('unavailable')])
def test_classifier_errors_do_not_start_capture(error, monkeypatch):
    def fail(*a,**kw): raise error
    monkeypatch.setattr(intent.llm,'complete',fail)
    assert not intent.immediate('ここまでの回答も入れてナレッジ化してほしい',[])


@pytest.fixture
def active(monkeypatch):
    available=dict(skills.FALLBACK)
    available['general-chat']=skills.Skill('general-chat','通常の会話','active','read',{'repo':'repo-1003-jarvis-claude'})
    monkeypatch.setattr(skills,'active',lambda:available)
    return available


@pytest.mark.parametrize('text',[
    'インタビューして、その内容をナレッジ化して',
    'まず聞いてから、この話を残して',
    'インタビューしてからObsidianにナレッジを入れて',
    'あとでナレッジ化して', 'ナレッジ化しないで。ナレッジ化してと勧めないで',
])
def test_all_capture_entries_defer_with_original_text(text,active):
    ctx=orc.Context(history=[Turn(Speaker.OWNER,'ピアノ教室について聞いてほしい')])
    before=list(ctx.history)
    result=orc.route(text,ctx,use_llm=False)
    assert result.skill=='general-chat'
    assert result.inputs=={'question':text}
    assert ctx.history==before


def test_questions_and_quotes_use_semantic_veto(active,monkeypatch):
    monkeypatch.setattr(intent.llm,'complete',lambda *a,**kw:'{"decision":"conversation"}')
    for text in ['「ナレッジ化して」と言うと何が起きる？', 'ナレッジ化するにはどうしたらいい？',
                 'ナレッジ化してと言ったのに動かなかった']:
        assert orc.route(text,orc.Context(),use_llm=True).skill=='general-chat'


def test_interview_executes_chat_then_final_explicit_capture(active,monkeypatch):
    conversation=[]; capture=[]; confirms=[]
    def chat(inputs,ctx,text):
        conversation.append((text,list(ctx.history)))
        return ('ピアノ教室で最も大切にしていることは何ですか？',[])
    def save(inputs,ctx,text):
        capture.append(list(ctx.history)); return ('候補を確認します',[])
    monkeypatch.setitem(orc.HANDLERS,'general-chat',chat)
    monkeypatch.setitem(orc.HANDLERS,'knowledge-capture',save)
    monkeypatch.setattr(orc,'is_knowledge_question',lambda text:False)
    ctx=orc.Context()
    first='ピアノ教室についてインタビューして、その内容をナレッジ化して'
    out=orc.handle(first,ctx,ask=lambda p:confirms.append(p) or 'y',use_llm=False)
    assert out.skill=='general-chat' and out.ok
    assert not capture and not confirms
    orc.add_turn(ctx,Speaker.OWNER,first)
    orc.add_turn(ctx,Speaker.AI,out.text)
    answer='子どもが自分で音楽を楽しめるようになることです'
    orc.handle(answer,ctx,use_llm=False)
    assert conversation[-1][1][-1].text==out.text
    orc.add_turn(ctx,Speaker.OWNER,answer)
    out=orc.handle('ここまでの会話をナレッジ化して',ctx,
                   ask=lambda p:confirms.append(p) or 'y',use_llm=False)
    assert out.skill=='knowledge-capture' and len(capture)==1 and len(confirms)==1
    assert any(turn.text==answer for turn in capture[0])


def test_prompt_policy_shared_by_sessions_and_one_shot():
    for tools in (True,False):
        system=prompts.rules(tools=tools)
        assert 'まずインタビューを始め' in system
        assert '知識化を毎回勧め' in system
    assert 'まずインタビューを始め' in prompts.chat_template()


def test_completed_interview_is_not_vetoed_as_unfinished(monkeypatch):
    monkeypatch.setattr(intent.llm,'complete',lambda *a,**kw:'{"decision":"capture_now"}')
    assert intent.immediate('インタビューして、その回答を得た。もう終了しましたのでナレッジ化して',[])
    assert intent.immediate('インタビューしてもらった内容をナレッジ化して',[])
