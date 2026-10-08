"""⑥民泊: 大洗Project OS（GitHub: unikarei/oarai-project-os-since2026）のdata/docsを取り込む。

対象リポジトリはprivateのため、`gh auth token`でGitHub APIに認証する。
JARVISはこのリポジトリを読み取るのみで、変更・コミット・importは一切行わない。

- `data/*.json`: 構造化データ（タスク・決定事項・リスク等）。要約せず整形JSONのまま保存する
- `docs/**/*.md`（`docs/templates/`配下の雛形は除外）、`README.md`、`AGENTS.md`: 全文を保存する
"""

import json

import requests

from config import OARAI_PROJECT_OS_REPO
from db import find_knowledge_by_url, find_source_by_url, insert_edge, insert_node, update_node_content
from github_auth import get_github_token

BRANCH = "main"
CATEGORY = "minpaku"
API_BASE = f"https://api.github.com/repos/{OARAI_PROJECT_OS_REPO}"

INCLUDE_TOP_LEVEL = ("data/", "docs/", "README.md", "AGENTS.md")
EXCLUDE_PREFIXES = ("docs/templates/",)


def _is_target_path(path: str) -> bool:
    if path in ("README.md", "AGENTS.md"):
        return True
    if not (path.startswith("data/") or path.startswith("docs/")):
        return False
    if any(path.startswith(p) for p in EXCLUDE_PREFIXES):
        return False
    return path.endswith(".json") or path.endswith(".md")


def list_target_paths(token: str) -> list[str]:
    res = requests.get(
        f"{API_BASE}/git/trees/{BRANCH}",
        params={"recursive": "1"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    res.raise_for_status()
    tree = res.json()["tree"]
    return [item["path"] for item in tree if item["type"] == "blob" and _is_target_path(item["path"])]


def fetch_raw_content(path: str, token: str) -> str:
    res = requests.get(
        f"{API_BASE}/contents/{path}",
        params={"ref": BRANCH},
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.raw"},
        timeout=15,
    )
    res.raise_for_status()
    return res.text


def _to_knowledge_text(path: str, raw_content: str) -> str:
    """JSONは整形して保存（要約しない）。Markdownはそのまま保存する。"""
    if not path.endswith(".json"):
        return raw_content
    try:
        parsed = json.loads(raw_content)
    except json.JSONDecodeError:
        return raw_content
    return f"# {path}\n\n```json\n{json.dumps(parsed, ensure_ascii=False, indent=2)}\n```"


def ingest_all() -> dict[str, list[str]]:
    """data/*.json・docs/**/*.mdをSource/Knowledgeとして取り込む。
    既存ノートは内容が変わっていればcontentを更新する（新規ノード追加はしない）。
    新規・更新それぞれのKnowledge IDの一覧を返す。
    """
    token = get_github_token()
    created, updated = [], []

    for path in list_target_paths(token):
        blob_url = f"https://github.com/{OARAI_PROJECT_OS_REPO}/blob/{BRANCH}/{path}"
        raw_content = fetch_raw_content(path, token)
        title = path.rsplit("/", 1)[-1]
        text = _to_knowledge_text(path, raw_content)

        existing_source = find_source_by_url(blob_url)
        if existing_source is None:
            source_id = insert_node(
                node_type="Source", category=CATEGORY, title=title, content=raw_content, url=blob_url,
                meta={"repo_path": path},
            )
            knowledge_id = insert_node(
                node_type="Knowledge", category=CATEGORY, title=f"{title}（大洗Project OS）",
                content=text, url=blob_url,
            )
            insert_edge(knowledge_id, source_id, "derived_from")
            created.append(knowledge_id)
        elif existing_source["content"] != raw_content:
            update_node_content(existing_source["id"], raw_content)
            existing_knowledge = find_knowledge_by_url(blob_url)
            if existing_knowledge:
                update_node_content(existing_knowledge["id"], text)
                updated.append(existing_knowledge["id"])

    return {"created": created, "updated": updated}


if __name__ == "__main__":
    result = ingest_all()
    print(f"新規Knowledge: {len(result['created'])}件 / 更新: {len(result['updated'])}件")
