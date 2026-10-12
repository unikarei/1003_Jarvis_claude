"""個人のObsidianボルトへの書き込み（新規ノート作成・既存ノートへの追記）。

手動トリガーのみで使う（run_task.py等からの自動書き込みは行わない）。
2026-10 から、書き込み先を GitHub Contents API から Mac mini のローカル Vault に変更した
（../docs/decisions.md ADR-0016）。安全確認と原子的な書き込みは vault.py が行う。読み込み後に他端末等で
編集された場合や、対象ノートが未同期の場合はエラーにし、上書きしない。Git の同期は Obsidian Git が行う。
"""

from datetime import date
from pathlib import Path

from vault import create_file, modify_file, read_file

DEFAULT_VAULT = "vault-oarai-obsidian"


def create_note(path: str, content: str, message: str | None = None, vault: str = DEFAULT_VAULT) -> Path:
    """新規ノートを作成する。同名ファイルが既に存在する場合はエラーにする。

    message は GitHub API 時代の引数（コミットメッセージ）で、後方互換のために残している（使わない）。
    """
    try:
        return create_file(vault, path, content)
    except FileExistsError:
        raise FileExistsError(f"{path} は既に存在します。append_note()で追記するか、別のパスを指定してください。") from None


def append_note(path: str, addition: str, message: str | None = None, vault: str = DEFAULT_VAULT) -> Path:
    """既存ノートの末尾に追記する。追記部分には日付とJARVIS追記である旨を明記する。"""
    try:
        content, sha = read_file(vault, path)
    except FileNotFoundError:
        raise FileNotFoundError(f"{path} が見つかりません。create_note()で新規作成してください。") from None

    footer = f"\n\n---\n（JARVIS追記: {date.today().isoformat()}）\n{addition}\n"
    return modify_file(vault, path, content + footer, expected_digest=sha)


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 4 or sys.argv[1] not in ("create", "append"):
        print("使い方: python update_obsidian.py create|append <パス> <本文>")
        print('例: python update_obsidian.py append "民泊@大洗/01_物件/大洗_物件概要.md" "追記内容"')
        sys.exit(1)

    action, path, text = sys.argv[1], sys.argv[2], sys.argv[3]
    written = create_note(path, text) if action == "create" else append_note(path, text)
    print(f"更新しました: {written}")
