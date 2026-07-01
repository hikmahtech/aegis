#!/usr/bin/env bash
# Usage: infra_restart_service.sh <context> <service_name>
# Force-update (rolling restart) a Docker Swarm service.
set -euo pipefail

CONTEXT="${1:?Usage: infra_restart_service.sh <context> <service_name>}"
SERVICE="${2:?service_name required}"

if ! [[ "$SERVICE" =~ ^[a-zA-Z0-9_.-]+$ ]]; then
    echo "Invalid service name: $SERVICE" >&2
    exit 1
fi

case "$CONTEXT" in
    swarm) DOCKER_CTX="swarm" ;;
    *) echo "Unknown context: $CONTEXT" >&2; exit 1 ;;
esac

OUTPUT=$(docker --context "$DOCKER_CTX" service update --force "$SERVICE" 2>&1) || {
    echo "$OUTPUT" >&2
    exit 1
}
jq -n --arg service "$SERVICE" --arg output "$OUTPUT" '{
    result: "restarted",
    service: $service,
    output: $output
}'
