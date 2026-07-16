"""AlertInvestigationFlow — smart alert pipeline with verification delay.

Every run anchors to a Todoist task. Either the caller passes an existing
``alert.todoist_task_id`` (clarify-APP path: classify_one routed a Jira
ticket to pandora_investigation) or the flow creates one in step 2.7 via
``CaptureActivities.capture_to_inbox`` with `extra_labels=["@pandora"]`.

Pipeline:

1.   Skip if resolved on arrival
2.   Dedup check (24h window) via `audit_log` + signature key
2.5. Mute short-circuit (per-source/service/workflow key)
2.6. Ensure Todoist track-task exists (reuse `todoist_task_id` if given,
     else `capture_to_inbox(#alert, ..., ["@pandora"])`)
3.   Verification delay → re-check if alert self-resolved
4.   Resolve alert to resource (repo) via `resolve_alert_resource`
5.   Gather knowledge context (runbooks, prior incidents)
6.   Investigate: Kimi if `resource_path` available, else LLM fallback
7.   Haiku assessment → structured verdict (actionable / not_actionable /
     inconclusive / self_resolved)
8.   Apply verdict (label task, mute, stage PR — depending on verdict shape)
8.5. Post `[Pandora] Investigation complete — <verdict>` final-comment on
     the track-task via `AlertActivities.post_task_note`
9.   Notify via chat (links to the Todoist task)
10.  Log investigation to `audit_log`

Start-comment (`[Pandora] Investigation started`) is posted between
steps 2.7 and 3.
"""

from __future__ import annotations

import re
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from html import escape as _html_escape

    from aegis.personalities import voice_line

    from aegis_worker.activities.active_work import ActiveWorkActivities
    from aegis_worker.activities.agent_registry import AgentRegistryActivities
    from aegis_worker.activities.alert_governance import (
        AlertGovernanceActivities,
        CheckMuteInput,
        CreateGithubPrInput,
        StagePendingPrInput,
        WriteMuteInput,
    )
    from aegis_worker.activities.alerts import (
        AlertActivities,
        build_alert_signature,
        is_infra_alert,
    )
    from aegis_worker.activities.capture import CaptureActivities
    from aegis_worker.activities.delivery import DeliveryActivities
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


def _safe_workflow_id_segment(text: str, max_len: int = 60) -> str:
    """Replace characters illegal in Temporal workflow IDs with dashes."""
    return re.sub(r"[^a-zA-Z0-9._\-]", "-", text)[:max_len]


def _build_mute_key(alert: dict) -> str:
    source = alert.get("source", "")
    service = alert.get("service", "")
    labels = alert.get("labels") or {}
    subkey = labels.get("workflow", "")  # GH workflow_run → workflow name
    if not source or not service:
        return ""
    return f"{source}:{service}:{subkey}"


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

    async def _safe_check_active_work(self, alert: dict, repo: str) -> dict:
        try:
            return await workflow.execute_activity_method(
                ActiveWorkActivities.check_active_work,
                args=[alert, repo],
                start_to_close_timeout=TIMEOUT_STANDARD,
                retry_policy=RETRY_ONCE,
            )
        except Exception as exc:
            workflow.logger.warning("alert_active_work_check_failed err=%s", str(exc)[:200])
            return {"active": False, "reasons": []}

    async def _safe_remediate_infra(
        self, alert: dict, track_task_id: str, title: str, source: str
    ) -> dict | None:
        """Try a one-shot auto-restart for a remediable swarm-service alert.

        Returns a terminal flow result dict when the service recovered (so the
        caller returns immediately and skips the agentic investigation), or
        None to fall through to the normal investigation — either because the
        alert isn't a remediable class / no service name, or the restart was
        issued but the service didn't converge back to healthy.
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
            try:
                await workflow.execute_activity_method(
                    AlertActivities.accumulate_digest_item,
                    args=[{"type": "auto_remediated", "title": title, "source": source}],
                    start_to_close_timeout=TIMEOUT_FAST,
                    retry_policy=NO_RETRY,
                )
            except Exception:
                pass
            return {
                "status": "auto_remediated",
                "task_id": track_task_id,
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
        return None

    @workflow.run
    async def run(self, alert: dict) -> dict:
        title = alert.get("title", "Unknown")
        fingerprint = alert.get("fingerprint", "")
        severity = alert.get("severity", "unknown")
        source = alert.get("source", "unknown")

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

        # ── Step 1: Skip if resolved on arrival ──
        if alert.get("resolved"):
            workflow.logger.info("alert_resolved_skip title=%s", title)
            await self._safe_event(f"⏭ AlertInvestigation — resolved, skipping: {title}")
            return {"status": "skipped_resolved", "task_id": None}

        # ── Step 2: Dedup check ──
        if fingerprint:
            dedup = await workflow.execute_activity_method(
                AlertActivities.check_dedup,
                args=[fingerprint, 24],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=FAST,
            )
            if dedup.get("is_duplicate"):
                workflow.logger.info("alert_dedup_skip fingerprint=%s", fingerprint)
                await self._safe_event(f"⏭ AlertInvestigation — duplicate, skipping: {title}")
                return {"status": "skipped_duplicate", "task_id": None}

        # ── Step 2.5: Mute short-circuit ──
        mute_key = _build_mute_key(alert)
        if mute_key:
            muted = await workflow.execute_activity(
                "check_alert_mute",
                CheckMuteInput(mute_key=mute_key),
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=FAST,
            )
            if muted:
                workflow.logger.info(f"alert_muted mute_key={mute_key} title={title}")
                await self._safe_event(f"🔕 Muted: {_html_escape(title)}")
                return {"status": "muted", "task_id": None}

        # ── Step 2.65: Routing config — infra_cluster (#91) ──
        # is_infra_alert/build_alert_signature can't read Settings/DB from
        # workflow code, so fetch the configured cluster label once here via
        # a tiny activity. workflow.patched guards in-flight runs started
        # before this change so they keep replaying the pre-patch (env-only)
        # behavior instead of non-deterministically diverging mid-history.
        infra_cluster = ""
        if workflow.patched("infra-cluster-from-settings"):
            routing = await workflow.execute_activity_method(
                AlertActivities.get_alert_routing_config,
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=FAST,
            )
            infra_cluster = routing.get("infra_cluster") or ""

        # ── Step 2.7: Signature dedup — attach to existing open task ──
        # Sentry mints a new issue id per stack-frame variation, so
        # check_dedup (fingerprint-exact) lets each variation through.
        # build_alert_signature collapses variations onto a single key
        # like sentry-class:<service>:<error_class>. If an open @pandora
        # task is already bound to this signature, post a recurrence
        # note and skip a duplicate investigation. Caller-supplied
        # todoist_task_id (clarify-APP path) bypasses signature dedup —
        # the caller has explicitly anchored to a specific task.
        signature = build_alert_signature(alert, infra_cluster)
        if signature and not alert.get("todoist_task_id"):
            existing_task_id = await workflow.execute_activity_method(
                AlertActivities.find_open_task_for_signature,
                args=[signature],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=FAST,
            )
            if existing_task_id:
                workflow.logger.info(
                    "alert_signature_dedup_hit signature=%s task_id=%s fingerprint=%s",
                    signature,
                    existing_task_id,
                    fingerprint,
                )
                recurrence_note = (
                    f"⚠️ Another occurrence of this error class\n"
                    f"Title: {title[:200]}\n"
                    f"Fingerprint: {fingerprint or '-'}"
                )
                await self._safe_post_note(existing_task_id, recurrence_note)
                await workflow.execute_activity_method(
                    AlertActivities.record_signature_recurrence,
                    args=[signature],
                    start_to_close_timeout=TIMEOUT_FAST,
                    retry_policy=NO_RETRY,
                )
                # Also write the per-fingerprint dedup audit row so a
                # re-fire of THIS exact Sentry issue short-circuits at
                # step 2 (check_dedup) instead of repeating the signature
                # lookup.
                try:
                    await workflow.execute_activity_method(
                        AlertActivities.log_alert,
                        args=[alert],
                        start_to_close_timeout=TIMEOUT_FAST,
                        retry_policy=NO_RETRY,
                    )
                except Exception:
                    pass
                await self._safe_event(
                    f"🪞 AlertInvestigation — recurrence, attached to existing task: {_html_escape(title)}"
                )
                return {
                    "status": "skipped_signature_dedup",
                    "task_id": None,
                    "todoist_task_id": existing_task_id,
                    "signature": signature,
                    "verdict": None,
                    "resource": None,
                    "investigation": "",
                }

        # ── Step 2.8: Ensure todoist track-task exists ──
        # Either the caller passed an existing task_id (clarify-APP path)
        # OR we create one now in the Inbox tagged @pandora. The same
        # task receives start- and final-comments and shows up in the
        # user's Pandora filter while the investigation runs.
        track_task_id: str | None = alert.get("todoist_task_id") or None
        if not track_task_id:
            capture_title = title[:120]
            capture_description = (alert.get("description") or "")[:2000]
            capture_external_id = (
                f"alert-{fingerprint}"
                if fingerprint
                else f"alert-{_safe_workflow_id_segment(title)}"
            )
            try:
                track_task_id = await workflow.execute_activity_method(
                    CaptureActivities.capture_to_inbox,
                    args=[
                        "#alert",
                        capture_external_id,
                        capture_title,
                        capture_description,
                        ["@pandora"],
                    ],
                    start_to_close_timeout=TIMEOUT_FAST,
                    retry_policy=NO_RETRY,
                )
            except Exception as exc:
                workflow.logger.warning(
                    "alert_track_task_capture_failed alert=%s err=%s",
                    title,
                    str(exc)[:200],
                )
                track_task_id = None
            if track_task_id and track_task_id.startswith("item-"):
                # Outbox path — comments to a temp_id will fail, log and
                # carry on; final-comment will retry by then. The audit
                # row in todoist_capture_idempotency will be backfilled
                # with the real id once outbox drains.
                workflow.logger.info("alert_track_task_outbox temp_id=%s", track_task_id)
            # Bind signature → newly-created task so future variations
            # attach to this task instead of spawning duplicates. Skipped
            # for outbox temp_ids — the next occurrence will rebind once
            # the real task id lands.
            if signature and track_task_id and not track_task_id.startswith("item-"):
                await workflow.execute_activity_method(
                    AlertActivities.record_signature_new_task,
                    args=[signature, track_task_id],
                    start_to_close_timeout=TIMEOUT_FAST,
                    retry_policy=NO_RETRY,
                )

        # ── Step 3: Verification delay ──
        delay_result = await workflow.execute_activity_method(
            AlertActivities.get_verification_delay,
            args=[alert],
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=NO_RETRY,
        )
        delay_seconds = delay_result.get("delay_seconds", 0)

        if delay_seconds > 0:
            workflow.logger.info(
                "alert_verification_delay seconds=%d reason=%s",
                delay_seconds,
                delay_result.get("reason", ""),
            )
            await self._safe_event(
                f"⏳ AlertInvestigation — waiting {delay_seconds}s verification: {title}"
            )
            await workflow.sleep(timedelta(seconds=delay_seconds))

            # Re-check if alert self-resolved during the delay
            window_minutes = delay_seconds // 60 + 2
            resolved_check = await workflow.execute_activity_method(
                AlertActivities.check_alert_resolved,
                args=[fingerprint, window_minutes],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=FAST,
            )
            if resolved_check.get("resolved"):
                workflow.logger.info("alert_self_resolved fingerprint=%s", fingerprint)
                await self._safe_event(
                    f"✅ AlertInvestigation — self-resolved during delay: {title}"
                )
                await self._safe_post_note(
                    track_task_id or "",
                    voice_line(agent_id, "investigation_self_resolved"),
                )
                try:
                    await workflow.execute_activity_method(
                        AlertActivities.accumulate_digest_item,
                        args=[{"type": "self_resolved", "title": title, "source": source}],
                        start_to_close_timeout=TIMEOUT_FAST,
                        retry_policy=NO_RETRY,
                    )
                except Exception:
                    pass
                # Write a dedup record so a flapping alert (re-fires within
                # the dedup window) is short-circuited by check_dedup
                # instead of spinning up another full investigation.
                try:
                    await workflow.execute_activity_method(
                        AlertActivities.log_alert,
                        args=[alert],
                        start_to_close_timeout=TIMEOUT_FAST,
                        retry_policy=NO_RETRY,
                    )
                except Exception:
                    pass
                return {
                    "status": "self_resolved",
                    "task_id": None,
                    "todoist_task_id": track_task_id,
                    "verdict": None,
                    "resource": None,
                    "investigation": "",
                }

        # ── Step 4: Resolve to resource ──
        # Infra/swarm alerts (NodeDown, DockerServiceDown, cluster=homelab-swarm, ...)
        # have no application code repo. Resolve them deterministically to
        # infra-gitops, skipping the LLM repo-match entirely.
        _is_infra = is_infra_alert(alert, infra_cluster)
        if _is_infra:
            # ── Step 4.0: Auto-remediation (force-restart) ──
            # A swarm service below desired replicas is usually a stuck/unplaced
            # task that an idempotent `service update --force` reschedules. Try
            # that one safe kick before the expensive agentic investigation; if
            # the service recovers we're done (and never burn the kimi budget).
            # Crash-loops are excluded by the activity — restarting them churns.
            remediation = await self._safe_remediate_infra(
                alert, track_task_id or "", title, source
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
                    # Raised from TIMEOUT_STANDARD (60s) to TIMEOUT_LLM (180s):
                    # gpt-oss:20b repo-match against ~160 resources peaks at 52s
                    # under normal load and grazes 60s under proxy pressure,
                    # causing all 3 FAST retries to time out → hard workflow
                    # failure. 180s gives 3× headroom over the observed p95.
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
        # storm is pure noise.
        _repo_from_human = False
        if resources_list and not _is_infra:
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
                    try:
                        await workflow.execute_activity_method(
                            AlertActivities.accumulate_digest_item,
                            args=[{"type": "not_actionable", "title": title, "source": source}],
                            start_to_close_timeout=TIMEOUT_FAST,
                            retry_policy=NO_RETRY,
                        )
                    except Exception:
                        pass
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
                _repo_from_human = True

        # ── Active-work guard: skip a repo that's under active work ──
        # Unless the operator just hand-picked the repo at Gate-0, check the
        # active-work signals (open PR / recent push / a matching due Todoist
        # task). If the repo is under active work, skip the investigation
        # entirely — the human is already on it. A human pick is an explicit
        # "investigate anyway", so it bypasses the guard. INFRA alerts never
        # reach the human pick, so the guard runs for them too (intended).
        # guard_repo is the github_repo slug when present, else the workspace
        # path (for a path-only repo the gh PR/push signals can't run and
        # degrade to no-signal, so the guard is best-effort/precision-only,
        # never a false skip).
        guard_repo = ""
        if resources_list:
            guard_repo = (
                resources_list[0].get("github_repo") or resources_list[0].get("resource_path") or ""
            )
        if guard_repo and not _repo_from_human:
            aw = await self._safe_check_active_work(alert, guard_repo)
            if aw.get("active"):
                reasons = "; ".join(aw.get("reasons") or [])
                await self._safe_event(f"⏸ Skipped {guard_repo} — under active work: {reasons}")
                if track_task_id:
                    await self._safe_post_note(
                        track_task_id,
                        f"⏸ Investigation skipped — {guard_repo} is under active work: {reasons}",
                    )
                try:
                    await workflow.execute_activity_method(
                        AlertActivities.accumulate_digest_item,
                        args=[{"type": "skipped_active_work", "title": title, "source": source}],
                        start_to_close_timeout=TIMEOUT_FAST,
                        retry_policy=NO_RETRY,
                    )
                except Exception:
                    pass
                return {
                    "status": "skipped_active_work",
                    "task_id": track_task_id,
                    "resolved_repo": guard_repo,
                    "active_work_reasons": aw.get("reasons") or [],
                }

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
            )
            if labels_str:
                infra_hint += f"\nAlert labels: {labels_str}"
            knowledge_context = (infra_hint + "\n\n" + knowledge_context).strip()

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
        # Fires for every non-Jira, non-self-resolved investigation, not
        # just the kimi-with-branches case the gate started life as
        # (2026-05-01 shape). Once kimi stopped reliably producing branches
        # — qwen3:14b assess timeouts, conservative grounding rule — the
        # user got "comments after comments on Todoist" with no chat
        # prompt to approve action. Bringing the gate back broadly
        # restores chat as the decision surface; Todoist remains the
        # record (2026-05-22 user ask).
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

            options: dict[str, str] = {}
            if branches:
                options["open_all_prs"] = f"📝 Open {len(branches)} PR(s)"
            options["mute_24h"] = "🔕 Mute 24h"
            options["ack"] = "✅ Acknowledge"
            if branches:
                options["discard"] = "🗑 Discard"

            g2 = await workflow.execute_child_workflow(
                InteractionFlow.run,
                InteractionFlowInput(
                    agent_id=agent_id,
                    kind="choice",
                    origin="alert_approve_pr",
                    prompt=prompt,
                    options=options,
                    timeout_seconds=172800,  # 48h
                    timeout_policy="archive",
                ),
                id=f"gate2-{_safe_workflow_id_segment(alert.get('fingerprint') or '')}-{workflow.info().workflow_id}",
            )
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
            if v2 == "discard":
                await self._safe_post_note(
                    track_task_id or "",
                    voice_line(agent_id, "fix_discarded"),
                )
                # The discard branch returned without logging — meaning the
                # alert never landed in audit_log, so a re-fire would not
                # be caught by step-2 dedup. Record it now. KG persistence
                # is intentionally skipped: a user-discarded fix shouldn't
                # poison future recall as a "prior diagnosis".
                try:
                    await workflow.execute_activity_method(
                        AlertActivities.log_alert,
                        args=[alert],
                        start_to_close_timeout=TIMEOUT_FAST,
                        retry_policy=NO_RETRY,
                    )
                except Exception:
                    pass
                return {
                    "status": "gate2_discarded",
                    "task_id": None,
                    "todoist_task_id": track_task_id,
                }
            if v2 == "mute_24h":
                # Mute the alert family for 24h, then fall through to the
                # normal verdict-comment + chat-info path so the user
                # still has the full verdict on Todoist.
                if mute_key:
                    try:
                        await workflow.execute_activity(
                            "write_alert_mute",
                            WriteMuteInput(
                                mute_key=mute_key,
                                ttl_seconds=86400,
                                reason="user_mute_24h_post_verdict",
                                created_by=g2.interaction_id,
                            ),
                            start_to_close_timeout=TIMEOUT_FAST,
                            retry_policy=FAST,
                        )
                    except Exception:
                        workflow.logger.warning(
                            "alert_post_verdict_mute_failed mute_key=%s", mute_key
                        )
                else:
                    workflow.logger.warning(
                        "alert_post_verdict_mute_unavailable_no_key title=%s", title
                    )
                    # Surface the silent-bail to the user's track-task so
                    # the empty-mute_key case isn't indistinguishable from
                    # a successful mute. Falls through to the normal
                    # verdict path below.
                    await self._safe_post_note(
                        track_task_id or "",
                        "🔕 Couldn't apply 24h mute (no mute_key); ignored",
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
                # `branches` is keyed by repo BASENAME (kimi's BRANCH: lines);
                # resource_path is workspace-relative and may be nested, so
                # index both lookups by its basename too.
                repo_to_github: dict[str, str] = {}
                repo_to_path: dict[str, str] = {}
                for r in resources_list:
                    rp = (r.get("resource_path") or "").rstrip("/")
                    if not rp:
                        continue
                    base_name = rp.rsplit("/", 1)[-1]
                    repo_to_github[base_name] = r.get("github_repo", "")
                    repo_to_path[base_name] = rp
                pr_urls: list[str] = []
                for repo_name, branch_name in branches.items():
                    github_repo = repo_to_github.get(repo_name, "")
                    if not github_repo or not branch_name:
                        continue
                    pending_pr_id = await workflow.execute_activity(
                        "stage_pending_pr",
                        StagePendingPrInput(
                            interaction_id=g2.interaction_id,
                            alert_fingerprint=alert.get("fingerprint", ""),
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
                            repo_path=repo_to_path.get(repo_name, ""),
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
        # ── Step 8: Compute final status (no task creation in v3) ──
        final_status = "logged"

        if verdict_status == "resolved":
            final_status = "resolved"
        elif verdict_status == "not_actionable":
            final_status = "not_actionable"
            try:
                await workflow.execute_activity_method(
                    AlertActivities.accumulate_digest_item,
                    args=[{"type": "not_actionable", "title": title, "source": source}],
                    start_to_close_timeout=TIMEOUT_FAST,
                    retry_policy=NO_RETRY,
                )
            except Exception:
                pass
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

        # ── Step 10: Log investigation ──
        try:
            await workflow.execute_activity_method(
                AlertActivities.log_alert,
                args=[alert],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=NO_RETRY,
            )
        except Exception:
            pass

        workflow.logger.info(
            "alert_investigation_complete",
            status=final_status,
            verdict_status=verdict_status,
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
        }
