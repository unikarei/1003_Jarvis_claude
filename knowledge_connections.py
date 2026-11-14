"""Vault・スキル・検索DBの接続状態を、書き込まずに確認する（T-2302）。"""
from contextlib import closing
import json
import re
import sqlite3

import config
import registry
import skills
import vault
from obsidian_note import normalize


def matches(text: str) -> bool:
    t = normalize(text)
    if re.search(r"保存して|記録して|残して|作って|追加して|追記して", t):
        return False
    return (t.rstrip('。？?') == '保存ツールの状態' or
            (any(w in t for w in ('ナレッジ', '知識', '書庫', 'obsidian', 'オブシディアン', '保存ツール')) and
             any(w in t for w in ('接続状況', '接続状態', '何につなが', 'どこにつなが', 'どれに接続'))))


def report() -> str:
    """ファイルを作らない。DB の不在・ロック・古いスキーマは不明として表示する。"""
    lines = ['ナレッジの接続状況（実測・読み取りのみ）']
    active = skills.active()
    for name in ('knowledge-answer', 'knowledge-capture', 'obsidian-note'):
        lines.append(f"スキル {name}: {'有効' if name in active else '無効・未登録'}")
    paths = registry.vault_paths()
    counts, pending, db_ready = {}, None, False
    if not config.DB_PATH.is_file():
        lines.append('検索DB: 未接続（ファイルなし）')
    else:
        try:
            with closing(sqlite3.connect(config.DB_PATH.resolve().as_uri() + '?mode=ro', uri=True, timeout=0.1)) as conn:
                conn.execute('PRAGMA query_only=ON')
                rows = conn.execute("SELECT url, meta FROM nodes WHERE type='Knowledge'").fetchall()
                for url, meta in rows:
                    data = json.loads(meta or '{}')
                    if not isinstance(data, dict):
                        raise ValueError('知識のメタデータが不正')
                    for rid, path in paths.items():
                        # 通常の取り込みは URL、CKC 保存は resource_id が由来を持つ。
                        if data.get('resource_id') == rid or f'/{path.name}/blob/' in (url or ''):
                            counts[rid] = counts.get(rid, 0) + 1
                db_ready = True
                lines.append(f'検索DB: 読み取り成功（知識 {len(rows)} 件）')
                pending = conn.execute("SELECT count(*) FROM ckc_candidates WHERE status='pending'").fetchone()[0]
        except (OSError, sqlite3.Error, ValueError, TypeError):
            lines.append('検索DBまたはCKC: 確認失敗（件数は未確認）')
            counts, pending, db_ready = {}, None, False
    for rid in config.FALLBACK_VAULTS:
        path = paths.get(rid)
        if path is None:
            lines.append(f'{rid}: この端末の配置先が未登録')
            continue
        try:
            vault.check_ready(path)
            state = '存在・Git操作中の印なし'
        except (vault.VaultError, OSError) as e:
            state = str(e)
        number = str(counts.get(rid, 0)) if db_ready else '未確認'
        lines.append(f'{path.name}: {state} / 検索DBの知識 {number} 件')
    lines.append(f"CKC保存待ち: {pending if pending is not None else '未確認'} 件")
    lines.append(f"会話の記憶: {'有効（設定値）' if config.MEMORY_ENABLED else '無効（設定値）'}")
    lines.append('保存は「ナレッジ化して」、ノートの編集は「Obsidian の〇〇に追加して」。内容と保存先を確認してから保存します。')
    lines.append('Vaultへの書き込み・Windows側の同期は未検証です。回答LLMの操作ツールを無効にしていても、Jarvis側の保存スキルは別に動きます。')
    return '\n'.join(lines)
