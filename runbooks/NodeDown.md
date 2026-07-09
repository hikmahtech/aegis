# NodeDown

A Swarm cluster node has stopped responding to heartbeats from the managers and is shown as `Down` in `docker node ls`.

## Most Likely Cause

Machine restart or network partition. In this homelab (mgr-1/mgr-2/mgr-3=managers, node-c=db, node-d=logs, node-a+node-b=GPU, node-e=general) the most common trigger is a node rebooting after an unattended-upgrades restart.

## Diagnostic Steps

1. `docker --context swarm node ls` — identify which node is `Down`
2. `ping <node-hostname>` — if unreachable the machine itself is down, not just Docker
3. `ssh <node-hostname> systemctl status docker` — if reachable, check Docker daemon
4. `docker --context swarm node inspect <node-id> --format "{{.Status.State}} {{.Status.Message}}"` — get detailed state message
5. `docker --context swarm service ps aegis_core --no-trunc` — confirm services rescheduled away from the down node

## Remediation

1. **If Docker daemon stopped** (machine is reachable): `ssh <node> sudo systemctl restart docker` — node should re-join within 30s
2. **If machine is unreachable/rebooting**: wait 2-3 minutes, then re-check `docker --context swarm node ls`
3. **If node stays Down after Docker restart**: `docker --context swarm node update --availability drain <node-id>` to move tasks, then investigate OS-level issues via `ssh`

## Escalate When

- The down node is a **manager** (mgr-1, mgr-2, or mgr-3) — losing a manager risks Raft quorum (need ≥ 2 of 3 managers healthy). If only 1 manager remains, the swarm can't elect a leader for writes.
- The down node is **node-c** (database node) — do NOT auto-restart Postgres/ClickHouse; potential WAL or data-integrity risk; escalate to human.
- Node stays `Down` >10 minutes after Docker restart — OS-level problem requiring manual investigation.
