"""会話評価: 本番からの隔離、課金拒否、固定セット、履歴と採点。"""
import json
import os
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from evaluation.conversation import runner as cr


def test_fixed_scenarios():
    data=cr.load_scenarios()
    items=data['scenarios']
    assert len(items)==110
    assert len({s['id'] for s in items})==110
    kinds={s['kind'] for s in items}
    assert len(kinds)==11
    for kind in kinds:
        assert Counter(s['split'] for s in items if s['kind']==kind)=={'dev':6,'val':2,'test':2}
    assert any(len(s['turns'])>1 for s in items)
    assert data['seed']==1901


def test_check_scopes_and_no_llm():
    checks={'max_chars':5,'must_not':['禁止'],'must_any':['候補'],'route':'knowledge-capture',
            'no_llm_needed':True,'confirmation':True,'include_confirmation':True}
    result=cr.check_turn(checks,'未実行','knowledge-capture',0,[{'prompt':'候補を確認します'}])
    assert all(result.values())
    assert not cr.check_turn(checks,'未実行','general-chat',1,[])['route']
    assert not cr.check_turn(checks,'未実行','knowledge-capture',1,[])['no_llm_needed']
    assert not cr.check_turn({**checks,'include_confirmation':False},'未実行','knowledge-capture',0,[{'prompt':'候補'}])['must_any']


@pytest.mark.parametrize('value',['{}','bad','{"scores":{"naturalness":true}}',
    json.dumps({'scores':{k:3 for k in cr.DIMENSIONS}})])
def test_judge_rejects_invalid(value):
    assert cr.parse_judge(value)[0] is None


def test_judge_scores():
    scores={k:2 for k in cr.DIMENSIONS}
    assert cr.parse_judge(json.dumps({'scores':scores,'reason':'適切'}))==(scores,'適切')


def test_macro_aggregation():
    rows=[{'id':'a','kind':'first','checks':0.,'score':0.,'ok':True},
          {'id':'b','kind':'second','checks':1.,'score':1.,'ok':True},
          {'id':'b','kind':'second','checks':1.,'score':1.,'ok':True}]
    result=cr.summarize(rows,False)
    assert result['overall']['score']==.5
    assert result['score_mode']=='checks_only'
    assert result['turns']==3


def test_copy_vault_names_content_and_symlink(tmp_path):
    source=tmp_path/'source';target=tmp_path/'copy';v=source/'vault'
    (v/'_jarvis'/'knowledge').mkdir(parents=True)
    (v/'_jarvis'/'knowledge'/'a.md').write_text('知識')
    (v/'notes').mkdir();(v/'notes'/'b.md').write_text('秘密の本文')
    (v/'.obsidian').mkdir();(v/'.obsidian'/'secret.md').write_text('設定')
    outside=tmp_path/'outside';outside.mkdir();(outside/'c.md').write_text('外')
    (v/'link').symlink_to(outside,target_is_directory=True)
    (v/'linked.md').symlink_to(outside/'c.md')
    result=cr.copy_vaults(source,target)
    assert result=={'notes':2,'content_copies':1}
    assert (target/'vault'/'_jarvis'/'knowledge'/'a.md').read_text()=='知識'
    assert (target/'vault'/'notes'/'b.md').read_text()==''
    assert not (target/'vault'/'link').exists()
    assert not (target/'vault'/'linked.md').exists()
    assert not (target/'vault'/'.obsidian').exists()
    assert (v/'notes'/'b.md').read_text()=='秘密の本文'


def configure_isolation(tmp_path,monkeypatch):
    import config,db,registry
    from evaluation import isolate
    root=tmp_path/'sandbox';root.mkdir()
    paths={'JARVIS_DB_PATH':root/'db.sqlite','JARVIS_VAULT_ROOT':root/'vaults','JARVIS_CKC_DATA_DIR':root/'ckc'}
    for env,p in paths.items():monkeypatch.setenv(env,str(p))
    monkeypatch.setenv(cr.ROOT_ENV,str(root))
    monkeypatch.setenv(isolate.ISOLATED_ENV,'1')
    monkeypatch.setenv(isolate.SOURCE_DB_ENV,str(tmp_path/'original.sqlite'))
    monkeypatch.setenv(cr.SOURCE_VAULT_ENV,str(tmp_path/'original-vaults'))
    monkeypatch.setenv(cr.SOURCE_CKC_ENV,str(tmp_path/'original-ckc'))
    monkeypatch.setattr(config,'DB_PATH',paths['JARVIS_DB_PATH'])
    monkeypatch.setattr(db,'DB_PATH',paths['JARVIS_DB_PATH'])
    monkeypatch.setattr(config,'VAULT_ROOT',paths['JARVIS_VAULT_ROOT'])
    monkeypatch.setattr(config,'CKC_DATA_DIR',paths['JARVIS_CKC_DATA_DIR'])
    monkeypatch.setattr(config,'LATENCY_LOG',root/'latency.jsonl')
    monkeypatch.setattr(config,'LLM_PROVIDER','ollama')
    monkeypatch.setattr(config,'OLLAMA_URL','http://localhost:11434')
    monkeypatch.setattr(registry,'vault_paths',lambda:{'v':root/'vaults'/'v'})
    return root


def test_isolation_checks_actual_and_environment_paths(tmp_path,monkeypatch):
    import config,db
    from evaluation.isolate import IsolationError
    root=configure_isolation(tmp_path,monkeypatch)
    cr.ensure_isolated()
    monkeypatch.setattr(db,'DB_PATH',tmp_path/'outside.db')
    with pytest.raises(IsolationError):cr.ensure_isolated()
    monkeypatch.setattr(db,'DB_PATH',config.DB_PATH)
    monkeypatch.setenv('JARVIS_CKC_DATA_DIR',str(tmp_path/'original-ckc'))
    with pytest.raises(IsolationError):cr.ensure_isolated()


def test_isolation_rejects_symlink_vault_and_remote_llm(tmp_path,monkeypatch):
    import config
    from evaluation.isolate import IsolationError
    root=configure_isolation(tmp_path,monkeypatch)
    outside=tmp_path/'outside';outside.mkdir()
    (root/'vaults').symlink_to(outside,target_is_directory=True)
    with pytest.raises(IsolationError):cr.ensure_isolated()
    (root/'vaults').unlink()
    monkeypatch.setattr(config,'OLLAMA_URL','https://example.test')
    with pytest.raises(IsolationError):cr.ensure_isolated()


def test_parent_copies_db_and_sets_all_paths(db_path,tmp_path,monkeypatch):
    import config
    source=tmp_path/'source-vaults';source.mkdir()
    args=SimpleNamespace(db=str(db_path),vault_root=str(source),output_dir=str(tmp_path.parent/'result'),model='local-model',skills=None,registry=None)
    calls=[]
    def run(cmd,env,cwd):
        root=Path(env[cr.ROOT_ENV]);calls.append(root)
        assert Path(env['JARVIS_DB_PATH']).is_file()
        assert Path(env['JARVIS_DB_PATH'])!=db_path
        assert all(cr._within(Path(env[k]),root) for k in ('JARVIS_DB_PATH','JARVIS_VAULT_ROOT','JARVIS_CKC_DATA_DIR'))
        assert env['JARVIS_LLM_PROVIDER']=='ollama'
        assert env['JARVIS_CHAT_MODEL']=='local-model'
        assert 'OPENAI_API_KEY' not in env
        assert 'ANTHROPIC_API_KEY' not in env
        assert cmd[-2:]==['--split','dev']
        return SimpleNamespace(returncode=0)
    monkeypatch.setenv('OPENAI_API_KEY','fake-for-test')
    assert cr.run_parent(args,['--split','dev'],run=run)==0
    assert not calls[0].exists()
    assert db_path.is_file()


def test_only_ollama_and_safe_handlers(monkeypatch):
    import llm,orchestrator
    monkeypatch.setitem(llm.PROVIDERS,'ollama',lambda prompt,**kwargs:'ローカル')
    counter=[0]
    with cr.ollama_only(counter,True):
        assert llm.complete('質問')=='ローカル'
        with pytest.raises(llm.LLMError):llm.complete('質問',provider='claude_cli')
        with pytest.raises(llm.LLMError):llm.ask_claude('質問')
        with pytest.raises(orchestrator.SkillError):orchestrator.HANDLERS['control-center']({},None,'')
        ctx=orchestrator.Context()
        ctx.harness='codex'                    # Owner が切り替えていても、評価では Ollama だけ（T-2002 の後）
        assert orchestrator.harness_order(ctx,'chat')==('ollama',)
        assert orchestrator.harness_order(ctx,'answer')==('ollama',)
        with pytest.raises(llm.LLMError):llm.complete('質問',provider='codex')
        with pytest.raises(llm.LLMError):llm._stream_codex('質問',lambda t:None)
    assert counter==[1]


def test_handle_history_and_always_decline(monkeypatch):
    import orchestrator,llm
    calls=[]
    def handle(text,ctx,ask):
        calls.append((ctx,list(ctx.history)))
        assert ctx.terminal is False and ctx.voice is True
        assert ask('書きますか')=='n'
        assert ctx.review('候補','確認')=='n'
        return orchestrator.Outcome('general-chat','default',{},'短い返事です',True,0)
    monkeypatch.setattr(orchestrator,'handle',handle)
    monkeypatch.setattr(cr,'judge_turn',lambda *args:({k:2 for k in cr.DIMENSIONS},'適切'))
    t={'text':'模擬の発言','checks':{'max_chars':160,'confirmation':True},'rubric':{k:'' for k in cr.DIMENSIONS}}
    items=[{'id':'a','kind':'smalltalk','split':'dev','turns':[t,t]}, {'id':'b','kind':'smalltalk','split':'dev','turns':[t]}]
    rows=cr.run_scenarios(items,True,'judge')
    assert len(calls[0][1])==0 and len(calls[1][1])==2 and len(calls[2][1])==0
    assert calls[0][0] is calls[1][0] and calls[0][0] is not calls[2][0]
    assert all(r['score']==1 for r in rows)
    assert all(c['answer']=='n' for r in rows for c in r['confirmation'])


def test_failed_empty_response_not_full_score(monkeypatch):
    import orchestrator
    monkeypatch.setattr(orchestrator,'handle',lambda *a,**kw:orchestrator.Outcome('general-chat','default',{},'',False,0,error='failed'))
    t={'text':'質問','checks':{'max_chars':30},'rubric':{k:'' for k in cr.DIMENSIONS}}
    rows=cr.run_scenarios([{'id':'x','kind':'smalltalk','split':'dev','turns':[t]}],False,'judge',available=False)
    assert rows[0]['score']<1 and not rows[0]['check_results']['execution_ok']


def test_test_limit_forbidden():
    with pytest.raises(SystemExit) as ex:cr.main(['--split','test','--limit','1'])
    assert ex.value.code==2


def test_output_guard_rejects_production_even_when_base_is_production(tmp_path,monkeypatch):
    import config
    from evaluation.isolate import IsolationError
    production=Path.home()/'work'/'0002_Jarvis2'/'1003_Jarvis_claude'
    monkeypatch.setattr(config,'BASE_DIR',production)
    with pytest.raises(IsolationError):
        cr.ensure_output_path(production/'data'/'eval',production/'jarvis.db',tmp_path/'v',tmp_path/'c')
    with pytest.raises(IsolationError):
        cr.ensure_output_path(tmp_path/'v'/'out',tmp_path/'db'/'source.db',tmp_path/'v',tmp_path/'c')
    with pytest.raises(IsolationError):
        cr.ensure_output_path(tmp_path/'c'/'out',tmp_path/'db'/'source.db',tmp_path/'v',tmp_path/'c')


def test_real_handle_capture_never_executes(monkeypatch):
    import orchestrator,skills
    skill=skills.FALLBACK['knowledge-capture']
    monkeypatch.setattr(skills,'active',lambda **kw:{skill.name:skill})
    monkeypatch.setattr(orchestrator,'route',lambda *a,**kw:orchestrator.Route('knowledge-capture',{},'rule'))
    def forbidden(*a,**kw):
        pytest.fail('n の後に抽出してはいけない')
    monkeypatch.setitem(orchestrator.HANDLERS,'knowledge-capture',forbidden)
    t={'text':'模擬の保存依頼','checks':{'route':'knowledge-capture','confirmation':True},'rubric':{k:'' for k in cr.DIMENSIONS}}
    rows=cr.run_scenarios([{'id':'x','kind':'capture','split':'dev','turns':[t]}],False,'judge')
    assert rows[0]['error']=='declined'
    assert rows[0]['confirmation'][0]['answer']=='n'
    assert rows[0]['llm_calls']==0


def test_cli_dispatch(monkeypatch):
    import evaluation.__main__ as cli
    monkeypatch.setattr(cr,'main',lambda args:7 if args==['--split','dev'] else 9)
    assert cli.main(['conversation','--split','dev'])==7
