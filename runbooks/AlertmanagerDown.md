# AlertmanagerDown

Alertmanager is unavailable — all Prometheus alert routing, deduplication, and grouping is offline. New alerts from Prometheus are not reaching AEGIS or any other receiver.

## Most Likely Cause

The `alertmanager` Swarm task has crashed, usually due to a malformed config pushed via Ansible (the `alertmanager.yml` is Jinja2-templated and a bad merge can produce invalid YAML).

## Diagnostic Steps

1. `docker --context swarm service ps alertmanager --no-trunc` — task state and exit reasons
2. `docker --context swarm service logs alertmanager --tail 50 --no-trunc` — look for `"error loading config"` or `"unmarshal errors"`
3. `curl -s http://node-d:9093/-/healthy` — direct probe (bypasses Traefik)
4. `curl -s http://node-d:9093/-/ready` — readiness check (healthy but not ready = still loading)

## Remediation

1. **If config error**: run `amtool check-config /etc/alertmanager/alertmanager.yml` (or validate the Jinja2 template in infra-gitops) before re-deploying
2. **Service crash without config error**: `docker --context swarm service update --force alertmanager`
3. **After fixing**: verify with `curl -s http://node-d:9093/api/v2/status | jq .uptime` — confirms Alertmanager is routing again

## Escalate When

- Alertmanager has been down **>30 minutes** — Prometheus may have dropped alerts that fired during the outage (they won't be re-sent once AM recovers)
- The AEGIS webhook receiver URL in `alertmanager.yml` is wrong — requires infra-gitops Ansible change and re-deploy
