"""単独の相づちだけを短く返し、依頼とノートの聞き返しを落とさない。"""
import pytest

import llm
import orchestrator as orc
import skills


@pytest.mark.parametrize('text', ['うぅん……。', 'ええっとね……', 'あのぉ', 'ふうむ！', 'なるほどなあ', 'そうですねぇ', 'んーー'])
def test_short_interjections_need_no_llm(monkeypatch, text):
    def forbidden(*args, **kwargs):
        pytest.fail('単独の間投詞にLLMを呼ばない')
    monkeypatch.setattr(llm, 'complete', forbidden)
    assert orc._chat({'question':text}, orc.Context(), text) == ('はい。', [])


@pytest.mark.parametrize('text', ['', 'うん？', 'ふむ、これは何の話？', 'あの場所を教えて',
                                  'ええと、予定を確認したい', 'そうですね、記録に残して',
                                  'なるほどという言葉の意味は？', 'まあよい方法を考えて',
                                  'はい、ノートに追記して'])
def test_questions_and_requests_are_not_shortcut(monkeypatch, text):
    calls=[]
    monkeypatch.setattr(llm, 'complete', lambda prompt, **kwargs: calls.append(prompt) or '通常の返事です')
    response, _ = orc._chat({'question':text}, orc.Context(), text)
    assert response == '通常の返事です'
    assert len(calls) == 1


def test_real_handle_does_not_drop_pending_note_input(monkeypatch):
    pending=orc.Pending('obsidian-note', {'item':'note'}, orc.clock(), {'note':1})
    ctx=orc.Context(pending=pending, terminal=False)
    received=[]
    def execute(route, actual_ctx, text, ask):
        received.append((route.skill, route.method, text, actual_ctx.pending))
        return orc.Outcome(route.skill,route.method,{},'名前を確認します。',True,0,private=True)
    monkeypatch.setattr(orc,'execute',execute)
    monkeypatch.setattr(orc,'route',lambda *args,**kwargs:pytest.fail('pendingは振り分け直さない'))
    result=orc.handle('うぅん',ctx,ask=lambda *args:'n')
    assert received == [('obsidian-note','pending','うぅん',pending)]
    assert ctx.pending is pending
    assert result.text == '名前を確認します。'


def test_real_handle_shortcut_has_no_provider(monkeypatch):
    active=skills.Skill('general-chat','会話','active','read',{'repo':'repo-1003-jarvis-claude'},
                        {'question':{'type':'string','required':True}})
    monkeypatch.setattr(skills,'active',lambda **kwargs:{'general-chat':active})
    monkeypatch.setattr(orc,'_has_project_id',lambda text:False)
    monkeypatch.setattr(llm,'complete',lambda *args,**kwargs:pytest.fail('LLM不要'))
    ctx=orc.Context(last_provider='old-provider')
    outcome=orc.handle('ふぅむ……',ctx,ask=lambda *args:'n',use_llm=False)
    assert outcome.skill == 'general-chat' and outcome.text == 'はい。' and outcome.ok
    assert ctx.last_provider is None
