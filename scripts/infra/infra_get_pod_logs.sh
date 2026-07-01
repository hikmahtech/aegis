#!/usr/bin/env bash
# Usage: infra_get_pod_logs.sh <context> <namespace> <pod_name> [tail] [container]
set -euo pipefail

CONTEXT="${1:?Usage: infra_get_pod_logs.sh <context> <namespace> <pod_name> [tail] [container]}"
NAMESPACE="${2:?namespace required}"
POD="${3:?pod_name required}"
TAIL="${4:-50}"
CONTAINER="${5:-}"

case "$CONTEXT" in
    acme-prod) KUBE_CTX="aws-prod" ;;
    acme-test) KUBE_CTX="aws-test" ;;
    *) echo "Unknown context: $CONTEXT" >&2; exit 1 ;;
esac

for val in "$NAMESPACE" "$POD" "$CONTAINER"; do
    if [ -n "$val" ] && ! [[ "$val" =~ ^[a-zA-Z0-9_.-]+$ ]]; then
        echo "Invalid argument: $val" >&2
        exit 1
    fi
done
if ! [[ "$TAIL" =~ ^[0-9]+$ ]]; then
    echo "Invalid tail: $TAIL" >&2
    exit 1
fi

ARGS=(--context "$KUBE_CTX" -n "$NAMESPACE" logs "$POD" --tail="$TAIL" --timestamps)
if [ -n "$CONTAINER" ]; then
    ARGS+=(-c "$CONTAINER")
fi

LOGS=$(kubectl "${ARGS[@]}" 2>&1 || true)
jq -n \
    --arg namespace "$NAMESPACE" \
    --arg pod "$POD" \
    --arg container "$CONTAINER" \
    --arg tail "$TAIL" \
    --arg logs "$LOGS" \
    '{namespace: $namespace, pod: $pod, container: $container, tail: ($tail | tonumber), logs: $logs}'
