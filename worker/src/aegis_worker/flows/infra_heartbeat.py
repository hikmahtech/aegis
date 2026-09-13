"""InfraHeartbeatFlow — 2-min swarm liveness/convergence poll (spec 2026-07-24).

Emits mostly on state transitions, never on unchanged steady state:
- node → Down (first sight counts)          → NodeDown alert (escalate)
- node Down → Ready                          → resolved event on the problem
- node Down → gone from `docker node ls`     → resolved event (#131)
- service stuck 2 consecutive ticks          → DockerServiceDown alert
  (routes into the existing auto-remediation in AlertInvestigationFlow)
- service confirmed-stuck > `restuck_hours`  → ServiceDownProlonged alert (#138)
- confirmed-stuck service converged          → resolved event on the problem
- collect failed `fail_threshold` in a row   → HeartbeatCollectFailed alert
- collect recovered                          → its resolved event
- the ingress stopped answering from outside → IngressUnreachable alert (#492)
- the ingress answers again                  → its resolved event

Recovery is `_resolve()` → `HubActivities.ingest_alert(resolved=True)`: the
hub closes the problem and the projector closes its task. The audit-log rows
and `check_alert_resolved` this flow used to write and read are gone with the
rest of the pre-hub dedupe machinery.

Children are spawned ABANDONED (sentry_poll pattern) — investigations carry
human gates and must outlive this 2-min tick. Dead-man ping fires only on a
successful collect so a dead AEGIS/node silences healthchecks.io.

**Why a pure transition diff was not enough** (issues #131 and #138). Reacting
only to change makes any state that gets *stuck* invisible forever, and that
showed up twice:

* #131 — the node diff walked `cur_nodes` only, so a node that dropped out of
  the swarm listing entirely was never compared with its previous state: its
  NodeDown alert never got a resolved row and its Down status was erased by the
  next state write, orphaning an escalation that can then only stop at
  ack/max-repeats/48h archive.
* #138 — `miniflux_miniflux` and `ollama_ollama-2` sat `confirmed`-stuck for
  over a day after three `AlertInvestigationFlow` runs crashed mid-deploy, with
  no retry and no human ever told, because "already confirmed" is not a
  transition.

They are *not* fixed by one mechanism, deliberately. #131 is still a diff — it
is the same prev-vs-cur comparison, just walking the keys that only exist on
the `prev` side, and it must resolve on the tick the node vanishes rather than
hours later. #138 has no transition to observe at all: the only signal is
elapsed time, so it needs the flow's stored state to carry *when* each service
was confirmed. Forcing #131 into the time-based path would delay a resolve that
should be immediate; forcing #138 into the diff would not fix it at all.

**Alert volume for the #138 re-investigation.** Transition-only logic exists so
a known-bad service is not re-alerted every 2 minutes. Since the problem hub
(PR 3b) the gate is one question to the hub: `stale_stuck_problems` returns the
still-stuck services whose open problem is older than `restuck_hours` and has
had no investigation event in that long. The hub records every investigation
(`record_investigation`), so the per-service clocks this flow used to keep in
its settings row are gone, and a re-investigation runs on the SAME problem the
first one did — one task, one timeline. The `ServiceDownProlonged` alertname
is kept for the flow's escalation behaviour (`escalate=True`: being down for a
day with the first investigation dead is precisely the case #138 says nobody
ever heard about) and for the Prometheus rule of the same name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.homelab import HomelabActivities
    from aegis_worker.activities.hub import HubActivities
    from aegis_worker.flows.alert_investigation import AlertInvestigationFlow
    from aegis_worker.shared.retry import FAST, NO_RETRY, TIMEOUT_FAST, TIMEOUT_STANDARD


def _safe_id_segment(text: str, max_len: int = 60) -> str:
    return re.sub(r"[^a-zA-Z0-9._\-]", "-", text)[:max_len]


def _hb_fingerprint(alertname: str, subject: str) -> str:
    return f"aegis-heartbeat:{alertname}:{subject}"


def build_heartbeat_alert(
    alertname: str,
    subject: str,
    infra_cluster: str,
    title: str,
    description: str,
    *,
    escalate: bool,
    service_name: str = "",
) -> dict:
    labels: dict = {"alertname": alertname}
    if infra_cluster:
        labels["cluster"] = infra_cluster
    if service_name:
        labels["service_name"] = service_name
    alert: dict = {
        "title": title,
        "description": description,
        "source": "aegis-heartbeat",
        "severity": "critical",
        "fingerprint": _hb_fingerprint(alertname, subject),
        "labels": labels,
        "escalate": escalate,
    }
    if service_name:
        alert["service"] = service_name
    return alert


@dataclass
class InfraHeartbeatConfig:
    agent_id: str = "pandoras-actor"
    fail_threshold: int = 3
    # Nodes expected to leave/rejoin the swarm (e.g. a dual-boot box): their
    # Down/Ready transitions send a plain FYI chat ping instead of spawning an
    # investigation. Lives in activities.config so schedule_sync propagates
    # edits live (≤5 min) without a redeploy.
    quiet_nodes: list[str] = field(default_factory=list)
    # The URL the ingress canary gets, from OUTSIDE the container — the public
    # or LAN route, so the request crosses the proxy a webhook crosses (#492).
    # Empty disables the canary, which is what a fresh deployment gets: AEGIS
    # cannot guess the operator's own hostname. Lives in activities.config so
    # schedule_sync propagates edits live.
    ingress_url: str = ""
    # Consecutive failed probes before the alert is raised. Two, because one
    # dropped request is what a rolling update of core looks like and this
    # alert escalates; at a 2-minute cadence that is a 4-minute fuse.
    ingress_fail_threshold: int = 2
    # The status the probe must get back, when the status alone says who
    # answered. Measure it rather than assume: with the admin SPA bundled its
    # catch-all claims every unmatched `/api/` GET, so a webhook path answers
    # 404 to a bare GET (405 only without the SPA). Prefer a route that answers
    # something a proxy never invents — see `/api/webhooks/ping`, 204. 0 accepts
    # anything under 500.
    ingress_expect_status: int = 0
    # Hours a service must sit `confirmed`-stuck before the flow re-investigates
    # it (#138), and equally the minimum gap between two re-investigations of
    # the same service. 24h, matching the fuse an operator would expect for
    # "this has been broken all day and nothing fixed it". 0 disables the path.
    # Lives in activities.config so schedule_sync propagates edits live.
    restuck_hours: int = 24


@workflow.defn
class InfraHeartbeatFlow:
    async def _ingest(self, alert: dict, *, resolved: bool) -> dict:
        """Record a transition on the problem hub. Never raises: a hub outage
        must not stop the heartbeat's own state from advancing."""
        try:
            return await workflow.execute_activity_method(
                HubActivities.ingest_alert,
                args=[alert, resolved],
                start_to_close_timeout=TIMEOUT_STANDARD,
                retry_policy=FAST,
            )
        except Exception as exc:  # noqa: BLE001
            workflow.logger.warning(
                "heartbeat_hub_ingest_failed fp=%s err=%s", alert.get("fingerprint"), str(exc)[:200]
            )
            return {"problem_id": None, "investigate": False, "action": "error"}

    async def _resolve(self, alertname: str, subject: str, cluster: str, service_name: str = "") -> None:
        alert = build_heartbeat_alert(
            alertname, subject, cluster, f"{alertname} resolved: {subject}", "", escalate=False,
            service_name=service_name,
        )
        await self._ingest(alert, resolved=True)

    async def _spawn(self, alert: dict, *, problem_id: str | None = None) -> bool:
        """Record the transition on the hub, then start the investigation as
        an ABANDONED child when the hub wants one. `problem_id` is given for
        a re-investigation of a problem the hub already holds."""
        if problem_id is None:
            ingested = await self._ingest(alert, resolved=False)
            if not ingested.get("investigate"):
                workflow.logger.info(
                    "heartbeat_hub_skip fp=%s action=%s",
                    alert.get("fingerprint"),
                    ingested.get("action"),
                )
                return False
            problem_id = ingested.get("problem_id")
            alert = {
                **alert,
                "problem_id": problem_id,
                "todoist_task_id": ingested.get("todoist_task_id"),
            }
            suffix = str(ingested.get("occurrences") or 1)
        else:
            alert = {**alert, "problem_id": problem_id}
            suffix = "re" + workflow.now().strftime("%Y%m%d%H%M%S")
        # Deterministic per-transition id (workflow.now() is deterministic
        # inside a workflow) so a later transition never collides with an
        # earlier child.
        stamp = workflow.now().strftime("%Y%m%d%H%M%S")
        child_id = (
            f"investigate-{problem_id or _safe_id_segment(alert['fingerprint'])}-{suffix}"
            if problem_id
            else f"aegis-heartbeat-{_safe_id_segment(alert['fingerprint'])}-{stamp}"
        )
        try:
            await workflow.start_child_workflow(
                AlertInvestigationFlow.run,
                alert,
                id=child_id,
                parent_close_policy=workflow.ParentClosePolicy.ABANDON,
            )
            return True
        except Exception as exc:  # noqa: BLE001 — already-started dedup is benign
            workflow.logger.warning(
                "heartbeat_spawn_skipped id=%s err=%s", child_id, str(exc)[:200]
            )
            return False

    @workflow.run
    async def run(self, config: InfraHeartbeatConfig) -> dict:
        prior = await workflow.execute_activity_method(
            HomelabActivities.read_heartbeat_state,
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=FAST,
        )
        current = await workflow.execute_activity_method(
            HomelabActivities.collect_infra_state,
            start_to_close_timeout=TIMEOUT_STANDARD,
            retry_policy=FAST,
        )
        routing = await workflow.execute_activity_method(
            HomelabActivities.get_heartbeat_routing,
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=NO_RETRY,
        )
        cluster = routing.get("infra_cluster") or ""
        spawned = 0

        # ── Ingress canary (#492) ──
        # Before the collect branch, because the way in can be broken while the
        # swarm is perfectly healthy — which is exactly what happened on
        # 2026-09-11, and why nothing noticed for 3.5 hours.
        #
        # It takes `ingress_fail_threshold` consecutive failures to raise, for
        # the same reason the collect path counts: a rolling update of core
        # drops one request, and this alert escalates and pings Slack the
        # moment it is raised.
        #
        # A deploy window DOES cover an Ansible deploy — this problem's subject
        # is `ingress`, kind `service`, and the role posts a window for every
        # name in its deploy-subjects list — but it covers nothing else. A
        # hand-run `service update --force` of core or of the proxy, which is
        # the runbook's own remedy, has no window and drops a tick. So the
        # count stays: two ticks is four minutes, which still finds a 3.5-hour
        # outage inside the first five minutes.
        #
        # The flag is separate from the count and does two things the count
        # cannot: it stops a second alert while the first is open, and it stops
        # a resolve going out every two minutes for a problem that never
        # existed.
        ingress_failing = bool(prior.get("ingress_failing"))
        ingress_fails = int(prior.get("ingress_fails") or 0)
        if workflow.patched("ingress-canary") and config.ingress_url:
            try:
                probe = await workflow.execute_activity_method(
                    HomelabActivities.probe_ingress,
                    args=[config.ingress_url, config.ingress_expect_status],
                    start_to_close_timeout=TIMEOUT_STANDARD,
                    retry_policy=NO_RETRY,
                )
            except Exception as exc:  # noqa: BLE001
                # A canary that cannot run must not take the swarm poll, the
                # state write and the dead-man ping down with it — the tick
                # matters more than the probe. Its siblings below are wrapped
                # for the same reason.
                workflow.logger.warning("heartbeat_ingress_probe_failed err=%s", str(exc)[:200])
                # Nothing was learned, so nothing changes: the count and the
                # flag both stand. In particular this is NOT an answer, so it
                # must not resolve an open problem — the earlier shim said
                # `ok=True` here and did exactly that, closing an outage nobody
                # had proved recovered and then re-raising it as brand new on
                # the next failing tick.
                probe = None
            if probe is not None:
                ingress_fails = 0 if probe.get("ok") else ingress_fails + 1
                if (
                    not probe.get("ok")
                    and not ingress_failing
                    and ingress_fails >= max(1, config.ingress_fail_threshold)
                ):
                    reached = (
                        f"answered {probe.get('status')}" if probe.get("status") else "did not answer"
                    )
                    if probe.get("error"):
                        # Keep the reason even when a status came back: a
                        # redirect off the host answers 200 and is still a fault.
                        reached += f" — {probe['error']}"
                    alert = build_heartbeat_alert(
                        "IngressUnreachable",
                        "ingress",
                        cluster,
                        "AEGIS cannot be reached from outside",
                        f"{probe.get('url')} {reached}, {ingress_fails} checks in a row. "
                        "Every inbound webhook — GitHub, Todoist, Alertmanager — is being "
                        "dropped for as long as this lasts, and no outside monitor can tell "
                        "AEGIS about it. Check the proxy in front of core and its route to "
                        "the core service.",
                        escalate=True,
                    )
                    if await self._spawn(alert):
                        spawned += 1
                    ingress_failing = True
                elif probe.get("ok") and ingress_failing:
                    await self._resolve("IngressUnreachable", "ingress", cluster)
                    ingress_failing = False

        # ── Collect failure path ──
        if not current.get("ok"):
            fail_count = int(prior.get("fail_count") or 0) + 1
            if fail_count == config.fail_threshold:
                alert = build_heartbeat_alert(
                    "HeartbeatCollectFailed",
                    "collect",
                    cluster,
                    "Infra heartbeat cannot reach the swarm",
                    f"{fail_count} consecutive collect failures. "
                    f"Last error: {current.get('error', '')}",
                    escalate=True,
                )
                if await self._spawn(alert):
                    spawned += 1
            await workflow.execute_activity_method(
                HomelabActivities.write_heartbeat_state,
                args=[
                    {
                        **prior,
                        "fail_count": fail_count,
                        "ingress_failing": ingress_failing,
                        "ingress_fails": ingress_fails,
                    }
                ],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=FAST,
            )
            return {
                "collect_ok": False,
                "alerts_spawned": spawned,
                "fail_count": fail_count,
                "ingress_failing": ingress_failing,
                "ingress_fails": ingress_fails,
            }

        # ── Success path: diff transitions ──
        prev_nodes: dict = prior.get("nodes") or {}
        cur_nodes: dict = current.get("nodes") or {}
        prev_stuck = set(prior.get("stuck") or [])
        prev_confirmed = set(prior.get("confirmed") or [])
        cur_stuck = set(current.get("stuck") or [])

        # A `deploying` window the deploy job never closed (it crashed, or
        # the operator rolled a service by hand) ends once the service has
        # converged. Safety net only — the deploy role posts `ok` itself —
        # so it never fails the tick.
        deploys_cleared = 0
        try:
            cleared = await workflow.execute_activity_method(
                HubActivities.clear_converged_deploys,
                args=[sorted(cur_stuck)],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=NO_RETRY,
            )
            deploys_cleared = len(cleared.get("cleared") or [])
        except Exception as exc:  # noqa: BLE001 — housekeeping, never the tick
            workflow.logger.warning("heartbeat_clear_deploys_failed err=%s", str(exc)[:200])

        nodes_down, nodes_recovered = [], []
        for name, status in cur_nodes.items():
            if status == "Down" and prev_nodes.get(name) != "Down":
                nodes_down.append(name)
            elif status == "Ready" and prev_nodes.get(name) == "Down":
                nodes_recovered.append(name)

        # #131: the other direction of the same diff — nodes that were in the
        # previous snapshot and are absent from this one (decommissioned host,
        # or a blank hostname dropped by collect_infra_state). One that was Down
        # owns a live NodeDown alert whose escalation would otherwise never
        # stop, and the state write below erases the evidence, so resolve it
        # here. A node that vanished while Ready has no open alert: nothing to
        # do beyond dropping it from the state.
        #
        # Guarded on a non-empty `cur_nodes`: an ok-but-empty node listing is a
        # collection anomaly, not nine simultaneous decommissions, and resolving
        # on it would close real escalations. AEGIS runs on this swarm, so a
        # genuinely zero-node swarm cannot reach this line.
        nodes_vanished = []
        if cur_nodes:
            nodes_vanished = sorted(
                name
                for name, status in prev_nodes.items()
                if name not in cur_nodes and status == "Down"
            )

        new_confirmed = (cur_stuck & prev_stuck) - prev_confirmed
        confirmed_now = (prev_confirmed | new_confirmed) & cur_stuck
        recovered_services = prev_confirmed - cur_stuck

        # #138: a service still stuck after `restuck_hours` is re-investigated,
        # once per `restuck_hours`. The hub knows when each problem was first
        # seen and when it was last investigated, so the per-service clocks
        # that used to live in the heartbeat state row are gone.
        stale: list[dict] = []
        if config.restuck_hours > 0 and confirmed_now:
            try:
                stale = await workflow.execute_activity_method(
                    HubActivities.stale_stuck_problems,
                    # The classes the heartbeat itself raises for a stuck
                    # service — asking without them returned that service's
                    # unrelated problems too.
                    args=[
                        sorted(confirmed_now),
                        float(config.restuck_hours),
                        ["dockerservicedown", "servicedownprolonged"],
                    ],
                    start_to_close_timeout=TIMEOUT_FAST,
                    retry_policy=FAST,
                )
            except Exception as exc:  # noqa: BLE001
                workflow.logger.warning("heartbeat_stale_lookup_failed err=%s", str(exc)[:200])

        quiet = set(config.quiet_nodes or [])
        quiet_notified = 0
        for node in nodes_down:
            if node in quiet:
                await workflow.execute_activity_method(
                    HomelabActivities.notify_node_transition,
                    args=[node, "down"],
                    start_to_close_timeout=TIMEOUT_FAST,
                    retry_policy=NO_RETRY,
                )
                quiet_notified += 1
                continue
            alert = build_heartbeat_alert(
                "NodeDown",
                node,
                cluster,
                f"Swarm node {node} down",
                f"Heartbeat poll saw node {node} transition to Down.",
                escalate=True,
            )
            if await self._spawn(alert):
                spawned += 1
        for svc in sorted(new_confirmed):
            alert = build_heartbeat_alert(
                "DockerServiceDown",
                svc,
                cluster,
                f"Service {svc} down",
                f"Heartbeat poll saw {svc} below desired replicas for 2 consecutive ticks.",
                escalate=False,
                service_name=svc,
            )
            if await self._spawn(alert):
                spawned += 1
        reinvestigated = 0
        for row in stale:
            svc = str(row.get("subject") or "")
            hours = int(row.get("hours") or 0)
            alert = build_heartbeat_alert(
                "ServiceDownProlonged",
                svc,
                cluster,
                f"PROLONGED: {svc} still down after {hours}h",
                f"{svc} has been below desired replicas for {hours}h and the original "
                f"investigation never resolved it. Re-investigating; this repeats at "
                f"most once every {config.restuck_hours}h. Silence it with the problem's "
                f"Mute 24h card, or `set_service_state` for a longer window.",
                escalate=True,
                service_name=svc,
            )
            if await self._spawn(alert, problem_id=str(row.get("id") or "")):
                reinvestigated += 1

        for node in nodes_vanished:
            await self._resolve("NodeDown", node, cluster)
            workflow.logger.info(
                "heartbeat_node_vanished_while_down node=%s — NodeDown resolved", node
            )
        for node in nodes_recovered:
            # Resolved row written for quiet nodes too — harmless, and it
            # closes out any alert fired before the node was quieted.
            await self._resolve("NodeDown", node, cluster)
            if node in quiet:
                await workflow.execute_activity_method(
                    HomelabActivities.notify_node_transition,
                    args=[node, "up"],
                    start_to_close_timeout=TIMEOUT_FAST,
                    retry_policy=NO_RETRY,
                )
                quiet_notified += 1
        for svc in sorted(recovered_services):
            # One problem per service on the hub: DockerServiceDown and its
            # PROLONGED re-investigations share it, so one resolve ends both.
            await self._resolve("DockerServiceDown", svc, cluster, service_name=svc)
        if int(prior.get("fail_count") or 0) >= config.fail_threshold:
            await self._resolve("HeartbeatCollectFailed", "collect", cluster)

        await workflow.execute_activity_method(
            HomelabActivities.write_heartbeat_state,
            args=[
                {
                    # `or prev_nodes`: an ok collect that yielded zero nodes is
                    # an anomaly (see nodes_vanished above); keeping the last
                    # good map means one bad sample cannot silently erase a
                    # Down status — the #131 orphan by another route.
                    "nodes": cur_nodes or prev_nodes,
                    "stuck": sorted(cur_stuck),
                    "confirmed": sorted(confirmed_now),
                    "fail_count": 0,
                    # Written explicitly: this success-path write REPLACES the
                    # state rather than merging into `prior`, so a key left out
                    # here is a key the canary forgets every two minutes.
                    "ingress_failing": ingress_failing,
                    "ingress_fails": ingress_fails,
                }
            ],
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=FAST,
        )
        await workflow.execute_activity_method(
            HomelabActivities.ping_deadman,
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=NO_RETRY,
        )
        return {
            "collect_ok": True,
            "alerts_spawned": spawned,
            "ingress_failing": ingress_failing,
            "ingress_fails": ingress_fails,
            "quiet_notified": quiet_notified,
            "nodes_down": len(nodes_down),
            "nodes_recovered": len(nodes_recovered),
            "nodes_vanished": len(nodes_vanished),
            "services_confirmed_stuck": len(new_confirmed),
            "services_reinvestigated": reinvestigated,
            "services_recovered": len(recovered_services),
            "deploys_cleared": deploys_cleared,
        }
