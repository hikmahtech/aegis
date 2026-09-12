# IngressUnreachable

AEGIS asked for its own public (or LAN) URL from inside the worker and did not get an answer. This is the canary on the way IN: while it is firing, every inbound webhook — GitHub, Todoist, Alertmanager — is being dropped, and **no outside monitor can tell AEGIS about it**, because the way it would be told is the thing that is broken.

The probe treats any HTTP reply under 500 as healthy, 401/404/405 included: the question is whether bytes reach core, not what core makes of them. So this alert means one of a transport failure, a timeout, or a 5xx.

## Most Likely Cause

The reverse proxy in front of core is up but has no working route to it — a stale backend registration or a broken overlay network. The proxy answers 502 or 504 while the core service itself is perfectly healthy, which is why core's own healthcheck stays green and nothing else notices.

Less likely, in rough order: the core service is genuinely down (its own alert should be firing too); DNS or the tunnel in front of the proxy; an expired TLS certificate.

## Diagnostic Steps

1. Ask core directly, bypassing the proxy — reach the service on the cluster's internal address. If core answers there and not through the proxy, it is the route, not the app.
2. Read the proxy's own logs for backend or registration errors around the first occurrence.
3. Run the same request the canary runs, verbosely, and note where it stops: DNS, TLS handshake, connect, or a 5xx body.
4. Check the alert's timeline for a deploy just before it started. A rolling update that drops a backend registration is the common trigger.

## Remediation

1. **Proxy has no healthy backend** — force a redeploy of the proxy so it re-registers every backend. This is the fix that has worked here before; it is idempotent and safe.
2. **Only core is unreachable, the proxy is fine** — force a redeploy of the core service and follow its own runbook.
3. **TLS or DNS** — see the certificate and DNS runbooks; do not delete existing certificates.

## Escalate When

- The proxy has been redeployed and the canary still fails: the fault is below the proxy (overlay network, host firewall, tunnel) and needs a human.
- It resolves and returns repeatedly. A flapping way-in loses webhooks every time, and each gap is invisible from outside.
