#!/usr/bin/env bash
# Usage: infra_list_nodes.sh <context>
# Lists Docker Swarm nodes for the given context.
set -euo pipefail

CONTEXT="${1:?Usage: infra_list_nodes.sh <context>}"

case "$CONTEXT" in
    swarm)
        docker --context swarm node ls --format '{{json .}}' | jq -cs '[.[] | {
            id: .ID,
            hostname: .Hostname,
            status: .Status,
            availability: .Availability,
            manager_status: .ManagerStatus,
            engine_version: .EngineVersion
        }]'
        ;;
    *)
        echo "Unknown context: $CONTEXT" >&2
        exit 1
        ;;
esac
