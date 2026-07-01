#!/usr/bin/env bash
# Cleans up git worktrees whose branches have merged/closed PRs.
# Usage: ./scripts/cleanup-worktrees.sh [--dry-run]

set -euo pipefail

DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

REPO_ROOT="$(git -C "$(dirname "$0")/.." rev-parse --show-toplevel)"
cd "$REPO_ROOT"

cleaned=0
skipped=0

while IFS= read -r line; do
    wt_path="${line#worktree }"

    # Skip the main worktree
    [[ "$wt_path" == "$REPO_ROOT" ]] && continue

    branch="$(git -C "$wt_path" branch --show-current 2>/dev/null || true)"
    [[ -z "$branch" ]] && continue

    # Check if the branch has a PR and its state
    pr_state="$(gh pr view "$branch" --json state --jq '.state' 2>/dev/null || echo "NONE")"

    case "$pr_state" in
        MERGED|CLOSED)
            if $DRY_RUN; then
                echo "[dry-run] Would remove: $wt_path (branch: $branch, PR: $pr_state)"
            else
                echo "Removing worktree: $wt_path (branch: $branch, PR: $pr_state)"
                git worktree remove "$wt_path" --force 2>/dev/null || rm -rf "$wt_path"
                git branch -D "$branch" 2>/dev/null || true
            fi
            cleaned=$((cleaned + 1))
            ;;
        *)
            echo "Keeping: $wt_path (branch: $branch, PR: $pr_state)"
            skipped=$((skipped + 1))
            ;;
    esac
done < <(git worktree list --porcelain | grep "^worktree ")

echo ""
echo "Done. Cleaned: $cleaned, Kept: $skipped"