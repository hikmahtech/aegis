#!/usr/bin/env bash
# Usage: infra_list_services.sh <context>
set -euo pipefail

CONTEXT="${1:?Usage: infra_list_services.sh <context>}"

case "$CONTEXT" in
    swarm)
        docker --context swarm service ls --format '{{json .}}' | jq -cs '[.[] | {
            id: .ID,
            name: .Name,
            mode: .Mode,
            replicas: .Replicas,
            image: .Image,
            ports: .Ports
        }]'
        ;;
    *)
        echo "Unknown context: $CONTEXT" >&2
        exit 1
        ;;
esac
