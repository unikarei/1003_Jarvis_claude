"""ローカルの Obsidian Vault への安全な書き込み（../docs/architecture.md §10.4、ADR-0016）。

- Vault は resource_id で指定し、パスは resolve_vault() だけが解決する（Architecture Registry の registry.json から）。
- 書き込み前に Git の状態（merge・rebase の途中、ロック）を確認し、一時ファイルから原子的に置き換える。
- 新規作成は既存ファイルを上書きしない。既存ファイルの変更は、未コミットの変更がなく、読み込み時から
  内容が変わっていない場合だけ、バックアップを取ってから行う。
- Git の commit・push・pull はしない（Mac mini では Obsidian Git が同期を担当する）。読み取りの status だけを使う。
"""

import hashlib
import os
import shutil
import subprocess
import tempfile
import unicodedata
from datetime import datetime
from pathlib import Path, PurePosixPath

import config
import registry

GIT_BUSY_MARKERS = ["MERGE_HEAD", "rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD", "REVERT_HEAD", "index.lock"]
PROTECTED_TOP_DIRS = {".git", ".obsidian", ".trash"}   # どの階層にあっても書かない・一覧に出さない（T-1702）
WRITABLE_SUFFIX = ".md"                                  # 作成・変更・削除は Markdown だけ（T-1702）


class VaultError(Exception):
    """Vault に書き込めない（Git の操作中、未コミットの変更、同時編集など）。何も書き込んでいない。"""


def resolve_vault(resource_id: str) -> Path:
    paths = registry.vault_paths()
    if resource_id not in paths:
        raise VaultError(f"未知の Vault です: {resource_id}（{', '.join(paths)}）")
    return paths[resource_id]


def digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _git_dir(vault: Path) -> Path:
    git = vault / ".git"
    if git.is_file():  # worktree・submodule の場合は "gitdir: <path>"
        git = (vault / git.read_text(encoding="utf-8").split(":", 1)[1].strip()).resolve()
    return git


def check_ready(vault: Path) -> None:
    """Vault が Git リポジトリで、Git の操作中でないことを確認する。"""
    if not vault.is_dir():
        raise VaultError(f"Vault が見つかりません: {vault}")
    git_dir = _git_dir(vault)
    if not git_dir.is_dir():
        raise VaultError(f"Vault が Git リポジトリではありません: {vault}")
    busy = [m for m in GIT_BUSY_MARKERS if (git_dir / m).exists()]
    if busy:
        raise VaultError(f"Vault で Git の操作中のため書き込みません（{', '.join(busy)}）: {vault}")


def _protected(parts) -> bool:
    """パスのどこかの階層が .git・.obsidian・.trash か。macOS の通常のファイルシステムは大文字・小文字を区別しない
    ため、各階層を NFC にしてから casefold して比べる（.OBSIDIAN も .obsidian の下を指す）。"""
    return any(unicodedata.normalize("NFC", p).casefold() in PROTECTED_TOP_DIRS for p in parts)


def target_path(vault: Path, relpath: str) -> Path:
    """Vault 内の相対パスを絶対パスにする。Vault の外や、どの階層でも .git・.obsidian・.trash の下を指すパス
    （記号のリンクで入るものを含む）は拒否する。"""
    rel = PurePosixPath(relpath)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts or _protected(rel.parts):
        raise VaultError(f"書き込めないパスです: {relpath}")
    path = (vault / rel).resolve()
    root = vault.resolve()
    if root not in path.parents:
        raise VaultError(f"Vault の外を指すパスです: {relpath}")
    if _protected(path.relative_to(root).parts):
        raise VaultError(f"書き込めないパスです: {relpath}")
    return path


def _writable_path(vault: Path, relpath: str) -> Path:
    """書き込み（作成・変更・削除）の対象。target_path の条件に加え、Markdown だけを許す。"""
    path = target_path(vault, relpath)
    if path.suffix.lower() != WRITABLE_SUFFIX or PurePosixPath(relpath).suffix.lower() != WRITABLE_SUFFIX:
        raise VaultError(f"Markdown（.md）以外には書き込みません: {relpath}")
    return path


def list_notes(resource_id: str) -> list[str]:
    """Vault の中の Markdown の相対パス（POSIX 形式・NFC・昇順）。.git・.obsidian・.trash の下（どの階層でも）と、
    記号のリンクで Vault の外・保護の場所を指すものは含めない。"""
    vault = resolve_vault(resource_id)
    root = vault.resolve()
    notes = []
    for path in vault.rglob("*.md"):
        rel = path.relative_to(vault)
        if _protected(rel.parts) or not path.is_file():
            continue
        real = path.resolve()
        if root not in real.parents or _protected(real.relative_to(root).parts):
            continue
        notes.append(unicodedata.normalize("NFC", rel.as_posix()))
    return sorted(notes)


def has_uncommitted_changes(vault: Path, path: Path) -> bool:
    """対象ファイルに未コミットの変更（未追跡を含む）があるか。ロックを取らない読み取りで確認する。"""
    out = subprocess.run(
        ["git", "--no-optional-locks", "status", "--porcelain", "--", str(path.relative_to(vault.resolve()))],
        cwd=vault, capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise VaultError(f"git status に失敗しました: {out.stderr.strip()[:200]}")
    return bool(out.stdout.strip())


def _write_temp(content: str) -> Path:
    tmp_dir = config.CKC_DATA_DIR / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=tmp_dir, suffix=".md")
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        f.write(content)
    os.chmod(name, 0o644)
    return Path(name)


def _numbered(path: Path, n: int) -> Path:
    return path if n == 1 else path.with_name(f"{path.stem}_{n}{path.suffix}")


def create_file(resource_id: str, relpath: str, content: str, number_if_exists: bool = False) -> Path:
    """新しいファイルを作る。既存ファイルは上書きしない（number_if_exists なら _2, _3 … を付ける）。"""
    vault = resolve_vault(resource_id)
    check_ready(vault)
    base = _writable_path(vault, relpath)
    base.parent.mkdir(parents=True, exist_ok=True)
    tmp = _write_temp(content)
    try:
        for n in range(1, 100 if number_if_exists else 2):
            path = _numbered(base, n)
            try:
                os.link(tmp, path)  # 同名があれば失敗する（作成と存在確認が原子的）
                return path
            except FileExistsError:
                continue
            except OSError as e:
                raise VaultError(f"ファイルを作成できません（一時ファイルと Vault が別のボリュームの可能性）: {e}") from None
        raise FileExistsError(f"既に存在します: {relpath}")
    finally:
        tmp.unlink(missing_ok=True)


def read_file(resource_id: str, relpath: str) -> tuple[str, str]:
    """ファイルの内容とハッシュを返す。変更するときは、このハッシュを modify_file() に渡す。"""
    path = target_path(resolve_vault(resource_id), relpath)
    if not path.is_file():
        raise FileNotFoundError(f"見つかりません: {relpath}")
    content = path.read_text(encoding="utf-8")
    return content, digest(content)


def _backup(resource_id: str, path: Path) -> Path:
    backup_dir = config.CKC_DATA_DIR / "backups" / resource_id
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{path.name}"
    shutil.copy2(path, backup)
    return backup


def modify_file(resource_id: str, relpath: str, new_content: str, expected_digest: str) -> Path:
    """既存ファイルを置き換える。未コミットの変更がある、または読み込み後に変わっていたら中止する。"""
    vault = resolve_vault(resource_id)
    check_ready(vault)
    path = _writable_path(vault, relpath)
    if not path.is_file():
        raise FileNotFoundError(f"見つかりません: {relpath}")
    if has_uncommitted_changes(vault, path):
        raise VaultError(f"{relpath} に未同期の変更があるため書き込みません。Obsidian Git の同期後に再実行してください。")
    if digest(path.read_text(encoding="utf-8")) != expected_digest:
        raise VaultError(f"{relpath} は読み込み後に変更されています。読み込み直してから再実行してください。")
    _backup(resource_id, path)
    tmp = _write_temp(new_content)
    try:
        check_ready(vault)
        if digest(path.read_text(encoding="utf-8")) != expected_digest:  # 置き換え直前にもう一度確認
            raise VaultError(f"{relpath} は読み込み後に変更されています。読み込み直してから再実行してください。")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


def delete_file(resource_id: str, relpath: str, expected_digest: str) -> Path:
    """既存のファイルを削除する（T-1702）。modify_file と同じ確認（Git の状態・パス・未同期の変更・読み込み後の変更、
    削除の直前にもう一度）をし、変更前のファイルを残してから消す。バックアップのパスを返す。"""
    vault = resolve_vault(resource_id)
    check_ready(vault)
    path = _writable_path(vault, relpath)
    if not path.is_file():
        raise FileNotFoundError(f"見つかりません: {relpath}")
    if has_uncommitted_changes(vault, path):
        raise VaultError(f"{relpath} に未同期の変更があるため削除しません。Obsidian Git の同期後に再実行してください。")
    if digest(path.read_text(encoding="utf-8")) != expected_digest:
        raise VaultError(f"{relpath} は読み込み後に変更されています。読み込み直してから再実行してください。")
    backup = _backup(resource_id, path)
    check_ready(vault)
    if digest(path.read_text(encoding="utf-8")) != expected_digest:  # 削除の直前にもう一度確認
        raise VaultError(f"{relpath} は読み込み後に変更されています。読み込み直してから再実行してください。")
    path.unlink()
    return backup


def restore_file(resource_id: str, relpath: str, content: str, expected_digest: str) -> Path:
    """書いた直後のファイルを、書く前の内容に戻す（T-1702。書いた後の DB の更新に失敗したとき）。
    今の内容が expected_digest（自分が書いた内容）のときだけ置き換える。書いた直後なので未同期の変更は確かめない
    （自分の書き込みが未同期の変更になっているため）。ほかの端末で変わっていたら VaultError で戻さない。"""
    vault = resolve_vault(resource_id)
    check_ready(vault)
    path = _writable_path(vault, relpath)
    if not path.is_file() or digest(path.read_text(encoding="utf-8")) != expected_digest:
        raise VaultError(f"{relpath} は書いた後に変更されたため、元に戻していません。")
    tmp = _write_temp(content)
    try:
        if digest(path.read_text(encoding="utf-8")) != expected_digest:
            raise VaultError(f"{relpath} は書いた後に変更されたため、元に戻していません。")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path
