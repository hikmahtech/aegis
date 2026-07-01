#!/usr/bin/env bash
# run_claude.sh — AEGIS remote Claude Code runner
#
# Usage: run_claude.sh <run_id> <core_url> <api_key> <repo_path> <work_branch> <base_branch>
#
# Pre-conditions:
#   - Claude Code CLI ('claude') is installed and on PATH
#   - /tmp/aegis-claude/<run_id>/prompt.txt contains the task prompt
#   - Repo is already cloned at <repo_path>
#   - Script updates Core API at <core_url>/api/admin/claude-runs/update on completion

set -euo pipefail

RUN_ID="$1"
CORE_URL="$2"
API_KEY="$3"
REPO_PATH="$4"
WORK_BRANCH="$5"
BASE_BRANCH="$6"

RUN_DIR="/tmp/aegis-claude/${RUN_ID}"
PROMPT_FILE="${RUN_DIR}/prompt.txt"
STREAM_FILE="${RUN_DIR}/stream.jsonl"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Write our PID for potential cancel_run
echo $$ > "${RUN_DIR}/pid"

_update_core() {
    local payload="$1"
    curl -s -o /dev/null -X POST "${CORE_URL}/api/admin/claude-runs/update" \
        -H "X-API-Key: ${API_KEY}" \
        -H "Content-Type: application/json" \
        -d "${payload}" || true
}

# Trap unexpected exits (e.g. cd failure, git errors) so the run record is
# never left stuck at "running". The trap fires on any ERR under set -e.
_on_error() {
    local exit_code=$?
    _update_core "{\"run_id\":\"${RUN_ID}\",\"status\":\"failed\",\"error_type\":\"script_error\",\"error_message\":\"run_claude.sh exited unexpectedly (code ${exit_code})\"}"
}
trap '_on_error' ERR

# Navigate to repo and prepare branch
cd "${REPO_PATH}"
git fetch origin 2>/dev/null || true
git checkout -B "${WORK_BRANCH}" "origin/${BASE_BRANCH}" 2>/dev/null \
    || git checkout "${WORK_BRANCH}" 2>/dev/null \
    || git checkout -b "${WORK_BRANCH}"

# Run Claude Code CLI with stream-json for observability
claude_exit=0
claude --allowedTools Edit,Bash --output-format stream-json --verbose --print "$(cat "${PROMPT_FILE}")" \
    > "${STREAM_FILE}" 2>&1 || claude_exit=$?

# Parse result fields (best-effort — failures produce empty strings)
BRANCH_NAME=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
COMMIT_SHA=$(git rev-parse HEAD 2>/dev/null || echo "")

# Try to extract pr_url from Claude output (look for GitHub PR URL pattern)
PR_URL=$(grep -oP 'https://github\.com/[^/]+/[^/]+/pull/[0-9]+' "${STREAM_FILE}" 2>/dev/null | head -1 || echo "")

# Determine final status
if [ "${claude_exit}" -eq 0 ]; then
    STATUS="succeeded"
    ERROR_TYPE=""
    ERROR_MSG=""
else
    STATUS="failed"
    ERROR_TYPE="nonzero_exit"
    ERROR_MSG="Claude Code exited with code ${claude_exit}"
fi

# Compose JSON payload (escape single quotes in branch names)
PAYLOAD=$(printf '{"run_id":"%s","status":"%s","branch_name":"%s","commit_sha":"%s","pr_url":"%s","error_type":"%s","error_message":"%s"}' \
    "${RUN_ID}" "${STATUS}" "${BRANCH_NAME}" "${COMMIT_SHA}" "${PR_URL}" "${ERROR_TYPE}" "${ERROR_MSG}")

_update_core "${PAYLOAD}"

# Parse stream output and POST metrics + conversation events (best-effort)
# Pass STATUS so the parser never overrides the status set by _update_core
python3 "${SCRIPT_DIR}/parse_claude_stream.py" \
    "${STREAM_FILE}" "${RUN_ID}" "${CORE_URL}" "${API_KEY}" "${STATUS}" || true

exit "${claude_exit}"
