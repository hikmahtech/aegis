# OtelCollectorTaskDown

Prometheus cannot scrape the OpenTelemetry collector task on one node (`up{job="otel-collector"} == 0`). The node is in the alert's `hostname` / `instance` labels. Apps on that node cannot export traces while it lasts; nothing else breaks.

## Most Likely Cause

Look at how many nodes are firing before looking at the collector.

- **Several nodes at once**: the collectors are almost never the problem. Prometheus lost its path to them: a node or network outage, the monitoring overlay, or Prometheus itself restarting. These clear on their own within minutes of the network settling.
- **One node**: that node's collector task stopped (crash, OOM kill, a deploy mid-roll) or the node itself is down.

## Diagnostic Steps

1. Count the firing `OtelCollectorTaskDown` alerts, and check for `NodeDown`, `PrometheusDown` or a swarm-wide `DockerServiceDown` storm in the same minutes. If they are there, work that alert instead.
2. `docker --context swarm service ps <collector-service> --no-trunc` — is the task on the named node running, restarting, or missing? Look at exit codes (137 = OOM kill).
3. `docker --context swarm service logs <collector-service> --tail 50 --no-trunc` — config errors show on start-up; memory-limiter or exporter errors show later.
4. If the task is running and healthy, the fault is the scrape path: check the Prometheus targets page for the `otel-collector` job and its last scrape error.

## Remediation

1. **Network or node event**: nothing to do on the collector. Confirm the alerts cleared once the node or network came back.
2. **Task not running** on a healthy node: `docker --context swarm service update --force <collector-service>` reschedules it.
3. **OOM kills**: raise the task's memory limit or tighten its `memory_limiter` in the deploy config, as a follow-up change.

## Escalate When

- It fires on every node for more than 15 minutes with no node or network alert beside it: Prometheus or the monitoring network is broken, and tracing is blind everywhere.
- The collector crash-loops on start-up after a deploy: a bad collector config was shipped and needs a config change, not a restart.
- Do not restart or redeploy the collector while the node it runs on is unreachable. It cannot help and adds churn.
