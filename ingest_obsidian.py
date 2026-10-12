"""個人の Obsidian Vault のノートを、ナレッジグラフに Source/Knowledge として取り込む。

2026-10 から、GitHub API ではなく Mac mini のローカルの Vault を読む（../docs/decisions.md ADR-0016、T-202）。
Vault の場所は Architecture Registry から得る（registry.py）。Vault の Git は操作しない（読み取りだけ）。
ノートは人間が整理した参照用ナレッジのため、要約せず全文を Knowledge として保存する。

- vault-oarai-obsidian: `民泊@大洗/` の下（カテゴリ minpaku）
- vault-jarvis-knowledge: Vault 全体。ただし `_jarvis/`（CKC が保存した知識は保存時にナレッジグラフへ登録済み）は除く

ノードの URL は従来の GitHub の blob URL と同じ形にする（既存のノードと重複させないため）。ファイル名は NFC に正規化する。
"""

import unicodedata
from dataclasses import dataclass
from pathlib import Path

from db import find_knowledge_by_url, find_source_by_url, insert_edge, insert_node, update_node_content
from vault import VaultError, resolve_vault

BRANCH = "main"


@dataclass(frozen=True)
class VaultSource:
    resource_id: str
    repo: str                   # GitHub の owner/name（URL の組み立てに使う）
    include_dir: str | None     # この下だけを取り込む（None なら Vault 全体）
    category: str | None
    exclude: frozenset[str] = frozenset()


SOURCES = [
    VaultSource("vault-oarai-obsidian", "unikarei/oarai-obsidian", "民泊@大洗", "minpaku", frozenset({
        "民泊@大洗/99_テンプレート/大洗_調査ノート雛形.md",  # 空のテンプレート
        "民泊@大洗/90_資料/資料の追加方法.md",  # 資料追加案内のみ
    })),
    VaultSource("vault-jarvis-knowledge", "unikarei/jarvis-knowledge", None, None),
]
SKIP_TOP_DIRS = {".git", ".obsidian", ".trash", "_jarvis"}


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def list_notes(source: VaultSource, vault_dir: Path) -> list[str]:
    """取り込む Markdown の Vault 内の相対パス（NFC、昇順）。"""
    base = vault_dir / source.include_dir if source.include_dir else vault_dir
    paths = []
    for path in base.rglob("*.md"):
        rel = _nfc(path.relative_to(vault_dir).as_posix())
        if rel.split("/", 1)[0] in SKIP_TOP_DIRS or rel in source.exclude:
            continue
        paths.append(rel)
    return sorted(paths)


def blob_url(source: VaultSource, rel: str) -> str:
    return f"https://github.com/{source.repo}/blob/{BRANCH}/{rel}"


def ingest_vault(source: VaultSource) -> dict[str, list[str]]:
    vault_dir = resolve_vault(source.resource_id)
    if not vault_dir.is_dir():
        raise VaultError(f"Vault が見つかりません: {vault_dir}")
    created, updated = [], []
    for rel in list_notes(source, vault_dir):
        content = (vault_dir / rel).read_text(encoding="utf-8")
        url = blob_url(source, rel)
        title = rel.rsplit("/", 1)[-1].removesuffix(".md")

        existing_source = find_source_by_url(url)
        if existing_source is None:
            source_id = insert_node(node_type="Source", category=source.category, title=title, content=content,
                                    url=url, meta={"vault_path": rel, "resource_id": source.resource_id})
            knowledge_id = insert_node(node_type="Knowledge", category=source.category, title=title,
                                       content=content, url=url)
            insert_edge(knowledge_id, source_id, "derived_from")
            created.append(knowledge_id)
        elif existing_source["content"] != content:
            update_node_content(existing_source["id"], content)
            existing_knowledge = find_knowledge_by_url(url)
            if existing_knowledge:
                update_node_content(existing_knowledge["id"], content)
                updated.append(existing_knowledge["id"])
    return {"created": created, "updated": updated}


def ingest_all() -> dict[str, list[str]]:
    """すべての Vault を取り込み、新規・更新の Knowledge ID を返す。読めない Vault は飛ばして表示する。"""
    result = {"created": [], "updated": []}
    for source in SOURCES:
        try:
            r = ingest_vault(source)
        except VaultError as e:
            print(f"[skip] {source.resource_id}: {e}")
            continue
        result["created"] += r["created"]
        result["updated"] += r["updated"]
    return result


if __name__ == "__main__":
    result = ingest_all()
    print(f"新規Knowledge: {len(result['created'])}件 / 更新: {len(result['updated'])}件")
    for kid in result["created"]:
        print(f"  [new] {kid}")
    for kid in result["updated"]:
        print(f"  [updated] {kid}")
