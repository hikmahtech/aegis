"""Follow a fix PR from merge to verified (#502).

When a person picks "Open PR(s)" on an investigation's Gate-2 card, the flow
opens the pull request and records it on the problem: an `investigation`
event whose payload carries `pr_urls`, plus a `github_pr` link
(`HubActivities.record_investigation`). Before this module nothing followed
the PR, so the problem sat in `fixing` whatever became of it.

Two halves:

* :func:`record_pr_closed` — the GitHub webhook says a PR closed. When an
  investigation opened it, the close is written on the problem and the
  problem moves (:func:`fix_status`): ``verifying`` once a fix merged and none
  is still open, ``waiting_human`` when every fix PR was closed unmerged.
* :func:`verify_fixes` — the hub sweep's check on every ``verifying`` problem.
  It resolves one once the alert has stayed clear for the window since the
  merge, and reopens one the alert came back to.

Three rules:

* **Only an investigation's PR is followed.** A coding session also links its
  PR (`report_progress`), but a merge there says nothing about whether an
  alert is fixed: a hand-written `@code` task has no alert to stay clear, and
  "resolve after 24 quiet hours" would close it on a timer. The test is an
  investigation event naming the URL in `pr_urls`, which only the flow writes.
* **The alert source owns whether a problem is live** (#484/#488). Every move
  goes through `hub.set_status` with the default ``investigation`` source, so
  a problem the alert already resolved stays resolved; the close is recorded
  on its timeline all the same.
* **An occurrence counts as the alert coming back only after a grace**, and
  never inside a deploy window. Right after a merge the old code is still
  running — CI has not built the image, or the rollout has not reached it —
  so an occurrence then is the bug the fix has not replaced yet, not proof
  the fix failed.

Nothing here touches Todoist. Both halves write `investigation` events with
``posted: False``, so the projector turns them into task comments, and the
moves are ordinary `state_change` rows: a resolve closes the task.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import structlog

from aegis.services.hub import Event, ingest_event, set_status

logger = structlog.get_logger()

# How long the alert must stay clear after a fix merged before the problem
# resolves, and how long after the merge an occurrence is still put down to
# the old code. Generic defaults; the hub sweep's `activities.config` row
# (`fix_verify_hours`, `fix_grace_hours`) overrides both.
VERIFY_HOURS_DEFAULT = 24.0
GRACE_HOURS_DEFAULT = 1.0

# The state of one fix PR, as the events on its problem tell it.
PR_OPEN, PR_MERGED, PR_CLOSED = "open", "merged", "closed"

# Problems that have an investigation event naming the PR in `pr_urls`, and
# the `github_pr` link that event's writer made. Closed problems are history.
_FOLLOWED_SQL = """
SELECT DISTINCT p.id::text AS id, p.status
FROM problems p
JOIN problem_links l ON l.problem_id = p.id AND l.link_kind = 'github_pr'
WHERE p.closed_at IS NULL AND lower(rtrim(l.ref, '/')) = $1
  AND EXISTS (
    SELECT 1 FROM problem_events e
    CROSS JOIN LATERAL jsonb_array_elements_text(
        CASE WHEN jsonb_typeof(e.payload->'pr_urls') = 'array'
             THEN e.payload->'pr_urls' ELSE '[]'::jsonb END) AS u(url)
    WHERE e.problem_id = p.id AND e.source = 'investigation'
      AND e.kind = 'investigation' AND lower(rtrim(u.url, '/')) = $1)
"""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def normalize_pr_url(url: str) -> str:
    """The form two PR urls are compared in. `gh pr create` prints the url
    GitHub returns and the webhook carries `html_url`; both are canonical, but
    nothing is lost by not trusting the case or a trailing slash."""
    return (url or "").strip().rstrip("/").lower()


def fix_status(states: Mapping[str, str]) -> str:
    """Where a problem goes, from the states of every fix PR it has.

    A PR still open means the fix is still being made. Once none is, one
    merge is enough to start watching the alert: the operator took a fix. No
    merge at all means every fix was turned down, and the problem is the
    operator's again."""
    values = set(states.values())
    if PR_OPEN in values:
        return "fixing"
    if PR_MERGED in values:
        return "verifying"
    return "waiting_human"


async def pr_states(pool: asyncpg.Pool, problem_id: str) -> dict[str, str]:
    """Every fix PR an investigation opened on the problem, keyed by
    normalised url, with its latest state: ``open`` until a close says
    otherwise. Read from the events, in the order they were written."""
    rows = await pool.fetch(
        "SELECT source, payload FROM problem_events "
        "WHERE problem_id = $1::uuid AND kind = 'investigation' "
        "  AND (payload ? 'pr_urls' OR payload ? 'pr') ORDER BY id",
        problem_id,
    )
    states: dict[str, str] = {}
    for row in rows:
        payload = row["payload"] or {}
        if row["source"] == "investigation" and isinstance(payload.get("pr_urls"), list):
            for url in payload["pr_urls"]:
                key = normalize_pr_url(str(url))
                if key:
                    states.setdefault(key, PR_OPEN)
        pr = payload.get("pr")
        if row["source"] == "github" and isinstance(pr, dict):
            key = normalize_pr_url(str(pr.get("url") or ""))
            if key in states and pr.get("state") in {PR_MERGED, PR_CLOSED}:
                states[key] = str(pr["state"])
    return states


def _closed_text(url: str, merged: bool, states: Mapping[str, str], status: str, was: str) -> str:
    head = f"Fix PR merged: {url}." if merged else f"Fix PR closed without merging: {url}."
    if was == "resolved":
        return f"{head} The alert had already cleared, so this stays resolved."
    still_open = sum(1 for s in states.values() if s == PR_OPEN)
    if status == "fixing":
        return f"{head} {still_open} more fix PR{'s' if still_open != 1 else ''} still open."
    if status == "verifying":
        lead = "Watching" if merged else "Another fix PR merged, so I am watching"
        return (
            f"{head} {lead} the alert now: I resolve this once it has stayed clear, "
            "and reopen it if it comes back."
        )
    return f"{head} The fix was not taken, so this is back with you."


async def record_pr_closed(
    pool: asyncpg.Pool,
    *,
    url: str,
    merged: bool,
    at: str = "",
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """A pull request closed. For every live problem an investigation opened
    it for: write the close on the timeline and move the problem to
    :func:`fix_status`. Returns one row per problem: ``problem_id``, ``state``
    (merged | closed), ``status`` (where the problem is now meant to be) and
    ``moved``.

    ``at`` is GitHub's `merged_at` / `closed_at`. It is in the event's id, so
    a retried or redelivered close is written once; a PR closed, reopened and
    closed again is two closes."""
    now = now or _utcnow()
    key = normalize_pr_url(url)
    if not key:
        return []
    state = PR_MERGED if merged else PR_CLOSED
    out: list[dict[str, Any]] = []
    for row in await pool.fetch(_FOLLOWED_SQL, key):
        problem_id, was = row["id"], row["status"]
        states = await pr_states(pool, problem_id)
        states[key] = state
        status = "resolved" if was == "resolved" else fix_status(states)
        text = _closed_text(url.strip(), merged, states, status, was)
        await ingest_event(
            pool,
            Event(
                source="github",
                external_id=f"pr-{state}:{problem_id}:{key}@{at or now.isoformat()}",
                kind="investigation",
                title=text[:200],
                severity="info",
                payload={
                    "pr": {"url": url.strip(), "state": state, "at": at},
                    "text": text,
                    "status": status,
                    # Not on the task yet: the projector posts it.
                    "posted": False,
                },
                occurred_at=now,
                problem_id=problem_id,
            ),
            now=now,
        )
        # The `investigation` source is what keeps a resolved problem
        # resolved, even if the alert cleared a moment ago (#488).
        moved = was != "resolved" and await set_status(
            pool, problem_id, status, reason=text[:300], now=now
        )
        out.append({"problem_id": problem_id, "state": state, "status": status, "moved": moved})
        logger.info(
            "hub_fix_pr_closed", problem_id=problem_id, state=state, status=status, moved=moved
        )
    return out


async def verify_fixes(
    pool: asyncpg.Pool,
    *,
    window_hours: float = VERIFY_HOURS_DEFAULT,
    grace_hours: float = GRACE_HOURS_DEFAULT,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Settle every ``verifying`` problem that can be settled. Returns one row
    per problem moved, ``{"problem_id", "action"}`` with action ``resolved``
    or ``reopened``.

    The clock starts when the problem moved to ``verifying``. An occurrence
    later than the grace after that, outside a deploy window, means the alert
    came back: the problem goes back to ``open`` and the task says so. With
    none, the problem resolves once the whole window has passed. Idempotent:
    a retry finds the problem already moved, and the notes' ids repeat."""
    now = now or _utcnow()
    window = timedelta(hours=max(float(window_hours), 0.0))
    grace = timedelta(hours=max(float(grace_hours), 0.0))
    rows = await pool.fetch(
        "SELECT p.id::text AS id, "
        "  (SELECT max(e.occurred_at) FROM problem_events e WHERE e.problem_id = p.id "
        "   AND e.kind = 'state_change' AND e.payload->>'status' = 'verifying') AS since "
        "FROM problems p WHERE p.status = 'verifying' AND p.closed_at IS NULL"
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        problem_id, since = row["id"], row["since"]
        if since is None:
            # Nothing says when watching began. Only this module moves a
            # problem to `verifying`, so this is a hand edit: leave it.
            continue
        back = await pool.fetchval(
            "SELECT min(occurred_at) FROM problem_events WHERE problem_id = $1::uuid "
            "AND kind = 'occurrence' AND occurred_at > $2 AND NOT (payload ? 'suppressed_by')",
            problem_id,
            since + grace,
        )
        if back is None and now - since < window:
            continue
        merged = [u for u, s in (await _fix_pr_urls(pool, problem_id)).items() if s == PR_MERGED]
        prs = ", ".join(merged) or "the fix PR"
        if back is not None:
            hours = (back - since).total_seconds() / 3600
            action, status, tag = "reopened", "open", "fix-back"
            text = (
                f"It came back at {back:%Y-%m-%d %H:%M} UTC, {hours:.1f}h after the fix merged "
                f"({prs}). Either the fix did not hold or it is not deployed yet, so this is "
                "open again."
            )
        else:
            action, status, tag = "resolved", "resolved", "fix-clear"
            text = (
                f"The alert stayed clear for {window.total_seconds() / 3600:g}h after the fix "
                f"merged ({prs}), so I am resolving this."
            )
        await ingest_event(
            pool,
            Event(
                source="hub",
                external_id=f"{tag}:{problem_id}:{since.isoformat()}",
                kind="investigation",
                title=text[:200],
                severity="info",
                payload={"text": text, "status": status, "posted": False},
                occurred_at=now,
                problem_id=problem_id,
            ),
            now=now,
        )
        if await set_status(pool, problem_id, status, reason=text[:300], now=now):
            out.append({"problem_id": problem_id, "action": action})
            logger.info("hub_fix_verified", problem_id=problem_id, action=action)
    return out


async def _fix_pr_urls(pool: asyncpg.Pool, problem_id: str) -> dict[str, str]:
    """The fix PRs a close was recorded for, with their latest state, keyed by
    the url as GitHub wrote it (a note a person reads) rather than normalised."""
    rows = await pool.fetch(
        "SELECT payload->'pr' AS pr FROM problem_events WHERE problem_id = $1::uuid "
        "AND kind = 'investigation' AND source = 'github' AND payload ? 'pr' ORDER BY id",
        problem_id,
    )
    out: dict[str, str] = {}
    for row in rows:
        pr = row["pr"] or {}
        if isinstance(pr, dict) and pr.get("url"):
            out[str(pr["url"])] = str(pr.get("state") or "")
    return out
