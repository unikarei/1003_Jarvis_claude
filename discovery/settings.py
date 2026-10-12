"""Discovery の設定。ワークスペースの外の既知の資産は「対象外」として扱う（../docs/spec.md §2.2）。"""

from pathlib import Path

import config

REGISTRY_DIR = Path(config.REGISTRY_JSON).resolve().parent.parent
WORKSPACE_DIR = config.BASE_DIR.parent if (config.BASE_DIR.parent / "AGENTS.md").exists() \
    else Path.home() / "work" / "0002_Jarvis2"
VAULT_PARENT = Path.home() / "work" / "obsidian"
OBSIDIAN_CONFIG = Path.home() / "Library" / "Application Support" / "obsidian" / "obsidian.json"
LAUNCHD_PREFIX = "com.ohide."
HTTP_TIMEOUT_SECONDS = 10

# ワークスペースの外の資産（登録しない。件数だけ記録する）
OUT_OF_SCOPE_LAUNCHD = {"com.ohide.ollama-server", "com.ohide.multiapp-launcher"}
OUT_OF_SCOPE_DOCKER_PREFIXES = ("pocs0108_", "database-youtube")  # PoCs0108（ワークスペース外）のコンテナ
IGNORED_WORKSPACE_DIRS = {".worktrees", "docs", "scripts", ".git"}
