# DockerServiceDown

A Swarm service has fewer running tasks than its desired replica count — one or more containers have exited and have not successfully restarted.

## Most Likely Cause

Task crash-loop: the container starts, crashes on launch (bad config, missing secret, OOM), and Swarm keeps retrying with exponential backoff. Second most common: a placement constraint can't be satisfied because the target node is Down.

## Diagnostic Steps

1. `docker --context swarm service ps <service-name> --no-trunc` — see task history and exit reasons (look for `Rejected`, `Failed`, exit codes)
2. `docker --context swarm service logs <service-name> --tail 100 --no-trunc` — read the crash reason from stdout/stderr
3. `docker --context swarm service inspect <service-name> --format "{{json .Spec.TaskTemplate.Placement}}"` — check placement constraints
4. `docker --context swarm node ls` — verify all required nodes are Up
5. `docker --context swarm service inspect <service-name> --format "{{json .Spec.TaskTemplate.Resources}}"` — check memory limits (OOMKilled)

## Remediation

1. **Fix the root cause** (see crash logs), then: `docker --context swarm service update --force <service-name>` to clear backoff and reschedule
2. **If placement constraint unsatisfiable** (required node is Down): either bring the node back up, or `docker --context swarm service update --constraint-rm <constraint> <service-name>` if another node can host it
3. **If OOMKilled**: temporarily increase the memory limit via service update; open a follow-up to tune the actual usage

## Escalate When

- Service is a **core infra** component: `aegis_core`, `aegis_worker`, `aegis_comms`, `temporal`, `prometheus`, `loki`, `grafana` — these affect the monitoring/investigation surface itself
- Exit code is **137 (OOMKilled)** on `node-c`-pinned services (postgres, clickhouse) — do NOT auto-increase RAM; escalate for capacity review
- Crash reason is **"secret not found"** or **"config not found"** — a Swarm secret/config was deleted; requires human to re-create it
