# ClickHouseDown

The ClickHouseDown OLAP database (on `node-c`) is unreachable — analytics queries and any pipeline writing to ClickHouse are failing.

## Most Likely Cause

Memory pressure on `node-c`: ClickHouse and PostgreSQL share the node. A large analytical query or a bulk insert can exhaust node-c's RAM, triggering OOM and killing the ClickHouse process.

## Diagnostic Steps

1. `docker --context swarm service ps clickhouse --no-trunc` — task state and exit reasons
2. `docker --context swarm service logs clickhouse --tail 50 --no-trunc` — look for `"Memory limit exceeded"`, `"SIGSEGV"`, or disk errors
3. `curl -s http://node-c:8123/ping` — direct health probe (returns `Ok.` when healthy)
4. `ssh node-c free -h` — check available memory on node-c
5. `ssh node-c df -h` — check disk (ClickHouse data directory can grow rapidly)

## Remediation

1. **If OOM (exit code 137)**: `docker --context swarm service update --force clickhouse` to restart; consider reducing `max_memory_usage` in ClickHouse config if recurring
2. **If disk full**: escalate — do NOT auto-prune ClickHouse data partitions without human review of what's safe to drop
3. **If node-c is unreachable**: see NodeDown runbook for node-c recovery

## Escalate When

- Crash logs show `"SIGSEGV"`, `"Segmentation fault"`, or `"checksum mismatch"` — potential data corruption; do NOT restart without human sign-off
- Disk on `node-c` is full — both ClickHouse and Postgres are affected; human needs to decide what to prune
- ClickHouse is down AND Postgres is also down — node-c is likely entirely offline; treat as NodeDown for node-c
