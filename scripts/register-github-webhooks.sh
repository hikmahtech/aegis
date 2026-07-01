#!/usr/bin/env bash
# scripts/register-github-webhooks.sh
# Register the AEGIS webhook on each repo passed as an argument. Idempotent —
# skips repos that already have a hook pointing at the AEGIS URL.
#
# Usage:
#   AEGIS_GITHUB_WEBHOOK_SECRET=<secret> ./scripts/register-github-webhooks.sh \
#       youruser/aegis youruser/cmemory ...

set -euo pipefail

AEGIS_URL="${AEGIS_URL:-https://aegis-api.example.com/api/webhooks/github}"
SECRET="${AEGIS_GITHUB_WEBHOOK_SECRET:?must be set — see group_vars/all.yml::aegis_github_webhook_secret}"

if [ "$#" -eq 0 ]; then
    echo "Usage: $0 <owner/repo> [<owner/repo> ...]" >&2
    exit 2
fi

for repo in "$@"; do
    echo "→ $repo"
    existing=$(gh api "repos/$repo/hooks" --jq \
        ".[] | select(.config.url==\"$AEGIS_URL\") | .id" 2>/dev/null || true)
    if [ -n "$existing" ]; then
        echo "  already registered (hook id $existing)"
        continue
    fi
    gh api "repos/$repo/hooks" -X POST \
        -f name=web \
        -f config[url]="$AEGIS_URL" \
        -f config[content_type]=json \
        -f config[secret]="$SECRET" \
        -f config[insecure_ssl]=0 \
        -f events[]=workflow_run \
        -f events[]=push \
        -f events[]=pull_request \
        -f events[]=release \
        -f events[]=issues \
        -F active=true \
        >/dev/null
    echo "  registered"
done
