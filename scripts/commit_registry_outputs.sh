#!/bin/bash
# Discovery / Guardian の出力を Registry リポジトリにコミットし、origin main に push する（../docs/architecture.md §11.3）。
#
# 実行: REGISTRY_REPO=<Registry のパス> scripts/commit_registry_outputs.sh（discover.py --commit から呼ばれる）
#
# - 下の4ファイルだけをコミットする（ADR-0013: Jarvis が承認なしに書ける出力先）。ほかの変更はステージしない。
# - main 以外のブランチ、merge・rebase の途中、ほかのファイルがステージ済み、リモートと fast-forward できない
#   場合は何もせず中止する。強制 push はしない（1004 の commit_reports.sh と同じ方式）。

set -uo pipefail

REPO="${REGISTRY_REPO:-$(cd "$(dirname "$0")/../.." && pwd)/jarvis-architecture-registry}"
REMOTE="${REGISTRY_REMOTE:-origin}"
BRANCH="main"
TARGETS=(discovered/latest.json generated/guardian-report.md generated/architecture.mmd generated/STATUS.md)

log() { echo "[$(date '+%Y-%m-%dT%H:%M:%S')] $*"; }
abort() { log "ABORT: $*"; exit 1; }

cd "$REPO" || abort "repository not found: $REPO"

current=$(git symbolic-ref --short -q HEAD || true)
[ "$current" = "$BRANCH" ] || abort "current branch is '${current:-detached HEAD}', not $BRANCH"

git_dir=$(git rev-parse --git-dir)
for marker in MERGE_HEAD rebase-merge rebase-apply CHERRY_PICK_HEAD; do
  [ -e "$git_dir/$marker" ] && abort "$marker exists (merge/rebase/cherry-pick in progress)"
done

exclude=()
for t in "${TARGETS[@]}"; do exclude+=(":(exclude)$t"); done
other_staged=$(git diff --cached --name-only -- . "${exclude[@]}")
[ -z "$other_staged" ] || abort "other files are already staged: $(echo "$other_staged" | tr '\n' ' ')"

git fetch -q "$REMOTE" "$BRANCH" || abort "git fetch $REMOTE $BRANCH failed"
upstream="$REMOTE/$BRANCH"
if ! git merge-base --is-ancestor "$upstream" HEAD; then
  git merge-base --is-ancestor HEAD "$upstream" || abort "local $BRANCH and $upstream have diverged"
  git merge -q --ff-only "$upstream" || abort "fast-forward to $upstream failed"
  log "fast-forwarded to $upstream ($(git rev-parse --short HEAD))"
fi

existing=()
for path in "${TARGETS[@]}"; do
  [ -e "$path" ] && existing+=("$path")
done
[ ${#existing[@]} -eq 0 ] || git add -- "${existing[@]}" || abort "git add failed"

if git diff --cached --quiet; then
  if [ "$(git rev-list --count "$upstream"..HEAD)" -eq 0 ]; then
    log "no changes in discovery outputs; nothing to commit"
    exit 0
  fi
  log "no new changes, but local $BRANCH is ahead of $upstream; pushing"
else
  git commit -q -m "discovery: $(date '+%Y-%m-%d %H:%M') の Registry と実態の差分" || abort "git commit failed"
  log "committed $(git rev-parse --short HEAD)"
fi

git push -q "$REMOTE" "$BRANCH" || abort "git push $REMOTE $BRANCH failed"
log "pushed to $upstream"
