#!/usr/bin/env bash
# Usage: infra_list_argocd_apps.sh <context> [filter]
# Acme uses ONE ArgoCD instance at argo-cd.stocko-infra.net:443 that
# manages both clusters. Apps are scoped via destination.name ("prod" / "test").
# Requires: argocd CLI logged in on node-a (one-time:
#   argocd login argo-cd.stocko-infra.net --sso --grpc-web)
set -euo pipefail

CONTEXT="${1:?Usage: infra_list_argocd_apps.sh <context> [filter]}"
FILTER="${2:-}"

ARGOCD_SERVER="argo-cd.stocko-infra.net:443"

case "$CONTEXT" in
    acme-prod) DEST_CLUSTER="prod" ;;
    acme-test) DEST_CLUSTER="test" ;;
    *) echo "Unknown context: $CONTEXT" >&2; exit 1 ;;
esac

if [ -n "$FILTER" ] && ! [[ "$FILTER" =~ ^[a-zA-Z0-9_.-]+$ ]]; then
    echo "Invalid filter: $FILTER" >&2
    exit 1
fi

OUTPUT=$(argocd app list --server "$ARGOCD_SERVER" --grpc-web -o json 2>&1) || {
    echo "$OUTPUT" >&2
    exit 1
}

# Filter to the selected cluster, then project to a compact shape.
JQ_FILTER=$(cat <<JQ
[.[] | select(.spec.destination.name == "$DEST_CLUSTER") | {
    name: .metadata.name,
    project: .spec.project,
    namespace: .spec.destination.namespace,
    cluster: .spec.destination.name,
    sync_status: .status.sync.status,
    health_status: .status.health.status,
    revision: (.status.sync.revision // "")[0:8],
    repo_url: .spec.source.repoURL,
    target_revision: .spec.source.targetRevision
}]
JQ
)

case "$FILTER" in
    degraded)    JQ_FILTER="$JQ_FILTER | map(select(.health_status == \"Degraded\"))" ;;
    outofsync)   JQ_FILTER="$JQ_FILTER | map(select(.sync_status == \"OutOfSync\"))" ;;
    synced)      JQ_FILTER="$JQ_FILTER | map(select(.sync_status == \"Synced\"))" ;;
    healthy)     JQ_FILTER="$JQ_FILTER | map(select(.health_status == \"Healthy\"))" ;;
    "")          ;;
    *)           JQ_FILTER="$JQ_FILTER | map(select(.sync_status == \"$FILTER\" or .health_status == \"$FILTER\"))" ;;
esac

echo "$OUTPUT" | jq -c "$JQ_FILTER"
