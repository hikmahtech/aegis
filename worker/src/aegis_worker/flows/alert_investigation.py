"""AlertInvestigationFlow — investigate one problem the hub handed over.

Since PR 3b the flow no longer owns an alert's identity: the problem hub
(`aegis.services.hub`) decides whether a signal is new, a repeat, suppressed
or muted, and a producer starts this flow only when the hub says
`investigate`. The input is the alert dict every producer builds, plus
``problem_id`` and the hub's ``todoist_task_id`` (the task the hub projected,
or the one clarify / a chat tool anchored the alert to). A caller that
predates the hub may omit `problem_id`; step 0 ingests the alert itself and
returns early when the hub would not have investigated.

Pipeline:
0.   Hub identity (`ingest_alert` when the caller gave no problem_id)
2.6. Escalating heads-up ping
3.   Verification delay by class (`hub.verify_seconds`) → `problem_status`
     re-check; a problem the hub already saw resolved ends here
4.   Resolve alert to resource (repo) via `resolve_alert_resource`;
     infra alerts try the one-shot auto-restart first — once per problem
     per window (#501): a problem that is back inside it is not restarted
     again, and goes to the investigation and to one card instead
5.   Gather knowledge context (runbooks, prior incidents)
6.   Investigate: coding CLI if `resource_path` available, else LLM fallback
7.   Assessment → structured verdict (actionable / not_actionable /
     inconclusive / self_resolved)
7.5. Gate 2 card (Open PR / Run fix / Mute 24h / Acknowledge / Discard),
     only when there is a decision (#500): a fix branch, proposed commands,
     an escalating alert, or a restart that did not stick. Escalating alerts
     race the card against the hub seeing the problem resolve
8.5. Post the final report on the task via `AlertActivities.post_task_note`
9.   Notify via chat (links to the Todoist task)
10.  Record the outcome on the problem (`record_investigation`)

Every transition the flow makes is recorded on the problem
(investigating → waiting_human / fixing / resolved) so the timeline, the
digest and the next session read one record.
"""

from __future__ import annotations

import asyncio
import re
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from html import escape as _html_escape

    from aegis.personalities import voice_line

    from aegis_worker.activities.agent_registry import AgentRegistryActivities
    from aegis_worker.activities.alert_governance import (
        AlertGovernanceActivities,
        CreateGithubPrInput,
        StagePendingPrInput,
    )
    from aegis_worker.activities.alerts import (
        AlertActivities,
        extract_proposed_commands,
        is_infra_alert,
        is_remediable_alert,
    )
    from aegis_worker.activities.delivery import DeliveryActivities
    from aegis_worker.activities.hub import HubActivities
    from aegis_worker.flows.interaction import InteractionFlow, InteractionFlowInput
    from aegis_worker.shared.retry import (
        FAST,
        NO_RETRY,
        RETRY_ONCE,
        TIMEOUT_CLAUDE,
        TIMEOUT_FAST,
        TIMEOUT_LLM,
        TIMEOUT_LONG,
        TIMEOUT_STANDARD,
    )

# The investigation pipeline is owned by whichever agent holds the `infra`
# behavior tag (issue #36), resolved once per run() — no literal id. With the
# default seeds that resolves to `pandoras-actor`, so behavior is unchanged.
_MAX_HINT_ROUNDS = 3

# `workflow.patched` ids. A run can wait 48h on its Gate-2 card, so some are
# always in flight across a deploy and replay their recorded commands through
# this code; each id keeps the old command sequence for them. Delete a guard
# (and its old branch) once no run started before it is open.
#
# #500: no Gate-2 card for a verdict with nothing to decide.
_PATCH_NO_CARD = "gate2-only-for-decisions"
# #501: look the problem up before an automatic restart; record every attempt.
_PATCH_RESTART_ONCE = "auto-restart-once-per-window"


def _safe_workflow_id_segment(text: str, max_len: int = 60) -> str:
    """Replace characters illegal in Temporal workflow IDs with dashes."""
    return re.sub(r"[^a-zA-Z0-9._\-]", "-", text)[:max_len]


def gate2_needs_decision(
    *, branches: dict, proposed_cmds: list[str], escalate: bool, restart_repeat: bool
) -> bool:
    """Whether a verdict earns a Gate-2 card (#500). Only when the card itself
    can do something: open a fix PR, run proposed commands, take the ack an
    escalating alert nags for, or hand over a restart that did not stick
    (#501). A card with only Mute and Acknowledge asks nothing: in the two
    weeks before this rule, 27 of the 38 answered verdict cards were a bare
    `ack`.

    The verdict status is deliberately not an input. `actionable` with no
    branch and no commands is work for a person, but nothing a card can
    approve, so it lives on the task like the rest."""
    return bool(branches) or bool(proposed_cmds) or escalate or restart_repeat


def _task_line(t: dict) -> str:
    """One `docker service ps` row as the operator reads it."""
    line = f"{t.get('node') or 'no node'} · {t.get('current_state') or '?'}"
    if t.get("error"):
        line += f" · {t['error']}"
    return line


def _restart_repeat_lines(rr: dict) -> tuple[str, list[str], list[str]]:
    """What happened (one sentence), then the evidence from the first
    restart, then what is different now: the tasks new since, or the current
    state when nothing is new. Shared by the task comment, the card and the
    investigation's context so all three say the same thing."""
    service = rr.get("service") or "the service"
    at = str(rr.get("restarted_at") or "")[:16].replace("T", " ")
    outcome = "it recovered" if rr.get("recovered") else "it did not recover"
    head = (
        f"{service} came back {rr.get('minutes_ago', '?')} min after the automatic "
        f"restart at {at} UTC ({rr.get('command') or 'docker service update --force'}; "
        f"{outcome})."
    )
    then = [_task_line(t) for t in rr.get("diagnostics_then") or []]
    new = rr.get("new_tasks") or []
    now = [_task_line(t) for t in (new or rr.get("diagnostics_now") or [])]
    return head, then, now


def _restart_repeat_note(rr: dict) -> str:
    head, then, now = _restart_repeat_lines(rr)
    lines = [
        f"🔁 {head} I'm not restarting it again: a restart that doesn't hold "
        "won't fix it. Investigating; one card will follow with what I find.",
        "",
        "What docker service ps said after that restart:"
        if then
        else "No evidence was kept from that restart.",
        *[f"  • {line}" for line in then],
    ]
    if now:
        lines += ["", "New since then:" if rr.get("new_tasks") else "Now:"]
        lines += [f"  • {line}" for line in now]
    return "\n".join(lines)


def _restart_repeat_card(rr: dict) -> str:
    head, then, now = _restart_repeat_lines(rr)
    parts = [
        f"🔁 <b>The restart did not stick.</b> {_html_escape(head)} "
        "I did not restart it again."
    ]
    if then:
        parts.append(
            "<b>After that restart:</b>\n"
            + "\n".join(f"  • {_html_escape(line)}" for line in then)
        )
    if now:
        label = "New since then" if rr.get("new_tasks") else "Now"
        parts.append(
            f"<b>{label}:</b>\n" + "\n".join(f"  • {_html_escape(line)}" for line in now)
        )
    return "\n\n".join(parts)


def _restart_repeat_context(rr: dict) -> str:
    """Put in front of the investigation, so it does not propose the restart
    that already failed."""
    head, then, now = _restart_repeat_lines(rr)
    lines = [
        f"CONTEXT: AEGIS force-restarted this service {rr.get('minutes_ago', '?')} min "
        f"ago and it came back. {head} Do not recommend another plain restart; find "
        "out why it does not stay up (placement constraints, resources, a crash on "
        "start) and what would fix that.",
    ]
    if then:
        lines += ["docker service ps after that restart:", *[f"- {line}" for line in then]]
    if now:
        lines += ["docker service ps now:", *[f"- {line}" for line in now]]
    return "\n".join(lines)


def _build_repo_confirm_prompt(
    *,
    title: str,
    source: str,
    severity: str,
    service: str,
    description: str,
    task_id: str,
    candidates: list[dict] | None = None,
) -> str:
    """Body for the Gate-0 repo-confirm card.

    The card used to be just a generic "which repo?" line + the bare title, so
    the user had no idea WHICH issue they were picking a repo for (caught
    2026-06-04). Include the source/severity/service, a description snippet, and
    — most importantly — a deep-link to the Todoist task so the full context is
    one tap away.

    `candidates` (top-5, each {resource_title, resource_path, github_repo,
    label, score}) renders a numbered menu — friendly title + repo identity +
    match strength — so near-identically-named repos (the whole reason picks go
    wrong) are actually distinguishable. The numbers line up 1:1 with the
    numbered buttons below the card.
    """
    lines = [
        "🔎 <b>Which repository is this about?</b>",
        (
            "I couldn't confirm it, so I won't start a code investigation until "
            "you pick one below (or “None” to skip)."
        ),
        "",
        f"<b>{_html_escape(title)}</b>",
    ]
    meta = " · ".join(p for p in (severity, source, service) if p)
    if meta:
        lines.append(_html_escape(meta))
    desc = (description or "").strip()
    if desc:
        snippet = desc[:240] + ("…" if len(desc) > 240 else "")
        lines += ["", _html_escape(snippet)]
    if candidates:
        lines.append("")
        for i, c in enumerate(candidates):
            friendly = (c.get("resource_title") or c.get("label") or "").strip()
            ident = (c.get("github_repo") or c.get("resource_path") or c.get("label") or "").strip()
            score = float(c.get("score") or 0.0)
            strength = (
                "strong match"
                if score >= 1.0
                else "possible match"
                if score >= 0.5
                else "weak match"
            )
            row = f"<b>{i + 1}.</b> {_html_escape(friendly)}"
            if ident and ident != friendly:
                row += f" — <code>{_html_escape(ident)}</code>"
            row += f" · <i>{strength}</i>"
            lines.append(row)
    if task_id and not task_id.startswith("item-"):
        url = f"https://app.todoist.com/app/task/{_html_escape(task_id)}"
        lines += ["", f"<a href='{url}'>Open the task on Todoist →</a>"]
    return "\n".join(lines)


@workflow.defn
class AlertInvestigationFlow:
    """Investigate and route production alerts with verification delay."""

    async def _safe_event(self, msg: str) -> None:
        """Send system event, swallowing failures."""
        try:
            await workflow.execute_activity_method(
                DeliveryActivities.send_system_event,
                args=[msg],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=NO_RETRY,
            )
        except Exception:
            pass

    async def _safe_send_message(
        self,
        agent_id: str,
        message: str,
        log_event: str,
    ) -> None:
        """Workflow-side equivalent of `activities.delivery.safe_send_message`:
        fire-and-forget chat send that logs *both* raised exceptions and
        `{ok: false}` dict returns under `log_event`. The activity-side helper
        can't be called directly from workflows (it needs a DeliveryActivities
        instance), so we mirror the same behaviour over `execute_activity_method`.
        """
        try:
            result = await workflow.execute_activity_method(
                DeliveryActivities.send_message,
                args=[agent_id, message, 0],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=NO_RETRY,
            )
        except Exception as exc:
            workflow.logger.warning("%s err=%s reason=raised", log_event, str(exc)[:200])
            return
        if isinstance(result, dict) and not result.get("ok"):
            workflow.logger.warning(
                "%s err=%s reason=ok_false",
                log_event,
                str(result.get("error", "ok=false"))[:200],
            )

    async def _safe_post_note(
        self,
        task_id: str,
        content: str,
        file_attachment: dict | None = None,
    ) -> None:
        """Best-effort attach a comment to the Todoist track-task. Comments
        are operationally useful but never a blocker for the investigation:
        if the connector returns ok=False (e.g. outbox fallback, transient
        5xx), the flow continues and the next comment retries.

        `file_attachment` is the blob returned by `upload_kimi_log` —
        when supplied the comment renders with a downloadable file.

        The activity appends a `Workflow run: [<id>](<temporal-ui-url>)`
        footer from `workflow.info()` so every comment links to this run's
        Temporal history page — callers no longer embed the workflow id in
        their `content`.

        Outbox temp-ids (`item-…`) are skipped here too: a comment to a
        temp_id always fails (Todoist rejects the unresolved id), so callers
        no longer need to wrap a bare post in
        `if track_task_id and not track_task_id.startswith("item-")`.
        """
        if not task_id or task_id.startswith("item-"):
            return
        info = workflow.info()
        try:
            await workflow.execute_activity_method(
                AlertActivities.post_task_note,
                args=[task_id, content, file_attachment, info.workflow_id, info.run_id],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=NO_RETRY,
            )
        except Exception:
            workflow.logger.warning("alert_post_task_note_failed task_id=%s", task_id)

    async def _record(
        self,
        problem_id: str,
        status: str,
        text: str,
        *,
        step: str,
        payload: dict | None = None,
    ) -> None:
        """Best-effort `record_investigation`: the outcome lands on the
        problem's timeline and moves its status; the flow already posted the
        text on the task itself, so the projector will not repeat it."""
        if not problem_id:
            return
        try:
            await workflow.execute_activity_method(
                HubActivities.record_investigation,
                args=[
                    {
                        "problem_id": problem_id,
                        "status": status,
                        "text": text,
                        # Idempotent per flow run and step: a replayed or
                        # retried step records once.
                        "external_id": f"{workflow.info().workflow_id}:{step}",
                        "posted": True,
                        "payload": payload or {},
                    }
                ],
                start_to_close_timeout=TIMEOUT_STANDARD,
                retry_policy=NO_RETRY,
            )
        except Exception as exc:  # noqa: BLE001
            workflow.logger.warning(
                "alert_record_investigation_failed step=%s err=%s", step, str(exc)[:200]
            )

    async def _safe_remediate_infra(
        self,
        alert: dict,
        track_task_id: str,
        title: str,
        source: str,
        problem_id: str,
        *,
        record_attempt: bool = False,
    ) -> dict | None:
        """Try a one-shot auto-restart for a remediable swarm-service alert.

        Returns a terminal flow result dict when the service recovered (so the
        caller returns immediately and skips the agentic investigation), or
        None to fall through to the normal investigation — either because the
        alert isn't a remediable class / no service name, or the restart was
        issued but the service didn't converge back to healthy.

        Every attempt lands on the problem with a `remediation` payload (the
        service, the command, whether it recovered, and what `docker service
        ps` said), which is what `recent_auto_restart` finds when the problem
        comes back (#501). A recovered restart always recorded; one that did
        not recover is recorded only when `record_attempt` — a history from
        before #501 has no such command to replay.
        """
        try:
            rem = await workflow.execute_activity_method(
                AlertActivities.remediate_infra_service,
                args=[alert],
                start_to_close_timeout=TIMEOUT_LONG,
                heartbeat_timeout=TIMEOUT_FAST,
                retry_policy=NO_RETRY,
            )
        except Exception as exc:
            workflow.logger.warning("alert_remediate_infra_failed err=%s", str(exc)[:200])
            return None
        if not rem.get("attempted"):
            return None
        service = rem.get("service") or "service"
        remediation = {
            "service": rem.get("service") or "",
            "command": rem.get("command") or "",
            "recovered": bool(rem.get("recovered")),
            "reason": rem.get("reason") or "",
            "diagnostics": rem.get("diagnostics") or [],
        }
        if rem.get("recovered"):
            await self._safe_event(
                f"🔧 Auto-restarted <b>{_html_escape(service)}</b> — it was below desired "
                f"replicas and converged back to healthy."
            )
            await self._safe_post_note(
                track_task_id or "",
                f"🔧 Auto-remediated: ran <code>docker service update --force "
                f"{_html_escape(service)}</code> and the service recovered to "
                f"running ≥ desired. No code investigation needed.",
            )
            await self._record(
                problem_id,
                "resolved",
                f"Auto-remediated: docker service update --force {service} and it recovered.",
                step="auto_remediated",
                payload={"remediation": remediation},
            )
            return {
                "status": "auto_remediated",
                "task_id": track_task_id,
                "problem_id": problem_id,
                "service": service,
                "command": rem.get("command", ""),
            }
        # Restart issued but it didn't recover → let the investigation dig in,
        # leaving a breadcrumb that the kick was already tried.
        await self._safe_post_note(
            track_task_id or "",
            f"🔧 Tried an auto-restart (<code>docker service update --force "
            f"{_html_escape(service)}</code>) but {_html_escape(service)} didn't "
            f"recover — investigating.",
        )
        if record_attempt:
            # Status unchanged: the investigation that follows moves it.
            await self._record(
                problem_id,
                "",
                f"Tried an auto-restart: docker service update --force {service}, "
                "and it did not recover.",
                step="auto_restart_unrecovered",
                payload={"remediation": remediation},
            )
        return None

    async def _recent_auto_restart(self, problem_id: str, alert: dict) -> dict | None:
        """The problem's automatic restart inside the window, or None (#501).
        A lookup that fails answers None, so the restart goes ahead as it
        always did: not knowing is no reason to leave a service down."""
        try:
            found = await workflow.execute_activity_method(
                AlertActivities.recent_auto_restart,
                args=[problem_id, alert],
                start_to_close_timeout=TIMEOUT_STANDARD,
                retry_policy=FAST,
            )
        except Exception as exc:  # noqa: BLE001
            workflow.logger.warning("alert_recent_auto_restart_failed err=%s", str(exc)[:200])
            return None
        return found if found.get("repeat") else None

    @workflow.run
    async def run(self, alert: dict) -> dict:
        title = alert.get("title", "Unknown")
        fingerprint = alert.get("fingerprint", "")
        severity = alert.get("severity", "unknown")
        source = alert.get("source", "unknown")
        # Escalating infra alerts (NodeDown / HeartbeatCollectFailed — the
        # heartbeat marks alert["escalate"]). Drives the heads-up ping and the
        # escalating Gate-2 card.
        _escalate = bool(alert.get("escalate"))

        # Owner of the alert pipeline = whoever holds the `infra` behavior tag
        # (issue #36). Every delivery/attribution/child-interaction below is
        # addressed to this agent instead of a literal id. No infra agent =>
        # no channel to drive the confirm/approve steps, so skip cleanly.
        resolved = await workflow.execute_activity_method(
            AgentRegistryActivities.resolve_agents,
            args=[["infra"]],
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=NO_RETRY,
        )
        agent_id = resolved.get("infra")
        if agent_id is None:
            workflow.logger.warning("alert_investigation_skipped_no_infra_agent title=%s", title)
            return {"status": "skipped_no_infra_agent", "task_id": None}

        workflow.logger.info("alert_investigation_starting title=%s severity=%s", title, severity)
        await self._safe_event(
            f"🔍 Alert investigation started: <b>{_html_escape(title)}</b> ({severity}/{source})"
        )

        # ── Step 0: Hub identity ──
        # The producer normally ingested the alert and handed us the problem
        # (and the task the hub projected). A caller that did not — an older
        # start site, a test — gets the same treatment here, and stops when
        # the hub would not have investigated (repeat occurrence, suppressed,
        # muted).
        problem_id = str(alert.get("problem_id") or "")
        track_task_id: str | None = str(alert.get("todoist_task_id") or "") or None
        if not problem_id:
            ingested = await workflow.execute_activity_method(
                HubActivities.ingest_alert,
                args=[alert, False],
                start_to_close_timeout=TIMEOUT_STANDARD,
                retry_policy=FAST,
            )
            problem_id = str(ingested.get("problem_id") or "")
            track_task_id = track_task_id or ingested.get("todoist_task_id") or None
            if not ingested.get("investigate", True):
                workflow.logger.info(
                    "alert_investigation_skipped_by_hub title=%s action=%s",
                    title,
                    ingested.get("action"),
                )
                await self._safe_event(
                    f"⏭ AlertInvestigation — hub says {ingested.get('action')}, skipping: {title}"
                )
                return {
                    "status": "skipped_by_hub",
                    "hub_action": ingested.get("action"),
                    "problem_id": problem_id or None,
                    "task_id": None,
                    "todoist_task_id": track_task_id,
                }
        elif not track_task_id:
            status_now = await workflow.execute_activity_method(
                HubActivities.problem_status,
                args=[problem_id],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=FAST,
            )
            track_task_id = status_now.get("todoist_task_id") or None

        # ── Step 2.6: Escalating heads-up ping ──
        # Escalating infra alerts (NodeDown / HeartbeatCollectFailed) get an
        # immediate heads-up chat ping so the owner knows a decision card is
        # coming and will be nagged until acked. The hub already filtered
        # repeats, suppressed and muted problems before this flow started, so
        # the ping is never noise. Non-escalating alerts skip it.
        if _escalate:
            await self._safe_send_message(
                agent_id=agent_id,
                message=(
                    f"🔴 <b>{_html_escape(title)}</b>\n"
                    f"{severity} · {source} — investigating now; "
                    f"I'll escalate until you ack the decision card."
                ),
                log_event="alert_heads_up_notify_failed",
            )

        # ── Step 2.65: Routing config — infra_cluster (#91) ──
        # is_infra_alert can't read Settings/DB from
        # workflow code, so fetch the configured cluster label once here via
        # a tiny activity. workflow.patched guards in-flight runs started
        # before this change so they keep replaying the pre-patch (env-only)
        # behavior instead of non-deterministically diverging mid-history.
        infra_cluster = ""
        owner_mention = ""
        # None = the pre-#498 built-in infra list, which is what a history
        # recorded before the list moved to the DB must replay against.
        infra_alertnames: list[str] | None = None
        if workflow.patched("infra-cluster-from-settings"):
            routing = await workflow.execute_activity_method(
                AlertActivities.get_alert_routing_config,
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=FAST,
            )
            infra_cluster = routing.get("infra_cluster") or ""
            owner_mention = routing.get("slack_owner_member_id") or ""
            if workflow.patched("infra-alertnames-from-settings"):
                infra_alertnames = routing.get("infra_alertnames")

        # ── Step 3: Verification delay ──
        # A flat per-class wait (`hub.verify_seconds`, served by the hub
        # activity so tests can shorten it) before spending any effort, then
        # ask the hub whether the problem already resolved — a blip that
        # self-heals costs nothing. The hub is the record: the alertmanager
        # `resolved` webhook and the heartbeat's recovery both land there as
        # `resolved` events, so the hub is asked even with no delay.
        delay = await workflow.execute_activity_method(
            HubActivities.verification_delay,
            args=[alert],
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=NO_RETRY,
        )
        delay_seconds = int(delay.get("delay_seconds") or 0)
        if problem_id:
            if delay_seconds > 0:
                workflow.logger.info("alert_verification_delay seconds=%d", delay_seconds)
                await self._safe_event(
                    f"⏳ AlertInvestigation — waiting {delay_seconds}s verification: {title}"
                )
                await workflow.sleep(timedelta(seconds=delay_seconds))
            # A hub that cannot answer must not kill the run: "unknown" is
            # "not resolved yet", and the investigation goes ahead.
            try:
                resolved_check = await workflow.execute_activity_method(
                    HubActivities.problem_status,
                    args=[problem_id],
                    start_to_close_timeout=TIMEOUT_FAST,
                    retry_policy=FAST,
                )
            except Exception as exc:  # noqa: BLE001
                workflow.logger.warning(
                    "alert_verification_status_failed problem_id=%s err=%s",
                    problem_id,
                    str(exc)[:200],
                )
                resolved_check = {"resolved": False}
            if resolved_check.get("resolved"):
                workflow.logger.info("alert_self_resolved problem_id=%s", problem_id)
                await self._safe_event(
                    f"✅ AlertInvestigation — self-resolved during delay: {title}"
                )
                await self._safe_post_note(
                    track_task_id or "",
                    voice_line(agent_id, "investigation_self_resolved"),
                )
                await self._record(
                    problem_id,
                    "resolved",
                    "Self-resolved during the verification delay.",
                    step="self_resolved",
                )
                return {
                    "status": "self_resolved",
                    "task_id": None,
                    "problem_id": problem_id,
                    "todoist_task_id": track_task_id,
                    "verdict": None,
                    "resource": None,
                    "investigation": "",
                }

        # ── Step 4: Resolve to resource ──
        # Infra/swarm alerts (NodeDown, DockerServiceDown, cluster=homelab-swarm, ...)
        # have no application code repo. Resolve them deterministically to
        # infra-gitops, skipping the LLM repo-match entirely.
        _is_infra = is_infra_alert(alert, infra_cluster, infra_alertnames)
        # The problem's last automatic restart, when it came back inside the
        # window (#501). Set only on that path; it forces the Gate-2 card.
        restart_repeat: dict | None = None
        if _is_infra:
            # ── Step 4.0: Auto-remediation (force-restart) ──
            # A swarm service below desired replicas is usually a stuck/unplaced
            # task that an idempotent `service update --force` reschedules. Try
            # that one safe kick before the expensive agentic investigation; if
            # the service recovers we're done (and never burn the kimi budget).
            # Crash-loops are excluded by the activity — restarting them churns.
            #
            # Once per problem per window (#501): a problem that was restarted
            # automatically a moment ago and is back is not restarted again. The
            # restart did not hold, so the next one would not either; it goes
            # to the investigation and to one card, with the first restart's
            # evidence.
            record_restart = bool(
                problem_id
                and is_remediable_alert(alert)
                and workflow.patched(_PATCH_RESTART_ONCE)
            )
            if record_restart:
                restart_repeat = await self._recent_auto_restart(problem_id, alert)
            if restart_repeat is not None:
                await self._safe_event(
                    f"🔁 {_html_escape(str(restart_repeat.get('service') or title))} came back "
                    f"after its automatic restart — not restarting it again."
                )
                # Todoist comments are plain text, like the verdict comment.
                await self._safe_post_note(track_task_id or "", _restart_repeat_note(restart_repeat))
                await self._record(
                    problem_id,
                    "",
                    _restart_repeat_note(restart_repeat),
                    step="restart_repeat",
                    payload={"restart_repeat": restart_repeat},
                )
            else:
                remediation = await self._safe_remediate_infra(
                    alert,
                    track_task_id or "",
                    title,
                    source,
                    problem_id,
                    record_attempt=record_restart,
                )
                if remediation is not None:
                    return remediation
            try:
                resource = await workflow.execute_activity_method(
                    AlertActivities.resolve_infra_resource,
                    args=[alert],
                    start_to_close_timeout=TIMEOUT_FAST,
                    retry_policy=NO_RETRY,
                )
            except Exception as exc:
                workflow.logger.warning(
                    "alert_resolve_infra_resource_failed err=%s", str(exc)[:200]
                )
                resource = {
                    "resource_id": None,
                    "resource_title": None,
                    "resource_path": None,
                    "github_repo": "",
                    "confidence": 0.0,
                    "source": "none",
                    "resources": [],
                }
        else:
            try:
                resource = await workflow.execute_activity_method(
                    AlertActivities.resolve_alert_resource,
                    args=[alert],
                    # Raised from TIMEOUT_STANDARD (60s) to TIMEOUT_LLM:
                    # gpt-oss:20b repo-match against ~160 resources peaks at 52s
                    # under normal load and grazes 60s under proxy pressure,
                    # causing all 3 FAST retries to time out → hard workflow
                    # failure. TIMEOUT_LLM (600s since #321) clears that p95
                    # many times over.
                    start_to_close_timeout=TIMEOUT_LLM,
                    retry_policy=FAST,
                )
            except Exception as exc:
                # resolve_alert_resource timed out or raised (LLM/proxy failure).
                # Fall back to null-resource so the flow continues to the
                # LLM-only investigate() path instead of dying here.
                workflow.logger.warning(
                    "alert_resolve_alert_resource_failed_fallback_to_null err=%s",
                    str(exc)[:200],
                )
                resource = {
                    "resource_id": None,
                    "resource_title": None,
                    "resource_path": None,
                    "github_repo": "",
                    "confidence": 0.0,
                    "source": "none",
                    "resources": [],
                }
        resource_title = resource.get("resource_title")
        # resolve_alert_resource / resolve_infra_resource always return a
        # `resources` list (empty or populated), so a populated resource_path
        # always carries a matching resources entry.
        resources_list = resource.get("resources") or []

        # ── Step 4.3: A label claim is application code (#498) ──
        # A repository claimed this alert by label (resources.metadata.
        # alert_labels) — e.g. a Dagster failure whose job the pipeline repo
        # owns, although "Dagster Pipeline Failure" is on the infra list. It is
        # a bug in that repo: investigate it as code (fix branch allowed, no
        # swarm framing) and skip Gate-0, because the claim is the operator's
        # explicit mapping and the content scorer cannot confirm it.
        claimed = resource.get("source") == "label_claim" and workflow.patched(
            "alert-label-claims"
        )
        if claimed:
            _is_infra = False

        # ── Step 4.4: Gate-0 — confirm the repo is relevant before kimi ──
        # resolve_alert_resource matches on alert.service. For the chat path
        # the service IS the pandora-picked repo, so the match is tautological
        # and can never catch a wrong pick. Score the resolved repo against the
        # ISSUE CONTENT; if not confident, ask the user to choose from the top
        # candidates before running a fix-capable kimi (deny-by-default).
        #
        # Infra alerts skip Gate-0 entirely: the infra-gitops resource was
        # resolved deterministically (no LLM ambiguity) so there is nothing to
        # confirm, and blocking with a "Which repo?" card for every NodeDown
        # storm is pure noise. A label claim (Step 4.3) skips it for the same
        # reason: nothing is ambiguous about an explicit mapping.
        if resources_list and not _is_infra and not claimed:
            resolved_rid = resources_list[0].get("resource_id") or ""
            rel = await workflow.execute_activity_method(
                AlertActivities.score_resource_relevance,
                args=[alert, resolved_rid],
                start_to_close_timeout=TIMEOUT_STANDARD,
                retry_policy=FAST,
            )
            if not rel.get("confident", True):
                # Bounded hint loop: present the top candidates; if the operator
                # replies with a free-text `hint:<text>` (allowed for the first
                # _MAX_HINT_ROUNDS rounds), re-run Gate-0 resolution with the
                # hint folded in and re-present. Otherwise honour their pick /
                # cancel. The card option keys are the candidate INDEX (not the
                # resource UUID): legacy callback_data was
                # `interaction:{id}:{key}` and is capped at 64 bytes — two UUIDs
                # blow past it (85B) and the card silently fails to send
                # (BUTTON_DATA_INVALID).
                round_n = 0
                chosen_c = None
                while True:
                    candidates = rel.get("candidates") or []
                    top = candidates[:5]
                    if not top:
                        break
                    options = {
                        str(i): f"{i + 1}. 📦 {c.get('label') or c.get('resource_title')}"
                        for i, c in enumerate(top)
                    }
                    options["none"] = "❌ None of these / cancel"
                    confirm_prompt = _build_repo_confirm_prompt(
                        title=title,
                        source=source,
                        severity=severity,
                        service=alert.get("service", ""),
                        description=alert.get("description", ""),
                        task_id=track_task_id or "",
                        candidates=top,
                    )
                    suffix = "" if round_n == 0 else f"-h{round_n}"
                    picked = await workflow.execute_child_workflow(
                        InteractionFlow.run,
                        InteractionFlowInput(
                            agent_id=agent_id,
                            kind="choice",
                            origin="alert_confirm_repo",
                            prompt=confirm_prompt,
                            options=options,
                            timeout_seconds=86400,
                            timeout_policy="archive",
                            allow_hint=(round_n < _MAX_HINT_ROUNDS),
                        ),
                        id=(
                            "repo-confirm-"
                            f"{_safe_workflow_id_segment(alert.get('fingerprint') or '')}"
                            f"{suffix}-{workflow.info().workflow_id}"
                        ),
                    )
                    if getattr(picked, "status", None) == "archived":
                        break
                    picked_val = ((picked.response or {}).get("value") or "").strip()
                    if picked_val.startswith("hint:") and round_n < _MAX_HINT_ROUNDS:
                        round_n += 1
                        rel = await workflow.execute_activity_method(
                            AlertActivities.reresolve_with_hint,
                            args=[alert, picked_val[len("hint:") :]],
                            start_to_close_timeout=TIMEOUT_STANDARD,
                            retry_policy=FAST,
                        )
                        continue
                    if picked_val.isdigit() and int(picked_val) < len(top):
                        chosen_rid = top[int(picked_val)].get("resource_id") or ""
                        chosen_c = next(
                            (
                                c
                                for c in (rel.get("candidates") or [])
                                if c["resource_id"] == chosen_rid
                            ),
                            None,
                        )
                    break

                if not chosen_c:
                    # No repo confirmed → do not run a fix-capable kimi on an
                    # unverified repo. Abort cleanly with a track-task note.
                    await self._safe_post_note(
                        track_task_id or "",
                        "🤔 Skipped — couldn't confirm which repository this "
                        "belongs to, so I did not start a code investigation.",
                    )
                    return {"status": "repo_unconfirmed", "task_id": track_task_id}

                resources_list = [
                    {
                        "resource_id": chosen_c["resource_id"],
                        "resource_title": chosen_c.get("resource_title"),
                        "resource_path": chosen_c.get("resource_path"),
                        "github_repo": chosen_c.get("github_repo", ""),
                        "confidence": 1.0,
                    }
                ]
                resource_title = chosen_c.get("resource_title")

        await self._record(
            problem_id,
            "investigating",
            f"Investigation started against {resource_title or 'no resource'}.",
            step="investigating",
        )

        # ── Step 4.5: Post start-comment on the track-task ──
        # We have the resource picked now, which is the useful piece of
        # information for the user to see while kimi runs.
        if track_task_id and not track_task_id.startswith("item-"):
            is_jira_src = source == "todoist-jira"
            start_event = "scoping_started" if is_jira_src else "investigation_started"
            start_msg = voice_line(agent_id, start_event, resource=resource_title or "auto")
            await self._safe_post_note(track_task_id, start_msg)

        # ── Step 5: Gather knowledge context (runbook + prior incidents) ──
        alert_name = (alert.get("labels") or {}).get("alertname", "")
        knowledge_context = ""
        try:
            knowledge_context = await workflow.execute_activity_method(
                AlertActivities.gather_alert_knowledge,
                args=[title, resource_title or "", alert_name],
                # Must exceed knowledge.py::_ASK_TIMEOUT (60s httpx read budget)
                # or the activity gets cancelled mid-request and kimi launches
                # with an empty knowledge_context. TIMEOUT_FAST=15s was too tight.
                start_to_close_timeout=timedelta(seconds=65),
                retry_policy=NO_RETRY,
            )
        except Exception:
            pass

        # ── Step 5.5: Prepend infra context hint for infra alerts ──
        # The coding agent investigates infra-gitops (ansible/swarm config).
        # Give it an explicit framing so it doesn't look for application code.
        if _is_infra:
            labels_str = ""
            _labels = alert.get("labels") or {}
            if isinstance(_labels, dict) and _labels:
                labels_str = ", ".join(f"{k}={v}" for k, v in _labels.items())
            infra_hint = (
                "CONTEXT: This is a Docker Swarm / homelab infrastructure alert — "
                "NOT an application code bug. Investigate cluster and service health "
                "using the infra-gitops ansible config and swarm state "
                "(e.g. `docker --context swarm node ls`, "
                "`docker --context swarm service ps <service>`, "
                "`docker --context swarm service logs <service>`). "
                "Do NOT look for application source code — focus on ansible roles, "
                "docker-compose/stack templates, and swarm service state."
                " End your report with a PROPOSED_COMMANDS: section — one `- <command>` "
                "line per safe, idempotent recovery command you recommend (max 5, "
                "e.g. `- docker --context swarm service update --force <svc>`). "
                "Propose ONLY read-safe or idempotent commands; omit the section if "
                "no command is warranted. The commands are NOT run automatically — "
                "a human approves them."
            )
            if labels_str:
                infra_hint += f"\nAlert labels: {labels_str}"
            knowledge_context = (infra_hint + "\n\n" + knowledge_context).strip()
        if restart_repeat is not None:
            # First thing the investigation reads: the restart already failed.
            knowledge_context = (
                _restart_repeat_context(restart_repeat) + "\n\n" + knowledge_context
            ).strip()

        # ── Step 6: Investigate ──
        investigation_output = ""
        inv_result: dict = {}
        investigation_source = "llm"
        kimi_attempted = bool(resources_list)
        is_jira = source == "todoist-jira"
        if kimi_attempted:
            try:
                inv_result = await workflow.execute_activity_method(
                    AlertActivities.run_investigation,
                    # allow_fix=not _is_infra: app-code alerts may stage a fix
                    # branch (→ Gate-2 "Open PR?"); infra alerts are investigate-only.
                    args=[alert, resources_list, knowledge_context, "", not _is_infra],
                    start_to_close_timeout=TIMEOUT_CLAUDE,
                    heartbeat_timeout=timedelta(minutes=2),
                    retry_policy=NO_RETRY,
                )
            except Exception as exc:
                # Activity timed out at the Temporal layer (NO_RETRY +
                # start_to_close exceeded) or raised. Surface as a
                # timed_out-shaped result so the rest of the flow handles
                # it like an in-activity timeout.
                workflow.logger.warning(
                    "alert_run_investigation_raised title=%s exc=%s",
                    title,
                    str(exc)[:200],
                )
                inv_result = {
                    "status": "timed_out",
                    "output": "",
                    "session_id": "",
                    "branch": "",
                    "branches": {},
                    # Deliberate no-fallback: the activity already burned its
                    # full budget; a second full attempt would double the walltime.
                    "engine": "",
                }

            # Fallback: a non-org (kimi) run that didn't succeed gets one retry
            # with the claude CLI (personal login) before degrading to LLM-only.
            # A separate activity so it has its own TIMEOUT_CLAUDE budget. Org
            # repos already ran claude (engine=="claude") — no point retrying.
            if inv_result.get("status") != "succeeded" and inv_result.get("engine") == "kimi":
                workflow.logger.warning(
                    "alert_kimi_failed_retry_claude title=%s status=%s",
                    title,
                    inv_result.get("status"),
                )
                try:
                    fb_result = await workflow.execute_activity_method(
                        AlertActivities.run_investigation,
                        args=[alert, resources_list, knowledge_context, "claude", not _is_infra],
                        start_to_close_timeout=TIMEOUT_CLAUDE,
                        heartbeat_timeout=timedelta(minutes=2),
                        retry_policy=NO_RETRY,
                    )
                    if fb_result.get("status") == "succeeded":
                        inv_result = fb_result
                except Exception as exc:
                    workflow.logger.warning(
                        "alert_claude_fallback_raised title=%s exc=%s",
                        title,
                        str(exc)[:200],
                    )

            inv_status = inv_result.get("status")
            if inv_status == "succeeded":
                investigation_output = inv_result.get("output", "")
                investigation_source = "kimi"
            elif is_jira and inv_status == "timed_out":
                # Jira scoping ran but kimi never wrote its STATUS footer
                # before TIMEOUT_CLAUDE. Take the partial transcript as the
                # scoping summary and skip Haiku — feeding partial output to
                # the LLM-fallback investigate() would throw away whatever
                # tool-grounded findings kimi did produce.
                investigation_output = inv_result.get("output", "")
                investigation_source = "kimi_partial"
                workflow.logger.warning(
                    "alert_kimi_partial_jira_scoping title=%s output_chars=%d",
                    title,
                    len(investigation_output),
                )
            else:
                # Kimi launch/run failed (missing repo checkout,
                # no code-bearing resource, timeout on non-Jira, etc.).
                # Feeding the error string to Haiku as "investigation_output"
                # makes it hallucinate confident verdicts from path-not-found
                # errors. Fall back to a real LLM-only investigation instead.
                workflow.logger.warning(
                    "alert_kimi_failed_falling_back_to_llm title=%s status=%s output=%s",
                    title,
                    inv_status or "unknown",
                    str(inv_result.get("output", ""))[:200],
                )

        if investigation_source == "llm":
            # LLM fallback — no code-bearing resource matched, or kimi failed.
            # Does NOT fire for kimi_partial; that path skips assess entirely.
            system_prompt = ""
            if knowledge_context:
                system_prompt = f"Prior knowledge context: {knowledge_context}\n\n"
            try:
                inv_result = await workflow.execute_activity_method(
                    AlertActivities.investigate,
                    args=[alert, system_prompt],
                    start_to_close_timeout=TIMEOUT_LLM,
                    retry_policy=NO_RETRY,
                )
            except Exception as exc:
                # Both kimi (above) and the LLM fallback have now failed, so the
                # flow can produce NO verdict at all. Before this guard the
                # workflow simply died here (NO_RETRY) — the track-task was left
                # at the "investigation has begun" note (Step 4.5) with no
                # closure, stranding the task forever. Post an explicit closure
                # note and return a terminal status so the user knows the
                # investigation aborted and the task is still theirs to action.
                workflow.logger.warning(
                    "alert_llm_fallback_failed_no_verdict title=%s exc=%s",
                    title,
                    repr(exc)[:200],
                )
                await self._safe_post_note(
                    track_task_id or "",
                    "⚠️ I couldn't complete the investigation "
                    "(the analysis step failed). The task is still open "
                    "for you.",
                )
                return {
                    "status": "investigation_failed",
                    "task_id": None,
                    "todoist_task_id": track_task_id,
                    "verdict": None,
                    "resource": resource_title,
                    "investigation": "",
                }
            investigation_output = inv_result.get("investigation", "")

        # ── Step 7: Haiku assessment (skipped on Jira partial output) ──
        if investigation_source == "kimi_partial":
            # Synthesize the verdict from kimi's partial transcript. Haiku
            # has no way to assess a half-finished scoping — and the lesson
            # in cmemory `ad86df08` warns that feeding partial kimi output to
            # Haiku leads to confidently-hallucinated verdicts.
            partial_summary = (investigation_output or "").strip()[-800:]
            verdict = {
                "status": "actionable",
                "root_cause": partial_summary,
                "suggested_fix": (
                    "⚠ Scoping timed out — review the partial findings above before acting."
                ),
                "confidence": 0.4,
            }
        else:
            try:
                verdict = await workflow.execute_activity_method(
                    AlertActivities.assess_investigation,
                    args=[alert, investigation_output],
                    start_to_close_timeout=TIMEOUT_LLM,
                    retry_policy=RETRY_ONCE,
                )
            except Exception as exc:
                # assess_investigation runs the verdict LLM (model_balanced /
                # qwen3:14b), which routinely exceeds TIMEOUT_LLM under proxy
                # load — every attempt hangs to the StartToClose ceiling. Rather
                # than failing the whole investigation with a bare "Activity
                # task timed out" (the user gets the "investigation has begun"
                # note and nothing else), degrade gracefully: emit an
                # inconclusive verdict that carries investigate()'s already-
                # successful output so the user still gets an actionable card.
                workflow.logger.warning(
                    "alert_assess_investigation_failed degraded_to_inconclusive exc=%s",
                    repr(exc)[:200],
                )
                verdict = {
                    "status": "inconclusive",
                    "root_cause": (investigation_output or "").strip()[:400],
                    "suggested_fix": "",
                    "confidence": 0.0,
                }
        verdict_status = verdict.get("status", "actionable")

        # ── Step 7a: Reconcile verdict against produced fix branches ──
        # If kimi actually committed a fix (non-empty `branches`), the run has
        # an actionable outcome by construction — a PR can be staged from it.
        # A non-actionable verdict status (inconclusive / not_actionable) in
        # that case is self-contradicting: the user saw both "1 PR staged" and
        # "the evidence is too thin to call" on the same run. Promote the
        # status to "actionable" so the Gate-2 prompt, the final track-task
        # comment (Step 8.5) and the chat ping (Step 9) all render an
        # actionable / PR outcome consistent with the staged fix.
        if (inv_result.get("branches") or {}) and verdict_status in (
            "inconclusive",
            "not_actionable",
        ):
            workflow.logger.info(
                "alert_verdict_reconciled_to_actionable from=%s title=%s",
                verdict_status,
                title,
            )
            verdict_status = "actionable"
            verdict["status"] = "actionable"

        # ── Step 7b: Persist verdict + transcript to the KG ──
        # Closes the cross-recall loop: future investigations of the
        # same resource / alert family can ask the knowledge graph for
        # prior diagnoses instead of re-deriving from scratch. Pre-fix,
        # only the LLM-fallback `investigate()` path ingested; the
        # kimi path's findings were lost between flows.
        try:
            await workflow.execute_activity_method(
                AlertActivities.record_verdict_to_kg,
                args=[alert, verdict, investigation_output],
                start_to_close_timeout=TIMEOUT_STANDARD,
                retry_policy=NO_RETRY,
            )
        except Exception:
            workflow.logger.warning("alert_record_verdict_to_kg_failed")

        # ── Step 7.5: Gate 2 — post-verdict decision gate ──
        # A card only when there is a decision (#500): a fix branch to open,
        # proposed commands to run, an escalating alert that nags until acked,
        # or a restart that did not stick (#501). See `gate2_needs_decision`.
        # Anything else is told rather than asked: the verdict comment on the
        # task (Step 8.5), the problem's timeline (Step 10) and the chat ping
        # (Step 9) — what an `ack` used to lead to, without the ack. Before
        # #500 every non-Jira, non-resolved verdict got a card (2026-05-22,
        # when chat had no verdict at all), and 27 of 38 answers were a bare
        # `ack`. "Mute 24h" for such a problem is on the admin Problems page.
        #
        # Options vary by context:
        #   • If kimi committed fixes (branches present): "Open PR(s)"
        #     is offered first + "Discard" is offered.
        #   • Always: "Mute 24h" and "Acknowledge" so the user can
        #     dispose of the alert without leaving chat.
        #
        # Source-gated: `todoist-jira` runs are scoping-only by contract
        # (the prompt says "Do NOT commit fixes. Do NOT create branches");
        # they go straight to the Todoist verdict comment + lean chat
        # info ping. No action gate for them.
        branches = inv_result.get("branches") or {}
        gate_skipped = is_jira or verdict_status == "resolved"
        # Human-approved remediation: an infra investigation asked to end its
        # transcript in a PROPOSED_COMMANDS: footer (Step 5.5's hint) may have
        # produced one. Parsed here (deterministic, workflow-safe) so both the
        # prompt and the options below can react to it. Never populated for
        # app-code alerts — running arbitrary commands only makes sense on the
        # infra-gitops host.
        proposed_cmds: list[str] = (
            extract_proposed_commands(investigation_output) if _is_infra else []
        )
        # Asked only when the answer can change something, so a run that does
        # carry a decision replays exactly as it did before the patch.
        no_decision_card = (
            not gate_skipped
            and not gate2_needs_decision(
                branches=branches,
                proposed_cmds=proposed_cmds,
                escalate=_escalate,
                restart_repeat=restart_repeat is not None,
            )
            and workflow.patched(_PATCH_NO_CARD)
        )
        if no_decision_card:
            gate_skipped = True
            workflow.logger.info("alert_gate2_no_decision_no_card verdict=%s", verdict_status)
        if not gate_skipped:
            # assess_investigation returns {status, root_cause, suggested_fix,
            # confidence}. The earlier `summary`/`severity`/`title` fallback
            # keys never landed in the verdict dict — they were dead reads.
            verdict_summary = (verdict.get("root_cause") or "")[:400]
            suggested_fix = (verdict.get("suggested_fix") or "")[:200]
            if branches:
                branch_summary = "\n".join(
                    f"  • {repo}: {branch}" for repo, branch in branches.items()
                )
                n = len(branches)
                prompt = (
                    f"🔧 <b>Fix proposed across {n} repo(s)</b>\n\n"
                    f"{branch_summary}\n\n"
                    f"Severity: {severity} · "
                    f"Confidence: {verdict.get('confidence', 0.0):.2f}\n"
                    f"{_html_escape(verdict_summary)}"
                )
            else:
                head_label = {
                    "actionable": "🔎 Investigation — actionable",
                    "inconclusive": "❓ Investigation — inconclusive",
                    "not_actionable": "ℹ️ Investigation — no action needed",
                }.get(verdict_status, "🔎 Investigation complete")
                prompt = (
                    f"<b>{head_label}</b>\n\n"
                    f"<b>{_html_escape(title)}</b>\n"
                    f"Severity: {severity} · Source: {source}"
                )
                if verdict_summary:
                    prompt += f"\n\nRoot cause: {_html_escape(verdict_summary)}"
                if suggested_fix:
                    prompt += f"\nSuggested: {_html_escape(suggested_fix)}"
                if proposed_cmds:
                    cmd_lines = "\n".join(
                        f"  <code>{_html_escape(c)}</code>" for c in proposed_cmds
                    )
                    prompt += f"\n\nProposed fix commands:\n{cmd_lines}"
            if restart_repeat is not None:
                prompt = _restart_repeat_card(restart_repeat) + "\n\n" + prompt

            options: dict[str, str] = {}
            if branches:
                options["open_all_prs"] = f"📝 Open {len(branches)} PR(s)"
            if proposed_cmds:
                options["run_fix"] = "🔧 Run fix"
            options["mute_24h"] = "🔕 Mute 24h"
            options["ack"] = "✅ Acknowledge"
            if branches:
                options["discard"] = "🗑 Discard"

            gate_input = InteractionFlowInput(
                agent_id=agent_id,
                kind="choice",
                origin="alert_approve_pr",
                prompt=prompt,
                options=options,
                timeout_seconds=172800,  # 48h
                timeout_policy="archive",
                # Escalating alerts nag the owner every 3 min (up to 10×) with an
                # @-mention until acked; non-escalating alerts pass metadata=None
                # so InteractionFlow's escalation loop is a no-op (byte-identical
                # to the pre-escalation single-wait behaviour).
                metadata=(
                    {
                        "escalation": {
                            "interval_minutes": 3,
                            "mention_id": owner_mention,
                            "max_repeats": 10,
                        }
                    }
                    if _escalate
                    else None
                ),
            )
            gate_id = (
                f"gate2-{_safe_workflow_id_segment(alert.get('fingerprint') or '')}"
                f"-{workflow.info().workflow_id}"
            )
            await self._record(
                problem_id,
                "waiting_human",
                f"Decision card posted: {verdict_status}.",
                step="gate2",
            )
            if not _escalate:
                # Unchanged path: spawn + await the decision card. In-flight
                # prod runs replay through exactly this branch.
                g2 = await workflow.execute_child_workflow(
                    InteractionFlow.run, gate_input, id=gate_id
                )
            else:
                # Escalating alert: race the decision card against the alert
                # self-resolving. Ask the hub every 3 min; if the problem
                # resolved while we await the human, auto-close the card
                # (signal self_resolved) so we stop nagging the owner about an
                # alert that already recovered.
                handle = await workflow.start_child_workflow(
                    InteractionFlow.run, gate_input, id=gate_id
                )
                gate_task = asyncio.ensure_future(handle)
                while True:
                    # workflow.wait is the deterministic, replay-safe equivalent
                    # of asyncio.wait (same (done, pending) return; no raise on
                    # timeout). The 180s timeout becomes a workflow timer.
                    # return_when is explicit (default is the same
                    # ALL_COMPLETED-vs-FIRST_COMPLETED distinction, moot for a
                    # single-future set, but the intent — stop waiting as soon
                    # as EITHER the gate resolves or the timer fires — should
                    # not depend on reading workflow.wait's default).
                    done, _ = await workflow.wait(
                        {gate_task}, timeout=180, return_when=asyncio.FIRST_COMPLETED
                    )
                    if gate_task in done:
                        g2 = gate_task.result()
                        break
                    # A raising recheck (activity exhausted its retries) must
                    # never propagate and kill the pending gate — treat it the
                    # same as "not resolved yet" and keep waiting for the next
                    # tick or the human's decision.
                    try:
                        recheck = await workflow.execute_activity_method(
                            HubActivities.problem_status,
                            args=[problem_id],
                            start_to_close_timeout=TIMEOUT_FAST,
                            retry_policy=FAST,
                        )
                    except Exception as exc:
                        workflow.logger.warning(
                            "alert_gate2_recheck_failed_keep_waiting fingerprint=%s err=%s",
                            fingerprint,
                            str(exc)[:200],
                        )
                        continue
                    if recheck.get("resolved"):
                        await handle.signal(
                            InteractionFlow.submit_response,
                            {"value": "self_resolved", "note": "auto-closed: alert resolved"},
                        )
                        g2 = await gate_task
                        break
            # Mirror Gate-1's archived-treatment: a 48h-ignored Gate-2 means
            # the user never decided. Don't fall through to the regular
            # verdict ping — drop a skip-comment on the track-task and short
            # out so we don't surface a "verdict complete" ping for an
            # archived decision the operator hasn't engaged with.
            if getattr(g2, "status", None) == "archived":
                workflow.logger.info(
                    "alert_gate2_archived_treating_as_skip fingerprint=%s",
                    fingerprint,
                )
                await self._safe_post_note(
                    track_task_id or "",
                    "⏭ Gate-2 archived (no decision in 48h). Skipping verdict ping.",
                )
                return {
                    "status": "gate2_archived",
                    "task_id": None,
                    "todoist_task_id": track_task_id,
                }
            v2 = ((g2.response or {}).get("value") or "").strip()
            if v2 == "self_resolved":
                # The self-resolve race auto-closed the card because the alert
                # recovered while we awaited the human. Log for dedup and short
                # out — there's no decision to act on.
                await self._safe_post_note(
                    track_task_id or "",
                    "✅ Self-resolved while awaiting your decision — card closed automatically.",
                )
                await self._record(
                    problem_id,
                    "resolved",
                    "Self-resolved while awaiting the decision card.",
                    step="self_resolved_during_gate",
                )
                return {
                    "status": "self_resolved_during_gate",
                    "task_id": None,
                    "problem_id": problem_id,
                    "todoist_task_id": track_task_id,
                }
            if v2 == "run_fix" and proposed_cmds:
                # A free-text note on the card overrides the parsed commands
                # (one command per line) — lets the operator correct/replace
                # what the LLM proposed without re-running the investigation.
                # No line here re-checks length/count: run_remediation_commands
                # applies the same caps (_MAX_REMEDIATION_COMMANDS /
                # _MAX_REMEDIATION_CMD_CHARS) to whatever it's handed, note or
                # not, so the human-typed override can't bypass them either.
                note = ((g2.response or {}).get("note") or "").strip()
                cmds = (
                    [ln.strip() for ln in note.splitlines() if ln.strip()]
                    if note
                    else proposed_cmds
                )
                try:
                    exec_result = await workflow.execute_activity_method(
                        AlertActivities.run_remediation_commands,
                        args=[cmds, inv_result.get("host", "")],
                        # 12 min: 5 commands × 120s + slack headroom. The
                        # activity now heartbeats continuously (background
                        # heartbeater), so heartbeat_timeout stays STANDARD while
                        # start_to_close is the real budget for the whole
                        # sequence — a `service update --force` no longer gets
                        # killed mid-run and the audit row is preserved.
                        start_to_close_timeout=timedelta(minutes=12),
                        heartbeat_timeout=TIMEOUT_STANDARD,
                        retry_policy=NO_RETRY,
                    )
                except Exception as exc:
                    # The activity timed out or raised (NO_RETRY). Don't strand
                    # the human-approved run silently — post an explicit manual-
                    # verify note to task + chat and return a distinct status.
                    err = str(exc)[:200]
                    workflow.logger.warning(
                        "alert_remediation_activity_failed title=%s err=%s", title, err
                    )
                    fail_note = (
                        "⚠️ Remediation execution failed or timed out — verify host "
                        f"state manually: {err}"
                    )
                    await self._safe_post_note(track_task_id or "", fail_note)
                    await self._safe_send_message(
                        agent_id=agent_id,
                        message=(
                            f"<b>Remediation failed</b> — {_html_escape(title)}\n"
                            f"<pre>{_html_escape(fail_note[:1500])}</pre>"
                        ),
                        log_event="alert_remediation_notify_failed",
                    )
                    return {
                        "status": "remediation_failed",
                        "task_id": None,
                        "todoist_task_id": track_task_id,
                        "refused": "activity_error",
                    }
                if exec_result.get("refused"):
                    outcome_note = f"🚫 Remediation refused: {exec_result['refused']}"
                else:
                    ran = exec_result.get("ran") or []
                    ok = all(r.get("exit_code") == 0 for r in ran)
                    detail = "\n".join(
                        f"$ {r['command']}\n  exit={r['exit_code']} {(r['stdout'] or r['stderr'])[:300]}"
                        for r in ran
                    )
                    outcome_note = f"{'✅' if ok else '⚠️'} Ran {len(ran)} command(s):\n{detail}"
                await self._safe_post_note(track_task_id or "", outcome_note)
                await self._safe_send_message(
                    agent_id=agent_id,
                    message=f"<b>Remediation result</b> — {_html_escape(title)}\n"
                    f"<pre>{_html_escape(outcome_note[:1500])}</pre>",
                    log_event="alert_remediation_notify_failed",
                )
                if not exec_result.get("refused"):
                    await workflow.sleep(timedelta(seconds=180))
                    post_check = await workflow.execute_activity_method(
                        HubActivities.problem_status,
                        args=[problem_id],
                        start_to_close_timeout=TIMEOUT_FAST,
                        retry_policy=FAST,
                    )
                    verdict_note = (
                        "✅ Verified: alert resolved after remediation."
                        if post_check.get("resolved")
                        else "⚠️ Alert not yet showing resolved — the next heartbeat tick "
                        "confirms recovery; investigate further if it re-fires."
                    )
                    await self._safe_post_note(track_task_id or "", verdict_note)
                    await self._record(
                        problem_id,
                        "resolved" if post_check.get("resolved") else "waiting_human",
                        outcome_note,
                        step="run_fix",
                    )
                else:
                    await self._record(problem_id, "waiting_human", outcome_note, step="run_fix")
                return {
                    # A refused run (read-only host, no commands, ...) never
                    # executed anything — surface it distinctly in workflow_runs
                    # rather than mislabelling it "remediated".
                    "status": (
                        "remediation_refused" if exec_result.get("refused") else "remediated"
                    ),
                    "task_id": None,
                    "todoist_task_id": track_task_id,
                    "commands_ran": len(exec_result.get("ran") or []),
                    "refused": exec_result.get("refused"),
                }
            if v2 == "discard":
                await self._safe_post_note(
                    track_task_id or "",
                    voice_line(agent_id, "fix_discarded"),
                )
                # KG persistence is intentionally skipped: a user-discarded
                # fix shouldn't poison future recall as a "prior diagnosis".
                await self._record(
                    problem_id, "waiting_human", "Proposed fix discarded.", step="discard"
                )
                return {
                    "status": "gate2_discarded",
                    "task_id": None,
                    "problem_id": problem_id,
                    "todoist_task_id": track_task_id,
                }
            if v2 == "mute_24h":
                # Mute the problem for 24h — occurrences are still counted,
                # nothing is projected or investigated — then fall through to
                # the normal verdict-comment + chat-info path so the user
                # still has the full verdict on Todoist.
                try:
                    await workflow.execute_activity_method(
                        HubActivities.mute_problem,
                        args=[problem_id, 24, g2.interaction_id],
                        start_to_close_timeout=TIMEOUT_FAST,
                        retry_policy=FAST,
                    )
                except Exception:
                    workflow.logger.warning(
                        "alert_post_verdict_mute_failed problem_id=%s", problem_id
                    )
                await self._safe_post_note(
                    track_task_id or "",
                    "🔕 Muted 24h by user.",
                )
            elif v2 == "ack":
                await self._safe_post_note(
                    track_task_id or "",
                    "✅ Acknowledged by user.",
                )
            if v2 == "open_all_prs":
                verdict_summary = (verdict.get("root_cause") or "")[:400]
                # `branches` is keyed by the engine's `BRANCH: <repo_name>:`
                # footer, and engines write either the checkout-dir basename
                # or the GitHub repo name (the prompt doesn't pin which). A
                # checkout folder named differently from its GitHub repo made
                # the old basename-only index miss and silently open 0 PRs
                # (issue #270) — so index both names, case-insensitively.
                repo_to_github: dict[str, str] = {}
                repo_to_path: dict[str, str] = {}
                for r in resources_list:
                    rp = (r.get("resource_path") or "").rstrip("/")
                    if not rp:
                        continue
                    gh = r.get("github_repo", "")
                    gh_name = gh.rsplit("/", 1)[-1].lower() if gh else ""
                    for key in (rp.rsplit("/", 1)[-1].lower(), gh_name):
                        if key:
                            repo_to_github[key] = gh
                            repo_to_path[key] = rp
                pr_urls: list[str] = []
                for repo_name, branch_name in branches.items():
                    github_repo = repo_to_github.get(repo_name.lower(), "")
                    if not github_repo or not branch_name:
                        continue
                    pending_pr_id = await workflow.execute_activity(
                        "stage_pending_pr",
                        StagePendingPrInput(
                            interaction_id=g2.interaction_id,
                            problem_id=problem_id,
                            repo=github_repo,
                            branch=branch_name,
                            title="AEGIS-proposed fix",
                            body=verdict_summary,
                            diff="",
                            kimi_session_id=inv_result.get("session_id", ""),
                        ),
                        start_to_close_timeout=TIMEOUT_FAST,
                        retry_policy=FAST,
                    )
                    pr_result = await workflow.execute_activity_method(
                        AlertGovernanceActivities.create_github_pr,
                        CreateGithubPrInput(
                            pending_pr_id=pending_pr_id,
                            repo=github_repo,
                            branch=branch_name,
                            host=inv_result.get("host", ""),
                            repo_path=repo_to_path.get(repo_name.lower(), ""),
                        ),
                        # 5 min — the activity runs two SSH subprocess calls
                        # (git push, gh pr create) each bounded at 60s
                        # internally (alert_governance.py:160, 199). The old
                        # TIMEOUT_FAST=15s ceiling was below the first
                        # subprocess wait alone.
                        start_to_close_timeout=TIMEOUT_LONG,
                        retry_policy=NO_RETRY,
                    )
                    pr_url = pr_result.get("pr_url", "")
                    if pr_url:
                        pr_urls.append(pr_url)

                if pr_urls:
                    n = len(pr_urls)
                    links_html = "\n".join(
                        f"  • <a href='{u}'>{_html_escape(u)}</a>" for u in pr_urls
                    )
                    voice_head = voice_line(agent_id, "pr_opened", count=n)
                    # _safe_send_message logs raised exceptions AND ok=false
                    # body returns (HTML parse, rate-limit, bot offline). The
                    # `_safe_event` below is the operator-visible fallback so
                    # we don't entirely lose the signal.
                    await self._safe_send_message(
                        agent_id=agent_id,
                        message=f"<b>{_html_escape(voice_head)}</b>\n{links_html}",
                        log_event="pr_opened_notify_failed",
                    )
                    await self._safe_event(f"📝 {n} PR(s) opened for: {_html_escape(title)}")
                    # Close the loop: stamp the track-task with the PR URLs
                    # so the user sees the outcome on the Todoist task that
                    # spawned the investigation, not only in chat.
                    if track_task_id and not track_task_id.startswith("item-"):
                        links_plain = "\n".join(f"  • {u}" for u in pr_urls)
                        await self._safe_post_note(
                            track_task_id,
                            f"{voice_head}\n\n{links_plain}",
                        )
                    await self._record(
                        problem_id,
                        "fixing",
                        f"{n} PR(s) opened: " + ", ".join(pr_urls),
                        step="prs_opened",
                        payload={"pr_urls": pr_urls},
                    )
                elif branches:
                    # User approved PRs and fix branches exist, yet none
                    # opened (mapping miss or create_github_pr failure).
                    # Silence here read as success (issue #270) — say it
                    # failed and name the surviving branches instead.
                    branch_list = ", ".join(f"{r}:{b}" for r, b in branches.items())
                    workflow.logger.warning(
                        "alert_open_all_prs_zero_opened title=%s branches=%s",
                        title,
                        branch_list,
                    )
                    await self._safe_event(
                        f"⚠️ Open-PR approved but 0 of {len(branches)} PR(s) "
                        f"opened for: {_html_escape(title)}"
                    )
                    if track_task_id and not track_task_id.startswith("item-"):
                        await self._safe_post_note(
                            track_task_id,
                            "⚠️ Open-PR was approved but no PR could be opened. "
                            f"Fix branch(es) still exist on the run host: {branch_list}",
                        )
        # ── Step 8: Compute final status (no task creation in v3) ──
        final_status = "logged"

        if verdict_status == "resolved":
            final_status = "resolved"
        elif verdict_status == "not_actionable":
            final_status = "not_actionable"
        elif verdict_status == "inconclusive":
            final_status = "inconclusive"

        # Voice-line headings differ for Jira-scoping vs alert runs;
        # see voice.py templates (scoping_* vs investigation_*). A
        # [partial] suffix tags kimi_partial runs so the user can spot
        # timeout-driven verdicts.
        partial_suffix = " [partial]" if investigation_source == "kimi_partial" else ""

        # ── Step 8.5: Post final-comment on the track-task ──
        # The track-task was ensured at step 2.7; we drop a structured
        # comment summarising the verdict so the user can drive the next
        # action from inside Todoist. Full text — Todoist comments cap
        # at 16k chars, well above any verdict shape we produce, so we
        # do NOT truncate root_cause / suggested_fix here. The chat
        # message in Step 9 is the slim version; this is the full one.
        kimi_attachment: dict | None = None
        kimi_attachment_name: str = ""
        # Upload the kimi transcript (if any) so the verdict comment
        # carries it as an attachment. Best-effort: a failure to upload
        # downgrades to a text-only comment.
        kimi_output_file = inv_result.get("output_file", "")
        if (
            track_task_id
            and not track_task_id.startswith("item-")
            and investigation_source in ("kimi", "kimi_partial")
            and kimi_output_file
        ):
            try:
                upload_res = await workflow.execute_activity_method(
                    AlertActivities.upload_kimi_log,
                    args=[
                        kimi_output_file,
                        f"{(alert.get('fingerprint') or 'run')}-{workflow.info().workflow_id[-8:]}",
                        inv_result.get("host", ""),  # effective kimi host ("" ⇒ base host)
                    ],
                    start_to_close_timeout=TIMEOUT_STANDARD,
                    retry_policy=NO_RETRY,
                )
                if upload_res.get("ok"):
                    kimi_attachment = upload_res.get("file_attachment")
                    kimi_attachment_name = upload_res.get("file_name") or ""
                else:
                    workflow.logger.warning(
                        "alert_kimi_log_upload_failed error=%s",
                        str(upload_res.get("error"))[:200],
                    )
            except Exception as exc:
                workflow.logger.warning("alert_kimi_log_upload_raised exc=%s", str(exc)[:200])

        if track_task_id and not track_task_id.startswith("item-"):
            root_cause_full = (verdict.get("root_cause") or "").strip()
            suggested_fix_full = (verdict.get("suggested_fix") or "").strip()
            # Map (kind, final_status) → voice event key.
            kind_prefix = "scoping" if is_jira else "investigation"
            if final_status == "resolved":
                final_msg = voice_line(agent_id, "investigation_self_resolved")
            elif final_status == "not_actionable":
                head = voice_line(agent_id, f"{kind_prefix}_not_actionable")
                final_msg = f"{head}\n\nReason: {root_cause_full}"
            elif final_status == "inconclusive":
                head = voice_line(agent_id, f"{kind_prefix}_inconclusive")
                final_msg = head
            else:
                head = voice_line(agent_id, f"{kind_prefix}_actionable")
                head_with_partial = f"{head}{partial_suffix}" if partial_suffix else head
                final_msg = (
                    f"{head_with_partial}\n\n"
                    f"{'Summary' if is_jira else 'Root cause'}: {root_cause_full}\n"
                    f"{'Next step' if is_jira else 'Suggested fix'}: {suggested_fix_full}"
                )
            if kimi_attachment_name:
                final_msg = f"{final_msg}\n\n📎 Transcript: {kimi_attachment_name}"
            if no_decision_card:
                final_msg += (
                    "\n\nNothing here needs your decision, so I sent no card. "
                    "If it keeps coming back, mute it on the Problems page."
                )
            await self._safe_post_note(track_task_id, final_msg, file_attachment=kimi_attachment)

        # ── Step 9: chat notification ──
        # Lean format: title + status + 1-line preview + Todoist link.
        # The full verdict (root_cause + suggested_fix) lives untruncated
        # on the Todoist task comment (Step 8.5) — the chat message is a ping,
        # not the artifact. A short preview (~160 chars) lets the user
        # decide whether to open the task without scrolling.
        kind_prefix = "scoping" if is_jira else "investigation"
        preview_src = (verdict.get("root_cause") or "").strip()
        preview = _html_escape(preview_src[:160]) + ("…" if len(preview_src) > 160 else "")

        if final_status == "resolved":
            head = _html_escape(voice_line(agent_id, "investigation_self_resolved"))
        elif final_status == "not_actionable":
            head = _html_escape(voice_line(agent_id, f"{kind_prefix}_not_actionable"))
        elif final_status == "inconclusive":
            head = _html_escape(voice_line(agent_id, f"{kind_prefix}_inconclusive"))
        else:
            head_raw = voice_line(agent_id, f"{kind_prefix}_actionable")
            partial_label = " [partial]" if partial_suffix else ""
            head = _html_escape(f"{head_raw}{partial_label}")

        msg_parts = [
            f"<b>{head}</b>\n\n",
            f"<b>{_html_escape(title)}</b>\n",
            f"{severity} · {source}",
        ]
        if preview:
            msg_parts.append(f"\n{preview}")
        msg = "".join(msg_parts)

        if track_task_id and not track_task_id.startswith("item-"):
            # Modern Todoist deeplink. The legacy /showTask?id= path
            # 404s on app.todoist.com — caught 2026-05-21 from prod.
            task_url = f"https://app.todoist.com/app/task/{track_task_id}"
            msg = f"{msg}\n\n<a href='{task_url}'>Full verdict on Todoist →</a>"

        await self._safe_send_message(
            agent_id=agent_id,
            message=msg,
            log_event="alert_verdict_notify_failed",
        )

        # ── Step 9.5: Additive voice note (no-op unless AEGIS_TTS_ENABLED) ──
        # Pandora reads the verdict aloud. Plain spoken text, not HTML; the full
        # verdict still lives in chat + the Todoist task.
        voice_text = f"Investigation complete for {title}. Status: {final_status}."
        if preview_src:
            voice_text += f" {preview_src[:600]}"
        try:
            await workflow.execute_activity_method(
                DeliveryActivities.send_voice,
                args=[agent_id, voice_text],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=NO_RETRY,
            )
        except Exception as exc:
            workflow.logger.warning("alert_verdict_voice_failed err=%s", str(exc)[:200])

        # ── Step 10: Record the outcome on the problem ──
        # `resolved` closes the problem; anything else leaves it with the
        # human, who has the full report on the task — with no card too,
        # which is the state an `ack` used to leave it in.
        decision_card = not gate_skipped
        await self._record(
            problem_id,
            "resolved" if final_status == "resolved" else "waiting_human",
            f"{final_status}: {(verdict.get('root_cause') or '')[:300]}",
            step="final",
            payload={
                "verdict": verdict_status,
                "resource": resource_title,
                "investigation_source": investigation_source,
                "decision_card": decision_card,
            },
        )

        # %-args, not keywords: a stdlib LoggerAdapter raises TypeError on
        # `status=...` whenever INFO is enabled (test_no_logger_kwargs.py
        # scans single lines, so this multi-line call slipped past it).
        workflow.logger.info(
            "alert_investigation_complete status=%s verdict_status=%s",
            final_status,
            verdict_status,
        )

        emoji = {
            "resolved": "✅",
            "not_actionable": "ℹ️",
            "inconclusive": "❓",
            "logged": "📝",
        }.get(final_status, "✓")
        await self._safe_event(f"{emoji} Alert done: <b>{_html_escape(title)}</b> → {final_status}")

        return {
            "status": final_status,
            "problem_id": problem_id,
            "verdict": verdict,
            "resource": resource_title,
            "investigation": investigation_output[:500],
            "todoist_task_id": track_task_id,
            # Observability: make kimi-vs-fallback queryable in workflow_runs.
            # Before this, a silent degrade to the LLM-only path was invisible in
            # the DB and only surfaced by inspecting kimi run-file mtimes on node-a.
            "investigation_source": investigation_source,
            "kimi_attempted": kimi_attempted,
            "resource_source": resource.get("source"),
            # Whether a Gate-2 card went out (#500), and whether this run was
            # a problem back inside its restart window (#501): cards per
            # investigation, countable in workflow_runs.
            "decision_card": decision_card,
            "restart_repeat": restart_repeat is not None,
        }
