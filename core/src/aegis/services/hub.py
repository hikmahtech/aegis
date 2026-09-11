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
  and no subject gets an empty key and is never auto-attached. There is no
  fuzzy "possibly the same as" match on the way in — an unmatched key creates.

The one exception to that last rule is a **group** (:func:`group_key`), and it
is exact rather than fuzzy. When the same failure keeps happening to different
entities — six Postiz posts wedged in one queue — an operator or the sweep's
LLM judge folds those problems into a single group problem keyed on the class
and the kind of subject alone. From then on an occurrence of that class whose
own key has no problem is *absorbed* by the group, so the seventh stuck post
joins the group instead of opening a seventh task. Absorption applies to
occurrences only, and only within one class and subject kind: the hub still
never guesses that two different failures are the same one.

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
# after it closes the old problem and starts a new one linked to it. A
# constant on purpose: it describes what an outage IS rather than an operator
# preference, and no deployment has wanted a different one. It becomes an
# `activities.config` key on the hub sweep row when one does.
REOPEN_WINDOW = timedelta(hours=24)

# The subject a group problem carries. `_slug` can only produce `[a-z0-9-]`,
# so no real subject can ever collide with a group's correlation key.
GROUP_SUBJECT = "*"
# The subject kind of a report about one Todoist task that names nothing else
# (`event_from_alert`). Never groupable: each is one person's report.
TASK_SUBJECT_KIND = "task"

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
        # Reconciliation findings: a statement whose closing balance disagrees
        # with the books, an account no statement arrived for, a file nothing
        # could parse, an instrument pass 2b has no entity scope for. The money
        # lane predates the hub by two days and never met it, so it grew its
        # own dedupe, its own noise guards and no alert path at all — see §15
        # of the statement-reconciliation spec, which puts it back here.
        "money",
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
    # True when the event's own key had no problem and a group problem for its
    # class took it. The caller learns which problem from `problem_id`.
    absorbed: bool = False

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


def group_key(klass: str, subject_kind: str) -> str:
    """``'{class}:{subject_kind}'`` — a group problem's identity.

    A group stands for one failure happening to many entities, so it is keyed
    on what the failure is and what kind of thing it happens to, never on
    which entity it happened to this time.

    ``''`` — not groupable — in four cases, and each one matters:

    * no subject kind: there is no "many entities" to speak of;
    * no class: an event the hub could not classify is the last thing that
      should be swept into a group with others it merely resembles;
    * class ``manual``: that is what `hub_project.ensure_problem_for_task`
      mints for a hand-written task, whose subject is the task itself. Three
      open ``@code`` tasks are three pieces of work, never one condition, and
      folding them would move one task's sessions, PRs and comments onto
      another;
    * subject kind ``task``: the same thing reached from the other side — a
      report keyed on the Todoist task it came from (`event_from_alert`).
      Three people's "noon is down" tasks are three reports, and a group
      would close two of them and swallow the next one's investigation.
    """
    k = _slug(klass)
    kind = _slug(subject_kind)
    if not k or k == "manual" or not kind or kind == TASK_SUBJECT_KIND:
        return ""
    return f"{k}:{kind}"


def group_correlation_key(gkey: str) -> str:
    """The ``correlation_key`` a group problem holds, from its group key."""
    return f"{gkey}:{GROUP_SUBJECT}"


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
        "resolved_at, closed_at, todoist_task_id, group_key, metadata "
        "FROM problems WHERE id = $1::uuid",
        problem_id,
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
        # The lock comes FIRST, and the duplicate claim is read under it.
        # Checked before the lock, two simultaneous deliveries of one event
        # both saw "not a duplicate", then serialised here and both counted an
        # occurrence — while the second event insert silently did nothing. A
        # retry was always safe; concurrent delivery was not.
        if key and not event.problem_id:
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", key)
        dup = await conn.fetchval(
            "SELECT problem_id::text FROM problem_events WHERE source = $1 AND external_id = $2",
            event.source,
            event.external_id,
        )
        if dup is not None:
            return IngestResult(dup, "duplicate", key)

        if event.problem_id:
            current = await conn.fetchrow(
                "SELECT id::text AS id, status, resolved_at, muted_until, occurrences, severity "
                "FROM problems WHERE id = $1::uuid FOR UPDATE",
                event.problem_id,
            )
            current = dict(current) if current else None
        elif key:
            current = await conn.fetchrow(
                "SELECT id::text AS id, status, resolved_at, muted_until, occurrences, severity "
                "FROM problems WHERE correlation_key = $1 AND closed_at IS NULL FOR UPDATE",
                key,
            )
            current = dict(current) if current else None
        else:
            current = None

        # The kind a new problem is STORED with, so the suppression lookup and
        # the sweep that promotes it later ask the same question. They used to
        # differ for a subject-less event ("service" here, "" in the row),
        # which let a window suppress an occurrence that the next sweep then
        # promoted while the window was still in force.
        subject_slug = _slug(event.subject)
        kind_slug = _slug(event.subject_kind) or ("service" if subject_slug else "")

        # No problem holds this key. Before creating one, ask whether a GROUP
        # problem has claimed this class of failure: six posts stuck in one
        # queue are one condition, and once they have been folded into a group
        # the seventh belongs there too rather than opening a seventh task.
        # Occurrences only — a `resolved` for one member says nothing about
        # the group, which recovers when its watchdog stops finding any member
        # (see hub_watch.reconcile_findings).
        absorbed = False
        group: dict[str, Any] | None = None
        if current is None and event.kind == "occurrence" and not event.problem_id:
            gkey = group_key(event.klass, kind_slug)
            if gkey:
                row = await conn.fetchrow(
                    "SELECT id::text AS id, status, resolved_at, muted_until, occurrences, "
                    "severity, title, group_key FROM problems "
                    "WHERE group_key = $1 AND closed_at IS NULL FOR UPDATE",
                    gkey,
                )
                if row is not None:
                    current, group, absorbed = dict(row), dict(row), True

        suppression = None
        if event.kind == "occurrence":
            suppression = await _active_suppression(conn, subject_slug, kind_slug, now)
        d = decide(current, event.kind, now=now, suppressed=suppression is not None)
        if d.action == "ignore":
            return IngestResult(None, "ignored", key)

        payload = dict(event.payload or {})
        if absorbed:
            # Which entity this occurrence was about. The group's own subject
            # is `*`, so without this the timeline could not name the member.
            payload["member_subject"] = subject_slug
            payload["member_key"] = key
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
            # A rollover of a GROUP stays a group. The old one resolved longer
            # ago than the reopen window, so this occurrence starts a fresh
            # problem — but the condition is the same one somebody already
            # decided was shared, and re-keying it on this member's subject
            # would throw that away and make the sweep re-judge the cluster
            # from scratch.
            rolled_group = group if (absorbed and d.action == "rollover") else None
            problem_id = await conn.fetchval(
                "INSERT INTO problems (correlation_key, class, subject, subject_kind, title, "
                "severity, status, first_seen_at, last_seen_at, occurrences, group_key) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $8, 1, $9) RETURNING id::text",
                group_correlation_key(rolled_group["group_key"]) if rolled_group else key,
                _slug(event.klass) or "manual",
                GROUP_SUBJECT if rolled_group else subject_slug,
                kind_slug,
                (rolled_group["title"] if rolled_group else event.title.strip())[:500],
                severity,
                d.status,
                occurred_at,
                rolled_group["group_key"] if rolled_group else None,
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
                # A problem is as bad as its worst occurrence (#486): a cert
                # that opened at 14 days as `warning` is `critical` once its
                # daily occurrence is. Raised, never lowered — one milder
                # occurrence does not make a once-critical problem fine. The
                # ordering is the one a group uses for its worst member;
                # imported here because `hub_group` imports this module.
                from aegis.services.hub_group import worst

                occurrences = await conn.fetchval(
                    "UPDATE problems SET occurrences = occurrences + 1, "
                    "last_seen_at = GREATEST(last_seen_at, $2), "
                    "status = COALESCE($3, status), "
                    "resolved_at = CASE WHEN $3 IS NULL THEN resolved_at ELSE NULL END, "
                    "severity = $4 "
                    "WHERE id = $1::uuid RETURNING occurrences",
                    problem_id,
                    occurred_at,
                    d.status,
                    worst([current.get("severity") or "", severity]),
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
        absorbed=absorbed,
    )
    return IngestResult(
        problem_id,
        action,
        key,
        occurrences=occurrences,
        muted=muted,
        suppressed=suppression is not None,
        absorbed=absorbed,
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
    pool: asyncpg.Pool,
    subjects: list[str],
    *,
    hours: float,
    classes: list[str] | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Live problems on ``subjects`` first seen more than ``hours`` ago with no
    `investigation` event in that long: due for a re-investigation. The
    heartbeat asks this for the services it still sees stuck, which replaces
    the per-service clocks it used to keep in a settings row.

    ``classes`` narrows it to the kinds of problem the caller means. Without
    it the subject alone matched, so a heartbeat asking about a stuck service
    also got back that service's unrelated memory alert and re-investigated
    it as "still stuck" — and a `waiting_human` problem sitting on an open
    gate card was re-investigated underneath the person answering it."""
    if not subjects:
        return []
    now = now or _utcnow()
    cutoff = now - timedelta(hours=max(float(hours), 0.0))
    rows = await pool.fetch(
        "SELECT p.id::text AS id, p.subject, p.class, p.first_seen_at, p.occurrences "
        "FROM problems p WHERE p.closed_at IS NULL AND p.status = ANY($3::text[]) "
        "  AND p.subject = ANY($1::text[]) AND p.first_seen_at < $2 "
        "  AND ($4::text[] IS NULL OR p.class = ANY($4::text[])) "
        "  AND NOT EXISTS (SELECT 1 FROM problem_events e WHERE e.problem_id = p.id "
        "                  AND e.kind = 'investigation' AND e.occurred_at >= $2) "
        "ORDER BY p.first_seen_at",
        subjects,
        cutoff,
        # `waiting_human` is excluded with `suppressed`: a problem sitting on
        # an open gate card is waiting for a person, not for another
        # investigation to talk over them.
        sorted(LIVE_STATUSES - {"suppressed", "waiting_human"}),
        [_slug(c) for c in classes] if classes else None,
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
    state_change event. Never resurrects a closed problem.

    False when nothing moved: the problem is missing, closed or already
    there — or it is ``resolved`` and ``source`` is an investigation asking
    for a live status, which is recorded by the caller as history and leaves
    the problem resolved (see below)."""
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
        reopening = row["status"] == "resolved" and status in LIVE_STATUSES
        if reopening and source == "investigation":
            # The alert source owns whether a problem is live; an
            # investigation only annotates it. A verdict that lands after the
            # alert cleared (prod 2140a366: resolved 10:17, "not actionable"
            # card 10:20) used to reopen the problem and its task for an
            # incident that was already over, and it stayed live for days.
            # The verdict is still on the timeline — the caller writes it as
            # an `investigation` event before calling this — but the status
            # stays `resolved`.
            #
            # This is also what the old reopen here was for, done properly:
            # an investigation reporting `fixing` after the alert cleared left
            # a closed task on a live problem, and the next occurrence
            # ATTACHED to it in silence. With the problem left resolved, the
            # next occurrence goes through `ingest_event` instead — a reopen
            # inside REOPEN_WINDOW, a new problem after it — so the task
            # follows and a fresh investigation is asked for.
            logger.info(
                "hub_status_held",
                problem_id=problem_id,
                status=status,
                held="resolved",
                reason=reason[:80],
            )
            return False
        # Any other caller coming BACK from `resolved` is a reopen: the stale
        # `resolved_at` has to go, or `close_resolved` never retires the
        # problem and the projector leaves its task completed while the
        # problem is live again. The event says `reopen` so the projector
        # uncompletes the task.
        await conn.execute(
            "UPDATE problems SET status = $2, "
            "resolved_at = CASE WHEN $2 = 'resolved' THEN $3 WHEN $4 THEN NULL "
            "ELSE resolved_at END "
            "WHERE id = $1::uuid",
            problem_id,
            status,
            now,
            reopening,
        )
        await conn.execute(
            "INSERT INTO problem_events (problem_id, source, external_id, kind, severity, "
            "payload, occurred_at) VALUES ($1::uuid, 'hub', $2, 'state_change', $3, $4, $5) "
            "ON CONFLICT (source, external_id) DO NOTHING",
            problem_id,
            f"{source}:{problem_id}:{status}:{now.isoformat()}",
            row["severity"],
            {
                # `resolve` and `reopen` are the two words the PROJECTOR acts
                # on: it closes a task on one and reopens it on the other.
                # Writing `set_status` for a move into `resolved` left the
                # problem resolved and its task open with no closing comment —
                # so an investigation that ended `resolved`, and the admin
                # panel's Resolve button, both said nothing to the human
                # looking at the task. A resolve reached this way is the same
                # event as a resolve reached by an incoming `resolved` alert.
                "action": (
                    "reopen" if reopening else ("resolve" if status == "resolved" else "set_status")
                ),
                "status": status,
                "reason": reason[:300],
            },
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
    occurrence_key: str = "",
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
    task_id = str(alert.get("todoist_task_id") or "").strip()
    if not subject and task_id:
        # A report about one Todoist task that names nothing else — clarify's
        # content-route alerts, whose class comes from the route. With no
        # subject it was keyed `nodedown::`, so every such report attached to
        # the first one and the hub answered "a repeat, don't investigate"
        # (#472). The task is the one thing it is certainly about. Only this
        # shape moves: an alertmanager rule or a Sentry issue with no subject
        # carries no task and keeps its one key per class.
        subject, subject_kind = task_id, TASK_SUBJECT_KIND

    if source == "sentry":
        # An issue reaches the hub twice — webhook and the 30-min poll — with
        # the same `lastSeen`; that is one occurrence, not two.
        stamp = str(raw.get("lastSeen") or raw.get("firstSeen") or occurred_at.isoformat())
    else:
        stamp = str(raw.get("endsAt" if resolved else "startsAt") or occurred_at.isoformat())
    # `occurrence_key` is for a caller that has a stamp of its own which
    # survives a retry. Alertmanager and Sentry payloads carry one
    # (`startsAt`, `lastSeen`); a heartbeat or a synthetic alert does not, so
    # without this the wall clock went into the id and a RETRIED ingest minted
    # a second occurrence — which attaches instead of creating, answers
    # `investigate=False`, and silently costs the alert its investigation.
    external_id = f"{fingerprint or _slug(str(alert.get('title') or 'alert'))}@{occurrence_key or stamp}"
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


# --- operator-side helpers (PR 5) --------------------------------------------


async def find_problem_for_task(pool: asyncpg.Pool, task_id: str) -> dict[str, Any] | None:
    """The problem behind a Todoist task: the one whose task it is, else the
    newest one linked to it. Live problems win over closed ones."""
    if not task_id:
        return None
    row = await pool.fetchrow(
        "SELECT p.id::text AS id FROM problems p "
        "LEFT JOIN problem_links l ON l.problem_id = p.id AND l.link_kind = 'todoist_task' "
        "WHERE p.todoist_task_id = $1 OR l.ref = $1 "
        "ORDER BY (p.closed_at IS NULL) DESC, p.last_seen_at DESC LIMIT 1",
        task_id,
    )
    return await get_problem(pool, row["id"]) if row else None


async def add_link(pool: asyncpg.Pool, problem_id: str, link_kind: str, ref: str) -> bool:
    """Attach a reference (a PR url, an issue, another problem). True when new."""
    ref = (ref or "").strip()
    if not ref:
        return False
    tag = await pool.execute(
        "INSERT INTO problem_links (problem_id, link_kind, ref) VALUES ($1::uuid, $2, $3) "
        "ON CONFLICT DO NOTHING",
        problem_id,
        link_kind,
        ref[:500],
    )
    return str(tag).endswith(" 1")


async def merge_problems(
    pool: asyncpg.Pool, keep_id: str, merge_id: str, *, by: str, now: datetime | None = None
) -> dict[str, Any]:
    """Fold ``merge_id`` into ``keep_id``: its events, links and sessions move,
    its occurrences count on the kept problem, and it closes with a `problem`
    link back so the history reads both ways.

    A wrong merge hides an outage, so there are exactly two callers: a person
    on the admin Problems page, and `hub_group.upgrade`, which folds problems
    of ONE class and subject kind into a group of that class after an LLM has
    agreed they are the same condition. The hub still never merges two
    different failures on a resemblance.

    Raises ValueError when either problem is missing, they are the same, or the
    kept one is already closed. The merged problem's own Todoist task is
    returned so the caller can retire it; the hub never touches Todoist.
    """
    now = now or _utcnow()
    if keep_id == merge_id:
        raise ValueError("keep_id and merge_id are the same problem")
    async with pool.acquire() as conn, conn.transaction():
        keep = await conn.fetchrow(
            "SELECT id::text AS id, status, severity, closed_at FROM problems "
            "WHERE id = $1::uuid FOR UPDATE",
            keep_id,
        )
        merged = await conn.fetchrow(
            "SELECT id::text AS id, status, severity, occurrences, first_seen_at, "
            "last_seen_at, todoist_task_id, closed_at FROM problems WHERE id = $1::uuid FOR UPDATE",
            merge_id,
        )
        if keep is None or merged is None:
            raise ValueError("both problems must exist")
        if keep["closed_at"] is not None:
            raise ValueError(f"problem {keep_id} is closed; merge into a live problem")
        moved_events = await conn.execute(
            "UPDATE problem_events SET problem_id = $1::uuid WHERE problem_id = $2::uuid",
            keep_id,
            merge_id,
        )
        # The moved events are HISTORY, not news. Without moving the kept
        # problem's watermark past them, the next projection replays the
        # duplicate's whole timeline as comments — and a moved `resolve`
        # completes the kept problem's task while the problem is still open.
        latest = await conn.fetchval(
            "SELECT COALESCE(max(id), 0) FROM problem_events WHERE problem_id = $1::uuid",
            keep_id,
        )
        await conn.execute(
            "UPDATE problems SET metadata = jsonb_set(metadata, '{projected_event_id}', "
            "to_jsonb(GREATEST(COALESCE((metadata->>'projected_event_id')::bigint, 0), $2::bigint))) "
            "WHERE id = $1::uuid",
            keep_id,
            int(latest or 0),
        )
        # The merged task stays the merged problem's (the caller retires it);
        # everything else the merged problem pointed at now hangs off the kept
        # one too.
        await conn.execute(
            "INSERT INTO problem_links (problem_id, link_kind, ref) "
            "SELECT $1::uuid, link_kind, ref FROM problem_links "
            "WHERE problem_id = $2::uuid AND link_kind <> 'todoist_task' AND ref <> $3 "
            "ON CONFLICT DO NOTHING",
            keep_id,
            merge_id,
            keep_id,
        )
        for a, b in ((keep_id, merge_id), (merge_id, keep_id)):
            await conn.execute(
                "INSERT INTO problem_links (problem_id, link_kind, ref) "
                "VALUES ($1::uuid, 'problem', $2) ON CONFLICT DO NOTHING",
                a,
                b,
            )
        await conn.execute(
            "UPDATE work_sessions SET problem_id = $1::uuid WHERE problem_id = $2::uuid",
            keep_id,
            merge_id,
        )
        await conn.execute(
            "UPDATE problems SET occurrences = occurrences + $2, "
            "first_seen_at = LEAST(first_seen_at, $3), last_seen_at = GREATEST(last_seen_at, $4) "
            "WHERE id = $1::uuid",
            keep_id,
            int(merged["occurrences"] or 0),
            merged["first_seen_at"],
            merged["last_seen_at"],
        )
        if merged["closed_at"] is None:
            await conn.execute(
                "UPDATE problems SET status = 'closed', closed_at = $2, "
                "resolved_at = COALESCE(resolved_at, $2) WHERE id = $1::uuid",
                merge_id,
                now,
            )
        await conn.execute(
            "INSERT INTO problem_events (problem_id, source, external_id, kind, severity, "
            "payload, occurred_at) VALUES ($1::uuid, 'hub', $2, 'state_change', $3, $4, $5) "
            "ON CONFLICT (source, external_id) DO NOTHING",
            keep_id,
            f"merge:{keep_id}:{merge_id}:{now.isoformat()}",
            keep["severity"],
            {"action": "merge", "merged": merge_id, "by": by[:100], "status": keep["status"]},
            now,
        )
    logger.info("hub_problems_merged", keep_id=keep_id, merge_id=merge_id, by=by)
    parts = str(moved_events).split()
    return {
        "keep_id": keep_id,
        "merge_id": merge_id,
        "events_moved": int(parts[-1]) if parts and parts[-1].isdigit() else 0,
        "merged_task_id": merged["todoist_task_id"] or "",
    }


# --- digest, close sweep and admin reads (PR 6) -------------------------------


async def digest(
    pool: asyncpg.Pool, *, hours: float = 24.0, now: datetime | None = None
) -> dict[str, Any]:
    """What the hub saw in the last ``hours``, from the events themselves.

    This replaces the `alert_digest_buffer` settings row, which was written by
    the investigation flow at four call sites and read once a day: an item
    appended by a flow that then failed was in the digest anyway, and one the
    flow never reached was missing from it forever. The problems and their
    events are the record, so the digest is a query over them and can be asked
    for twice.

    Returns counts plus the problems themselves, newest first, so the caller
    can render prose without a second round trip.
    """
    now = now or _utcnow()
    since = now - timedelta(hours=max(float(hours), 0.0))
    rows = await pool.fetch(
        "SELECT p.id::text AS id, p.title, p.class, p.subject, p.subject_kind, p.severity, "
        "       p.status, p.occurrences, p.first_seen_at, p.last_seen_at, p.resolved_at, "
        "       p.muted_until, p.todoist_task_id, "
        "       (p.first_seen_at >= $1) AS is_new, "
        "       EXISTS (SELECT 1 FROM problem_events e WHERE e.problem_id = p.id "
        "               AND e.kind = 'investigation' AND e.occurred_at >= $1) AS investigated "
        "FROM problems p "
        "WHERE EXISTS (SELECT 1 FROM problem_events e WHERE e.problem_id = p.id "
        "              AND e.occurred_at >= $1) "
        "ORDER BY p.last_seen_at DESC",
        since,
    )
    problems = [dict(r) for r in rows]
    live = [p for p in problems if p["status"] in LIVE_STATUSES]
    counts = {
        "total": len(problems),
        "new": sum(1 for p in problems if p["is_new"]),
        "open": sum(1 for p in live if p["status"] not in {"suppressed"}),
        "suppressed": sum(1 for p in problems if p["status"] == "suppressed"),
        "muted": sum(
            1
            for p in problems
            if p["muted_until"] is not None and _aware(p["muted_until"], now) > now
        ),
        "resolved": sum(1 for p in problems if p["status"] in {"resolved", "closed"}),
        "investigated": sum(1 for p in problems if p["investigated"]),
        "occurrences": sum(int(p["occurrences"] or 0) for p in problems),
    }
    return {"since": since, "counts": counts, "problems": problems}


async def close_resolved(
    pool: asyncpg.Pool, *, days: float = 7.0, limit: int = 200, now: datetime | None = None
) -> list[str]:
    """Close problems resolved longer than ``days`` ago. Returns their ids.

    Closing is what frees the correlation key for a genuinely new problem with
    the same subject: the partial unique index covers open keys only. A
    resolved problem is kept live for the reopen window and then some, so a
    service that flaps back the same week attaches to its own history rather
    than starting a fresh one.

    The cutoff is INCLUSIVE, so ``days=0`` means "everything resolved, now" —
    which is what the admin panel's close button asks for on a problem it has
    just resolved. A strict comparison there closed nothing at all.
    """
    now = now or _utcnow()
    cutoff = now - timedelta(days=max(float(days), 0.0))
    rows = await pool.fetch(
        "UPDATE problems SET status = 'closed', closed_at = $1 "
        "WHERE id IN (SELECT id FROM problems WHERE status = 'resolved' AND closed_at IS NULL "
        "             AND resolved_at IS NOT NULL AND resolved_at <= $2 "
        "             ORDER BY resolved_at LIMIT $3) "
        "RETURNING id::text AS id",
        now,
        cutoff,
        max(1, int(limit)),
    )
    ids = [r["id"] for r in rows]
    for problem_id in ids:
        await pool.execute(
            "INSERT INTO problem_events (problem_id, source, external_id, kind, severity, "
            "payload, occurred_at) VALUES ($1::uuid, 'hub', $2, 'state_change', 'info', $3, $4) "
            "ON CONFLICT (source, external_id) DO NOTHING",
            problem_id,
            f"close:{problem_id}:{now.isoformat()}",
            {"action": "close", "reason": f"resolved more than {days:g} days ago"},
            now,
        )
    if ids:
        logger.info("hub_problems_closed", count=len(ids), days=days)
    return ids


async def close_problem(
    pool: asyncpg.Pool, problem_id: str, *, now: datetime | None = None
) -> bool:
    """Close ONE resolved problem. False when it is missing, already closed, or
    not resolved.

    The admin panel's close button used to call ``close_resolved(days=0)``,
    which closes every resolved problem in the database — including ones whose
    resolution has not been projected yet, and a closed problem is never
    projected again, so their tasks were left open with no closing comment.
    """
    now = now or _utcnow()
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "SELECT status FROM problems WHERE id = $1::uuid AND closed_at IS NULL FOR UPDATE",
            problem_id,
        )
        if row is None or row["status"] != "resolved":
            return False
        await conn.execute(
            "UPDATE problems SET status = 'closed', closed_at = $2 WHERE id = $1::uuid",
            problem_id,
            now,
        )
        await conn.execute(
            "INSERT INTO problem_events (problem_id, source, external_id, kind, severity, "
            "payload, occurred_at) VALUES ($1::uuid, 'hub', $2, 'state_change', 'info', $3, $4) "
            "ON CONFLICT (source, external_id) DO NOTHING",
            problem_id,
            f"close:{problem_id}:{now.isoformat()}",
            {"action": "close", "reason": "closed by hand"},
            now,
        )
    logger.info("hub_problem_closed", problem_id=problem_id)
    return True


async def list_problems(
    pool: asyncpg.Pool,
    *,
    status: str = "",
    subject: str = "",
    include_closed: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Problems for the admin list, newest activity first. Live only unless
    ``include_closed``; ``status`` narrows to one status."""
    where = ["TRUE" if include_closed else "p.closed_at IS NULL"]
    args: list[Any] = []
    if status:
        args.append(status)
        where.append(f"p.status = ${len(args)}")
    if subject:
        args.append(_slug(subject))
        where.append(f"p.subject = ${len(args)}")
    args.extend([max(1, min(int(limit), 500)), max(0, int(offset))])
    rows = await pool.fetch(
        "SELECT p.id::text AS id, p.correlation_key, p.class, p.subject, p.subject_kind, "
        "       p.title, p.severity, p.status, p.first_seen_at, p.last_seen_at, p.occurrences, "
        "       p.muted_until, p.resolved_at, p.closed_at, p.todoist_task_id, p.group_key "
        f"FROM problems p WHERE {' AND '.join(where)} "
        f"ORDER BY p.last_seen_at DESC LIMIT ${len(args) - 1} OFFSET ${len(args)}",
        *args,
    )
    return [dict(r) for r in rows]


async def problem_detail(
    pool: asyncpg.Pool, problem_id: str, *, events: int = 50
) -> dict[str, Any] | None:
    """One problem with everything hanging off it: its events, its links, its
    sessions and the window suppressing it, if any."""
    from aegis.services import work_sessions

    problem = await get_problem(pool, problem_id)
    if problem is None:
        return None
    async with pool.acquire() as conn:
        window = await _active_suppression(
            conn, problem["subject"], problem["subject_kind"], _utcnow()
        )
    links = [
        dict(r)
        for r in await pool.fetch(
            "SELECT link_kind, ref, created_at FROM problem_links "
            "WHERE problem_id = $1::uuid ORDER BY created_at",
            problem_id,
        )
    ]
    sessions = (
        await work_sessions.list_for_task(pool, problem["todoist_task_id"])
        if problem["todoist_task_id"]
        else []
    )
    return {
        "problem": problem,
        "events": await list_events(pool, problem_id, limit=events),
        "links": links,
        "sessions": sessions,
        "window": dict(window) if window else None,
    }
