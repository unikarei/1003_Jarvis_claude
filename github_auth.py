"""GitHub API認証の共通ヘルパー。ghコマンドの認証情報を使う（トークンは本リポジトリに保存しない）。"""

import subprocess


def get_github_token() -> str:
    result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True)
    return result.stdout.strip()
