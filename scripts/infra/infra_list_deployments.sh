#!/usr/bin/env bash
# Usage: infra_list_deployments.sh <context> [namespace]
set -euo pipefail

CONTEXT="${1:?Usage: infra_list_deployments.sh <context> [namespace]}"
NAMESPACE="${2:-}"

case "$CONTEXT" in
    acme-prod) KUBE_CTX="aws-prod" ;;
    acme-test) KUBE_CTX="aws-test" ;;
    *) echo "Unknown context: $CONTEXT" >&2; exit 1 ;;
esac

if [ -n "$NAMESPACE" ] && ! [[ "$NAMESPACE" =~ ^[a-zA-Z0-9_.-]+$ ]]; then
    echo "Invalid namespace: $NAMESPACE" >&2
    exit 1
fi

ARGS=(--context "$KUBE_CTX" get deployments -o json)
if [ -n "$NAMESPACE" ]; then
    ARGS+=(-n "$NAMESPACE")
else
    ARGS+=(-A)
fi

OUTPUT=$(kubectl "${ARGS[@]}" 2>&1) || {
    echo "$OUTPUT" >&2
    exit 1
}

echo "$OUTPUT" | jq -c '[.items[] | {
    name: .metadata.name,
    namespace: .metadata.namespace,
    replicas_desired: .spec.replicas,
    replicas_available: (.status.availableReplicas // 0),
    replicas_ready: (.status.readyReplicas // 0),
    replicas_updated: (.status.updatedReplicas // 0),
    image: .spec.template.spec.containers[0].image,
    age: .metadata.creationTimestamp
}]'
