"""個人の Obsidian Vault のノートを、ナレッジグラフに Source/Knowledge として取り込む。

2026-10 から、GitHub API ではなく Mac mini のローカルの Vault を読む（../docs/decisions.md ADR-0016、T-202）。
Vault の場所は Architecture Registry から得る（registry.py）。Vault の Git は操作しない（読み取りだけ）。
ノートは人間が整理した参照用ナレッジのため、要約せず全文を Knowledge として保存する。

- vault-oarai-obsidian: `民泊@大洗/` の下（カテゴリ minpaku）
- vault-jarvis-knowledge: Vault 全体。ただし `_jarvis/`（CKC が保存した知識は保存時にナレッジグラフへ登録済み）は除く

毎日 05:45 に launchd（`com.ohide.jarvis.ingest-obsidian`）が `--scheduled` で動かす（T-1801、統合ワークスペースの spec §3.3.13）。

ノードの URL は従来の GitHub の blob URL と同じ形にする（既存のノードと重複させないため）。ファイル名は NFC に正規化する。
"""

import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from db import find_knowledge_by_url, find_source_by_url, insert_edge, insert_node, source_urls_with_prefix, \
    update_node_content
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


def missing_notes(source: VaultSource, vault_dir: Path) -> list[str]:
    """取り込み済みなのに、今の Vault の取り込みの範囲にないノートの相対パス（消えた・移動した・範囲の外に出た）。
    知識は消さない（データの削除は Owner の判断、spec §3.3.13）。"""
    prefix = f"https://github.com/{source.repo}/blob/{BRANCH}/"
    present = set(list_notes(source, vault_dir))
    return sorted(url[len(prefix):] for url in source_urls_with_prefix(prefix) if url[len(prefix):] not in present)


JOB_NAME = "Obsidian の取り込み"


def run_scheduled(sources: list[VaultSource] | None = None, notify=None, out=print) -> int:
    """毎日の定期の取り込み（launchd の com.ohide.jarvis.ingest-obsidian、T-1801、spec §3.3.13）。
    Vault ごとに取り込み、新規・更新・消えたノートの件数（と消えたノートの相対パス）だけを出す（本文は出さない）。
    読めない Vault・失敗した Vault は飛ばしてほかを続け、最後に失敗を通知して 1 を返す。すべて成功なら 0。"""
    if notify is None:
        from discovery.notify import notify_failure as notify
    stamp = datetime.now().isoformat(timespec="seconds")
    failed = []
    for source in sources if sources is not None else SOURCES:
        try:
            vault_dir = resolve_vault(source.resource_id)
            result = ingest_vault(source)
            gone = missing_notes(source, vault_dir)
        except Exception as e:  # noqa: BLE001  1 つの Vault の失敗で、ほかの Vault を止めない
            failed.append(f"{source.resource_id}: {type(e).__name__}")
            out(f"{stamp} {source.resource_id} 失敗: {type(e).__name__}: {str(e)[:200]}")
            continue
        out(f"{stamp} {source.resource_id} 新規 {len(result['created'])} 件・更新 {len(result['updated'])} 件・"
            f"消えたノート {len(gone)} 件")
        for rel in gone:
            out(f"{stamp} {source.resource_id}   消えたノート（知識は残しています）: {rel}")
    if failed:
        notify(JOB_NAME, "、".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    if "--scheduled" in sys.argv[1:]:
        sys.exit(run_scheduled())
    result = ingest_all()
    print(f"新規Knowledge: {len(result['created'])}件 / 更新: {len(result['updated'])}件")
    for kid in result["created"]:
        print(f"  [new] {kid}")
    for kid in result["updated"]:
        print(f"  [updated] {kid}")
