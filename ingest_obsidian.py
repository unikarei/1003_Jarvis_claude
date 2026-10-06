"""個人のObsidianボルト（GitHub: unikarei/oarai-obsidian）の民泊ノートを取り込む。

対象リポジトリはprivateのため、`gh auth token`（ghコマンドの認証情報）でGitHub APIに
アクセスする。ローカルへのclone・ネストしたgitリポジトリは作らない。
ノートは既に人間が整理した参照用ナレッジのため、要約せず全文をKnowledgeとして保存する。
"""

import requests

from db import find_knowledge_by_url, find_source_by_url, insert_edge, insert_node, update_node_content
from github_auth import get_github_token

REPO_OWNER = "unikarei"
REPO_NAME = "oarai-obsidian"
BRANCH = "main"
TARGET_DIR = "民泊@大洗"
CATEGORY = "minpaku"
API_BASE = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}"

EXCLUDE_PATHS = {
    f"{TARGET_DIR}/99_テンプレート/大洗_調査ノート雛形.md",  # 空のテンプレート
    f"{TARGET_DIR}/90_資料/資料の追加方法.md",  # 資料追加案内のみ
}


def list_markdown_paths(token: str) -> list[str]:
    res = requests.get(
        f"{API_BASE}/git/trees/{BRANCH}",
        params={"recursive": "1"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    res.raise_for_status()
    tree = res.json()["tree"]
    return [
        item["path"]
        for item in tree
        if item["type"] == "blob"
        and item["path"].startswith(f"{TARGET_DIR}/")
        and item["path"].endswith(".md")
        and item["path"] not in EXCLUDE_PATHS
    ]


def fetch_raw_content(path: str, token: str) -> str:
    res = requests.get(
        f"{API_BASE}/contents/{path}",
        params={"ref": BRANCH},
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.raw"},
        timeout=15,
    )
    res.raise_for_status()
    return res.text


def ingest_all() -> dict[str, list[str]]:
    """ボルト内のMarkdownノートをSource/Knowledgeノードとして取り込む。
    既存ノートは内容が変わっていればcontentを更新する（新規ノード追加はしない）。
    新規・更新それぞれのKnowledge IDの一覧を返す。
    """
    token = get_github_token()
    created, updated = [], []
    for path in list_markdown_paths(token):
        blob_url = f"https://github.com/{REPO_OWNER}/{REPO_NAME}/blob/{BRANCH}/{path}"
        content = fetch_raw_content(path, token)
        title = path.rsplit("/", 1)[-1].removesuffix(".md")

        existing_source = find_source_by_url(blob_url)
        if existing_source is None:
            source_id = insert_node(
                node_type="Source", category=CATEGORY, title=title, content=content, url=blob_url,
                meta={"vault_path": path},
            )
            knowledge_id = insert_node(
                node_type="Knowledge", category=CATEGORY, title=title, content=content, url=blob_url,
            )
            insert_edge(knowledge_id, source_id, "derived_from")
            created.append(knowledge_id)
        elif existing_source["content"] != content:
            update_node_content(existing_source["id"], content)
            existing_knowledge = find_knowledge_by_url(blob_url)
            if existing_knowledge:
                update_node_content(existing_knowledge["id"], content)
                updated.append(existing_knowledge["id"])

    return {"created": created, "updated": updated}


if __name__ == "__main__":
    result = ingest_all()
    print(f"新規Knowledge: {len(result['created'])}件 / 更新: {len(result['updated'])}件")
    for kid in result["created"]:
        print(f"  [new] {kid}")
    for kid in result["updated"]:
        print(f"  [updated] {kid}")
