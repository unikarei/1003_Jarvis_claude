"""保存依頼・接続診断・知識化の受け入れ（本番には書かない）。"""
import pytest
import orchestrator as orc
import obsidian_note
import skills
from ckc.models import Speaker, Turn

@pytest.mark.parametrize('text', [
    '売り主は売却に前向き。進捗として要約して記録しておいて。',
    'この話を残して', 'この話を残しておいてください', 'この内容を保存してください', '毎朝30分練習する。覚えておいて',
    'ナレッジとして保存して',
])
def test_save_commands(text, monkeypatch):
    monkeypatch.setattr(skills, 'active', lambda: dict(skills.FALLBACK))
    assert orc.route(text, orc.Context()).skill == 'knowledge-capture'

@pytest.mark.parametrize('text', [
    'この話を保存しないで', 'この話を残してくれる？', '進捗は記録してある',
    'ファイルを保存して', 'この話を残してと言ったのに保存されない',
    'この話の記録は不要。保存して',
])
def test_noncommands(text):
    assert not obsidian_note.is_capture_request(text)

def test_recognition_alias_preserves_edit_route():
    assert obsidian_note.matches('大洗のオブシリアンに進捗ノートを作って')
    assert orc.route('大洗のオブシリアンに進捗ノートを作って', orc.Context()).skill == 'obsidian-note'

def test_inline_save_content_reaches_capture(monkeypatch):
    import chat_actions
    text = '売り主は売却に前向き。進捗として記録しておいて'
    ctx = orc.Context(terminal=False, review=lambda *args: 'later')
    seen = []
    monkeypatch.setattr(chat_actions, 'run_ckc_with_cards', lambda history, *a: (seen.extend(history) or '候補', False))
    orc._capture({}, ctx, text)
    assert seen == [Turn(Speaker.OWNER, text)]
    orc._capture({}, ctx, text)
    assert len(ctx.history) == 1

@pytest.mark.parametrize('text', ['ナレッジの接続状況', 'Obsidianの接続状態は？', '保存ツールの状態'])
def test_diagnostic_route_without_llm(text):
    assert orc.route(text, orc.Context()).skill == 'knowledge-stats'


def test_diagnostic_is_read_only_and_reports_both_vaults(db_path, make_vault, monkeypatch):
    import db
    import knowledge_connections as connections
    jk, oarai = make_vault('jarvis-knowledge'), make_vault('oarai-obsidian')
    db.insert_node('Knowledge', title='汎用', meta={'resource_id': 'vault-jarvis-knowledge'})
    db.insert_node('Knowledge', title='大洗', url='https://github.com/unikarei/oarai-obsidian/blob/main/test.md')
    before = db_path.read_bytes()
    text = connections.report()
    assert 'jarvis-knowledge: 存在' in text and 'oarai-obsidian: 存在' in text
    assert text.count('検索DBの知識 1 件') == 2
    assert '書き込み・Windows側の同期は未検証' in text
    assert db_path.read_bytes() == before
    (jk / '.git/index.lock').touch()
    assert 'Git の操作中' in connections.report()


def test_diagnostic_missing_db_does_not_create_file(tmp_path, monkeypatch):
    import config
    import knowledge_connections as connections
    path = tmp_path / 'missing.db'
    monkeypatch.setattr(config, 'DB_PATH', path)
    monkeypatch.setattr(skills, 'active', lambda: {})
    text = connections.report()
    assert 'ファイルなし' in text and '無効・未登録' in text and not path.exists()

@pytest.mark.parametrize('project,vault_name', [('piano', 'jarvis-knowledge'), ('oarai-minpaku', 'oarai-obsidian')])
@pytest.mark.parametrize('choice', ['save', 'reject'])
def test_save_review_then_retrieve_in_new_context(project, vault_name, choice, db_path, ckc_data_dir, make_vault, monkeypatch):
    import json
    import llm
    import knowledge_engine as ke
    from ckc import store
    from ckc.models import CandidateStatus
    make_vault('jarvis-knowledge')
    make_vault('oarai-obsidian')
    active = dict(skills.FALLBACK)
    monkeypatch.setattr(skills, 'active', lambda: active)
    body = '検証用の合言葉は青空の灯台と決めた'
    request = body + '。進捗として記録しておいて'
    prompts, cards = [], []

    def fake(prompt, **kwargs):
        prompts.append(prompt)
        if '"items"' in prompt:
            return json.dumps({'items': [{'kind': 'Decision', 'title': '検証用の合言葉', 'body': body,
                'projects': [project], 'category': None, 'confidence': 'confirmed',
                'evidence': [{'quote': body, 'speaker': 'owner'}]}]}, ensure_ascii=False)
        return body + '。[1]'

    monkeypatch.setattr(llm, 'complete', fake)
    ctx = orc.Context(terminal=False, review=lambda card, spoken: cards.append(card) or choice)
    outcome = orc.handle(request, ctx, ask=lambda *a: 'y', use_llm=False)
    assert outcome.ok, outcome.text
    assert cards and vault_name in cards[0]
    rows = store.list_candidates(CandidateStatus.PUBLISHED if choice == 'save' else CandidateStatus.REJECTED)
    assert len(rows) == 1
    paths = list((__import__('config').VAULT_ROOT / vault_name / '_jarvis').rglob('*.md'))
    if choice == 'reject':
        assert not paths and not ke.search('合言葉')
        return
    assert len(paths) == 1 and body in paths[0].read_text()
    assert not list((__import__('config').VAULT_ROOT / ('oarai-obsidian' if vault_name == 'jarvis-knowledge' else 'jarvis-knowledge') / '_jarvis').rglob('*.md'))
    fresh = orc.Context(harness='ollama')
    answer = orc.handle('検証用の合言葉を教えて', fresh, use_llm=False)
    assert answer.ok and body in answer.text
    assert answer.items and store.get_candidate_row(rows[0].candidate_id)['ckc_id'] in [item['knowledge_id'] for item in answer.items]
    assert body in prompts[-1]  # 回答を返す LLM に保存済みの資料が実際に渡る


def test_diagnostic_does_not_swallow_save_command():
    import knowledge_connections
    assert not knowledge_connections.matches('ナレッジの接続状態を記録して')

@pytest.mark.parametrize('project,vault_name', [('piano', 'jarvis-knowledge'), ('oarai-minpaku', 'oarai-obsidian')])
def test_browser_inline_save_runs_real_capture(project, vault_name, db_path, ckc_data_dir, make_vault, monkeypatch):
    import json
    import llm
    import config
    from tests.test_voice_web import Fake, make_session, wav
    from tests.test_voice_web_speech import Clock, until
    from ckc import store
    from ckc.models import CandidateStatus
    make_vault('jarvis-knowledge')
    make_vault('oarai-obsidian')
    monkeypatch.setattr(skills, 'active', lambda: dict(skills.FALLBACK))
    text = '検証用の予定は毎朝30分と決めた。進捗として記録しておいて'
    monkeypatch.setattr(llm, 'complete', lambda *a, **kw: json.dumps({'items': [{
        'kind': 'Decision', 'title': '検証用の予定', 'body': '予定は毎朝30分', 'projects': [project],
        'category': None, 'confidence': 'confirmed', 'evidence': [{'quote': '予定は毎朝30分と決めた', 'speaker': 'owner'}]}]}))
    session, events, _ = make_session(Fake(text=text), handle=lambda t, c, ask=None: orc.handle(t, c, ask=ask, use_llm=False), clock=Clock())
    try:
        session.submit(wav(), 48000)
        events.wait('approval')
        first = events.of('approval')[0]
        assert session.approve(1, first['id'], 'run') == 'ok'
        until(lambda: len(events.of('approval')) == 2)
        card = events.of('approval')[1]
        assert vault_name in card['text']
        assert session.approve(1, card['id'], 'run') == 'ok'
        events.wait('turn_done')
        assert len(store.list_candidates(CandidateStatus.PUBLISHED)) == 1
        assert len(list((config.VAULT_ROOT / vault_name / '_jarvis').rglob('*.md'))) == 1
    finally:
        session.end()



def test_obsidian_knowledge_request_includes_target_context(monkeypatch):
    import chat_actions
    text = '売り主は売却に前向き。大洗のObsidianにナレッジを入れて'
    seen = []
    ctx = orc.Context(terminal=False, review=lambda *a: 'later')
    monkeypatch.setattr(chat_actions, 'run_ckc_with_cards', lambda history, *a: (seen.extend(history) or '候補', False))
    orc._capture({}, ctx, text)
    assert seen[-1].text == text
