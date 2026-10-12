"""Architecture Registry（第0層）の読み込み。Vault の場所とプロジェクトの正本は Registry にある（ADR-0019）。

Registry の generated/registry.json（../jarvis-architecture-registry/ARCHITECTURE.md §3.3）を標準ライブラリで読む。
読めないときは config の予備の値で動き続け、警告を1回だけ出す。
"""

import json
import os
import warnings
from pathlib import Path

import config

_cache: dict | None = None
_loaded = False


def reset_cache() -> None:
    global _cache, _loaded
    _cache, _loaded = None, False


def load() -> dict | None:
    """registry.json を読む。読めなければ None（警告を1回だけ出す）。"""
    global _cache, _loaded
    if not _loaded:
        _loaded = True
        try:
            data = json.loads(Path(config.REGISTRY_JSON).read_text(encoding="utf-8"))
            if not isinstance(data.get("projects"), dict) or not isinstance(data.get("resources"), dict):
                raise ValueError("projects / resources がありません")
            _cache = data
        except (OSError, ValueError) as e:
            warnings.warn(f"Architecture Registry を読めないため、予備の設定値で動きます（{config.REGISTRY_JSON}: {e}）",
                          stacklevel=2)
            _cache = None
    return _cache


def _locator_for_host(locator) -> str | None:
    if isinstance(locator, dict):
        value = locator.get(config.HOST_ID)
        return None if value in (None, "unknown") else value
    return locator


def vault_paths() -> dict[str, Path]:
    """Vault の resource_id → この端末でのパス。

    JARVIS_VAULT_ROOT が設定されているとき（開発・テスト）は、Vault のフォルダ名だけを使い VAULT_ROOT の下に置く。
    """
    data = load()
    if data is None:
        names = dict(config.FALLBACK_VAULTS)
        return {rid: config.VAULT_ROOT / name for rid, name in names.items()}
    paths = {}
    for rid, res in data["resources"].items():
        if res.get("resource_type") != "vault":
            continue
        locator = _locator_for_host(res.get("locator"))
        if not locator:
            continue
        path = Path(os.path.expanduser(locator))
        paths[rid] = config.VAULT_ROOT / path.name if "JARVIS_VAULT_ROOT" in os.environ else path
    return paths


def projects() -> dict[str, tuple[str, str | None]]:
    """project_id → (知識の保存先 Vault の resource_id, 1003 のカテゴリ)。"""
    data = load()
    if data is None:
        return dict(config.FALLBACK_PROJECTS)
    return {pid: (p.get("default_vault") or config.DEFAULT_VAULT, p.get("jarvis_category"))
            for pid, p in data["projects"].items()}
