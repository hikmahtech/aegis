"""JiraSyncFlow — close Todoist tasks whose Jira issue is already resolved.

The companion to `email_task_links`, and the one that covers the gap email
cannot: Jira sends no notification for a transition you made yourself, so a
ticket you close is invisible to mail triage forever. Asking Jira sees all of
them.

Read and write are separate activities, so a run that only wants to look
(`dry_run`) simply never calls the second one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.jira import DEFAULT_KEY_PATTERN
    from aegis_worker.shared.retry import ACT_RETRY, NO_RETRY

_FETCH_TIMEOUT = timedelta(seconds=120)
_WRITE_TIMEOUT = timedelta(seconds=180)


@dataclass
class JiraSyncConfig:
    agent_id: str = "sebas"
    #: Issue-key prefix on the Todoist title. Both a Postgres ARE (the DB filter)
    #: and a Python regex (the extraction), so keep it to syntax common to both.
    key_pattern: str = DEFAULT_KEY_PATTERN
    max_tasks: int = 100
    #: Report what would close without closing it. Worth one run whenever the
    #: pattern or the Jira project changes.
    dry_run: bool = False


@workflow.defn(name="JiraSyncFlow")
class JiraSyncFlow:
    @workflow.run
    async def run(self, config: JiraSyncConfig) -> dict:
        states = await workflow.execute_activity(
            "fetch_jira_task_states",
            args=[config.key_pattern, config.max_tasks],
            start_to_close_timeout=_FETCH_TIMEOUT,
            retry_policy=ACT_RETRY,
        )
        resolved = states.get("resolved") or []
        summary = {
            "checked": states.get("checked", 0),
            "unresolved": states.get("unresolved", 0),
            "resolved": len(resolved),
            "reason": states.get("reason"),
            "keys": [r.get("key") for r in resolved],
            "dry_run": config.dry_run,
        }
        if not resolved or config.dry_run:
            return {**summary, "closed": 0, "queued": 0, "failed": 0}

        written = await workflow.execute_activity(
            "close_resolved_jira_tasks",
            args=[resolved],
            start_to_close_timeout=_WRITE_TIMEOUT,
            retry_policy=NO_RETRY,  # the activity is already per-task best-effort
        )
        return {**summary, **written}
