#!/usr/bin/env bash
# Usage: infra_inspect_service.sh <context> <service_name>
set -euo pipefail

CONTEXT="${1:?Usage: infra_inspect_service.sh <context> <service_name>}"
SERVICE="${2:?service_name required}"

# Sanitize
if ! [[ "$SERVICE" =~ ^[a-zA-Z0-9_.-]+$ ]]; then
    echo "Invalid service name: $SERVICE" >&2
    exit 1
fi

case "$CONTEXT" in
    swarm) DOCKER_CTX="swarm" ;;
    *) echo "Unknown context: $CONTEXT" >&2; exit 1 ;;
esac

INSPECT=$(docker --context "$DOCKER_CTX" service inspect "$SERVICE" --format '{{json .}}' 2>&1) || {
    echo "$INSPECT" >&2
    exit 1
}
TASKS=$(docker --context "$DOCKER_CTX" service ps "$SERVICE" --no-trunc --format '{{json .}}' 2>&1 | jq -s '.' || echo '[]')

jq -n --argjson inspect "$INSPECT" --argjson tasks "$TASKS" '{
    id: $inspect.ID,
    name: $inspect.Spec.Name,
    image: $inspect.Spec.TaskTemplate.ContainerSpec.Image,
    mode: (if $inspect.Spec.Mode.Replicated then "replicated" else "global" end),
    replicas_desired: ($inspect.Spec.Mode.Replicated.Replicas // null),
    update_status: ($inspect.UpdateStatus // null),
    created_at: $inspect.CreatedAt,
    updated_at: $inspect.UpdatedAt,
    tasks: [$tasks[] | {
        id: .ID,
        name: .Name,
        node: .Node,
        desired_state: .DesiredState,
        current_state: .CurrentState,
        error: .Error,
        ports: .Ports
    }]
}'
