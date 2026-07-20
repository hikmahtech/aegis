"""Alert investigation activities."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aegis.llm import parse_llm_json
from aegis.observability import log_audit, record_llm_call
from aegis.security import SPOTLIGHT_INSTRUCTION, assess_rule_of_two, spotlight
from temporalio import activity

# Cap on Kimi investigation output kept in the activity return value.
_INVESTIGATION_OUTPUT_CAP = 8 * 1024

# Kimi's prompt contract (see run_investigation prompt below) instructs it to
# end its final assistant turn with exactly one of these tokens. Treating the
# presence of any of these as "done" prevents the polling activity from
# returning on partial output and feeding Haiku a truncated transcript.
_KIMI_STATUS_RE = re.compile(
    r"^STATUS:\s*("
    # Production-alert RCA verbs (default path)
    r"investigated|insufficient_evidence|alert_unclear"
    # Jira-scoping verbs (alert.source == "todoist-jira" path)
    r"|scoped|needs_human|out_of_scope"
    r")\b",
    re.MULTILINE,
)

_KIMI_BRANCH_RE = re.compile(r"^BRANCH:\s*(.+)$", re.MULTILINE)

# alert.source value used by ClarifyActivities when a Todoist task matches
# ^APP-\d+: and is routed to AlertInvestigationFlow as a Jira-scoping run.
# Kimi's prompt + Haiku's verdict mapping both branch on this value.
_JIRA_SOURCE = "todoist-jira"


# Alertnames that identify infra/swarm alerts. Checked lowercase.
# These alerts have no application code repo — route directly to infra-gitops.
INFRA_ALERTNAMES: frozenset[str] = frozenset(
    {
        "nodedown",
        "dockerservicedown",
        "lokidown",
        "criticalendpointdown",
        "postgresqldown",
        "clickhousedown",
        "prometheusdown",
        "alertmanagerdown",
        "tempordown",
        "gpucriticaltemperature",
        "dagster pipeline failure",
        "hostoutofmemory",
        "hostmemorylimitreached",
        "hostdiskspacefull",
        "hostdiskreadlatency",
        "hostdiskwritelatency",
        "containermemorylimitreached",
        "containerkilledbysigterm",
        "containerkilledbysigkill",
    }
)

# Slug / github_repo of the resource that infra alerts route to.
_HOMELAB_GITOPS_SLUG = "repo-infra-gitops"
_HOMELAB_GITOPS_REPO = "example/infra-gitops"

# Infra alert classes safe to auto-remediate with a `service update --force`.
# A force-restart reschedules a stuck/unplaced task (the DockerServiceDown /
# ServiceDownProlonged case). ServiceCrashLooping is deliberately EXCLUDED —
# restarting a crash-loop just churns it; that needs investigation, not a kick.
_REMEDIABLE_ALERTNAMES = frozenset({"dockerservicedown", "servicedownprolonged"})
# Recovery poll budget after the restart: 6 × 5s = 30s of convergence wait.
_REMEDIATE_POLLS = 6
_REMEDIATE_POLL_INTERVAL_S = 5

# Matches "owner/repo" shaped hints (e.g. "acme/brand-new-repo").
_HINT_REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")


def is_infra_alert(alert: dict, infra_cluster: str = "") -> bool:
    """Return True when the alert is an infrastructure / swarm alert.

    Matches on alertname (checked against INFRA_ALERTNAMES) OR on the
    `cluster` label equalling `infra_cluster` (Settings.infra_cluster — admin
    Integrations page, AEGIS_INFRA_CLUSTER env fallback; blank ⇒ cluster
    matching is off). Callers pass the value explicitly — workflows fetch it
    once via AlertActivities.get_alert_routing_config since they can't read
    Settings/DB directly. Infra alerts have no application code repo and
    should be routed directly to infra-gitops instead of going through the
    LLM repo-match.
    """
    labels = alert.get("labels") or {}
    if not isinstance(labels, dict):
        labels = {}
    cluster = (labels.get("cluster") or "").strip()
    if infra_cluster and cluster == infra_cluster:
        return True
    alertname = (labels.get("alertname") or "").strip().lower()
    return alertname in INFRA_ALERTNAMES


def build_alert_signature(alert: dict, infra_cluster: str = "") -> str:
    """Derive a coarse cluster key for related alerts (beyond fingerprint).

    Sentry mints a fresh issue id for every stack-frame variation of the
    same underlying error, so check_dedup (which keys on the exact
    fingerprint) lets each variation through as a new investigation.
    This signature groups by (project_slug, error_class) so all the
    variations of e.g. paramiko `IncompatiblePeer` collapse onto a single
    open task. Returns "" when no stable signature can be derived; the
    caller then falls back to fingerprint-only dedup.

    alertmanager/prometheus/grafana re-fire each occurrence with a fresh
    `fingerprint`, so they bypass fingerprint dedup the same way and create
    duplicate tasks/investigations. They get a stable signature keyed on
    `service` + the alert's `alertname` label (or a slugified title), so all
    re-fires of e.g. a Dagster pipeline failure collapse onto one open task.

    Infra/swarm alerts (is_infra_alert) key the signature on
    `{source}-class:{cluster}:{alertname}` (NOT instance/service) so a
    NodeDown storm across node-b/node-a/node-d collapses to ONE signature and the
    dedup gate prevents duplicate investigations.
    """
    source = (alert.get("source") or "").strip()
    if source == "sentry":
        raw = alert.get("raw_payload") or {}
        if not isinstance(raw, dict):
            return ""
        metadata = raw.get("metadata") or {}
        if not isinstance(metadata, dict):
            return ""
        error_class = (metadata.get("type") or "").strip()
        service = (alert.get("service") or "").strip()
        if not error_class or not service:
            return ""
        return f"sentry-class:{service}:{error_class}"

    if source in {"alertmanager", "prometheus", "grafana"}:
        labels = alert.get("labels") or {}
        alertname = ""
        if isinstance(labels, dict):
            alertname = (labels.get("alertname") or "").strip()

        # Infra/swarm storm collapse: key on cluster+alertname, NOT instance/service,
        # so one outage (N nodes down) maps to ONE signature and one open task.
        if is_infra_alert(alert, infra_cluster):
            cluster = (labels.get("cluster") or "").strip() if isinstance(labels, dict) else ""
            subkey = alertname.lower()
            if not subkey:
                title = (alert.get("title") or "").strip()
                subkey = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40]
            return f"{source}-class:{cluster or 'infra'}:{subkey}"

        service = (alert.get("service") or "").strip()
        subkey = alertname.lower()
        if not subkey:
            title = (alert.get("title") or "").strip()
            slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
            subkey = slug[:40]
        if not service and not subkey:
            return ""
        return f"{source}-class:{service}:{subkey}"

    return ""


def _iter_kimi_assistant_text(raw: str):
    """Yield decoded text content from each assistant message in a coding-CLI
    stream-json output (kimi or claude).

    Both engines run in `--output-format stream-json`, so each non-empty line
    is a JSON event. Kimi assistant turns are flat:

        {"role":"assistant","content":[
          {"type":"think","text":"..."},
          {"type":"text","text":"...STATUS: scoped"}
        ]}

    Claude wraps the same shape one level down under "message":

        {"type":"assistant","message":{"role":"assistant","content":[
          {"type":"text","text":"...STATUS: scoped"}
        ]}}

    The STATUS/BRANCH lines the agent promises to emit live INSIDE one of
    those `text` fields. Searching the raw file with a multiline regex misses
    them because the `\\n` between log content and `STATUS:` is a JSON
    escape, not a real newline. Decoding via json.loads restores the real
    newlines, so regexes that key on `^STATUS:` (multiline) match again.

    Non-JSON lines (e.g. kimi's trailing "To resume this session: kimi -r
    <id>") and non-assistant events (role=tool, role=user, plain session
    init events, claude system/result events) are skipped — we only care
    about the agent's own assertions.
    """
    if not raw:
        return
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            evt = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if evt.get("role") != "assistant":
            # claude shape: assistant payload nested under "message"
            msg = evt.get("message")
            if not (isinstance(msg, dict) and msg.get("role") == "assistant"):
                continue
            evt = msg
        content = evt.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            text = block.get("text")
            if isinstance(text, str) and text:
                yield text


def _kimi_output_complete(raw: str) -> bool:
    """Return True when kimi's stream-json output contains its final STATUS:
    footer.

    Checks each assistant message's decoded text content (stream-json path)
    and also falls back to the raw input for plain-text scenarios — the
    fallback keeps simplified test fixtures working and is harmless in
    production where assistant-wrapped text dominates.
    """
    if not raw:
        return False
    for text in _iter_kimi_assistant_text(raw):
        if _KIMI_STATUS_RE.search(text):
            return True
    # Plain-text fallback for tests and any future kimi mode that doesn't
    # wrap output in JSON. The regex still requires `^STATUS:` at column 1
    # of a real line, so tool-result JSON noise won't false-positive.
    return bool(_KIMI_STATUS_RE.search(raw))


def _extract_kimi_transcript(raw: str) -> str:
    """Return a human-readable transcript of a kimi stream-json run.

    Concatenates every assistant-text block in order with blank-line
    separators. Falls back to the raw input when the file isn't
    stream-json (e.g. plain-text test fixtures). Skips JSON wrapping
    so the resulting text is greppable / pasteable.
    """
    if not raw:
        return ""
    chunks = list(_iter_kimi_assistant_text(raw))
    if chunks:
        return "\n\n".join(c.strip() for c in chunks if c.strip())
    return raw.strip()


def _parse_kimi_branches(raw: str, primary_repo: str) -> dict[str, str]:
    """Extract BRANCH: lines from kimi's stream-json output.

    Format kimi is asked to emit: `BRANCH: <repo_name>:<branch_name>` per
    repo where it committed a fix. Backward-compat: a `BRANCH: <branch>`
    line without a repo prefix is assigned to `primary_repo`.

    Like STATUS, BRANCH lines live inside assistant text; we extract them
    via `_iter_kimi_assistant_text` so the same stream-json wrapping
    doesn't hide them. Falls back to the raw input for plain-text test
    fixtures.
    """
    branches: dict[str, str] = {}

    def _ingest(match_text: str) -> None:
        for m in _KIMI_BRANCH_RE.finditer(match_text):
            value = m.group(1).strip()
            if ":" in value:
                repo_name, branch_name = value.split(":", 1)
                branches[repo_name.strip()] = branch_name.strip()
            else:
                branches[primary_repo] = value

    seen_assistant = False
    for text in _iter_kimi_assistant_text(raw):
        seen_assistant = True
        _ingest(text)
    if not seen_assistant:
        # Plain-text fallback
        _ingest(raw or "")
    return branches


def _build_alert_investigation_prompt(
    title: str,
    severity: str,
    description: str,
    runbook: str,
    fix_branch: str,
) -> str:
    """Production-alert RCA prompt (the original kimi prompt shape)."""
    # The alert title/description come from Sentry/alertmanager (untrusted) —
    # spotlight them so an injected instruction in an alert can't steer the
    # coding agent. Severity (enum) and runbook (ours) stay trusted.
    untrusted = f"Title: {title}\n"
    if description:
        untrusted += f"Description: {description[:500]}\n"
    prompt = (
        "Investigate this production alert. Use Shell, Read, Glob and other tools "
        "to gather concrete evidence — never speculate.\n\n"
        "You are working inside an isolated checkout of ONE repository. Work ONLY "
        "within your current working directory — do not read or modify files in "
        "other repositories or elsewhere on this host.\n\n"
        f"{SPOTLIGHT_INSTRUCTION}\n\n"
        f"Severity: {severity}\n\n"
        f"{spotlight(untrusted, kind='alert')}\n"
    )
    if runbook:
        prompt += f"\nRunbook (trusted):\n{runbook}\n"
    prompt += (
        "\nRules — read carefully:\n"
        "1. Every claim about the system MUST come from a tool call you actually made "
        "(file content, command output, log line). If you didn't observe it, do not claim it.\n"
        "2. If a needed file, command, log, or service is unavailable, errors out, or you "
        "lack access, STATE THAT explicitly. Do not paper over the gap.\n"
        "3. If you cannot gather enough evidence to identify a root cause, say so — do not "
        "invent one. The alert title and description are NOT evidence on their own.\n"
    )
    if fix_branch:
        prompt += (
            "4. If your evidence points to a clear, low-risk, LOCALIZED fix (a few lines, "
            "no schema changes, no broad refactor), IMPLEMENT it: create branch "
            f"`{fix_branch}`, commit with a message explaining the evidence, and output a "
            "line: BRANCH: <repo_name>:<branch_name>. The commit becomes a DRAFT PR a human "
            "reviews and approves before merge, so a focused, evidence-backed fix is "
            "welcome. But do NOT commit a speculative, broad, or risky change — if the root "
            "cause is unclear or the fix is non-trivial, report the diagnosis WITHOUT "
            "committing.\n"
        )
    else:
        prompt += "4. Do not propose or commit speculative fixes.\n"
    prompt += (
        "5. The LAST line of your output MUST be exactly one of:\n"
        "     STATUS: investigated\n"
        "     STATUS: insufficient_evidence: <what you could not check>\n"
        "     STATUS: alert_unclear: <what about the alert text was unactionable>\n"
        "   Use 'investigated' only when your RCA is grounded in tool observations.\n"
    )
    return prompt


def _build_jira_scoping_prompt(
    title: str,
    description: str,
    runbook: str,
) -> str:
    """Jira-ticket scoping prompt (alert.source == todoist-jira).

    Asks kimi to scope the work — affected files, suspected cause, suggested
    next step — rather than to root-cause an alert. No fix commits, no
    BRANCH: lines. Different STATUS verbs let the assess step distinguish
    a real scoping outcome from kimi's "I gave up" footer.
    """
    prompt = (
        "Scope this Jira ticket. Use Shell, Read, Glob and other tools to identify "
        "what code/configuration the ticket is about — never speculate.\n\n"
        "You are working inside an isolated checkout of ONE repository. Work ONLY "
        "within your current working directory — do not read or modify files in "
        "other repositories or elsewhere on this host.\n\n"
        f"Ticket: {title}\n"
    )
    if description:
        prompt += f"Description: {description[:1000]}\n"
    if runbook:
        prompt += f"\nContext from knowledge base:\n{runbook}\n"
    prompt += (
        "\nYour job is scoping, not fixing. Produce a short report covering:\n"
        "  - Affected files / modules (with paths)\n"
        "  - Suspected cause or what currently happens vs. what the ticket expects\n"
        "  - Suggested next step for whoever picks this up\n"
        "\nRules — read carefully:\n"
        "1. Every claim MUST come from a tool call you actually made (file content, "
        "command output, log line). If you didn't observe it, do not claim it.\n"
        "2. If a needed file, command, or repo is unavailable, STATE THAT explicitly. "
        "Do not paper over the gap.\n"
        "3. Do NOT commit fixes. Do NOT create branches. Scoping only.\n"
        "4. The LAST line of your output MUST be exactly one of:\n"
        "     STATUS: scoped\n"
        "     STATUS: needs_human: <what you couldn't determine without a human>\n"
        "     STATUS: out_of_scope: <why this ticket isn't actionable from code>\n"
        "   Use 'scoped' only when your report is grounded in tool observations.\n"
    )
    return prompt


def _decode_metadata(row: Any) -> dict:
    """Decode a resource row's `metadata` column to a dict.

    asyncpg's jsonb codec usually returns a dict already, but a string can
    slip through (legacy double-encoded rows); decode defensively and fall
    back to {} on anything unparseable.
    """
    m = row["metadata"]
    if isinstance(m, str):
        try:
            return json.loads(m)
        except Exception:
            return {}
    return m or {}


def _coding_match(rid: Any, title: Any, meta: dict, confidence: float) -> dict:
    """Build a resource-match dict carrying resource-scoped coding routing.

    `engine` ('claude'|'kimi'|'') and `claude_account` (a CLAUDE_CONFIG_DIR
    account label; kimi ignores it) come from the resource's metadata and let
    the caller pin the coding run's engine + profile per repo.
    """
    meta = meta if isinstance(meta, dict) else {}
    return {
        "resource_id": str(rid),
        "resource_title": title,
        "resource_path": meta.get("path"),
        "github_repo": (meta.get("github_repo") or "").strip(),
        "engine": (meta.get("engine") or "").strip().lower(),
        "claude_account": (meta.get("claude_account") or "").strip(),
        "confidence": confidence,
    }


@dataclass
class AlertActivities:
    """Activities for alert investigation."""

    db_pool: Any = None
    llm_client: Any = None
    knowledge_connector: Any = None
    remote_script: Any = None
    model_balanced: str = "qwen3:14b"
    # Remote path to the Kimi CLI binary on the investigation host.
    # Populated from settings.kimi_cli_binary_path at worker bootstrap.
    # Empty means Kimi investigations are disabled — run_investigation falls
    # back to a "not configured" result instead of a hardcoded default.
    kimi_binary: str = ""
    # CLAUDE_CONFIG_DIR used when run_investigation retries a failed non-org kimi
    # run with the claude CLI (engine_override="claude"). Empty ⇒ default config.
    claude_personal_config_dir: str = ""
    # Directory containing per-alert runbook Markdown files (<AlertName>.md).
    # Populated from settings.runbooks_dir at worker bootstrap.
    runbooks_dir: str = ""
    # TodoistConnector instance; wired post-construction in worker/__main__
    # because todoist_connector is initialised later in the bootstrap.
    # Used by post_task_note to attach start/final investigation comments
    # to the Todoist task that anchors each AlertInvestigationFlow run.
    todoist_connector: Any = None
    # HomelabConnector instance; wired at worker bootstrap. Used by
    # remediate_infra_service to force-restart a degraded swarm service and
    # poll it back to healthy. None ⇒ auto-remediation is a no-op (the flow
    # falls through to the normal investigation).
    homelab_connector: Any = None
    # Temporal Web UI base url + namespace, used by post_task_note to turn the
    # `Workflow run:` footer into a clickable link to the run's history page.
    # Populated from settings.temporal_ui_url / temporal_namespace at bootstrap;
    # empty url => the footer degrades to the plain (non-clickable) marker.
    temporal_ui_url: str = ""
    temporal_namespace: str = "default"
    # Prometheus `cluster` label that marks infra/swarm alerts. Injected from
    # Settings.infra_cluster (admin Integrations page; AEGIS_INFRA_CLUSTER
    # env fallback). Blank = cluster-label matching off.
    infra_cluster: str = ""

    @activity.defn
    async def get_alert_routing_config(self) -> dict:
        """Settings-derived routing knobs for the flow (workflows can't read
        Settings/DB — mirror of the AgentRegistryActivities pattern)."""
        return {"infra_cluster": self.infra_cluster}

    async def _effective_runbooks_dir(self) -> str:
        """Runbooks dir, DB-first: the infra coding block (via the connector)
        wins over the env-populated dataclass field. Defensive around test
        doubles/mocks without an awaitable coding_settings."""
        if self.remote_script is not None:
            try:
                dir_ = (await self.remote_script.coding_settings()).get("runbooks_dir")
                if dir_:
                    return dir_
            except Exception:  # noqa: BLE001 — mock/legacy connector
                pass
        return self.runbooks_dir

    def _read_runbook(self, alert_name: str, runbooks_dir: str | None = None) -> str:
        """Return the runbook Markdown for alert_name, or '' if absent/stub."""
        runbooks_dir = self.runbooks_dir if runbooks_dir is None else runbooks_dir
        if not runbooks_dir or not alert_name:
            return ""
        base = Path(runbooks_dir)
        # Try exact case first, then lowercase
        for candidate in (alert_name, alert_name.lower()):
            path = base / f"{candidate}.md"
            if path.exists():
                try:
                    content = path.read_text(encoding="utf-8").strip()
                except OSError:
                    return ""
                # Skip stub files — treat as "no runbook"
                if "TODO: fill in" in content:
                    return ""
                return content
        return ""

    @activity.defn
    async def post_task_note(
        self,
        task_id: str,
        content: str,
        file_attachment: dict | None = None,
        workflow_id: str | None = None,
        run_id: str | None = None,
    ) -> dict:
        """Attach a comment (Todoist `note_add`) to an existing task.

        Used by AlertInvestigationFlow to post the start- and final-
        comments that record the investigation status on the Todoist
        task that anchors the run. Returns {ok, error} — failures are
        swallowed by the flow because comment delivery is best-effort.

        `file_attachment` is the blob returned by `upload_kimi_log` /
        `TodoistConnector.upload_file()`. When supplied, the comment
        renders with a downloadable file in Todoist (web + mobile).

        `workflow_id` + `run_id` (from `workflow.info()`) append a
        `Workflow run: [<id>](<temporal-ui-url>)` footer so the comment
        links straight to the run's Temporal history page. The literal
        `Workflow run:` token is always present (clickable or plain) so
        clarify's loop-guard SQL keeps excluding these machine notes.

        Connector envelope: `{ok, data, error, retryable, external_ref}`.
        Envelope-ok merely means the HTTP call returned 200; the
        per-command status lives in `data.sync_status[uuid]`. A command
        rejected by Todoist (invalid item, malformed args) appears as
        envelope-ok with sync_status[uuid] = {"error": "..."}. Callers
        that only check the envelope get a false success — observed
        2026-05-20 on the first prod run where envelope-ok came back
        but no comment landed on the task. Treat per-command anything
        other than the literal string "ok" as failure.
        """
        if not self.todoist_connector or not task_id or not content:
            return {"ok": False, "error": "missing_connector_or_args"}
        from aegis.connectors.todoist import TodoistConnector

        if workflow_id:
            from aegis_worker.shared.temporal_links import workflow_run_footer

            footer = workflow_run_footer(
                self.temporal_ui_url, workflow_id, run_id or "", self.temporal_namespace
            )
            if footer:
                content = f"{content}\n\n{footer}"

        cmd = TodoistConnector.build_note_add_command(task_id, content, file_attachment)
        result = await self.todoist_connector.commands([cmd])
        status = TodoistConnector.check_sync_status(result, [cmd["uuid"]])
        if status["ok"]:
            return {"ok": True, "error": None}
        if status["envelope_error"]:
            activity.logger.warning(
                "post_task_note_envelope_failed task_id=%s error=%s",
                task_id,
                str(status["envelope_error"])[:200],
            )
            return {"ok": False, "error": status["envelope_error"]}
        rejected = status["rejected"].get(cmd["uuid"])
        activity.logger.warning(
            "post_task_note_command_rejected task_id=%s status=%s",
            task_id,
            str(rejected)[:200],
        )
        return {"ok": False, "error": f"command_rejected: {rejected}"}

    @activity.defn
    async def upload_kimi_log(
        self,
        output_file: str,
        filename_hint: str,
        host: str = "",
    ) -> dict:
        """Fetch the kimi stream-json output, extract its assistant text,
        and upload it to Todoist as a plain-text log.

        Returns `{ok, file_attachment, file_name, error}` — caller
        threads `file_attachment` into the next `post_task_note` call so
        the verdict comment renders with a viewable transcript.

        The full raw stream-json (~100KB–1MB+ per run, mostly tool
        results and JSON envelopes) is reduced to the assistant-text
        transcript, which is uploaded uncompressed so Todoist previews it
        inline instead of forcing a download+gunzip. The reduced
        transcript is well inside Todoist's per-upload cap.
        """
        if not self.remote_script or not self.todoist_connector or not output_file:
            return {
                "ok": False,
                "error": "missing_connector_or_output_file",
                "file_attachment": None,
                "file_name": "",
            }
        try:
            raw = await self.remote_script.fetch_kimi_run_output(output_file, host=host)
        except Exception as exc:
            activity.logger.warning(
                "upload_kimi_log_fetch_failed file=%s error=%s",
                output_file,
                str(exc)[:200],
            )
            return {
                "ok": False,
                "error": f"fetch_failed: {str(exc)[:200]}",
                "file_attachment": None,
                "file_name": "",
            }
        if not raw:
            return {
                "ok": False,
                "error": "empty_output",
                "file_attachment": None,
                "file_name": "",
            }
        transcript = _extract_kimi_transcript(raw)
        if not transcript:
            return {
                "ok": False,
                "error": "empty_transcript",
                "file_attachment": None,
                "file_name": "",
            }
        safe_hint = re.sub(r"[^A-Za-z0-9._-]+", "-", filename_hint or "kimi")[:80].strip("-")
        filename = f"kimi-{safe_hint or 'run'}.log"
        body = transcript.encode("utf-8")
        env = await self.todoist_connector.upload_file(
            filename=filename,
            content=body,
            content_type="text/plain",
        )
        if env.get("ok"):
            return {
                "ok": True,
                "error": None,
                "file_attachment": env.get("data"),
                "file_name": filename,
            }
        activity.logger.warning(
            "upload_kimi_log_upload_failed file=%s error=%s",
            output_file,
            str(env.get("error"))[:200],
        )
        return {
            "ok": False,
            "error": env.get("error") or "upload_failed",
            "file_attachment": None,
            "file_name": filename,
        }

    @activity.defn
    async def check_dedup(self, fingerprint: str, window_hours: int = 24) -> dict:
        """Check if this alert was recently investigated (dedup)."""
        if not self.db_pool or not fingerprint:
            return {"is_duplicate": False}

        row = await self.db_pool.fetchrow(
            "SELECT id FROM audit_log WHERE target_type = 'alert' AND target_id = $1 "
            "AND action = 'alert_investigated' "
            "AND created_at > NOW() - INTERVAL '1 hour' * $2 LIMIT 1",
            fingerprint,
            window_hours,
        )
        return {"is_duplicate": row is not None}

    @activity.defn
    async def find_open_task_for_signature(self, signature: str) -> str | None:
        """Return the task_id of the open @pandora task that owns this
        signature, or None if no open task is bound to it.

        Joins alert_dedup_index against todoist_tasks so a completed
        (is_completed=true) or deleted task falls through and the next
        occurrence creates a fresh task. A stale binding to a deleted
        Todoist task (missing from the projection) also falls through.
        """
        if not self.db_pool or not signature:
            return None
        row = await self.db_pool.fetchrow(
            """
            SELECT adi.task_id
            FROM alert_dedup_index adi
            JOIN todoist_tasks tt ON tt.id = adi.task_id
            WHERE adi.signature = $1 AND tt.is_completed = FALSE
            LIMIT 1
            """,
            signature,
        )
        return row["task_id"] if row else None

    @activity.defn
    async def record_signature_recurrence(self, signature: str) -> None:
        """Bump occurrence_count + last_seen_at for an existing signature.

        No-op when the row is missing (race against task deletion); the
        caller has already posted the recurrence note on the existing
        task and the next occurrence will rebind via record_signature_new_task.
        """
        if not self.db_pool or not signature:
            return
        await self.db_pool.execute(
            """
            UPDATE alert_dedup_index
            SET last_seen_at = now(),
                occurrence_count = occurrence_count + 1
            WHERE signature = $1
            """,
            signature,
        )

    @activity.defn
    async def record_signature_new_task(self, signature: str, task_id: str) -> None:
        """Bind a signature to a freshly-captured @pandora task.

        Upserts because the prior task for this signature may have been
        completed/deleted: in that case the index still holds the stale
        task_id and the next alert should rebind to the new one. The
        UPSERT resets first_seen_at and occurrence_count so the new task's
        recurrence stats start fresh.
        """
        if not self.db_pool or not signature or not task_id:
            return
        await self.db_pool.execute(
            """
            INSERT INTO alert_dedup_index
                (signature, task_id, first_seen_at, last_seen_at, occurrence_count)
            VALUES ($1, $2, now(), now(), 1)
            ON CONFLICT (signature) DO UPDATE SET
                task_id = EXCLUDED.task_id,
                first_seen_at = now(),
                last_seen_at = now(),
                occurrence_count = 1
            """,
            signature,
            task_id,
        )

    @activity.defn
    async def investigate(self, alert: dict, agent_system_prompt: str = "") -> dict:
        """Use LLM to investigate the alert and assess root cause."""
        title = alert.get("title", "Unknown")
        severity = alert.get("severity", "unknown")
        source = alert.get("source", "unknown")
        description = alert.get("description", "")
        labels = alert.get("labels", {})
        raw_payload = alert.get("raw_payload", {})

        # Derive service from labels if not explicitly set
        service = (
            alert.get("service", "")
            or labels.get("service", "")
            or labels.get("instance", "")
            or labels.get("job", "unknown")
        )

        # Build context sections
        sections = [
            f"Title: {title}",
            f"Source: {source}",
            f"Severity: {severity}",
            f"Service: {service}",
        ]
        if description:
            sections.append(f"Description: {description[:500]}")
        if labels:
            label_str = ", ".join(f"{k}={v}" for k, v in labels.items())
            sections.append(f"Labels: {label_str[:500]}")
        if raw_payload:
            sections.append(f"Raw payload:\n{str(raw_payload)[:2000]}")

        # The alert context (title/description/labels/raw payload) is untrusted
        # Sentry/alertmanager content — spotlight it as data, not instructions.
        prompt = (
            "Investigate this production alert and provide a brief assessment.\n\n"
            f"{SPOTLIGHT_INSTRUCTION}\n\n"
            + spotlight("\n".join(sections), kind="alert")
            + "\n\nProvide:\n1. Likely root cause (1-2 sentences)\n"
            "2. Is this actionable? (yes/no)\n"
            "3. Recommended fix (1-2 sentences)\n"
            "4. Can this be fixed automatically via code? (yes/no)"
        )

        if not self.llm_client:
            return {"investigation": "LLM not available", "actionable": True, "auto_fixable": False}

        start = time.monotonic()
        result = await self.llm_client.think(
            prompt,
            # Balanced (gpt-oss), not model_light (gemma4:e2b): this is the
            # last-resort verdict when no repo resolved or kimi failed. On gemma
            # it produced confident-sounding but ungrounded speculation that read
            # like a real verdict; gpt-oss gives an honest, usable assessment.
            model=self.model_balanced,
            system_prompt=agent_system_prompt
            or "You are a site reliability engineer investigating production alerts.",
        )
        latency_ms = int((time.monotonic() - start) * 1000)
        await record_llm_call(
            self.db_pool,
            model=result.get("model", self.model_balanced),
            prompt_tokens=result.get("prompt_tokens", 0),
            completion_tokens=result.get("completion_tokens", 0),
            latency_ms=latency_ms,
            purpose="alert_investigation",
            agent_id=alert.get("agent_id"),
        )

        investigation = result.get("response", "")
        lower = investigation.lower()
        actionable = any(
            w in lower for w in ["action", "fix", "restart", "investigate", "update", "deploy"]
        )
        auto_fixable = (
            "yes" in lower.split("automatically")[-1][:50] if "automatically" in lower else False
        )

        # Ingest findings into knowledge service
        if self.knowledge_connector and investigation:
            try:
                await self.knowledge_connector.ingest_content(
                    url=alert.get("url", f"aegis://alert/{alert.get('fingerprint', 'unknown')}"),
                    title=f"Alert investigation: {title}",
                    source_type="alert",
                    raw_text=investigation,
                    tags=["alert"],
                )
            except Exception as exc:
                activity.logger.warning("knowledge_ingest_failed: %s", str(exc))

        return {
            "investigation": investigation,
            "actionable": actionable,
            "auto_fixable": auto_fixable,
            "model": result.get("model"),
        }

    @activity.defn
    async def gather_alert_knowledge(self, title: str, project: str, alert_name: str = "") -> str:
        """Return runbook + KG prior-incident context for an alert.

        Prepends the static per-alert runbook (if one exists) before the
        knowledge-graph answer so the investigation model sees structured
        checklists before free-form history.
        """
        parts: list[str] = []

        runbook = self._read_runbook(alert_name, await self._effective_runbooks_dir())
        if runbook:
            parts.append(f"Runbook:\n{runbook}")

        if self.knowledge_connector:
            try:
                question = f"What do I know about: {title}"
                if project:
                    question += f" in {project}"
                question += "? Include any prior incidents or investigations."
                result = await self.knowledge_connector.ask(
                    question=question,
                    max_sources=5,
                    min_confidence=0.3,
                )
                kg = result.get("answer", "")
                if kg:
                    parts.append(f"Prior knowledge:\n{kg}")
            except Exception as exc:
                # Tier-1 KG cache miss → fall through to slower LLM resolution.
                # Logging makes the cache-miss visible so degraded KS is
                # diagnosable from worker_logs rather than from "why is
                # alert resolution slow?" downstream.
                activity.logger.warning("gather_alert_knowledge_kg_failed err=%s", str(exc)[:200])

        return "\n\n".join(parts)

    @activity.defn
    async def log_alert(self, alert: dict) -> None:
        """Log alert to audit_log for dedup tracking."""
        if not self.db_pool:
            return
        await log_audit(
            self.db_pool,
            actor=f"alert:{alert.get('source', 'unknown')}",
            action="alert_investigated",
            target_type="alert",
            target_id=alert.get("fingerprint", ""),
            details={
                "title": alert.get("title", "")[:100],
                "severity": alert.get("severity", ""),
            },
        )

    @activity.defn
    async def resolve_infra_resource(self, alert: dict) -> dict:
        """Deterministically resolve an infra/swarm alert to the infra-gitops resource.

        Infra alerts (alertmanager NodeDown, DockerServiceDown, etc.) have no
        application code repo — they should always investigate against the
        infra-gitops ansible/swarm config. This activity looks up the
        infra-gitops resource row directly (by slug or github_repo) and
        returns it in the same dict shape as resolve_alert_resource so the
        flow can proceed to run_investigation without going through the LLM
        repo-match or Gate-0.

        Falls back to the null-resource dict if the row is missing from the
        DB (→ LLM-only investigate() path instead of failing).
        """
        null_result = {
            "resource_id": None,
            "resource_title": None,
            "resource_path": None,
            "github_repo": "",
            "confidence": 0.0,
            "source": "none",
            "resources": [],
        }
        if not self.db_pool:
            return null_result
        try:
            row = await self.db_pool.fetchrow(
                "SELECT id, title, metadata FROM resources "
                "WHERE slug = $1 OR metadata->>'github_repo' = $2 "
                "LIMIT 1",
                _HOMELAB_GITOPS_SLUG,
                _HOMELAB_GITOPS_REPO,
            )
        except Exception as exc:
            activity.logger.warning(
                "resolve_infra_resource_db_failed err=%s", str(exc)[:200]
            )
            return null_result
        if not row:
            activity.logger.warning(
                "resolve_infra_resource_not_found slug=%s", _HOMELAB_GITOPS_SLUG
            )
            return null_result
        meta = _decode_metadata(row)
        matched = {
            "resource_id": str(row["id"]),
            "resource_title": row["title"],
            "resource_path": meta.get("path") or "",
            "github_repo": meta.get("github_repo") or _HOMELAB_GITOPS_REPO,
            "confidence": 1.0,
        }
        return {**matched, "source": "infra", "resources": [matched]}

    @activity.defn
    async def remediate_infra_service(self, alert: dict) -> dict:
        """Auto-remediate a swarm service that is below desired replicas by
        issuing an idempotent `service update --force`, then verifying it
        converged back to healthy.

        Only the DockerServiceDown / ServiceDownProlonged classes are eligible
        (see `_REMEDIABLE_ALERTNAMES`): a force-restart reschedules a stuck task
        but does not fix a genuine crash-loop. The service name comes from the
        alert labels — `service_name` (DockerServiceDown) or `service`
        (label_replace'd alerts) — falling back to the top-level `service`.

        Returns {attempted, recovered, service, command, output, reason}.
        `attempted=False` means nothing was done and the caller should proceed
        to the normal investigation. `recovered=True` means the service is back
        to running >= desired and no investigation is needed.
        """
        result: dict = {
            "attempted": False,
            "recovered": False,
            "service": "",
            "command": "",
            "output": "",
            "reason": "",
        }
        labels = alert.get("labels") or {}
        if not isinstance(labels, dict):
            labels = {}
        alertname = (labels.get("alertname") or "").strip().lower()
        if alertname not in _REMEDIABLE_ALERTNAMES:
            result["reason"] = f"not_remediable_class:{alertname or 'unknown'}"
            return result
        service = (
            labels.get("service_name")
            or labels.get("service")
            or alert.get("service")
            or ""
        ).strip()
        if not service:
            result["reason"] = "no_service_name"
            return result
        if not self.homelab_connector:
            result["reason"] = "no_homelab_connector"
            return result

        result["service"] = service
        result["attempted"] = True
        result["command"] = f"docker service update --force {service}"

        env = await self.homelab_connector.restart_service(service)
        if not env.get("ok"):
            result["reason"] = f"restart_failed:{str(env.get('error'))[:150]}"
            result["output"] = str(env.get("error") or "")[:300]
            activity.logger.warning(
                "remediate_infra_service_restart_failed service=%s error=%s",
                service,
                str(env.get("error"))[:200],
            )
            return result

        # Poll list_services for convergence (running >= desired, desired > 0).
        recovered = False
        for _ in range(_REMEDIATE_POLLS):
            await asyncio.sleep(_REMEDIATE_POLL_INTERVAL_S)
            activity.heartbeat()
            svc_env = await self.homelab_connector.list_services()
            if not svc_env.get("ok"):
                continue
            for s in svc_env.get("data") or []:
                if s.get("name") == service:
                    desired = s.get("replicas_desired") or 0
                    actual = s.get("replicas_actual") or 0
                    if desired > 0 and actual >= desired:
                        recovered = True
                    break
            if recovered:
                break

        result["recovered"] = recovered
        result["reason"] = "recovered" if recovered else "restart_issued_not_converged"
        activity.logger.info(
            "remediate_infra_service service=%s recovered=%s", service, recovered
        )
        return result

    @activity.defn
    async def resolve_alert_resource(self, alert: dict) -> dict:
        """Map an alert to matching resources using KG cache then LLM, with rule-based expansion.

        Returns backward-compatible top-level fields plus a 'resources' list for multi-repo
        investigation. source: "knowledge" | "llm" | "auto_registered" | "none"
        """
        null_result = {
            "resource_id": None,
            "resource_title": None,
            "resource_path": None,
            "github_repo": "",
            "confidence": 0.0,
            "source": "none",
            "resources": [],
        }

        if not self.db_pool:
            return null_result

        fingerprint = alert.get("fingerprint", "")
        kg_query = f"alert:{fingerprint} relates_to resource"

        # Tier 1: KG cache check. The KG only proves alert→resource_id —
        # path/github_repo are re-read from the CURRENT resources row, because
        # cached metadata goes stale when WorkspaceRepoSyncFlow moves a
        # checkout (flat → categorized path) or prunes the resource entirely.
        # A vanished row falls through to the live tiers below.
        if self.knowledge_connector:
            try:
                results = await self.knowledge_connector.search(kg_query, limit=3)
                for hit in results:
                    score = hit.get("score", 0)
                    content = hit.get("content", "")
                    metadata = hit.get("metadata", {}) or {}
                    if score >= 0.7 and "relates_to" in content and metadata.get("resource_id"):
                        row = await self.db_pool.fetchrow(
                            "SELECT id, title, metadata FROM resources WHERE id = $1::uuid",
                            metadata["resource_id"],
                        )
                        if not row:
                            activity.logger.info(
                                "resolve_alert_resource_kg_stale resource_id=%s",
                                metadata["resource_id"],
                            )
                            continue
                        row_meta = row.get("metadata") or {}
                        row_meta = row_meta if isinstance(row_meta, dict) else {}
                        # Respect the allow-list: a cached alert→resource link
                        # must not fire a coding run on a since-disabled repo.
                        if str(row_meta.get("coding_enabled") or "").lower() != "true":
                            continue
                        matched = _coding_match(row["id"], row["title"], row_meta, score)
                        return {**matched, "source": "knowledge", "resources": [matched]}
            except Exception as exc:
                # Tier-1 KG cache miss for resource resolution — fall through
                # to LLM. Log so KS flakiness is observable.
                activity.logger.warning(
                    "resolve_alert_resource_kg_lookup_failed err=%s", str(exc)[:200]
                )

        # Fetch candidate resources for LLM to choose from. When the alert
        # carries a `resource_tag_filter` (e.g. ["acme"] for APP-<n>:
        # Jira tickets), restrict candidates to resources whose `tags`
        # overlap the filter — otherwise the LLM gets dragged into picking
        # between every personality's repo.
        # ALLOW-LIST: alert investigation only ever acts on resources the user
        # has explicitly opted in for coding — kind='repository' AND
        # metadata.coding_enabled='true'. This keeps the LLM/service matcher
        # from dragging an auto-registered or unrelated resource into a live
        # shell+PR coding run. Mark a repo on the admin Resources page.
        coding_gate = "kind = 'repository' AND metadata->>'coding_enabled' = 'true'"
        tag_filter = alert.get("resource_tag_filter") or []
        if tag_filter:
            rows = await self.db_pool.fetch(
                "SELECT id, title, kind, url, metadata FROM resources "
                f"WHERE ({coding_gate}) AND tags && $1::text[] ORDER BY title",
                tag_filter,
            )
        else:
            rows = await self.db_pool.fetch(
                f"SELECT id, title, kind, url, metadata FROM resources WHERE {coding_gate} ORDER BY title"
            )

        # Tier 1.5: deterministic service → resource match. Sentry/alert
        # payloads carry a `service` (e.g. "bcp") that is frequently an exact
        # match for a registered resource's metadata.path or the basename of
        # its github_repo. The Tier-2 LLM (model_light / gemma4:e2b) is
        # unreliable at this trivial string match and silently returns no
        # resource, forcing the flow onto the LLM-only investigate() path and
        # skipping kimi. Match it here so e.g. service="bcp" routes straight to
        # acme/bcp with full confidence before we ever ask the LLM.
        service = (alert.get("service") or "").strip()
        service_base = service.rsplit("/", 1)[-1].lower()
        if service_base:
            # Tier 1.4: explicit Sentry project → resource. A Sentry issue's
            # `service` is the project slug; a resource can pin it via
            # metadata.sentry_project for deterministic routing (no LLM guess).
            for row in rows:
                meta = row.get("metadata") or {}
                if not isinstance(meta, dict):
                    continue
                sentry_project = str(meta.get("sentry_project") or "").strip().lower()
                if sentry_project and sentry_project == service.lower():
                    matched = _coding_match(row["id"], row["title"], meta, 1.0)
                    return {**matched, "source": "sentry_project", "resources": [matched]}
            # Tier 1.5: deterministic service basename → path/github_repo match.
            for row in rows:
                meta = row.get("metadata") or {}
                if not isinstance(meta, dict):
                    continue
                # path is workspace-relative and may be nested
                # ("acme/bcp") — compare its basename.
                path = str(meta.get("path") or "").strip().lower()
                path_base = path.rsplit("/", 1)[-1]
                github_repo = str(meta.get("github_repo") or "").strip()
                repo_base = github_repo.rsplit("/", 1)[-1].lower()
                if service_base in {path_base, repo_base} or service.lower() == github_repo.lower():
                    matched = _coding_match(row["id"], row["title"], meta, 1.0)
                    return {**matched, "source": "service_match", "resources": [matched]}

        if not self.llm_client:
            return null_result

        # Build resource list for prompt
        resource_lines = []
        for row in rows:
            meta = row.get("metadata") or {}
            path = meta.get("path", "") if isinstance(meta, dict) else ""
            resource_lines.append(
                f"- id={row['id']} title={row['title']} kind={row.get('kind', '')} path={path}"
            )

        title = alert.get("title", "")
        source = alert.get("source", "")
        service = alert.get("service", "")
        description = alert.get("description", "")

        prompt = (
            "You are matching a production alert to relevant code repositories.\n\n"
            f"Alert:\n"
            f"  Title: {title}\n"
            f"  Source: {source}\n"
            f"  Service: {service}\n"
            f"  Description: {description[:500]}\n\n"
            f"Available resources:\n" + "\n".join(resource_lines) + "\n\n"
            'Return JSON only: {"resources": [{"resource_id": "<id>", "resource_title": "<title>", "confidence": <0.0-1.0>}, ...]}\n'
            "Return up to 3 resources ordered by relevance. Only include resources with confidence >= 0.5. "
            "Return an empty list if nothing matches."
        )

        start = time.monotonic()
        llm_result = await self.llm_client.think(
            prompt,
            # Balanced (gpt-oss), not model_light (gemma4:e2b): picking the right
            # repo out of ~160 is well beyond the 2B model, which silently
            # returned nothing and stranded the investigation on the LLM-only
            # fallback.
            model=self.model_balanced,
            system_prompt="You are a site reliability engineer mapping alerts to repositories.",
        )
        latency_ms = int((time.monotonic() - start) * 1000)
        await record_llm_call(
            self.db_pool,
            model=llm_result.get("model", self.model_balanced),
            prompt_tokens=llm_result.get("prompt_tokens", 0),
            completion_tokens=llm_result.get("completion_tokens", 0),
            latency_ms=latency_ms,
            purpose="alert_resource_resolution",
            agent_id=alert.get("agent_id"),
        )

        response_text = llm_result.get("response", "")
        parsed = parse_llm_json(response_text)
        if not isinstance(parsed, dict):
            activity.logger.warning(
                "resolve_resource_parse_failed response=%s", response_text[:200]
            )
            return null_result

        # Support both old {"resource_id": ...} and new {"resources": [...]} shapes
        raw_resources = parsed.get("resources")
        if raw_resources is None:
            # Backward compat: single-resource response
            single_id = parsed.get("resource_id")
            single_conf = float(parsed.get("confidence", 0.0))
            if single_id and single_conf >= 0.5:
                raw_resources = [
                    {
                        "resource_id": single_id,
                        "resource_title": parsed.get("resource_title"),
                        "confidence": single_conf,
                    }
                ]
            else:
                raw_resources = []

        # Confident picks clear the 0.5 bar. When none do but the model still
        # surfaced candidates — common for Jira tickets where `service` is the
        # generic org ("acme"), not a repo, so nothing service-matches —
        # keep the top sub-threshold picks and tag the resolution
        # "llm_unconfirmed". The flow's Gate-0 then content-confirms or asks the
        # user instead of silently degrading to the LLM-only investigate() and
        # skipping kimi entirely. Returning null here was the root cause of the
        # "every Jira investigation needs a human eye" degradation.
        confident_picks = [r for r in raw_resources if float(r.get("confidence", 0.0)) >= 0.5]
        if confident_picks:
            raw_resources = confident_picks[:3]
            resolution_source = "llm"
        else:
            raw_resources = sorted(
                raw_resources, key=lambda r: float(r.get("confidence", 0.0)), reverse=True
            )[:3]
            resolution_source = "llm_unconfirmed"

        if not raw_resources:
            # Auto-register new GitHub repos seen for the first time so they
            # SHOW UP on the Resources page — but coding_enabled=false, so the
            # allow-list keeps them out of live coding runs until the user opts
            # in. We return null_result (no actionable match): an unlisted repo
            # never triggers a shell+PR run; the flow degrades to LLM-only.
            if source == "github" and service:
                repo_name = service.rsplit("/", 1)[-1]
                slug = f"repo-{repo_name}"
                async with self.db_pool.acquire() as conn:
                    existing_id = await conn.fetchval(
                        "SELECT id FROM resources WHERE slug = $1", slug
                    )
                    if not existing_id:
                        await conn.execute(
                            "INSERT INTO resources (kind, slug, title, url, tags, metadata) "
                            "VALUES ($1, $2, $3, $4, $5, $6::jsonb)",
                            "repository",
                            slug,
                            service,
                            f"https://github.com/{service}",
                            ["github", "repository", "pandoras-actor"],
                            # Pass a dict, not json.dumps(...) — the pool's jsonb codec
                            # already applies json.dumps; pre-stringifying double-encodes
                            # it into a JSONB string scalar (metadata->>'coding_enabled'
                            # would then read NULL).
                            {"github_repo": service, "coding_enabled": False},
                        )
                        activity.logger.info(
                            "alert_resource_auto_registered_disabled slug=%s service=%s "
                            "(mark coding_enabled to allow investigation)",
                            slug,
                            service,
                        )
            return null_result

        # Build enriched resource list from DB rows
        rows_by_id = {str(row["id"]): row for row in rows}
        enriched: list[dict] = []
        for r in raw_resources:
            rid = str(r.get("resource_id", ""))
            row = rows_by_id.get(rid)
            if not row:
                continue
            meta = row.get("metadata") or {}
            meta = meta if isinstance(meta, dict) else {}
            enriched.append(
                {
                    **_coding_match(rid, r.get("resource_title") or row["title"], meta,
                                    float(r.get("confidence", 0.0))),
                    "resource_path": meta.get("path") or "",
                    "kind": row.get("kind", ""),
                }
            )

        if not enriched:
            return null_result

        # Phase 2: rule-based expansion — connector/service → add infra-gitops.
        # Matched on the path BASENAME: the workspace-relative path is nested
        # ("infrastructure/infra-gitops").
        def _is_homelab(path: Any) -> bool:
            return str(path or "").rstrip("/").rsplit("/", 1)[-1] == "infra-gitops"

        kinds_in_list = {r["kind"] for r in enriched}
        if kinds_in_list & {"connector", "service"}:
            homelab_in_list = any(_is_homelab(r["resource_path"]) for r in enriched)
            if not homelab_in_list:
                homelab_row = next(
                    (
                        row
                        for row in rows
                        if _is_homelab((row.get("metadata") or {}).get("path"))
                    ),
                    None,
                )
                if homelab_row:
                    hmeta = homelab_row.get("metadata") or {}
                    enriched.append(
                        {
                            "resource_id": str(homelab_row["id"]),
                            "resource_title": homelab_row["title"],
                            "resource_path": hmeta.get("path", "infra-gitops"),
                            "github_repo": hmeta.get("github_repo", "example/infra-gitops"),
                            "kind": homelab_row.get("kind", "repository"),
                            "confidence": 0.9,
                        }
                    )

        primary = enriched[0]
        resource_id = primary["resource_id"]
        resource_title = primary["resource_title"]
        resource_path = primary["resource_path"] or None

        # ponytail: resolution is no longer cached as a graph claim (no knowledge
        # graph). The resolver runs per-alert; prior-incident context comes from
        # chunk search in gather_alert_knowledge.

        return {
            "resource_id": resource_id,
            "resource_title": resource_title,
            "resource_path": resource_path,
            "github_repo": primary.get("github_repo", ""),
            "confidence": primary["confidence"],
            "source": resolution_source,
            "resources": [{k: v for k, v in r.items() if k != "kind"} for r in enriched],
        }

    @activity.defn
    async def score_resource_relevance(self, alert: dict, resolved_resource_id: str) -> dict:
        """Gate-0: deterministically score whether the resolved repo is
        relevant to the issue, so the flow can auto-proceed when confident or
        ask the user to pick the right repo when not.

        Fetches all repository resources, runs the pure scorer
        (`aegis_worker.relevance.score_resources`), and enriches the returned
        candidates with full resource fields so the flow can rebuild its
        resources_list from the user's pick.

        Fail-open: any error (no pool, DB failure) returns confident=True with
        no candidates, so an infra hiccup proceeds as before instead of
        blocking every investigation. Only genuine ambiguity asks.

        Returns: {confident: bool, resolved_resource_id: str,
                  candidates: [{resource_id, resource_title, resource_path,
                                github_repo, label, score}]}.
        """
        proceed = {
            "confident": True,
            "resolved_resource_id": resolved_resource_id,
            "candidates": [],
        }
        if not self.db_pool:
            return proceed
        try:
            result, candidates = await self._score_and_enrich(alert, resolved_resource_id)
            return {
                "confident": result.confident,
                "resolved_resource_id": resolved_resource_id,
                "candidates": candidates,
            }
        except Exception as exc:
            activity.logger.warning(f"score_resource_relevance_failed error={exc}")
            return proceed

    async def _score_and_enrich(
        self, alert: dict, resolved_resource_id: str, hint: str = ""
    ) -> tuple[Any, list[dict]]:
        """Fetch repository resources, run the pure scorer, and enrich the
        returned candidates with full resource fields.

        Shared by score_resource_relevance (no hint) and reresolve_with_hint
        (hint passed through). Returns (RelevanceResult, candidates).
        """
        from aegis_worker import relevance

        rows = await self.db_pool.fetch(
            "SELECT id, title, metadata FROM resources WHERE kind = 'repository'"
        )
        scored_input = [
            {"id": str(r["id"]), "title": r["title"], "metadata": _decode_metadata(r)}
            for r in rows
        ]
        result = relevance.score_resources(alert, scored_input, resolved_resource_id, hint=hint)
        by_id = {str(r["id"]): r for r in rows}
        candidates = []
        for c in result.candidates:
            row = by_id.get(c.resource_id)
            meta = _decode_metadata(row) if row is not None else {}
            candidates.append(
                {
                    "resource_id": c.resource_id,
                    "resource_title": (row["title"] if row is not None else c.label),
                    "resource_path": str(meta.get("path") or ""),
                    "github_repo": str(meta.get("github_repo") or ""),
                    "label": c.label,
                    "score": c.score,
                }
            )
        return result, candidates

    @activity.defn
    async def reresolve_with_hint(self, alert: dict, hint: str) -> dict:
        """Re-run Gate-0 resolution with an operator hint.

        Folds the hint into the scorer (Task 7) and, when the hint looks like
        `owner/repo`, prepends it as a synthetic top candidate even if it is not
        a configured resource. Always returns confident=False so the flow
        re-presents Gate-0 for confirmation.
        """
        empty: dict = {"confident": False, "candidates": []}
        h = (hint or "").strip()

        def _hint_candidate(score: float) -> dict:
            # A synthetic candidate for an `owner/repo` hint that isn't a
            # configured resource — lets the operator still pick it.
            return {
                "resource_id": h,
                "resource_title": h,
                "resource_path": "",
                "github_repo": h,
                "label": f"{h} (from hint)",
                "score": score,
            }

        if not self.db_pool:
            if _HINT_REPO_RE.match(h):
                return {"confident": False, "candidates": [_hint_candidate(1.0)]}
            return empty
        try:
            _result, candidates = await self._score_and_enrich(alert, "", hint=hint)
            if _HINT_REPO_RE.match(h) and not any(c["github_repo"] == h for c in candidates):
                top_score = candidates[0]["score"] if candidates else 1.0
                candidates.insert(0, _hint_candidate(top_score))
            return {"confident": False, "candidates": candidates[:5]}
        except Exception as exc:
            activity.logger.warning(f"reresolve_with_hint_failed error={exc}")
            return empty

    @activity.defn
    async def check_alert_resolved(self, fingerprint: str, window_minutes: int = 10) -> dict:
        """Check if a matching resolve event arrived within the time window."""
        if not self.db_pool or not fingerprint:
            return {"resolved": False}

        row = await self.db_pool.fetchrow(
            "SELECT id FROM audit_log WHERE target_type = 'alert' AND target_id = $1 "
            "AND action = 'alert_received' AND details->>'resolved' = 'true' "
            "AND created_at > NOW() - INTERVAL '1 minute' * $2 LIMIT 1",
            fingerprint,
            window_minutes,
        )
        return {"resolved": row is not None}

    @activity.defn
    async def get_verification_delay(self, alert: dict) -> dict:
        """Determine how long to wait before investigating an alert.

        Pattern-based defaults keyed off the alert title — deterministic and
        offline. The previous KG-lookup tier was speculative (no custom delay
        was ever stored) and added a per-alert KS network call under a 15s
        activity timeout, so a slow proxy could time the whole activity out and
        fail the investigation. Dropped.
        """
        title_lower = alert.get("title", "").lower()

        # Service/core/node down → 5 minutes
        if re.search(r"(service|core|node)\s*down", title_lower):
            return {"delay_seconds": 300, "reason": "Service down pattern — 5 min verification"}

        # Pipeline / success rate → 10 minutes
        if re.search(r"(pipeline|success\s*rate)", title_lower):
            return {"delay_seconds": 600, "reason": "Pipeline pattern — 10 min verification"}

        # Disk / storage / memory / OOM → immediate
        if re.search(r"(disk|storage|memory|oom)", title_lower):
            return {"delay_seconds": 0, "reason": "Resource exhaustion — immediate investigation"}

        # Default
        return {"delay_seconds": 180, "reason": "Default verification delay — 3 min"}

    @activity.defn
    async def run_investigation(
        self,
        alert: dict,
        resources: list[dict],
        runbook: str,
        engine_override: str = "",
        allow_fix: bool = True,
    ) -> dict:
        """Run a coding-CLI investigation on remote host via RemoteScriptConnector.

        resources: list from resolve_alert_resource["resources"]. The first entry
        with a code path is the primary; the agent runs against it in an isolated
        per-run worktree (sandbox confinement) — secondary repos are not exposed.

        engine_override="claude" forces the claude CLI (the kimi→claude fallback
        the flow invokes as a second attempt for a non-org repo); it runs under
        self.claude_personal_config_dir so the fallback uses the personal login.
        Returns: {status, output, session_id, branch, branches}
        """
        if not self.remote_script:
            return {
                "status": "failed",
                "output": "Remote script connector not available",
                "session_id": "",
                "branch": "",
                "branches": {},
                "output_file": "",
            }
        if not self.kimi_binary:
            return {
                "status": "failed",
                "output": "Kimi binary path not configured (kimi_cli_binary_path)",
                "session_id": "",
                "branch": "",
                "branches": {},
                "output_file": "",
            }
        if not resources:
            return {
                "status": "failed",
                "output": "No resources to investigate",
                "session_id": "",
                "branch": "",
                "branches": {},
                "output_file": "",
            }

        # Kimi needs a checkout path. Connectors/runbooks/mcp_servers in the
        # resources list have no path — pick the first resource that does.
        primary_idx = next(
            (i for i, r in enumerate(resources) if r.get("resource_path")),
            -1,
        )
        if primary_idx < 0:
            return {
                "status": "no_code_resource",
                "output": "No resource with a code path — LLM fallback only",
                "session_id": "",
                "branch": "",
                "branches": {},
                "output_file": "",
            }
        primary = resources[primary_idx]
        resource_path = primary.get("resource_path", "")

        title = alert.get("title", "Unknown")
        severity = alert.get("severity", "unknown")
        description = alert.get("description", "")
        fingerprint = alert.get("fingerprint", "")
        is_jira = alert.get("source") == _JIRA_SOURCE

        branch_slug = re.sub(r"[^a-z0-9-]", "-", fingerprint.lower())[:40].strip("-")
        # Fixes are staged only for non-Jira AND fix-allowed alerts. Infra/swarm
        # alerts pass allow_fix=False — they're investigate-only (auto-restart
        # remediation already ran; we don't auto-commit to infra-gitops).
        fix_branch = (
            f"aegis-fix/{branch_slug}"
            if (not is_jira and allow_fix and branch_slug)
            else ""
        )

        if is_jira:
            prompt = _build_jira_scoping_prompt(title, description, runbook)
        else:
            prompt = _build_alert_investigation_prompt(
                title, severity, description, runbook, fix_branch
            )

        # Rule of Two: a coding run reads an untrusted alert (input), gets repo
        # shell access (sensitive), and can open a PR (external state) — all
        # three, so it MUST stay human-gated (the flow's Gate-0/Gate-2 + draft-PR
        # review provide that). Log the assessment for observability; the gate
        # itself lives in flows/alert_investigation.py.
        rot = assess_rule_of_two(
            untrusted_input=True,
            sensitive_access=True,
            external_state_change=bool(fix_branch),
        )
        activity.logger.info(
            "rule_of_two_assessed capabilities=%s requires_human_gate=%s",
            rot["held"],
            rot["requires_human_gate"],
        )

        # Workspace-relative checkout path ("acme/bcp"); kimi emits
        # `BRANCH: <repo_name>:<branch>` keyed by the repo's BASENAME, so
        # branch bookkeeping uses repo_key throughout.
        repo = resource_path.rstrip("/")
        repo_key = repo.rsplit("/", 1)[-1]
        primary_github_repo = primary.get("github_repo") or ""

        # Resource-scoped routing: a repo can pin its engine (claude|kimi) and,
        # for claude, its CLAUDE_CONFIG_DIR account label. The resource engine
        # wins over the caller's engine_override (the kimi→claude fallback);
        # kimi ignores the account. Falls through to org routing when unset.
        res_engine = (primary.get("engine") or "").strip().lower()
        effective_engine_override = res_engine if res_engine in ("claude", "kimi") else engine_override
        res_claude_account = primary.get("claude_account") or ""

        worktree_path = ""
        inv_host = ""
        try:
            run_result = await self.remote_script.start_kimi_run(
                repo,
                prompt,
                kimi_binary=self.kimi_binary,
                github_repo=primary_github_repo,
                engine_override=effective_engine_override,
                claude_account=res_claude_account,
                claude_config_dir=(
                    # Only the fallback path supplies an explicit dir; a
                    # resource-pinned account takes the claude_account route.
                    self.claude_personal_config_dir
                    if effective_engine_override == "claude" and not res_claude_account
                    else ""
                ),
            )
            if run_result.get("status") == "failed":
                return {
                    "status": "failed",
                    "output": run_result.get("error", "Kimi launch failed"),
                    "session_id": "",
                    "branch": "",
                    "branches": {},
                    "output_file": "",
                }

            # Isolated per-run worktree (empty if start_kimi_run fell back to
            # the shared clone). Cleaned up in `finally`; the fix branch kimi
            # creates persists in the shared .git, so create_github_pr (which
            # pushes from the shared clone) is unaffected.
            worktree_path = run_result.get("worktree_path", "")
            inv_host = run_result.get("host", "")
            output_file = run_result.get("output_file", "")
            session_id = ""

            max_iterations = 60
            latest_raw = ""
            for _ in range(max_iterations):
                raw = await self.remote_script.fetch_kimi_run_output(output_file, host=inv_host)
                if raw:
                    latest_raw = raw
                    # Parse session_id from first stream-json event
                    for line in raw.splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            evt = json.loads(line)
                            if evt.get("session_id"):
                                session_id = evt["session_id"]
                        except (json.JSONDecodeError, AttributeError):
                            pass
                        break
                    if _kimi_output_complete(raw):
                        branches = _parse_kimi_branches(raw, primary_repo=repo_key)
                        primary_branch = branches.get(repo_key, fix_branch if branches else "")
                        return {
                            "status": "succeeded",
                            "output": raw[-_INVESTIGATION_OUTPUT_CAP:],
                            "session_id": session_id,
                            "branch": primary_branch,
                            "branches": branches,
                            "output_file": output_file,
                            "host": inv_host,
                            "engine": run_result.get("engine", "kimi"),
                        }

                activity.heartbeat()
                await asyncio.sleep(30)

            return {
                "status": "timed_out",
                "output": (
                    latest_raw[-_INVESTIGATION_OUTPUT_CAP:]
                    if latest_raw
                    else "Investigation exceeded 30 minute timeout"
                ),
                "session_id": session_id,
                "branch": "",
                "branches": {},
                "output_file": output_file,
                "host": inv_host,
                "engine": run_result.get("engine", "kimi"),
            }
        except Exception as exc:
            activity.logger.error("run_investigation_failed error=%s", str(exc))
            return {
                "status": "failed",
                "output": f"Investigation error: {str(exc)[:500]}",
                "session_id": "",
                "branch": "",
                "branches": {},
                "output_file": "",
            }
        finally:
            if worktree_path:
                try:
                    await self.remote_script.remove_worktree(worktree_path, host=inv_host)
                except Exception:
                    pass

    @activity.defn
    async def assess_investigation(self, alert: dict, investigation_output: str) -> dict:
        """Use Haiku to produce structured verdict from investigation output.

        Returns: {status, root_cause, suggested_fix, confidence}
        """
        if not self.llm_client:
            return {
                "status": "actionable",
                "root_cause": "LLM not available — manual review needed",
                "suggested_fix": "",
                "confidence": 0.0,
            }

        title = alert.get("title", "Unknown")
        severity = alert.get("severity", "unknown")
        is_jira = alert.get("source") == _JIRA_SOURCE

        if is_jira:
            prompt = (
                "Assess this Jira-ticket scoping run and provide a structured verdict.\n\n"
                f"Ticket: {title}\n\n"
                f"Scoping output:\n{investigation_output[:3000]}\n\n"
                "Map the scoping STATUS verb to a verdict status:\n"
                '- `STATUS: scoped` → status="actionable". Put the affected files / '
                "suspected cause summary in root_cause; put the suggested next step in "
                "suggested_fix.\n"
                '- `STATUS: needs_human` → status="inconclusive". Leave root_cause and '
                "suggested_fix empty.\n"
                '- `STATUS: out_of_scope` → status="not_actionable". Put the reason in '
                "root_cause; leave suggested_fix empty.\n"
                "- No STATUS footer present, OR no concrete observations (tool outputs, "
                'file contents, command results) → status="inconclusive", root_cause="", '
                'suggested_fix="". Do not invent content to fill the fields.\n\n'
                "Return JSON only:\n"
                '{"status": "<actionable|inconclusive|not_actionable>", '
                '"root_cause": "<scoping summary, reason, or empty string>", '
                '"suggested_fix": "<next step, or empty string>", '
                '"confidence": <0.0-1.0>}'
            )
        else:
            prompt = (
                "Assess this alert investigation and provide a structured verdict.\n\n"
                f"Alert: {title} (severity: {severity})\n\n"
                f"Investigation output:\n{investigation_output[:3000]}\n\n"
                "Rules:\n"
                "- If the investigation ends with `STATUS: insufficient_evidence` or "
                "`STATUS: alert_unclear`, OR contains no concrete observations (tool outputs, "
                'file contents, log lines, command results), return status="inconclusive" '
                'with root_cause="" and suggested_fix="". Do not invent a root cause to fill '
                "the field — passing through 'no evidence' is the correct answer.\n"
                "- Otherwise return one of resolved|actionable|not_actionable based "
                "on what the investigation actually established. "
                '`not_actionable` covers "no action needed"; `actionable` covers '
                "everything that requires a fix.\n\n"
                "Return JSON only:\n"
                '{"status": "<resolved|actionable|not_actionable|inconclusive>", '
                '"root_cause": "<brief root cause, or empty string if inconclusive>", '
                '"suggested_fix": "<recommended fix, or empty string if inconclusive>", '
                '"confidence": <0.0-1.0>}'
            )

        start = time.monotonic()
        result = await self.llm_client.think(
            prompt,
            model=self.model_balanced,
            system_prompt="You are a site reliability engineer assessing alert investigations.",
            db_pool=self.db_pool,
            purpose="alert_assessment",
            agent_id=alert.get("agent_id"),
        )
        latency_ms = int((time.monotonic() - start) * 1000)
        await record_llm_call(
            self.db_pool,
            model=result.get("model", self.model_balanced),
            prompt_tokens=result.get("prompt_tokens", 0),
            completion_tokens=result.get("completion_tokens", 0),
            latency_ms=latency_ms,
            purpose="alert_assessment",
            agent_id=alert.get("agent_id"),
        )

        response_text = result.get("response", "")
        parsed = parse_llm_json(response_text)
        if not isinstance(parsed, dict):
            activity.logger.warning("assess_parse_failed response=%s", response_text[:200])
            return {
                "status": "actionable",
                "root_cause": response_text[:200],
                "suggested_fix": "",
                "confidence": 0.0,
            }

        valid_statuses = {
            "resolved",
            "actionable",
            "not_actionable",
            "inconclusive",
        }
        status = parsed.get("status", "actionable")
        if status not in valid_statuses:
            status = "actionable"

        root_cause = parsed.get("root_cause", "") or ""
        suggested_fix = parsed.get("suggested_fix", "") or ""
        confidence = float(parsed.get("confidence", 0.0))

        # The LLM sometimes returns a full verdict JSON OBJECT (as a string)
        # nested inside root_cause, which would otherwise get posted to the
        # user's Todoist task verbatim as broken JSON. Unwrap it.
        stripped = root_cause.strip()
        if stripped.startswith("{"):
            try:
                inner = json.loads(stripped)
                if isinstance(inner, dict):
                    root_cause = inner.get("root_cause", root_cause) or ""
                    suggested_fix = inner.get("suggested_fix", suggested_fix) or ""
                    inner_status = inner.get("status")
                    if inner_status in valid_statuses:
                        status = inner_status
                    if "confidence" in inner:
                        confidence = float(inner.get("confidence") or 0.0)
            except (json.JSONDecodeError, ValueError, TypeError):
                pass

        # Don't present an empty verdict as actionable.
        if confidence <= 0.0 and not suggested_fix.strip() and not root_cause.strip():
            status = "inconclusive"

        return {
            "status": status,
            "root_cause": root_cause,
            "suggested_fix": suggested_fix,
            "confidence": confidence,
        }

    @activity.defn
    async def record_verdict_to_kg(
        self, alert: dict, verdict: dict, investigation_output: str
    ) -> dict:
        """Persist an alert / scoping verdict into the knowledge graph so
        that the NEXT investigation of a related alert can recall what
        the prior diagnosis was.

        Closes the loop the audit on 2026-05-21 surfaced: `investigate()`
        already ingests its LLM-fallback text (alerts.py:411-422), but
        the kimi path's assess output never reached the KG. Resource
        cross-referencing also writes a `relates_to` claim so the next
        alert against the same resource can find prior incidents.
        """
        if not self.knowledge_connector:
            return {"ingested": False, "reason": "no_connector"}
        fingerprint = alert.get("fingerprint", "")
        title = alert.get("title", "Unknown")
        source = alert.get("source", "")
        verdict_status = verdict.get("status") or "unknown"
        confidence = float(verdict.get("confidence") or 0.0)
        url = alert.get("url") or f"aegis://alert/{fingerprint or 'unknown'}"
        tags = ["alert", source] if source else ["alert"]

        try:
            # 1) The full investigation transcript (free-text, semantic
            #    search against this is what gather_alert_knowledge uses).
            if investigation_output:
                await self.knowledge_connector.ingest_content(
                    url=url,
                    title=f"Alert investigation: {title}",
                    source_type="alert_investigation",
                    raw_text=investigation_output[:8000],
                    tags=tags,
                    metadata={
                        "fingerprint": fingerprint,
                        "status": verdict_status,
                        "confidence": confidence,
                    },
                )
            # ponytail: structured-claim recording dropped (no knowledge graph).
            # The free-text transcript ingested above is the searchable record.
            return {"ingested": bool(investigation_output)}
        except Exception as exc:
            activity.logger.warning("record_verdict_to_kg_failed: %s", str(exc)[:200])
            return {"ingested": False, "reason": str(exc)[:200]}

    async def _read_digest_buffer(self) -> dict:
        """Read + normalize the alert digest buffer from settings.

        Returns a dict shaped {"items": [...]}, defaulting to an empty
        buffer when the row is missing or malformed.
        """
        raw = await self.db_pool.fetchval(
            "SELECT value::text FROM settings WHERE key = 'alert_digest_buffer'"
        )
        buffer = json.loads(raw) if raw else {"items": []}
        if not isinstance(buffer, dict):
            buffer = {"items": []}
        return buffer

    @activity.defn
    async def accumulate_digest_item(self, item: dict) -> None:
        """Append item to the alert digest buffer in settings table."""
        if not self.db_pool:
            return

        buffer = await self._read_digest_buffer()
        items = buffer.get("items", [])
        items.append(item)
        # Cap at 500 to prevent unbounded growth
        if len(items) > 500:
            items = items[-500:]
        buffer["items"] = items

        await self.db_pool.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES ('alert_digest_buffer', $1, NOW()) "
            "ON CONFLICT (key) DO UPDATE SET value = $1, updated_at = NOW()",
            buffer,
        )

    @activity.defn
    async def build_alert_digest(self) -> dict:
        """Build a formatted digest from the alert buffer and clear it.

        Returns: {message: str, count: int}
        """
        if not self.db_pool:
            return {"message": "", "count": 0}

        buffer = await self._read_digest_buffer()
        items = buffer.get("items", [])

        if not items:
            return {"message": "", "count": 0}

        # Group by type
        type_labels = {
            "auto_remediated": "Auto-Remediated (restart)",
            "self_resolved": "Self-Resolved",
            "sentry_suppressed": "Sentry Suppressed",
            "not_actionable": "Not Actionable",
        }

        sections = []
        for item_type, label in type_labels.items():
            type_items = [i for i in items if i.get("type") == item_type]
            if not type_items:
                continue
            # Count by title
            title_counts = Counter(i.get("title", "Unknown")[:100] for i in type_items)
            lines = [
                f"  - {title} (x{count})" if count > 1 else f"  - {title}"
                for title, count in title_counts.most_common()
            ]
            sections.append(f"<b>{label}</b> ({len(type_items)}):\n" + "\n".join(lines))

        message = "<b>Alert Digest</b>\n\n" + "\n\n".join(sections)

        # Clear buffer
        await self.db_pool.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES ('alert_digest_buffer', '{\"items\": []}'::jsonb, NOW()) "
            "ON CONFLICT (key) DO UPDATE SET value = '{\"items\": []}'::jsonb, updated_at = NOW()"
        )

        return {"message": message, "count": len(items)}
