"""保存先 Vault の振り分け（../docs/spec.md §3.3.8）。

大洗だけに関わる知識は oarai-obsidian、それ以外（空・ほかのプロジェクト・複数）は jarvis-knowledge。
複数のプロジェクトにまたがる知識も jarvis-knowledge に1つだけ保存する（正本は1つ）。
"""

import config

OARAI_PROJECT = "oarai-minpaku"


def route_vault(projects: list[str]) -> str:
    if projects == [OARAI_PROJECT]:
        return config.PROJECTS[OARAI_PROJECT][0]
    return config.DEFAULT_VAULT


def default_category(projects: list[str]) -> str | None:
    """プロジェクトが1つなら、その 1003 のカテゴリを返す。"""
    if len(projects) == 1 and projects[0] in config.PROJECTS:
        return config.PROJECTS[projects[0]][1]
    return None
