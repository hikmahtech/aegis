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

The infra lane's producers (alertmanager, the swarm heartbeat, service drift,
alert investigations), the deploy/maintenance/outage windows that held their
problems back, the settle windows and mutes moved to the DevOps vertical
(a2-devops) with that lane. What stays is the hub for v1's own producers.
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
# caller gets a ValueError rather than minting a problem of an unknown origin
# that no digest query would ever group. Every entry has a producer: add a
# source with the code that sends it, not before. The infra lane's sources
# (`alertmanager`, `heartbeat`, `drift`, `investigation`) left with it for the
# DevOps vertical (a2-devops); their old events stay as history.
SOURCES = frozenset(
    {
        "flow_health",
        "delivery",
        "expiry",
        "social",
        "llm_governor",
        "chat",
        "session",
        "manual",
        # Reconciliation findings: a statement whose closing balance disagrees
        # with the books, an account no statement arrived for, a file nothing
        # could parse, an instrument pass 2b has no entity scope for. The money
        # lane predates the hub by two days and never met it, so it grew its
        # own dedupe, its own noise guards and no alert path at all — see §15
        # of the statement-reconciliation spec, which puts it back here.
        "money",
        # The hub's own state_change rows.
        "hub",
        # An RSS feed that stopped fetching (`feed_failing`, three fetches in
        # a row) or stopped publishing (`feed_stale`): RssIngestFlow's
        # findings (#511). The research agent owns the feed list.
        "feeds",
        # The research agent's own problems (#513): a tracked topic's round of
        # news (`services/research_topics.py`) and a `#research` task's
        # question (`hub_project.ensure_problem_for_task`).
        "research",
        # An integration that has failed its consecutive-failure threshold
        # (`services/connector_health.py`, #571). That tracker predates the hub
        # and its one Slack ping was the only notice a dead connector ever
        # produced — Calibre was down for two days with `alerted: true` and no
        # problem behind it.
        "connector",
    }
)
KINDS = frozenset({"occurrence", "resolved", "investigation", "plan", "session_note"})
# A tracked topic's round of news (#513). Not an outage: the hub digest leaves
# it out, and the projector gives it a task only once it earns one.
TOPIC_CLASS = "topic"
# A `#research` task's own problem (#513): a question Raphael owns.
QUESTION_CLASS = "question"
SEVERITIES = frozenset({"critical", "error", "warning", "info"})
# Problem statuses. `investigating` and `waiting_human` were the alert
# investigation's; they stay readable for the problems that still carry them.
LIVE_STATUSES = frozenset({"open", "investigating", "waiting_human"})
STATUSES = LIVE_STATUSES | {"resolved", "closed"}

_SLUG_RE = re.compile(r"[^a-z0-9_.]+")
_SEGMENT_CAP = 80
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
    # True while a mute set before the infra lane left is still in force.
    muted: bool = False
    # True when the event's own key had no problem and a group problem for its
    # class took it. The caller learns which problem from `problem_id`.
    absorbed: bool = False

    @property
    def investigate(self) -> bool:
        """Whether this event is fresh — worth a card: the hub decides, the
        producer notifies. A problem is fresh when it appears (or comes back),
        never on a repeat occurrence, never while muted."""
        return self.action in {"created", "reopened", "rolled_over"} and not self.muted

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
) -> Decision:
    """The transition table. ``current`` is the open-or-resolved problem
    holding the event's key (or the one it named), ``None`` when there is none.

    Pure, so the whole matrix is unit-tested without a database.
    """
    if kind == "occurrence":
        if current is None or current["status"] == "closed":
            return Decision("create", "open")
        if current["status"] in LIVE_STATUSES:
            return Decision("attach")
        # resolved
        resolved_at = _aware(current.get("resolved_at"), now)
        if now - resolved_at <= reopen_window:
            return Decision("reopen", "open")
        return Decision("rollover", "open")
    if kind == "resolved":
        if current is None or current["status"] in {"resolved", "closed"}:
            # Nothing to resolve. A resolved event on an already-resolved
            # problem is still worth keeping as history.
            return Decision("ignore" if current is None else "note")
        return Decision("resolve", "resolved")
    # investigation / plan / session_note: history on a problem the producer
    # named or the key found. Never creates.
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


async def record_state_change(
    conn: Any,
    problem_id: Any,
    external_id: str,
    *,
    severity: str,
    payload: dict,
    occurred_at: datetime,
) -> None:
    """One `state_change` row on the hub's own timeline.

    Every transition the hub makes writes one of these and nothing else does:
    the digest and the timeline read them rather than diffing `problems`
    rows. Idempotent on `(source, external_id)` like every other occurrence,
    so a retried write is a no-op rather than a second entry.

    `conn` is a connection inside a transaction, or the pool where the write
    stands alone.
    """
    await conn.execute(
        "INSERT INTO problem_events (problem_id, source, external_id, kind, severity, "
        "payload, occurred_at) VALUES ($1::uuid, 'hub', $2, 'state_change', $3, $4, $5) "
        "ON CONFLICT (source, external_id) DO NOTHING",
        problem_id,
        external_id,
        severity,
        payload,
        occurred_at,
    )


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
                "SELECT id::text AS id, status, resolved_at, muted_until, occurrences, severity, "
                "class FROM problems WHERE id = $1::uuid FOR UPDATE",
                event.problem_id,
            )
            current = dict(current) if current else None
        elif key:
            current = await conn.fetchrow(
                "SELECT id::text AS id, status, resolved_at, muted_until, occurrences, severity, "
                "class FROM problems WHERE correlation_key = $1 AND closed_at IS NULL FOR UPDATE",
                key,
            )
            current = dict(current) if current else None
        else:
            current = None

        # The kind a new problem is STORED with.
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
                    "severity, title, group_key, class FROM problems "
                    "WHERE group_key = $1 AND closed_at IS NULL FOR UPDATE",
                    gkey,
                )
                if row is not None:
                    current, group, absorbed = dict(row), dict(row), True

        d = decide(current, event.kind, now=now)
        if d.action == "ignore":
            return IngestResult(None, "ignored", key)

        payload = dict(event.payload or {})
        if absorbed:
            # Which entity this occurrence was about. The group's own subject
            # is `*`, so without this the timeline could not name the member.
            payload["member_subject"] = subject_slug
            payload["member_key"] = key
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
            if d.action in {"attach", "reopen"}:
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
            await record_state_change(
                conn,
                problem_id,
                f"{event.source}:{event.external_id}:{d.action}",
                severity=severity,
                payload={"action": d.action, "status": d.status},
                occurred_at=occurred_at,
            )

    action = {
        "create": "created",
        "attach": "attached",
        "reopen": "reopened",
        "rollover": "rolled_over",
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
        absorbed=absorbed,
    )


async def set_status(
    pool: asyncpg.Pool,
    problem_id: str,
    status: str,
    *,
    reason: str,
    source: str = "hub",
    now: datetime | None = None,
) -> bool:
    """Move a live problem to ``status`` (the Problems page's Resolve, a
    completed task, a topic's round), writing the state_change event. Never
    resurrects a closed problem.

    False when nothing moved: the problem is missing, closed or already
    there."""
    if status not in STATUSES or status == "closed":
        raise ValueError(f"cannot set status {status!r}")
    now = now or _utcnow()
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "SELECT status, severity FROM problems WHERE id = $1::uuid "
            "AND closed_at IS NULL FOR UPDATE",
            problem_id,
        )
        if row is None or row["status"] == status:
            return False
        reopening = row["status"] == "resolved" and status in LIVE_STATUSES
        # A caller coming BACK from `resolved` is a reopen: the stale
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
        await record_state_change(
            conn,
            problem_id,
            f"{source}:{problem_id}:{status}:{now.isoformat()}",
            severity=row["severity"],
            payload={
                # `resolve` and `reopen` are the two words the PROJECTOR acts
                # on: it closes a task on one and reopens it on the other.
                # Writing `set_status` for a move into `resolved` left the
                # problem resolved and its task open with no closing comment,
                # so the admin panel's Resolve button said nothing to the
                # human looking at the task. A resolve reached this way is the same
                # event as a resolve reached by an incoming `resolved` alert.
                "action": (
                    "reopen" if reopening else ("resolve" if status == "resolved" else "set_status")
                ),
                "status": status,
                "reason": reason[:300],
            },
            occurred_at=now,
        )
    logger.info("hub_status_set", problem_id=problem_id, status=status, reason=reason[:80])
    return True


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
    """Fold ``merge_id`` into ``keep_id``: its events and links move, its
    occurrences count on the kept problem, and it closes with a `problem` link
    back so the history reads both ways.

    A wrong merge hides an outage, so every caller is a deliberate decision:
    a person on the admin Problems page; a person or agent through the
    `merge_problems` chat tool (withheld from coding runs); and
    `hub_group.upgrade`, which folds problems of ONE class and subject kind into
    a group of that class after an LLM has agreed they are the same condition,
    or into a group that already stands for the class. The hub still never
    merges two different failures on a resemblance.

    Raises ValueError when either problem is missing, they are the same, or the
    kept one is already closed. The merged problem's own Todoist task is
    returned so the caller can retire it (`hub_project.retire_merged_task`);
    the hub never touches Todoist.
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
        await record_state_change(
            conn,
            keep_id,
            f"merge:{keep_id}:{merge_id}:{now.isoformat()}",
            severity=keep["severity"],
            payload={
                "action": "merge",
                "merged": merge_id,
                "by": by[:100],
                "status": keep["status"],
            },
            occurred_at=now,
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


# Sources whose problems the hub digest leaves out, told apart by the source
# of their first occurrence. `feeds` is Raphael's (#511): a broken feed is a
# `#feeds` task he owns, not an incident.
DIGEST_SKIPPED_SOURCES = ("feeds",)


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
        # Raphael's research problems — a topic's round of news, a `#research`
        # task's question — are not problems the hub digest reports (#513);
        # Raphael's briefing has its own topics line.
        "  AND p.class <> ALL($2::text[]) "
        # Nor is a feed that broke (#511): that is Raphael's `#feeds` task.
        # Told apart by the source of the first occurrence, the same rule that
        # picks a task's owner (`hub_project._first_source`).
        "  AND COALESCE((SELECT e.source FROM problem_events e "
        "                 WHERE e.problem_id = p.id AND e.kind = 'occurrence' "
        "                 ORDER BY e.id LIMIT 1), '') <> ALL($3::text[]) "
        "ORDER BY p.last_seen_at DESC",
        since,
        [TOPIC_CLASS, QUESTION_CLASS],
        list(DIGEST_SKIPPED_SOURCES),
    )
    problems = [dict(r) for r in rows]
    live = [p for p in problems if p["status"] in LIVE_STATUSES]
    counts = {
        "total": len(problems),
        "new": sum(1 for p in problems if p["is_new"]),
        "open": len(live),
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
        await record_state_change(
            pool,
            problem_id,
            f"close:{problem_id}:{now.isoformat()}",
            severity="info",
            payload={"action": "close", "reason": f"resolved more than {days:g} days ago"},
            occurred_at=now,
        )
    if ids:
        logger.info("hub_problems_closed", count=len(ids), days=days)
    return ids


async def close_problem(
    pool: asyncpg.Pool,
    problem_id: str,
    *,
    now: datetime | None = None,
    reason: str = "closed by hand",
) -> bool:
    """Close ONE resolved problem. False when it is missing, already closed, or
    not resolved.

    The admin panel's close button used to call ``close_resolved(days=0)``,
    which closes every resolved problem in the database — including ones whose
    resolution has not been projected yet, and a closed problem is never
    projected again, so their tasks were left open with no closing comment.

    ``reason`` goes on the close event. The default is the Problems page's
    Close; an automated caller (a topic's round ending) says why instead, or
    the timeline credits the user with a close they never made.
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
        await record_state_change(
            conn,
            problem_id,
            f"close:{problem_id}:{now.isoformat()}",
            severity="info",
            payload={"action": "close", "reason": reason},
            occurred_at=now,
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
        "       p.muted_until, p.resolved_at, p.closed_at, p.todoist_task_id, p.group_key, "
        "       p.metadata->'projection' AS projection "
        f"FROM problems p WHERE {' AND '.join(where)} "
        f"ORDER BY p.last_seen_at DESC LIMIT ${len(args) - 1} OFFSET ${len(args)}",
        *args,
    )
    return [dict(r) for r in rows]


async def problem_detail(
    pool: asyncpg.Pool, problem_id: str, *, events: int = 50
) -> dict[str, Any] | None:
    """One problem with everything hanging off it: its events and its links."""
    problem = await get_problem(pool, problem_id)
    if problem is None:
        return None
    links = [
        dict(r)
        for r in await pool.fetch(
            "SELECT link_kind, ref, created_at FROM problem_links "
            "WHERE problem_id = $1::uuid ORDER BY created_at",
            problem_id,
        )
    ]
    return {
        "problem": problem,
        "events": await list_events(pool, problem_id, limit=events),
        "links": links,
    }
