"""個人のObsidianボルト（GitHub: unikarei/oarai-obsidian）の民泊ノートを取り込む。

対象リポジトリはprivateのため、`gh auth token`（ghコマンドの認証情報）でGitHub APIに
アクセスする。ローカルへのclone・ネストしたgitリポジトリは作らない。
ノートは既に人間が整理した参照用ナレッジのため、要約せず全文をKnowledgeとして保存する。
"""

import subprocess

import requests

from db import find_source_by_url, insert_edge, insert_node

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


def _get_github_token() -> str:
    """ghコマンドの認証情報からGitHubトークンを取得する（本リポジトリには保存しない）。"""
    result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True)
    return result.stdout.strip()


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


def ingest_all() -> list[str]:
    """ボルト内のMarkdownノートをSource/Knowledgeノードとして取り込み、新規Knowledge IDの一覧を返す。"""
    token = _get_github_token()
    new_knowledge_ids = []
    for path in list_markdown_paths(token):
        blob_url = f"https://github.com/{REPO_OWNER}/{REPO_NAME}/blob/{BRANCH}/{path}"
        if find_source_by_url(blob_url) is not None:
            continue

        content = fetch_raw_content(path, token)
        title = path.rsplit("/", 1)[-1].removesuffix(".md")

        source_id = insert_node(
            node_type="Source", category=CATEGORY, title=title, content=content, url=blob_url,
            meta={"vault_path": path},
        )
        knowledge_id = insert_node(
            node_type="Knowledge", category=CATEGORY, title=title, content=content, url=blob_url,
        )
        insert_edge(knowledge_id, source_id, "derived_from")
        new_knowledge_ids.append(knowledge_id)

    return new_knowledge_ids


if __name__ == "__main__":
    new_ids = ingest_all()
    print(f"新規Knowledge: {len(new_ids)}件")
    for kid in new_ids:
        print(f"  {kid}")
