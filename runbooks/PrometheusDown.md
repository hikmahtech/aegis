# PrometheusDown

Prometheus itself is unscrappable — the monitoring stack's own metrics endpoint is not responding. AEGIS and Grafana are now operating blind; no new metric data is flowing in.

## Most Likely Cause

The `prometheus` Swarm service task has crashed or is in restart backoff. In this homelab Prometheus runs on `node-d` (log node); disk pressure on `node-d` is the most common trigger (TSDB can't write new blocks).

## Diagnostic Steps

1. `docker --context swarm service ps prometheus --no-trunc` — task state and crash history
2. `docker --context swarm service logs prometheus --tail 50 --no-trunc` — look for `"opening storage failed"`, `"no space left"`, or config parse errors
3. `ssh node-d df -h` — check disk on the monitoring node (TSDB writes to `/var/lib/prometheus` or a bind mount)
4. `ssh node-d free -h` — check memory (Prometheus can OOM under high cardinality)
5. `curl -s http://node-d:9090/-/healthy` — direct health probe bypassing Traefik

## Remediation

1. **If disk full on node-d**: do NOT auto-delete; escalate (Prometheus TSDB data is the evidence trail for active incidents)
2. **Service crash/backoff**: `docker --context swarm service update --force prometheus` — clears restart backoff
3. **Config parse error** (rule or scrape config malformed): fix the YAML in infra-gitops repo and re-run Ansible to redeploy

## Escalate When

- Prometheus is down AND **another critical alert is firing** — investigation is severely degraded; prioritise restoring Prometheus first
- Disk on `node-d` is >90% full — requires human capacity decision (extend volume, adjust retention, or move TSDB)
- Prometheus crashed with `"corruption"` or `"WAL"` errors in logs — do NOT restart blindly; data integrity at risk
