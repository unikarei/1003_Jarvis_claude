"""承認された候補の保存（Publish）: Vault の _jarvis/knowledge/ にノートを作り、ナレッジグラフに登録する。

- 書き込みは vault.py を通す（Git の状態確認、上書きしない新規作成、原子的な書き込み）。
- 書き込む前に秘密情報をもう一度検出する。
- 「既存を置き換え」の場合は、新しいノートを作ってから、古いノートの status を superseded にする
  （古いノートの本文は変えない）。古いノートを更新できなかった場合は警告を返し、あとで再実行できる。
- Vault への書き込みが失敗したら、候補は approved のまま残し、ナレッジグラフにも登録しない。
"""

import json
from dataclasses import dataclass

import db
import vault
from ckc import store
from ckc.models import CandidateStatus, KnowledgeNote, NoteStatus, SourceRef, new_id, now_iso
from ckc.route import route_vault
from ckc.render import note_filename, note_relpath, parse_note, render_note
from ckc.secrets import find_secrets


class PublishError(Exception):
    """保存を中止した。Vault にもナレッジグラフにも何も書いていない。"""


@dataclass
class PublishResult:
    ckc_id: str
    resource_id: str
    note_relpath: str
    warning: str | None = None


def _source_url(conversation_id: str) -> str:
    return f"ckc://conversation/{conversation_id}"


def _ensure_source_node(conv) -> str:
    url = _source_url(conv.conversation_id)
    existing = db.find_source_by_url(url)
    if existing:
        return existing["id"]
    return db.insert_node("Source", title=conv.title, content=None, url=url,
                          meta={"ckc_conversation_id": conv.conversation_id, "origin": conv.origin.value})


def mark_superseded(old_ckc_id: str, new_ckc_id: str) -> None:
    """古い知識のノートとグラフの status を superseded にする（本文は変えない）。"""
    node = db.get_node(old_ckc_id)
    if node is None:
        raise PublishError(f"置き換える知識が見つかりません: {old_ckc_id}")
    meta = json.loads(node["meta"] or "{}")
    content, sha = vault.read_file(meta["resource_id"], meta["note_relpath"])
    note = parse_note(content)
    note.status = NoteStatus.SUPERSEDED
    if new_ckc_id not in note.superseded_by:
        note.superseded_by.append(new_ckc_id)
    vault.modify_file(meta["resource_id"], meta["note_relpath"], render_note(note), expected_digest=sha)
    meta.update(status=NoteStatus.SUPERSEDED.value, superseded_by=note.superseded_by)
    db.update_node_meta(old_ckc_id, meta)


def publish(candidate_id: str) -> PublishResult:
    row = store.get_candidate_row(candidate_id)
    c = store.get_candidate(candidate_id)
    if c.status != CandidateStatus.APPROVED:
        raise PublishError(f"承認されていない候補は保存しません（状態: {c.status.value}）")
    texts = [c.title, c.body, *(e.quote for e in c.evidence)]
    if any(find_secrets(t) for t in texts):
        raise PublishError("秘密情報の可能性があるため保存しません。候補を修正してください。")

    conv = store.load_conversation(c.conversation_id)
    supersedes = [row["supersedes"]] if row["supersedes"] else []
    now = now_iso()
    note = KnowledgeNote(
        ckc_id=new_id("ckc"), kind=c.kind, status=NoteStatus.ACTIVE, title=c.title, body=c.body,
        projects=c.projects, category=c.category, confidence=c.confidence,
        source=SourceRef(conv.origin, conv.conversation_id, conv.title, conv.captured_at, conv.occurred_at),
        evidence=c.evidence, created=now, approved=now, supersedes=supersedes,
    )
    resource_id = c.resource_id or route_vault(c.projects)
    try:
        path = vault.create_file(resource_id, note_relpath(note, note_filename(now, c.title)), render_note(note),
                                 number_if_exists=True)
    except (vault.VaultError, OSError) as e:
        raise PublishError(f"Vault に書き込めませんでした（候補は承認済みのまま残します）: {e}") from None
    relpath = path.relative_to(vault.resolve_vault(resource_id).resolve()).as_posix()

    source_id = _ensure_source_node(conv)
    db.insert_node("Knowledge", category=c.category, title=c.title, content=c.body, node_id=note.ckc_id,
                   url=f"ckc://knowledge/{note.ckc_id}",
                   meta={"ckc_id": note.ckc_id, "kind": c.kind.value, "status": NoteStatus.ACTIVE.value,
                         "projects": c.projects, "confidence": c.confidence.value, "resource_id": resource_id,
                         "note_relpath": relpath, "conversation_id": conv.conversation_id,
                         "supersedes": supersedes})
    db.insert_edge(note.ckc_id, source_id, "derived_from")
    store.set_status(candidate_id, CandidateStatus.PUBLISHED, ckc_id=note.ckc_id, note_relpath=relpath)

    warning = None
    for old in supersedes:
        try:
            mark_superseded(old, note.ckc_id)
        except (PublishError, vault.VaultError, OSError, ValueError, KeyError) as e:
            warning = (f"新しい知識は保存しましたが、古い知識 {old} の状態を更新できませんでした: {e}。"
                       f"同期後に `ckc_cli.py supersede {old} {note.ckc_id}` で再実行してください。")
    return PublishResult(note.ckc_id, resource_id, relpath, warning)
