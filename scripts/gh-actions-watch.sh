#!/usr/bin/env bash
# Usage: watch -n10 -c scripts/gh-actions-watch.sh
# Shows recent GitHub Actions runs with color-coded status

set -euo pipefail

LIMIT=${1:-15}

# ANSI colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
GRAY='\033[0;90m'
BOLD='\033[1m'
RESET='\033[0m'

colorize_status() {
  local status="$1" conclusion="$2"
  if [[ "$status" == "completed" ]]; then
    case "$conclusion" in
      success)    printf "${GREEN}%-10s${RESET}" "success" ;;
      failure)    printf "${RED}%-10s${RESET}" "failure" ;;
      cancelled)  printf "${GRAY}%-10s${RESET}" "cancelled" ;;
      *)          printf "${YELLOW}%-10s${RESET}" "$conclusion" ;;
    esac
  elif [[ "$status" == "in_progress" ]]; then
    printf "${BLUE}%-10s${RESET}" "running"
  elif [[ "$status" == "queued" || "$status" == "waiting" ]]; then
    printf "${YELLOW}%-10s${RESET}" "$status"
  else
    printf "%-10s" "$status"
  fi
}

printf "${BOLD}%-10s %-38s %-30s %-10s %s${RESET}\n" \
  "STATUS" "WORKFLOW" "COMMIT" "DURATION" "STARTED"
printf '%0.s─' {1..100}; echo

gh run list --limit "$LIMIT" \
  --json status,conclusion,name,displayTitle,createdAt,updatedAt \
  --jq $'.[] | [.status, (.conclusion // ""), .name, .displayTitle, .createdAt, .updatedAt] | join("\\u001f")' |
while IFS=$'\x1f' read -r status conclusion workflow commit created updated; do
  # Calculate duration
  start_epoch=$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "$created" +%s 2>/dev/null || echo 0)
  if [[ "$status" == "completed" ]]; then
    end_epoch=$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "$updated" +%s 2>/dev/null || echo 0)
  else
    end_epoch=$(date +%s)
  fi
  elapsed=$(( end_epoch - start_epoch ))
  mins=$(( elapsed / 60 ))
  secs=$(( elapsed % 60 ))
  duration="${mins}m${secs}s"

  # Truncate long strings
  workflow="${workflow:0:36}"
  commit="${commit:0:28}"

  # Relative time
  now=$(date +%s)
  ago=$(( now - start_epoch ))
  if (( ago < 60 )); then
    age="${ago}s ago"
  elif (( ago < 3600 )); then
    age="$(( ago / 60 ))m ago"
  elif (( ago < 86400 )); then
    age="$(( ago / 3600 ))h ago"
  else
    age="$(( ago / 86400 ))d ago"
  fi

  colorize_status "$status" "$conclusion"
  printf " %-38s %-30s %-10s %s\n" "$workflow" "$commit" "$duration" "$age"
done
