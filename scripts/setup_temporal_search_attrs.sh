#!/usr/bin/env bash
# Sprint 25b: Register custom search attributes with Temporal namespace.
# One-time setup — run after Temporal server is up (dev or production).
#
# Usage:
#   bash scripts/setup_temporal_search_attrs.sh
#   TEMPORAL_ADDRESS=temporal:7233 bash scripts/setup_temporal_search_attrs.sh
set -e

TEMPORAL_ADDRESS="${TEMPORAL_ADDRESS:-localhost:7233}"
NAMESPACE="${TEMPORAL_NAMESPACE:-default}"

echo "Registering search attributes on ${TEMPORAL_ADDRESS} namespace=${NAMESPACE} ..."

temporal operator search-attribute create \
  --address "$TEMPORAL_ADDRESS" \
  --namespace "$NAMESPACE" \
  --name TriggeredBy \
  --type Keyword 2>/dev/null || echo "  TriggeredBy already exists (ok)"

temporal operator search-attribute create \
  --address "$TEMPORAL_ADDRESS" \
  --namespace "$NAMESPACE" \
  --name TriggerSource \
  --type Keyword 2>/dev/null || echo "  TriggerSource already exists (ok)"

echo "Done. Search attributes: TriggeredBy (Keyword), TriggerSource (Keyword)"
