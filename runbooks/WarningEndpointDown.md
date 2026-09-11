# WarningEndpointDown

A synthetic HTTPS probe to a warning-tier endpoint has failed for more than 5 minutes (`probe_success{probe_tier="warning"} == 0`). The URL is in the alert's `instance` label. Warning tier means a site or tool that matters but is not core infrastructure: an app, an admin dashboard.

## Most Likely Cause

The service behind the URL is down or restarting, most often because a node it depends on went down (its own node, or the node running its database). Less often: the reverse proxy has lost the route, DNS failed, or the TLS certificate expired.

## Diagnostic Steps

1. Check what else is firing. A `DockerServiceDown` or `ServiceCrashLooping` for the backing service, or a `NodeDown`, explains this alert. Work that one first.
2. `curl -sv <instance> -o /dev/null` — the failure mode tells you where to look: DNS error, TLS error, connection refused, a 5xx from the proxy (the backend is unreachable) or a 5xx from the app itself.
3. `docker --context swarm service ps <backing-service> --no-trunc` and `docker --context swarm service logs <backing-service> --tail 50` — is it running, and what did it log last? A database connection error points at the database, not the app.
4. If only this URL fails and the service is healthy, check the reverse proxy's view of it (router and backend registration) and the probe target itself.

## Remediation

1. **Backing service down after a node or database outage**: once the dependency is back, `docker --context swarm service update --force <backing-service>` clears a service that ran out of restart attempts.
2. **Crash on start-up**: read the logs and fix the cause (config, secret, migration). A restart alone will loop.
3. **TLS expired**: see `TLSCertExpiringCritical`.

## Escalate When

- The endpoint is still down 15 minutes after its backing service and dependencies are healthy.
- Several endpoints fail at once with healthy backends: that is the reverse proxy, DNS or the uplink. Treat it as `CriticalEndpointDown`.
- Do not change DNS records, delete certificates or edit proxy routing to make the probe pass. Those need a human.
