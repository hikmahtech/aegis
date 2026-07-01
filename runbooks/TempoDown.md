# TempoDown

Tempo (distributed tracing backend) is unreachable — trace-based debugging is unavailable. AEGIS, Grafana, and any service using the OTel collector to send spans will silently drop traces.

## Most Likely Cause

Service crash on `node-d` (log node), often from disk pressure. Tempo stores traces on the same node as Loki; when `node-d` disk fills, Tempo crashes before Loki in most cases (Tempo writes more aggressively).

## Diagnostic Steps

1. `docker --context swarm service ps tempo --no-trunc` — task state and exit reasons
2. `docker --context swarm service logs tempo --tail 50 --no-trunc` — look for `"no space left on device"` or `"block flusher"` errors
3. `ssh node-d df -h` — disk usage on node-d
4. `curl -s http://node-d:3200/ready` — readiness probe (returns `ready` when healthy)
5. `docker --context swarm service ps loki --no-trunc` — check if Loki is also affected (shared disk pressure)

## Remediation

1. **If node-d has disk pressure**: check if Loki is also failing (shared root cause); escalate to human for disk decisions
2. **Service crash without disk issue**: `docker --context swarm service update --force tempo`
3. **After restart**: validate with `curl -s http://node-d:3200/ready` — should return `ready`; then confirm via Grafana Explore → Tempo that traces appear

## Escalate When

- Tempo AND Loki are both down — full observability surface (logs + traces) is gone; human required
- Disk on `node-d` is >80% — both Tempo and Loki are at risk; capacity decision needed before restart
- Tempo crash is accompanied by `"corruption"` in logs — do NOT restart without human review
