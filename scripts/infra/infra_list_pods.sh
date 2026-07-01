#!/usr/bin/env bash
# Usage: infra_list_pods.sh <context> [namespace] [status_filter]
set -euo pipefail

CONTEXT="${1:?Usage: infra_list_pods.sh <context> [namespace] [status_filter]}"
NAMESPACE="${2:-}"
STATUS_FILTER="${3:-}"

case "$CONTEXT" in
    acme-prod) KUBE_CTX="aws-prod" ;;
    acme-test) KUBE_CTX="aws-test" ;;
    *) echo "Unknown context: $CONTEXT" >&2; exit 1 ;;
esac

# Validate
for val in "$NAMESPACE" "$STATUS_FILTER"; do
    if [ -n "$val" ] && ! [[ "$val" =~ ^[a-zA-Z0-9_.-]+$ ]]; then
        echo "Invalid argument: $val" >&2
        exit 1
    fi
done

ARGS=(--context "$KUBE_CTX" get pods -o json)
if [ -n "$NAMESPACE" ]; then
    ARGS+=(-n "$NAMESPACE")
else
    ARGS+=(-A)
fi

OUTPUT=$(kubectl "${ARGS[@]}" 2>&1) || {
    echo "$OUTPUT" >&2
    exit 1
}

JQ_FILTER='[.items[] | {
    name: .metadata.name,
    namespace: .metadata.namespace,
    phase: .status.phase,
    ready: ((([.status.containerStatuses[]? | select(.ready)] | length) | tostring) + "/" + (([.status.containerStatuses[]?] | length) | tostring)),
    restarts: ([.status.containerStatuses[]?.restartCount] | add // 0),
    waiting_reason: ([.status.containerStatuses[]?.state.waiting.reason] | map(select(. != null)) | .[0] // null),
    age: .metadata.creationTimestamp,
    node: .spec.nodeName
}]'

if [ -n "$STATUS_FILTER" ]; then
    JQ_FILTER="$JQ_FILTER | map(select(.phase == \"$STATUS_FILTER\" or .waiting_reason == \"$STATUS_FILTER\"))"
fi

echo "$OUTPUT" | jq -c "$JQ_FILTER"
