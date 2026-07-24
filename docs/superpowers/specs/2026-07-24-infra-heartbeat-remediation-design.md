# Proactive infra detection & remediation — design

**Date:** 2026-07-24
**Status:** Approved (pending spec review)
**Driver:** 2026-07-24 incident — two swarm nodes hard-reset (~2 min power flap); one came back with node-pinned services stuck `0/1`. AEGIS never alerted: Alertmanager's `NodeDown` self-resolved before firing (webhook skips `status=resolved`), the only internal poll (`ServiceDriftFlow`) runs every 4h, and the hand-captured Todoist task triggered nothing.

## Problem

Three seams in otherwise-working machinery:

1. **Detection is push-only and slow.** Node liveness is detected solely by an Alertmanager push into `POST /api/webhooks/alert` (`core/src/aegis/api/routes/webhooks.py`). A brief flap that self-resolves never fires; nothing in AEGIS polls `docker node ls`; the 4h `ServiceDriftFlow` is replica/OOM-only.
2. **Alerts don't reliably reach the user.** A single Slack card + Todoist comment; silence is indistinguishable from "seen and fine".
3. **Infra investigations can't act.** `AlertInvestigationFlow` passes `allow_fix=False` for infra (`worker/src/aegis_worker/flows/alert_investigation.py:866`) — output is a diagnosis and at most a draft PR against homelab-gitops, never "here is the command, approve to run it". A human-created Todoist task describing an incident is inert.

## Decisions (from brainstorming)

- **Autonomy:** notify + investigate, human approves fixes (Gate-style). Exception: the *existing, already-live* auto-remediation for `DockerServiceDown`/`ServiceDownProlonged` (`_REMEDIABLE_ALERTNAMES`, `activities/alerts.py:91` → `remediate_infra_service` → `docker service update --force`) is kept and becomes reachable from heartbeat-sourced alerts too. Approved explicitly.
- **Reachability:** escalate in Slack (re-ping with @-mention) until acked or self-resolved.
- **Approach:** extend AEGIS internally (no monitoring-stack changes, no external watchdog service) + a one-line healthchecks.io dead-man ping.

## Design

### 1. `InfraHeartbeatFlow` — fast liveness/convergence poll

New scheduled flow, cron every 2 min, seed row in `config/seed/activities.yaml`, `agent_id: pandoras-actor` (resolved via `infra` tag at runtime, per convention). Registered in `worker/__main__.py` behind `homelab_enabled` like the other homelab flows.

Per tick:

1. **Collect** — one activity calls `HomelabConnector` for:
   - `docker node ls --format json` (new connector method `list_nodes()`; same `docker --context` transport as existing methods);
   - existing service collection (`docker service ls`) for `replicas_actual < replicas_desired`.
2. **Compare** against the previous snapshot, stored in a single `settings` jsonb row (`infra_heartbeat_state`) read/written by activities (workflows can't hit the DB). No new table.
3. **Emit on transitions only** (steady state emits nothing — no per-tick spam):
   - node `Ready → Down` → synthetic alert `{alertname: NodeDown, source: aegis-heartbeat, labels: {node}}`;
   - node `Down → Ready` while any service cluster-wide is stuck `0/x` (node-pinned tasks may or may not report placement — don't try to attribute) → synthetic `{alertname: DockerServiceDown, source: aegis-heartbeat}` per stuck service (this routes into the existing auto-remediation);
   - node `Down → Ready`, everything converged → no alert (a resolved-on-arrival investigation for the earlier NodeDown closes it).
   Synthetic alerts start `AlertInvestigationFlow` as ABANDONED child workflows (same pattern as `SentryPollFlow`), workflow id keyed on `aegis-heartbeat-<alertname>-<node|service>` so Temporal id-dedup suppresses accidental duplicates. Existing flow-side dedup (24h audit-log fingerprint, signature → open `@pandora` task) applies unchanged.
4. **Dead-man ping** — fire-and-forget GET to a healthchecks.io URL from a settings key (`infra_heartbeat_ping_url`); skipped when unset. Covers "AEGIS's own node (pop-think-os) went down with the cluster" — healthchecks nags out-of-band when pings stop.

Failure posture: if the SSH collect fails N consecutive ticks (N=3), emit one synthetic `alertname: HeartbeatCollectFailed` alert — a broken heartbeat must itself alert, not silently stop. The dead-man ping is only sent on a *successful* tick, so healthchecks catches total AEGIS death while `HeartbeatCollectFailed` catches partial (SSH/leader) death.

Noise: ~720 runs/day added to `workflow_runs` — acceptable; existing `CleanupFlow` retention handles it. The `WorkflowRunRecorderInterceptor` event feed should skip no-op heartbeat ticks (only post on transitions) to keep the Telegram/system feed quiet.

### 2. Escalate-until-ack — `InteractionFlow` escalation policy

- New optional `escalation` object in interaction metadata: `{interval_minutes, mention: bool, max_repeats}`.
- `InteractionFlow` already awaits the `submit_response` signal; wrap in a loop: `workflow.wait_condition(..., timeout=interval)` — on each timeout, re-dispatch the card with an @-mention (Slack member id from settings key `slack_owner_member_id`). Stop on response or `max_repeats` (default 10).
- **Self-resolve cancels nagging:** in `AlertInvestigationFlow`, the infra gate is raced against a periodic `check_alert_resolved` recheck (loop: await child result with a 3-min timeout; on timeout re-check resolution; resolved → cancel the `InteractionFlow` child, post a "self-resolved" note, exit).
- Only critical infra alerts (heartbeat-sourced `NodeDown`, `HeartbeatCollectFailed`, and unrecovered `DockerServiceDown`) set `escalation`; all other interactions keep today's single-card behavior.

### 3. Approve-to-run remediation

- For infra alerts, the investigation prompt (kimi CLI via `run_investigation`, LLM fallback) is extended to require structured `proposed_commands`: list of `{host, command, rationale}`. The assessment activity (`assess_investigation`) extracts/validates them into the verdict payload.
- Infra Gate 2 options become **Run fix / Skip / Mute 24h**, card body shows the exact commands verbatim.
- On approve: new activity `run_remediation_commands` executes them via `RemoteScriptConnector.run_on_host` (arbitrary host cmd) or `HomelabConnector` (swarm ops), honoring `infra.read_only` (refuse + report, never bypass). Caps: ≤5 commands, ≤500 chars each, 120s timeout per command. stdout/stderr posted back to the same Slack thread + Todoist task note; then `check_alert_resolved` re-runs and the final verdict is reported. Every execution audit-logged (`action='remediation_executed'`, commands + exit codes in payload).
- **Edit path:** the existing Slack-card note input — an approve response carrying a note runs the note's command(s) *instead of* the proposal (human-authored, owner-authenticated; same caps and audit).
- `allow_fix=False` for the kimi *investigation run* is unchanged — the coding CLI still never mutates; all mutation goes through the gated `run_remediation_commands` path.

### 4. Reactive bridge — Todoist task → investigation

`ClarifyFlow` already spawns `AlertInvestigationFlow` for `APP-\d+` Jira tasks (`spawn_kind == "pandora_investigation"`, `flows/clarify.py:299`). Add an infra-incident detector to the same `_RuleSet` path in `activities/clarify.py`: an inbox task whose text matches infra-incident shape (node/service/swarm/down/unreachable + known node hostnames — sourced from the `infra` table at rule-build time, with a small static fallback list) → spawn an investigation with a synthetic alert built from the task text (`source: todoist-infra`), anchored to that `todoist_task_id` so all notes land on the user's own task. Picked up within the normal 5-min clarify tick. Follows the clarify-watermark rule: spawn counts as a terminal state (`interaction_spawned`).

### 5. Config & seeds

- `config/seed/activities.yaml`: `infra-heartbeat-2m` row (cron `*/2 * * * *`).
- `schedule_sync.py`: `_ACTIVITY_TYPE_MAP` entry `InfraHeartbeatFlow`.
- `worker/__main__.py`: register flow + activities (both explicit lists — note the two-list gotcha from PR #90).
- Settings keys (admin-editable via CONFIG_REGISTRY): `infra_heartbeat_ping_url`, `slack_owner_member_id`. State key `infra_heartbeat_state` is internal.
- No new migration expected (settings rows + existing tables); if audit payloads need an index later, that's a follow-up.

### 6. Testing

Existing patterns only (`ActivityEnvironment` + respx; `WorkflowEnvironment.start_time_skipping()` + `Worker`):

- Heartbeat: transition matrix — Ready→Down fires once; steady Down fires nothing more; Down→Ready+stuck fires `DockerServiceDown`; Down→Ready+converged fires nothing; 3× collect failure fires `HeartbeatCollectFailed`; dead-man ping only on success.
- Escalation: time-skipping test — unanswered gate re-dispatches with mention, stops at ack; stops at `max_repeats`; parent cancels child on self-resolve.
- Remediation: approve → commands executed, capped, audit-logged; `read_only` host refused; note-override replaces proposal; reject/skip executes nothing.
- Clarify bridge: infra-shaped task spawns investigation + bumps watermark; ordinary task untouched.

## Out of scope

- Prometheus/Alertmanager rule changes (approach B — rejected).
- Any standalone watchdog outside AEGIS (approach C — rejected; dead-man ping covers it).
- Widening the auto-remediation whitelist beyond the existing two alertnames.
- Auto-fix PRs to homelab-gitops config (existing draft-PR flow unchanged).

## Rollout

Ship behind existing gates: flow inert until the schedule seed lands and `homelab_enabled` is true (already true in prod). Escalation/mention inert until `slack_owner_member_id` set; dead-man inert until `infra_heartbeat_ping_url` set. Deploy = normal worker release.
