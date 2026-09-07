"""Problem hub — one record per problem; every signal about it is an event.

Spec: docs/superpowers/specs/2026-09-07-problem-hub-design.md.

Before the hub, every producer of an operational signal created its own
Todoist task or Slack card and then deduped against *that*, each with its own
key — thirteen of them, and dedupe keyed on a task that might not exist yet or
might already be closed. Here the problem is the record: a producer describes
what it saw as an :class:`Event`, :func:`ingest_event` decides whether that is
a new problem or another occurrence of an open one, and everything downstream
(task, comments, sessions, digest) hangs off the ``problems`` row.

Three rules the rest of the codebase relies on:

* **Identity is one function.** :func:`correlation_key` is the only place a
  problem's identity is computed. A producer never computes a key, it fills
  in ``klass`` / ``subject`` / ``subject_kind`` and lets the hub decide.
* **Idempotency is per event, not per problem.** ``(source, external_id)`` is
  unique on ``problem_events``, so a retried signal attaches nothing twice —
  which means an *occurrence* id must differ per occurrence (fingerprint plus
  start time), not per alert rule, or the second firing of a rule is a
  "duplicate" forever.
* **Attaching to the wrong problem hides an outage; creating a duplicate is
  recoverable.** So every doubt resolves to "create": an event with no class
  and no subject gets an empty key and is never auto-attached, and the fuzzy
  (LLM) match the spec describes only ever *suggests* (PR 5).

Core and the worker both import this module (the worker already imports
``aegis.services.*``), so the two packages cannot drift on what a problem is.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import structlog

logger = structlog.get_logger()

# An occurrence within this long after a problem resolved reopens it; one
# after it closes the old problem and starts a new one linked to it. Becomes
# an `activities.config` key on the hub sweep row in PR 6.
REOPEN_WINDOW = timedelta(hours=24)

# Closed vocabularies. A producer outside these is a wiring mistake, and the
# route turns the ValueError into a 400 rather than minting a problem of an
# unknown origin that no digest query would ever group.
SOURCES = frozenset(
    {
        "alertmanager",
        "prometheus",
        "grafana",
        "sentry",
        "heartbeat",
        "flow_health",
        "delivery",
        "drift",
        "expiry",
        "social",
        "llm_governor",
        "github",
        "ansible",
        "chat",
        "investigation",
        "session",
        "manual",
        "hub",  # the hub's own state_change rows
    }
)
KINDS = frozenset(
    {"occurrence", "resolved", "investigation", "plan", "session_note", "human_note"}
)
SEVERITIES = frozenset({"critical", "error", "warning", "info"})
# Problem statuses. `suppressed` = seen while its subject was deploying or in
# maintenance (see `service_state`); it is live, counted, and not projected.
LIVE_STATUSES = frozenset(
    {"open", "investigating", "waiting_human", "fixing", "verifying", "suppressed"}
)
STATUSES = LIVE_STATUSES | {"resolved", "closed"}
# `service_state.state`. The first two suppress; `degraded` and `ok` are
# information (`ok` clears the row).
SERVICE_STATES = frozenset({"deploying", "maintenance", "degraded", "ok"})
SUPPRESSING_STATES = frozenset({"deploying", "maintenance"})
# A `deploying` row the deploy job never cleared is cleared by the heartbeat
# once the service has converged and the row is at least this old — two
# heartbeat ticks, so a row set just before a rollout starts is not cleared
# by the pre-rollout snapshot.
CONVERGE_GRACE = timedelta(minutes=4)

_SLUG_RE = re.compile(r"[^a-z0-9_.]+")
_SEGMENT_CAP = 80
# Heartbeat fingerprints are `aegis-heartbeat:{alertname}:{subject}`; the
# subject (node name or service) lives only there, not in the labels.
_HEARTBEAT_FP_RE = re.compile(r"^aegis-heartbeat:([^:]+):(.*)$")
_NODE_CLASSES = frozenset({"nodedown"})
# The alert dicts today's producers build carry these `source` values; the
# hub keys events on its own closed vocabulary. Anything else is `manual`.
_SOURCE_ALIASES = {
    "aegis-heartbeat": "heartbeat",
    "todoist-jira": "chat",
    "todoist-chat": "chat",
    "todoist-infra": "chat",
}

# How long an investigation waits before spending effort, by class, so a
# blip that self-resolves costs nothing. Was a regex over the alert title;
# the class is the same information without the guessing. Resource
# exhaustion (disk / memory / OOM) investigates at once.
VERIFY_SECONDS_DEFAULT = 180
_VERIFY_SECONDS = {
    "nodedown": 300,
    "dockerservicedown": 300,
    "servicedownprolonged": 0,
    "heartbeatcollectfailed": 0,
}
_VERIFY_AT_ONCE = ("disk", "storage", "memory", "oom")


def verify_seconds(klass: str) -> int:
    k = _slug(klass)
    if k in _VERIFY_SECONDS:
        return _VERIFY_SECONDS[k]
    if any(word in k for word in _VERIFY_AT_ONCE):
        return 0
    return VERIFY_SECONDS_DEFAULT


@dataclass(frozen=True)
class Event:
    """What a producer saw. See the module docstring for the three rules."""

    source: str
    external_id: str
    kind: str
    title: str
    subject: str = ""
    subject_kind: str = ""
    klass: str = ""
    severity: str = "warning"
    payload: dict[str, Any] = field(default_factory=dict)
    occurred_at: datetime | None = None
    # A producer that already knows its problem (an investigation report, a
    # session note) names it and skips correlation.
    problem_id: str | None = None


@dataclass(frozen=True)
class IngestResult:
    problem_id: str | None
    # created | attached | reopened | rolled_over | resolved | noted | duplicate | ignored
    action: str
    key: str
    occurrences: int = 0
    muted: bool = False
    # True when the event landed inside a deploy/maintenance window: stored
    # and counted, but nothing downstream should notify on it.
    suppressed: bool = False

    @property
    def investigate(self) -> bool:
        """Whether this event should start an investigation: the hub decides,
        the producer dispatches. A problem is investigated when it appears
        (or comes back), never on a repeat occurrence, never while suppressed
        or muted."""
        return (
            self.action in {"created", "reopened", "rolled_over", "promoted"}
            and not self.suppressed
            and not self.muted
        )

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "investigate": self.investigate}


@dataclass(frozen=True)
class Decision:
    """Pure outcome of :func:`decide`: what to do and the status to move to
    (``None`` = leave the status alone)."""

    action: str
    status: str | None = None


def _slug(text: str, cap: int = _SEGMENT_CAP) -> str:
    return _SLUG_RE.sub("-", (text or "").strip().lower()).strip("-")[:cap]


def slug(text: str) -> str:
    """The hub's normalisation of a subject or class, for callers that must
    match what `correlation_key` stored."""
    return _slug(text)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(ts: datetime | None, default: datetime) -> datetime:
    if ts is None:
        return default
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def normalize_severity(value: str) -> str:
    v = (value or "").strip().lower()
    if v in SEVERITIES:
        return v
    if v in {"warn", "minor"}:
        return "warning"
    if v in {"fatal", "major", "page"}:
        return "critical"
    if v in {"notice", "debug"}:
        return "info"
    return "warning"


def correlation_key(event: Event) -> str:
    """``'{class}:{subject_kind}:{subject}'``, lowercased and slug-safe.

    ``''`` when there is neither a class nor a subject: such an event creates
    a problem and is never attached to one. A subject with no class is keyed
    under ``manual`` so two hand-captured reports about the same service still
    meet. Stale and failing are different classes on purpose: they have
    different fixes.
    """
    klass = _slug(event.klass)
    subject = _slug(event.subject)
    if not klass and not subject:
        return ""
    kind = _slug(event.subject_kind) or ("service" if subject else "")
    return f"{klass or 'manual'}:{kind}:{subject}"


def validate_event(event: Event) -> None:
    """Raise ``ValueError`` on an event the hub must not store."""
    if event.source not in SOURCES:
        raise ValueError(f"unknown source {event.source!r}")
    if event.kind not in KINDS:
        raise ValueError(f"unknown kind {event.kind!r}")
    if not (event.external_id or "").strip():
        raise ValueError("external_id is required")
    if not (event.title or "").strip():
        raise ValueError("title is required")
    if event.problem_id is not None and not (event.problem_id or "").strip():
        raise ValueError("problem_id must be non-empty when given")


def decide(
    current: dict[str, Any] | None,
    kind: str,
    *,
    now: datetime,
    reopen_window: timedelta = REOPEN_WINDOW,
    suppressed: bool = False,
) -> Decision:
    """The transition table. ``current`` is the open-or-resolved problem
    holding the event's key (or the one it named), ``None`` when there is none.
    ``suppressed`` says the event's subject is inside a deploy/maintenance
    window right now.

    Pure, so the whole matrix is unit-tested without a database.
    """
    if kind == "occurrence":
        target = "suppressed" if suppressed else "open"
        if current is None or current["status"] == "closed":
            return Decision("create", target)
        if current["status"] == "suppressed":
            # Still inside the window: another quiet occurrence. Outside it:
            # the deploy did not fix this, so it becomes a real open problem.
            return Decision("attach") if suppressed else Decision("promote", "open")
        if current["status"] in LIVE_STATUSES:
            return Decision("attach")
        # resolved
        resolved_at = _aware(current.get("resolved_at"), now)
        if now - resolved_at <= reopen_window:
            return Decision("reopen", target)
        return Decision("rollover", target)
    if kind == "resolved":
        if current is None or current["status"] in {"resolved", "closed"}:
            # Nothing to resolve. A resolved event on an already-resolved
            # problem is still worth keeping as history.
            return Decision("ignore" if current is None else "note")
        return Decision("resolve", "resolved")
    # investigation / plan / session_note / human_note: history on a problem
    # the producer named or the key found. Never creates.
    if current is None:
        return Decision("ignore")
    return Decision("note")


async def get_problem(pool: asyncpg.Pool, problem_id: str) -> dict[str, Any] | None:
    row = await pool.fetchrow(
        "SELECT id::text AS id, correlation_key, class, subject, subject_kind, title, "
        "severity, status, first_seen_at, last_seen_at, occurrences, muted_until, "
        "resolved_at, closed_at, todoist_task_id, github_issue, metadata "
        "FROM problems WHERE id = $1::uuid",
        problem_id,
    )
    return dict(row) if row else None


async def find_open_problem(pool: asyncpg.Pool, key: str) -> dict[str, Any] | None:
    """The problem currently holding ``key``: open, or resolved but not yet
    closed. ``None`` for an empty key — uncorrelated problems are never found."""
    if not key:
        return None
    row = await pool.fetchrow(
        "SELECT id::text AS id, status, resolved_at, muted_until, occurrences "
        "FROM problems WHERE correlation_key = $1 AND closed_at IS NULL",
        key,
    )
    return dict(row) if row else None


async def list_events(
    pool: asyncpg.Pool, problem_id: str, limit: int = 50
) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        "SELECT id, source, external_id, kind, severity, payload, occurred_at, created_at "
        "FROM problem_events WHERE problem_id = $1::uuid "
        "ORDER BY occurred_at DESC, id DESC LIMIT $2",
        problem_id,
        limit,
    )
    return [dict(r) for r in rows]


async def ingest_event(
    pool: asyncpg.Pool, event: Event, *, now: datetime | None = None
) -> IngestResult:
    """Record ``event`` against the problem it belongs to, creating one when
    needed. One transaction; never notifies; never touches Todoist.

    Concurrency: creates for the same key are serialised on a transaction-
    scoped advisory lock over the key, so two producers reporting the same
    outage in the same second get one problem. A producer treats a raised
    error as "not recorded" and retries; the ``(source, external_id)`` claim
    makes the retry safe.
    """
    validate_event(event)
    now = now or _utcnow()
    occurred_at = _aware(event.occurred_at, now)
    key = correlation_key(event)
    severity = normalize_severity(event.severity)

    async with pool.acquire() as conn, conn.transaction():
        dup = await conn.fetchval(
            "SELECT problem_id::text FROM problem_events WHERE source = $1 AND external_id = $2",
            event.source,
            event.external_id,
        )
        if dup is not None:
            return IngestResult(dup, "duplicate", key)

        if event.problem_id:
            current = await conn.fetchrow(
                "SELECT id::text AS id, status, resolved_at, muted_until, occurrences "
                "FROM problems WHERE id = $1::uuid FOR UPDATE",
                event.problem_id,
            )
            current = dict(current) if current else None
        elif key:
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", key)
            current = await conn.fetchrow(
                "SELECT id::text AS id, status, resolved_at, muted_until, occurrences "
                "FROM problems WHERE correlation_key = $1 AND closed_at IS NULL FOR UPDATE",
                key,
            )
            current = dict(current) if current else None
        else:
            current = None

        suppression = None
        if event.kind == "occurrence":
            suppression = await _active_suppression(
                conn, _slug(event.subject), _slug(event.subject_kind) or "service", now
            )
        d = decide(current, event.kind, now=now, suppressed=suppression is not None)
        if d.action == "ignore":
            return IngestResult(None, "ignored", key)

        payload = dict(event.payload or {})
        if suppression is not None:
            payload["suppressed_by"] = {
                "state": suppression["state"],
                "set_by": suppression["set_by"],
                "note": suppression["note"],
                "until_at": suppression["until_at"].isoformat() if suppression["until_at"] else None,
            }
        muted = False
        if d.action in {"create", "rollover"}:
            if d.action == "rollover":
                await conn.execute(
                    "UPDATE problems SET status = 'closed', closed_at = $2 WHERE id = $1::uuid",
                    current["id"],
                    now,
                )
            problem_id = await conn.fetchval(
                "INSERT INTO problems (correlation_key, class, subject, subject_kind, title, "
                "severity, status, first_seen_at, last_seen_at, occurrences) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $8, 1) RETURNING id::text",
                key,
                _slug(event.klass) or "manual",
                _slug(event.subject),
                _slug(event.subject_kind) or ("service" if _slug(event.subject) else ""),
                event.title.strip()[:500],
                severity,
                d.status,
                occurred_at,
            )
            occurrences = 1
            if d.action == "rollover":
                await conn.execute(
                    "INSERT INTO problem_links (problem_id, link_kind, ref) "
                    "VALUES ($1::uuid, 'problem', $2) ON CONFLICT DO NOTHING",
                    problem_id,
                    current["id"],
                )
        else:
            problem_id = current["id"]
            muted_until = current.get("muted_until")
            muted = muted_until is not None and _aware(muted_until, now) > now
            if d.action in {"attach", "reopen", "promote"}:
                occurrences = await conn.fetchval(
                    "UPDATE problems SET occurrences = occurrences + 1, "
                    "last_seen_at = GREATEST(last_seen_at, $2), "
                    "status = COALESCE($3, status), "
                    "resolved_at = CASE WHEN $3 IS NULL THEN resolved_at ELSE NULL END "
                    "WHERE id = $1::uuid RETURNING occurrences",
                    problem_id,
                    occurred_at,
                    d.status,
                )
            elif d.action == "resolve":
                await conn.execute(
                    "UPDATE problems SET status = 'resolved', resolved_at = $2 "
                    "WHERE id = $1::uuid",
                    problem_id,
                    occurred_at,
                )
                occurrences = int(current.get("occurrences") or 0)
            else:  # note
                occurrences = int(current.get("occurrences") or 0)

        await conn.execute(
            "INSERT INTO problem_events (problem_id, source, external_id, kind, severity, "
            "payload, occurred_at) VALUES ($1::uuid, $2, $3, $4, $5, $6, $7) "
            "ON CONFLICT (source, external_id) DO NOTHING",
            problem_id,
            event.source,
            event.external_id,
            event.kind,
            severity,
            payload,
            occurred_at,
        )
        if d.status is not None:
            # The transition itself is history: the digest and the timeline
            # read it rather than diffing problem rows.
            await conn.execute(
                "INSERT INTO problem_events (problem_id, source, external_id, kind, severity, "
                "payload, occurred_at) VALUES ($1::uuid, 'hub', $2, 'state_change', $3, $4, $5) "
                "ON CONFLICT (source, external_id) DO NOTHING",
                problem_id,
                f"{event.source}:{event.external_id}:{d.action}",
                severity,
                {"action": d.action, "status": d.status},
                occurred_at,
            )

    action = {
        "create": "created",
        "attach": "attached",
        "reopen": "reopened",
        "rollover": "rolled_over",
        "promote": "promoted",
        "resolve": "resolved",
        "note": "noted",
    }[d.action]
    logger.info(
        "hub_event_ingested",
        source=event.source,
        kind=event.kind,
        action=action,
        problem_id=problem_id,
        key=key,
    )
    return IngestResult(
        problem_id,
        action,
        key,
        occurrences=occurrences,
        muted=muted,
        suppressed=suppression is not None,
    )


async def _active_suppression(
    conn: asyncpg.Connection, subject: str, subject_kind: str, now: datetime
) -> asyncpg.Record | None:
    """The `service_state` row that suppresses ``subject`` right now, if any.
    An exact match wins over the `*` wildcard (a whole-kind or global
    maintenance window, e.g. a planned power cut)."""
    return await conn.fetchrow(
        "SELECT subject, subject_kind, state, until_at, set_by, note FROM service_state "
        "WHERE state = ANY($4::text[]) AND (until_at IS NULL OR until_at > $3) "
        "AND ((subject = $1 AND subject_kind = $2) "
        "     OR (subject = '*' AND subject_kind IN ($2, '*'))) "
        "ORDER BY (subject = '*') LIMIT 1",
        subject,
        subject_kind,
        now,
        sorted(SUPPRESSING_STATES),
    )


async def set_service_state(
    pool: asyncpg.Pool,
    subject: str,
    state: str,
    *,
    subject_kind: str = "service",
    minutes: int | None = None,
    until_at: datetime | None = None,
    set_by: str,
    note: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Declare what is happening to ``subject``. ``ok`` clears the row; the
    other states upsert it, open-ended unless ``minutes`` or ``until_at`` is
    given. Raises ``ValueError`` on an unknown state or empty subject."""
    state = (state or "").strip().lower()
    if state not in SERVICE_STATES:
        raise ValueError(f"unknown state {state!r}")
    subj = "*" if (subject or "").strip() == "*" else _slug(subject)
    kind = "*" if (subject_kind or "").strip() == "*" else (_slug(subject_kind) or "service")
    if not subj:
        raise ValueError("subject is required")
    if not (set_by or "").strip():
        raise ValueError("set_by is required")
    now = now or _utcnow()
    if state == "ok":
        cleared = await pool.fetchval(
            "DELETE FROM service_state WHERE subject = $1 AND subject_kind = $2 RETURNING subject",
            subj,
            kind,
        )
        logger.info("service_state_cleared", subject=subj, subject_kind=kind, set_by=set_by)
        return {"subject": subj, "subject_kind": kind, "state": "ok", "cleared": cleared is not None}
    if until_at is None and minutes is not None:
        until_at = now + timedelta(minutes=max(int(minutes), 1))
    row = await pool.fetchrow(
        "INSERT INTO service_state (subject, subject_kind, state, until_at, set_by, note, updated_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7) "
        "ON CONFLICT (subject, subject_kind) DO UPDATE SET state = EXCLUDED.state, "
        "until_at = EXCLUDED.until_at, set_by = EXCLUDED.set_by, note = EXCLUDED.note, "
        "updated_at = EXCLUDED.updated_at "
        "RETURNING subject, subject_kind, state, until_at, set_by, note, updated_at",
        subj,
        kind,
        state,
        _aware(until_at, now) if until_at else None,
        set_by.strip(),
        (note or "").strip()[:500],
        now,
    )
    logger.info("service_state_set", subject=subj, subject_kind=kind, state=state, set_by=set_by)
    return dict(row)


async def list_service_states(
    pool: asyncpg.Pool, *, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Every row still in force: open-ended, or with an `until_at` in the future."""
    rows = await pool.fetch(
        "SELECT subject, subject_kind, state, until_at, set_by, note, updated_at "
        "FROM service_state WHERE until_at IS NULL OR until_at > $1 "
        "ORDER BY updated_at DESC",
        now or _utcnow(),
    )
    return [dict(r) for r in rows]


async def clear_converged_deploys(
    pool: asyncpg.Pool, stuck_subjects: list[str], *, now: datetime | None = None
) -> list[str]:
    """Clear `deploying` service rows whose service is no longer below its
    desired replicas — the safety net for a deploy job that crashed before
    posting `ok`. Rows younger than ``CONVERGE_GRACE`` and the `*` wildcard
    are left alone."""
    now = now or _utcnow()
    rows = await pool.fetch(
        "DELETE FROM service_state WHERE state = 'deploying' AND subject_kind = 'service' "
        "AND subject <> '*' AND NOT (subject = ANY($1::text[])) AND updated_at < $2 "
        "RETURNING subject",
        [_slug(x) for x in stuck_subjects],
        now - CONVERGE_GRACE,
    )
    cleared = [r["subject"] for r in rows]
    if cleared:
        logger.info("service_state_converged", subjects=cleared)
    return cleared


async def stale_open_problems(
    pool: asyncpg.Pool, subjects: list[str], *, hours: float, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Live problems on ``subjects`` first seen more than ``hours`` ago with no
    `investigation` event in that long: due for a re-investigation. The
    heartbeat asks this for the services it still sees stuck, which replaces
    the per-service clocks it used to keep in a settings row."""
    if not subjects:
        return []
    now = now or _utcnow()
    cutoff = now - timedelta(hours=max(float(hours), 0.0))
    rows = await pool.fetch(
        "SELECT p.id::text AS id, p.subject, p.class, p.first_seen_at, p.occurrences "
        "FROM problems p WHERE p.closed_at IS NULL AND p.status = ANY($3::text[]) "
        "  AND p.subject = ANY($1::text[]) AND p.first_seen_at < $2 "
        "  AND NOT EXISTS (SELECT 1 FROM problem_events e WHERE e.problem_id = p.id "
        "                  AND e.kind = 'investigation' AND e.occurred_at >= $2) "
        "ORDER BY p.first_seen_at",
        subjects,
        cutoff,
        sorted(LIVE_STATUSES - {"suppressed"}),
    )
    return [
        {**dict(r), "hours": round((now - _aware(r["first_seen_at"], now)).total_seconds() / 3600, 1)}
        for r in rows
    ]


async def promote_expired_suppressions(
    pool: asyncpg.Pool, *, now: datetime | None = None
) -> list[str]:
    """Every `suppressed` problem whose window has passed becomes `open`: the
    deploy or maintenance did not make it go away. Returns the promoted ids."""
    now = now or _utcnow()
    promoted: list[str] = []
    async with pool.acquire() as conn, conn.transaction():
        rows = await conn.fetch(
            "SELECT id::text AS id, subject, subject_kind, severity FROM problems "
            "WHERE status = 'suppressed' AND closed_at IS NULL FOR UPDATE"
        )
        for row in rows:
            if await _active_suppression(conn, row["subject"], row["subject_kind"], now):
                continue
            await conn.execute(
                "UPDATE problems SET status = 'open' WHERE id = $1::uuid", row["id"]
            )
            await conn.execute(
                "INSERT INTO problem_events (problem_id, source, external_id, kind, severity, "
                "payload, occurred_at) VALUES ($1::uuid, 'hub', $2, 'state_change', $3, $4, $5) "
                "ON CONFLICT (source, external_id) DO NOTHING",
                row["id"],
                f"promote:{row['id']}:{now.isoformat()}",
                row["severity"],
                {"action": "promote", "status": "open", "reason": "suppression_expired"},
                now,
            )
            promoted.append(row["id"])
    if promoted:
        logger.info("hub_suppressions_promoted", count=len(promoted))
    return promoted


async def set_status(
    pool: asyncpg.Pool,
    problem_id: str,
    status: str,
    *,
    reason: str,
    source: str = "investigation",
    now: datetime | None = None,
) -> bool:
    """Move a live problem to ``status`` (an investigation's own transitions:
    investigating / waiting_human / fixing / resolved), writing the
    state_change event. False when the problem is missing, closed, or already
    there. Never resurrects a closed problem."""
    if status not in STATUSES or status == "closed":
        raise ValueError(f"cannot set status {status!r}")
    now = now or _utcnow()
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "SELECT status, severity FROM problems WHERE id = $1::uuid AND closed_at IS NULL "
            "FOR UPDATE",
            problem_id,
        )
        if row is None or row["status"] == status:
            return False
        await conn.execute(
            "UPDATE problems SET status = $2, "
            "resolved_at = CASE WHEN $2 = 'resolved' THEN $3 ELSE resolved_at END "
            "WHERE id = $1::uuid",
            problem_id,
            status,
            now,
        )
        await conn.execute(
            "INSERT INTO problem_events (problem_id, source, external_id, kind, severity, "
            "payload, occurred_at) VALUES ($1::uuid, 'hub', $2, 'state_change', $3, $4, $5) "
            "ON CONFLICT (source, external_id) DO NOTHING",
            problem_id,
            f"{source}:{problem_id}:{status}:{now.isoformat()}",
            row["severity"],
            {"action": "set_status", "status": status, "reason": reason[:300]},
            now,
        )
    logger.info("hub_status_set", problem_id=problem_id, status=status, reason=reason[:80])
    return True


async def mute_problem(
    pool: asyncpg.Pool,
    problem_id: str,
    *,
    hours: float,
    by: str,
    reason: str = "",
    now: datetime | None = None,
) -> datetime | None:
    """Silence a problem until ``now + hours``: occurrences are still recorded
    and counted, nothing is projected or investigated. The mute key *is* the
    problem (the old `alert_mutes` table and its four key namespaces are gone). Returns the new
    `muted_until`, or None when the problem is missing or closed."""
    now = now or _utcnow()
    until = now + timedelta(hours=max(float(hours), 0.0))
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "UPDATE problems SET muted_until = $2 WHERE id = $1::uuid AND closed_at IS NULL "
            "RETURNING severity",
            problem_id,
            until,
        )
        if row is None:
            return None
        await conn.execute(
            "INSERT INTO problem_events (problem_id, source, external_id, kind, severity, "
            "payload, occurred_at) VALUES ($1::uuid, 'hub', $2, 'state_change', $3, $4, $5) "
            "ON CONFLICT (source, external_id) DO NOTHING",
            problem_id,
            f"mute:{problem_id}:{now.isoformat()}",
            row["severity"],
            {"action": "mute", "until": until.isoformat(), "by": by, "reason": reason[:300]},
            now,
        )
    logger.info("hub_problem_muted", problem_id=problem_id, until=until.isoformat(), by=by)
    return until


def event_from_alert(
    alert: dict[str, Any],
    *,
    occurred_at: datetime,
    resolved: bool = False,
) -> Event:
    """Translate the alert dict every current producer builds (alertmanager,
    grafana, sentry, heartbeat, clarify's synthetic alerts) into an
    :class:`Event`. This is the seam PR 3 swaps the producers over at.

    The occurrence id is the fingerprint **plus** the occurrence time, because
    an alertmanager fingerprint is stable per label set and a rule that fires
    again next week is a new occurrence, not a duplicate.
    """
    raw_source = str(alert.get("source") or "").strip()
    source = _SOURCE_ALIASES.get(raw_source, raw_source)
    if source not in SOURCES:
        source = "manual"
    labels = alert.get("labels") if isinstance(alert.get("labels"), dict) else {}
    raw = alert.get("raw_payload") if isinstance(alert.get("raw_payload"), dict) else {}
    fingerprint = str(alert.get("fingerprint") or "").strip()
    alertname = str(labels.get("alertname") or "").strip()

    klass = alertname
    subject = str(labels.get("service_name") or labels.get("service") or "").strip()
    subject_kind = "service" if subject else ""
    if source == "sentry":
        meta = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        klass = str(meta.get("type") or "").strip() or (
            f"sentry-{raw.get('id')}" if raw.get("id") else ""
        )
        subject = str(alert.get("service") or "").strip()
        subject_kind = "service" if subject else ""
    elif source == "heartbeat":
        m = _HEARTBEAT_FP_RE.match(fingerprint)
        if m:
            klass = klass or m.group(1)
            if not subject:
                subject = m.group(2).strip()
                subject_kind = "node" if klass.lower() in _NODE_CLASSES else "service"
    if not subject:
        node = str(labels.get("node") or labels.get("nodename") or "").strip()
        if node and (klass.lower() in _NODE_CLASSES or not alert.get("service")):
            subject, subject_kind = node, "node"
        else:
            subject = str(alert.get("service") or "").strip()
            subject_kind = "service" if subject else ""

    if source == "sentry":
        # An issue reaches the hub twice — webhook and the 30-min poll — with
        # the same `lastSeen`; that is one occurrence, not two.
        stamp = str(raw.get("lastSeen") or raw.get("firstSeen") or occurred_at.isoformat())
    else:
        stamp = str(raw.get("endsAt" if resolved else "startsAt") or occurred_at.isoformat())
    external_id = f"{fingerprint or _slug(str(alert.get('title') or 'alert'))}@{stamp}"
    if resolved:
        external_id += "@resolved"
    return Event(
        source=source,
        external_id=external_id,
        kind="resolved" if resolved else "occurrence",
        title=str(alert.get("title") or alertname or "Alert").strip(),
        subject=subject,
        subject_kind=subject_kind,
        klass=klass,
        severity=str(alert.get("severity") or "warning"),
        payload={
            "fingerprint": fingerprint,
            "labels": labels,
            "description": str(alert.get("description") or "")[:2000],
        },
        occurred_at=occurred_at,
    )
