# CriticalEndpointDown

A Blackbox exporter synthetic probe has detected that a critical public endpoint is returning a non-2xx status or timing out. The failing endpoint is identified in the alert's `instance` label. Affected endpoints include grafana, prometheus, alertmanager, dagster, and aegis-api — all routed through Traefik.

## Most Likely Cause

Traefik (the Swarm ingress/reverse proxy) has crashed or lost its Docker backend registration. Less likely: the underlying service itself is down (check DockerServiceDown runbook for that service) or a TLS certificate has expired.

## Diagnostic Steps

1. Check the alert's `instance` label to identify the failing endpoint
2. `docker --context swarm service ps traefik --no-trunc` — Traefik is the ingress; a crash here takes down all public endpoints simultaneously
3. `curl -v https://<failing-endpoint>` — check if TLS handshake succeeds (certificate error vs connection refused vs 5xx from backend)
4. `docker --context swarm service logs traefik --tail 30 --no-trunc` — look for backend registration errors or TLS renewal failures
5. `dig <failing-hostname>` — verify DNS resolution is still pointing to the homelab's public IP

## Remediation

1. **If multiple endpoints are down simultaneously → Traefik issue**: `docker --context swarm service update --force traefik`
2. **If only one endpoint is down → backend service down**: check `docker --context swarm service ps <service-name>` and apply that service's runbook
3. **If TLS error in curl output**: check `TLSCertExpiringCritical` runbook; may need `docker --context swarm service logs traefik | grep -i "acme\|cert"` to diagnose renewal failure

## Escalate When

- All endpoints are down AND Traefik restart doesn't fix it — DNS or upstream firewall issue requiring manual investigation
- TLS certificate has expired (curl shows `SSL certificate problem: certificate has expired`) — ACME renewal failed; requires infra-gitops Ansible change or manual cert renewal; do NOT auto-delete existing certs
- Endpoint is `aegis-api.example.com` — AEGIS webhook receiver is down; GitHub/Alertmanager events are being dropped
