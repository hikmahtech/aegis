# PostgreSQLDown

The primary PostgreSQL instance (on `node-c`) is unreachable — all AEGIS core services (aegis_core, aegis_worker) are database-blind and likely throwing connection errors.

## Most Likely Cause

`node-c` (the dedicated database node) itself has gone offline, or the `postgres` Swarm service has crashed. Since node-c also hosts ClickHouse, a disk-full condition is the most common cause in this homelab — node-c fills up from TSDB, WAL, or ClickHouse data.

## Diagnostic Steps

1. `docker --context swarm node ls | grep node-c` — confirm node-c is Up in the swarm
2. `ping node-c` — confirm machine reachability
3. `docker --context swarm service ps postgres --no-trunc` — task state and crash history
4. `docker --context swarm service logs postgres --tail 50 --no-trunc` — look for `"FATAL"`, `"PANIC"`, disk errors, or `"could not open file"` messages
5. `ssh node-c df -h` — check disk on node-c (WAL + data directories)

## Remediation

1. **If node-c is unreachable/rebooting**: wait 2-3 minutes for machine restart; Postgres should auto-start via Swarm
2. **If node-c is reachable and service is down with no error logs**: `docker --context swarm service update --force postgres` — clears restart backoff
3. **After restart**: verify with `docker --context swarm service logs postgres --tail 20` — should show `"database system is ready to accept connections"`

## Escalate When

- **ALWAYS** escalate to human before taking action if logs show `"PANIC"`, `"FATAL: could not open file"`, or `"invalid page"` — these indicate potential data corruption; do NOT restart without assessment
- Disk on `node-c` is >90% full — do NOT auto-delete files (WAL segments, pg_wal) without human sign-off
- Postgres has been down >5 minutes — aegis_core and aegis_worker are accumulating errors; Temporal workflows may be timing out
