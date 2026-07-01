# LokiDown

Loki (log aggregation) is unreachable — log-based investigation is unavailable for any ongoing incident. Grafana log panels will show errors; AEGIS investigation quality is degraded.

## Most Likely Cause

Disk full on `node-d` (the log node): Loki writes compressed log chunks to disk and the retention-based compactor has fallen behind. This is the most common cause in this homelab.

## Diagnostic Steps

1. `docker --context swarm service ps loki --no-trunc` — task state and exit code
2. `docker --context swarm service logs loki --tail 50 --no-trunc` — look for `"no space left on device"`, `"chunk flush failed"`, or `"WAL error"`
3. `ssh node-d df -h` — check disk on node-d (Loki chunks are typically under `/var/lib/loki` or a bind mount)
4. `curl -s http://node-d:3100/ready` — readiness probe
5. `docker --context swarm node ls | grep node-d` — confirm node-d is Up

## Remediation

1. **If disk full on node-d**: do NOT auto-delete Loki chunks — they may contain evidence for current incidents. Escalate.
2. **Service crash without disk issue**: `docker --context swarm service update --force loki`
3. **If node-d is unreachable**: see NodeDown runbook for node-d recovery

## Escalate When

- Disk on `node-d` is >90% full — requires human to decide: extend volume, reduce retention, or move chunks
- Loki is down AND another critical alert is actively firing — investigation surface is degraded; restore Loki first
- Loki and Tempo are both down — logging and tracing are both unavailable; human intervention required
