#!/usr/bin/env bash
# Usage: infra_get_service_logs.sh <context> <service_name> [tail]
set -euo pipefail

CONTEXT="${1:?Usage: infra_get_service_logs.sh <context> <service_name> [tail]}"
SERVICE="${2:?service_name required}"
TAIL="${3:-50}"

if ! [[ "$SERVICE" =~ ^[a-zA-Z0-9_.-]+$ ]]; then
    echo "Invalid service name: $SERVICE" >&2
    exit 1
fi
if ! [[ "$TAIL" =~ ^[0-9]+$ ]]; then
    echo "Invalid tail: $TAIL" >&2
    exit 1
fi

case "$CONTEXT" in
    swarm) DOCKER_CTX="swarm" ;;
    *) echo "Unknown context: $CONTEXT" >&2; exit 1 ;;
esac

# Logs are not JSON — return as text wrapped in a JSON object for consistency
LOGS=$(docker --context "$DOCKER_CTX" service logs --tail "$TAIL" --timestamps "$SERVICE" 2>&1 || true)
jq -n --arg logs "$LOGS" --arg service "$SERVICE" --arg tail "$TAIL" '{
    service: $service,
    tail: ($tail | tonumber),
    logs: $logs
}'
