#!/usr/bin/env bash
# Usage: infra_sync_argocd_app.sh <context> <app_name>
# Acme uses ONE ArgoCD instance at argo-cd.stocko-infra.net:443 that
# manages both clusters. The app_name should include its cluster suffix
# (e.g. "core-api-prod" or "core-api-test") as that is how ArgoCD names them.
# The <context> is validated against the app's destination cluster to prevent
# accidental cross-environment syncs.
set -euo pipefail

CONTEXT="${1:?Usage: infra_sync_argocd_app.sh <context> <app_name>}"
APP="${2:?app_name required}"

ARGOCD_SERVER="argo-cd.stocko-infra.net:443"

if ! [[ "$APP" =~ ^[a-zA-Z0-9_.-]+$ ]]; then
    echo "Invalid app name: $APP" >&2
    exit 1
fi

case "$CONTEXT" in
    acme-prod) EXPECTED_CLUSTER="prod" ;;
    acme-test) EXPECTED_CLUSTER="test" ;;
    *) echo "Unknown context: $CONTEXT" >&2; exit 1 ;;
esac

# Verify the app really targets the expected cluster before syncing.
ACTUAL_CLUSTER=$(argocd app get "$APP" --server "$ARGOCD_SERVER" --grpc-web -o json 2>&1 \
    | jq -r '.spec.destination.name // empty') || {
    echo "Failed to fetch app $APP from ArgoCD" >&2
    exit 1
}
if [ -z "$ACTUAL_CLUSTER" ]; then
    echo "App '$APP' not found or missing destination.name" >&2
    exit 1
fi
if [ "$ACTUAL_CLUSTER" != "$EXPECTED_CLUSTER" ]; then
    echo "Context mismatch: $APP targets cluster '$ACTUAL_CLUSTER', but context is '$CONTEXT' (expected '$EXPECTED_CLUSTER')" >&2
    exit 1
fi

OUTPUT=$(argocd app sync "$APP" --server "$ARGOCD_SERVER" --grpc-web --timeout 90 2>&1) || {
    echo "$OUTPUT" >&2
    exit 1
}

jq -n --arg app "$APP" --arg cluster "$ACTUAL_CLUSTER" --arg output "$OUTPUT" '{
    result: "sync_triggered",
    app: $app,
    cluster: $cluster,
    output: $output
}'
