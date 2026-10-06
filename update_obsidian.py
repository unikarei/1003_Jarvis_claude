"""個人のObsidianボルト（oarai-obsidian）への書き込み（新規ノート作成・既存ノートへの追記）。

手動トリガーのみで使う（run_task.py等からの自動書き込みは行わない）。
GitHub Contents APIのsha突き合わせにより、取得後に他端末等で編集された場合は
409エラーとなり上書きしない（ボルトのREADMEにある「編集前に取得、編集後に保存」の運用方針に準拠）。
"""

import base64
from datetime import date

import requests

from github_auth import get_github_token
from ingest_obsidian import API_BASE, BRANCH

CONFLICT_MESSAGE = (
    "{path} は取得後に他端末等で更新されており、競合が発生しました。"
    "ingest_obsidian.pyで最新内容を取得してから再実行してください。"
)


def get_note(path: str, token: str) -> dict | None:
    """ノートの現在のcontentとshaを取得する。存在しない場合はNoneを返す。"""
    res = requests.get(
        f"{API_BASE}/contents/{path}",
        params={"ref": BRANCH},
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    if res.status_code == 404:
        return None
    res.raise_for_status()
    data = res.json()
    return {"content": base64.b64decode(data["content"]).decode("utf-8"), "sha": data["sha"]}


def _put_note(path: str, content: str, message: str, sha: str | None, token: str) -> str:
    body = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": BRANCH,
    }
    if sha:
        body["sha"] = sha

    res = requests.put(
        f"{API_BASE}/contents/{path}",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    if res.status_code == 409:
        raise RuntimeError(CONFLICT_MESSAGE.format(path=path))
    res.raise_for_status()
    return res.json()["content"]["html_url"]


def create_note(path: str, content: str, message: str | None = None) -> str:
    """新規ノートを作成する。同名ファイルが既に存在する場合はエラーにする。"""
    token = get_github_token()
    if get_note(path, token) is not None:
        raise FileExistsError(f"{path} は既に存在します。append_note()で追記するか、別のパスを指定してください。")
    return _put_note(path, content, message or f"JARVIS: {path} を新規作成", sha=None, token=token)


def append_note(path: str, addition: str, message: str | None = None) -> str:
    """既存ノートの末尾に追記する。追記部分には日付とJARVIS追記である旨を明記する。"""
    token = get_github_token()
    note = get_note(path, token)
    if note is None:
        raise FileNotFoundError(f"{path} が見つかりません。create_note()で新規作成してください。")

    footer = f"\n\n---\n（JARVIS追記: {date.today().isoformat()}）\n{addition}\n"
    return _put_note(path, note["content"] + footer, message or f"JARVIS: {path} に追記", sha=note["sha"], token=token)


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 4 or sys.argv[1] not in ("create", "append"):
        print("使い方: python update_obsidian.py create|append <パス> <本文>")
        print('例: python update_obsidian.py append "民泊@大洗/01_物件/大洗_物件概要.md" "追記内容"')
        sys.exit(1)

    action, path, text = sys.argv[1], sys.argv[2], sys.argv[3]
    url = create_note(path, text) if action == "create" else append_note(path, text)
    print(f"更新しました: {url}")
