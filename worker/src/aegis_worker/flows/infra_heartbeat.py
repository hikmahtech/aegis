"""InfraHeartbeatFlow — 2-min swarm liveness/convergence poll (spec 2026-07-24).

Emits mostly on state transitions, never on unchanged steady state:
- node → Down (first sight counts)          → NodeDown alert (escalate)
- node Down → Ready                          → resolved audit row
- node Down → gone from `docker node ls`     → resolved audit row (#131)
- service stuck 2 consecutive ticks          → DockerServiceDown alert
  (routes into the existing auto-remediation in AlertInvestigationFlow)
- service confirmed-stuck > `restuck_hours`  → ServiceDownProlonged alert (#138)
- confirmed-stuck service converged          → resolved audit row
- collect failed `fail_threshold` in a row   → HeartbeatCollectFailed alert
- collect recovered                          → its resolved audit row

Recovery writes the same audit_log row shape the Alertmanager webhook writes
(action=alert_received, details.resolved=true), so check_alert_resolved and
the whole self-resolve machinery work unchanged for heartbeat alerts.

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
a known-bad service is not re-alerted every 2 minutes, so the re-investigate
path is gated three ways, following `FlowHealthWatchdogFlow`:

1. threshold — a service must have been `confirmed` for `restuck_hours`
   (default 24h) before it is eligible at all;
2. dedup — `reinvestigated_at` in the heartbeat state row ratchets forward on
   every successful spawn, so one confirmed-stuck service yields at most ONE
   re-investigation per `restuck_hours`, not one per tick. That ledger lives in
   the state row rather than `audit_log` because the threshold already forces
   this flow to remember a per-service timestamp — a second ledger would be two
   guards for one rule. (`alert_dedup_index` is the wrong substrate regardless:
   its `task_id` is NOT NULL and joined to `todoist_tasks`, so it dedups
   @pandora Todoist tasks, not alerts.) `AlertInvestigationFlow`'s own
   resolved-aware `check_dedup` is an independent downstream backstop;
3. mutes — `AlertInvestigationFlow` step 2.5 already short-circuits on
   `alert_mutes` with `_build_mute_key` = `aegis-heartbeat:<service>:`, so the
   operator kill switch is the existing one and the alert body prints it.

The re-investigation reuses the existing `ServiceDownProlonged` alertname
(Prometheus has the same-named 2h rule for exactly this "prolonged outage the
5m alert failed to get actioned" case) rather than re-firing DockerServiceDown:
a distinct alertname means a distinct fingerprint, which is what keeps it from
being swallowed by the original alert's own 24h fingerprint dedup, while the
shared signature (`infra-class:<cluster>:servicedownprolonged:<svc>`) still
collapses it onto one @pandora task. It is `escalate=True` — being down for a
day with the first investigation dead is precisely the case #138 says nobody
ever heard about.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.homelab import HomelabActivities
    from aegis_worker.flows.alert_investigation import AlertInvestigationFlow
    from aegis_worker.shared.retry import FAST, NO_RETRY, TIMEOUT_FAST, TIMEOUT_STANDARD


def _safe_id_segment(text: str, max_len: int = 60) -> str:
    return re.sub(r"[^a-zA-Z0-9._\-]", "-", text)[:max_len]


def _hb_fingerprint(alertname: str, subject: str) -> str:
    return f"aegis-heartbeat:{alertname}:{subject}"


def _hours_since(iso: object, now: datetime) -> float | None:
    """Hours between an ISO stamp written by a previous tick and `now`.

    None means "no usable stamp" — absent, or unparseable/naive from a
    hand-edited state row. The single caller re-stamps it to `now` rather than
    guessing an age, so neither a first sighting nor a corrupt value can make a
    service look old enough to re-investigate.
    """
    try:
        then = datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return None
    if then.tzinfo is None:
        return None
    return (now - then).total_seconds() / 3600.0


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
    # Hours a service must sit `confirmed`-stuck before the flow re-investigates
    # it (#138), and equally the minimum gap between two re-investigations of
    # the same service. 24h, matching the fuse an operator would expect for
    # "this has been broken all day and nothing fixed it". 0 disables the path.
    # Lives in activities.config so schedule_sync propagates edits live.
    restuck_hours: int = 24


@workflow.defn
class InfraHeartbeatFlow:
    async def _spawn(self, alert: dict) -> bool:
        """ABANDONED child; an already-running same-id child is benign."""
        # Deterministic per-transition stamp (workflow.now() is deterministic
        # inside a workflow) so a LATER transition of the same node/service
        # never collides with a completed/running earlier child — that
        # collision is what let a genuine re-fire get silently dropped as an
        # already-started duplicate. Fingerprints stay deterministic (recovery
        # audit rows must still match), only the child workflow id is unique.
        stamp = workflow.now().strftime("%Y%m%d%H%M%S")
        child_id = (
            f"aegis-heartbeat-{_safe_id_segment(alert['labels']['alertname'].lower())}-"
            f"{_safe_id_segment(alert['fingerprint'].rsplit(':', 1)[-1])}-{stamp}"
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
                args=[{**prior, "fail_count": fail_count}],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=FAST,
            )
            return {"collect_ok": False, "alerts_spawned": spawned, "fail_count": fail_count}

        # ── Success path: diff transitions ──
        prev_nodes: dict = prior.get("nodes") or {}
        cur_nodes: dict = current.get("nodes") or {}
        prev_stuck = set(prior.get("stuck") or [])
        prev_confirmed = set(prior.get("confirmed") or [])
        cur_stuck = set(current.get("stuck") or [])

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

        # #138: per-service clocks for the confirmed-stuck set. `confirmed_at`
        # is stamped once (it answers "how long has this been broken");
        # `reinvestigated_at` ratchets on each re-investigation and is what
        # makes the re-investigate path fire once per restuck_hours instead of
        # once per 2-min tick. Both are pruned to `confirmed_now`, so a service
        # that recovers loses its clocks and starts fresh if it breaks again.
        now = workflow.now()
        now_iso = now.isoformat()
        confirmed_at = {
            svc: ts
            for svc, ts in (prior.get("confirmed_at") or {}).items()
            if svc in confirmed_now
        }
        reinvestigated_at = {
            svc: ts
            for svc, ts in (prior.get("reinvestigated_at") or {}).items()
            if svc in confirmed_now
        }
        stale_stuck: list[str] = []
        for svc in sorted(confirmed_now):
            stuck_for = _hours_since(confirmed_at.get(svc), now)
            if stuck_for is None:
                # First sighting on this clock — including every service that
                # was already `confirmed` when this change shipped. Start the
                # clock now rather than treating an unknown age as infinite,
                # which would alert on every known-bad service the moment the
                # deploy lands.
                confirmed_at[svc] = now_iso
                continue
            if config.restuck_hours <= 0 or stuck_for < config.restuck_hours:
                continue
            last = _hours_since(reinvestigated_at.get(svc), now)
            if last is not None and last < config.restuck_hours:
                continue
            stale_stuck.append(svc)

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
        for svc in stale_stuck:
            hours = int(_hours_since(confirmed_at[svc], now) or 0)  # never None here
            alert = build_heartbeat_alert(
                "ServiceDownProlonged",
                svc,
                cluster,
                f"PROLONGED: {svc} still down after {hours}h",
                f"{svc} has been below desired replicas since {confirmed_at[svc]} "
                f"({hours}h) and the original investigation never resolved it. "
                f"Re-investigating; this repeats at most once every "
                f"{config.restuck_hours}h. Silence it with: INSERT INTO alert_mutes "
                f"(mute_key, muted_until) VALUES ('aegis-heartbeat:{svc}:', "
                f"now() + interval '7 days');",
                escalate=True,
                service_name=svc,
            )
            if await self._spawn(alert):
                spawned += 1
                reinvestigated += 1
                # Ratchet BEFORE the state write below — this is the dedup, and
                # only a spawn that actually started may consume the budget.
                reinvestigated_at[svc] = now_iso

        for node in nodes_vanished:
            await workflow.execute_activity_method(
                HomelabActivities.record_heartbeat_resolved,
                args=[_hb_fingerprint("NodeDown", node)],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=NO_RETRY,
            )
            workflow.logger.info(
                "heartbeat_node_vanished_while_down node=%s — NodeDown resolved", node
            )
        for node in nodes_recovered:
            # Resolved row written for quiet nodes too — harmless, and it
            # closes out any alert fired before the node was quieted.
            await workflow.execute_activity_method(
                HomelabActivities.record_heartbeat_resolved,
                args=[_hb_fingerprint("NodeDown", node)],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=NO_RETRY,
            )
            if node in quiet:
                await workflow.execute_activity_method(
                    HomelabActivities.notify_node_transition,
                    args=[node, "up"],
                    start_to_close_timeout=TIMEOUT_FAST,
                    retry_policy=NO_RETRY,
                )
                quiet_notified += 1
        for svc in sorted(recovered_services):
            # Both fingerprints: a service that was re-investigated owns a live
            # ServiceDownProlonged escalation too, and it needs the same
            # resolved row or it nags after the outage is over.
            for alertname in ("DockerServiceDown", "ServiceDownProlonged"):
                await workflow.execute_activity_method(
                    HomelabActivities.record_heartbeat_resolved,
                    args=[_hb_fingerprint(alertname, svc)],
                    start_to_close_timeout=TIMEOUT_FAST,
                    retry_policy=NO_RETRY,
                )
        if int(prior.get("fail_count") or 0) >= config.fail_threshold:
            await workflow.execute_activity_method(
                HomelabActivities.record_heartbeat_resolved,
                args=[_hb_fingerprint("HeartbeatCollectFailed", "collect")],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=NO_RETRY,
            )

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
                    "confirmed_at": confirmed_at,
                    "reinvestigated_at": reinvestigated_at,
                    "fail_count": 0,
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
            "quiet_notified": quiet_notified,
            "nodes_down": len(nodes_down),
            "nodes_recovered": len(nodes_recovered),
            "nodes_vanished": len(nodes_vanished),
            "services_confirmed_stuck": len(new_confirmed),
            "services_reinvestigated": reinvestigated,
            "services_recovered": len(recovered_services),
        }
