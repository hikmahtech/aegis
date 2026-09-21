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

from aegis.errors import error_text
from aegis.services import hub_cards

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
# unknown origin that no digest query would ever group. Every entry has a
# producer: add a source with the code that sends it, not before. (Grafana and
# Prometheus alerts arrive through Alertmanager's webhook as `alertmanager`;
# the Ansible role and a deploy job write `service_state`, not events.)
SOURCES = frozenset(
    {
        "alertmanager",
        "sentry",
        "heartbeat",
        "flow_health",
        "delivery",
        "drift",
        "expiry",
        "social",
        "llm_governor",
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
        # A fix PR an investigation opened was merged or closed: the GitHub
        # webhook, through `hub_fix.record_pr_closed` (#502).
        "github",
        # The hub's own state_change rows, and the sweep's verdict on a
        # merged fix (`hub_fix.verify_fixes`).
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
# A tracked topic's round of news (#513). Not an outage: the infra digest leaves
# it out, and the projector gives it a task only once it earns one.
TOPIC_CLASS = "topic"
# A `#research` task's own problem (#513): a question Raphael owns.
QUESTION_CLASS = "question"
# A cluster outage (#630): several nodes down at once, from alertmanager's
# `ClusterOutage` rule (label `aegis_class: outage`) or from the heartbeat
# counting nodes that are not ready. It has no subject, so both producers land
# on the one key `outage::`. While it is live the hub keeps a `service_state`
# window (`*`/`*`, state `outage`) that records infra problems without raising
# them; see `_open_outage_window`. A live `nodedown` holds back its own
# services the same way, one row per service (`_hold_services`, #633).
OUTAGE_CLASS = "outage"
# The `service_state.state` of that window.
OUTAGE_STATE = "outage"
# The sources an outage window holds back: the monitoring stack, the swarm
# heartbeat, and AEGIS's own watchdogs whose findings an outage produces by
# the dozen (flows failing, cards undelivered, services drifting, connectors
# unreachable). Every other source is a judgement an outage does not explain
# — money, research, feeds, a person's report, a Sentry issue, an expiring
# certificate — and is raised as usual.
OUTAGE_SOURCES = frozenset(
    {"alertmanager", "heartbeat", "flow_health", "delivery", "drift", "connector"}
)
# How long the outage window stays up after the outage resolves. Services on
# the returning nodes take a few minutes to converge, and a problem promoted
# before then earns a task for something that is about to clear on its own.
# A constant for the same reason as `REOPEN_WINDOW`: it describes what the end
# of an outage looks like, not an operator preference.
OUTAGE_TAIL = timedelta(minutes=10)
# The longest the cluster-wide window stays up (#633), counted from when the
# outage began, or began again after it had resolved. A later occurrence never
# moves it. noon does not power on by itself, and wow was once off for five
# days: a window as long as the outage held back every unrelated fault on the
# healthy nodes for all that time. Once it passes, the sweep promotes what is
# still broken while the outage problem stays open. A node that is still down
# keeps holding back its own services (`_hold_services`), which is not capped.
OUTAGE_MAX = timedelta(hours=6)
SEVERITIES = frozenset({"critical", "error", "warning", "info"})
# Problem statuses. `suppressed` = seen while its subject was deploying or in
# maintenance, or during a cluster outage (see `service_state`); it is live,
# counted, and not projected.
LIVE_STATUSES = frozenset(
    {"open", "investigating", "waiting_human", "fixing", "verifying", "suppressed"}
)
STATUSES = LIVE_STATUSES | {"resolved", "closed"}
# `service_state.state`. `deploying`, `maintenance` and `outage` suppress;
# `degraded` and `ok` are information (`ok` clears the row). `outage` is the
# window the hub itself opens while an outage problem is live, and it
# suppresses only `OUTAGE_SOURCES` (`_active_suppression`).
SERVICE_STATES = frozenset({"deploying", "maintenance", OUTAGE_STATE, "degraded", "ok"})
SUPPRESSING_STATES = frozenset({"deploying", "maintenance", OUTAGE_STATE})
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


SETTLE_SETTINGS_KEY = "hub_settle_seconds"


def verify_seconds(klass: str) -> int:
    k = _slug(klass)
    if k in _VERIFY_SECONDS:
        return _VERIFY_SECONDS[k]
    if any(word in k for word in _VERIFY_AT_ONCE):
        return 0
    return VERIFY_SECONDS_DEFAULT


async def verify_seconds_for(pool: asyncpg.Pool, klass: str) -> int:
    """`verify_seconds` with the operator's overrides on top.

    The `hub_settle_seconds` settings row maps a class to seconds —
    `{"servicecrashlooping": 600}` — and a class it does not name keeps the
    code default above. How long a class takes to prove itself is a property
    of the operator's own homelab, not of AEGIS, so it belongs in the DB
    (#537); the defaults stay generic. The key `*` stands for every class, so
    `{"*": 0}` is how an operator who wants no waiting at all turns the whole
    thing off.

    Read leniently, like every other merged settings row: a malformed value
    must never stop an alert being handled, so a non-integer is ignored
    rather than raised.
    """
    row = await pool.fetchrow("SELECT value FROM settings WHERE key = $1", SETTLE_SETTINGS_KEY)
    override = (row["value"] if row else None) or {}
    if isinstance(override, dict):
        raw = override.get(_slug(klass), override.get("*"))
        if raw is not None:
            try:
                return max(0, int(raw))
            except (TypeError, ValueError):
                logger.warning("hub_settle_seconds_bad_value", klass=klass, value=raw)
    return verify_seconds(klass)


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
                    "severity, title, group_key, class FROM problems "
                    "WHERE group_key = $1 AND closed_at IS NULL FOR UPDATE",
                    gkey,
                )
                if row is not None:
                    current, group, absorbed = dict(row), dict(row), True

        suppression = None
        # The outage problem is never held back by a window, its own included:
        # it is what the window is for (#630).
        if event.kind == "occurrence" and _slug(event.klass) != OUTAGE_CLASS:
            suppression = await _suppression_or_none(
                conn, subject_slug, kind_slug, now, source=event.source
            )
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
            await record_state_change(
                conn,
                problem_id,
                f"{event.source}:{event.external_id}:{d.action}",
                severity=severity,
                payload={"action": d.action, "status": d.status},
                occurred_at=occurred_at,
            )
        if d.action == "resolve":
            # A resolved problem's decision cards retire with it (#629), and
            # the windows it kept start their tail (#630, #633).
            await _retire_cards_quietly(conn, problem_id, now)
            await _release_holds(conn, problem_id, now)
        stored_class = (
            _slug(event.klass) or "manual"
            if d.action in {"create", "rollover"}
            else str(current.get("class") or "")
        )
        if event.kind == "occurrence" and stored_class == OUTAGE_CLASS:
            await _open_outage_window(
                conn,
                problem_id=problem_id,
                now=now,
                # A new stretch of the outage starts its own window; a repeat
                # occurrence never moves the end of the one it has.
                began=occurred_at if d.action in {"create", "rollover", "reopen"} else None,
            )
        elif event.kind == "occurrence" and stored_class in _NODE_CLASSES:
            # The heartbeat names the services that had a task on the node.
            services = payload.get("services")
            if isinstance(services, list) and services:
                await _hold_services(
                    conn,
                    problem_id=problem_id,
                    node=subject_slug,
                    services=services,
                    now=now,
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
    conn: asyncpg.Connection,
    subject: str,
    subject_kind: str,
    now: datetime,
    source: str = "",
) -> asyncpg.Record | None:
    """The `service_state` row that suppresses ``subject`` right now, if any.
    An exact match wins over the `*` wildcard (a whole-kind or global
    maintenance window, e.g. a planned power cut).

    An `outage` row counts only for a ``source`` in `OUTAGE_SOURCES`: the
    window a cluster outage opens holds back what the outage explains, never
    a money finding or a person's report. A caller that names no source (the
    task block, the chat tool) does not see it at all."""
    return await conn.fetchrow(
        "SELECT subject, subject_kind, state, until_at, set_by, note FROM service_state "
        "WHERE state = ANY($4::text[]) AND (until_at IS NULL OR until_at > $3) "
        "AND (state <> $6 OR $5) "
        "AND ((subject = $1 AND subject_kind = $2) "
        "     OR (subject = '*' AND subject_kind IN ($2, '*'))) "
        "ORDER BY (subject = '*') LIMIT 1",
        subject,
        subject_kind,
        now,
        sorted(SUPPRESSING_STATES),
        source in OUTAGE_SOURCES,
        OUTAGE_STATE,
    )


async def _suppression_or_none(
    conn: asyncpg.Connection,
    subject: str,
    subject_kind: str,
    now: datetime,
    *,
    source: str = "",
) -> asyncpg.Record | None:
    """:func:`_active_suppression` for the ingest path, which fails open
    (spec §10): a window the hub cannot read suppresses nothing, so the alert
    is still recorded and still raised.

    The savepoint is what makes that true. The lookup runs inside the ingest
    transaction, and a failed statement aborts a Postgres transaction — caught
    without one, every later statement in the ingest would fail anyway.
    """
    try:
        async with conn.transaction():
            return await _active_suppression(conn, subject, subject_kind, now, source)
    except Exception as exc:  # noqa: BLE001 — fail open: an alert beats a window
        logger.warning("hub_service_state_unreadable", subject=subject, error=error_text(exc))
        return None


async def _retire_cards_quietly(conn: asyncpg.Connection, problem_id: str, now: datetime) -> None:
    """Retire a resolved problem's pending decision cards (`hub_cards.retire`)
    inside the transaction that resolved it, so no resolve path can leave a
    live **Run fix** behind (#629).

    Under a savepoint and failing open, like the suppression lookup: a card
    the hub could not retire stays live, which is the old behaviour, but the
    resolve itself is never lost to it. The Slack edit and the end of the
    waiting flow come from the worker (`HubActivities.retire_cards`)."""
    try:
        async with conn.transaction():
            retired = await hub_cards.retire(
                conn, problem_id, reason=hub_cards.RESOLVED, now=now
            )
    except Exception as exc:  # noqa: BLE001 — the resolve matters more than the card
        logger.warning("hub_cards_retire_failed", problem_id=problem_id, error=error_text(exc))
        return
    if retired:
        logger.info("hub_cards_retired", problem_id=problem_id, count=len(retired), reason="resolved")


# The `service_state.set_by` of a row a live problem keeps (#630, #633). The
# hub's own rows are found by it, so each problem ends exactly the rows it set.
def _hold_owner(problem_id: str) -> str:
    return f"hub:{problem_id}"


# The guard every hold write shares: a hub row takes over an existing row only
# when that row has expired, or is another hub row. An operator's window that
# is still in force — a deploy, a planned power cut — is never overwritten,
# and so never later shortened by the hub ending its own. A statement that
# uses it must pass `OUTAGE_STATE` as `$1` and now as `$4`.
_TAKE_OVER = (
    "(service_state.until_at IS NOT NULL AND service_state.until_at <= $4) "
    "OR (service_state.state = $1 AND service_state.set_by LIKE 'hub:%')"
)


async def _open_outage_window(
    conn: asyncpg.Connection, *, problem_id: str, now: datetime, began: datetime | None
) -> None:
    """Open the window a live outage problem keeps (#630): the `*`/`*`
    `service_state` row, state `outage`, ending `OUTAGE_MAX` after the outage
    began (#633).

    ``began`` is when this stretch of the outage started: its first
    occurrence, or the occurrence that reopened it after it had resolved. A
    reopen keeps `first_seen_at`, so counting from that alone would give a
    second power cut on the same day no window at all. ``None`` is a repeat
    occurrence of a live outage, which never moves the end of a window this
    problem already holds; if it holds none, one opens that ends
    `OUTAGE_MAX` after `first_seen_at`.

    Once the end passes, `promote_expired_suppressions` opens what is still
    broken while the outage problem stays open. Savepoint, fail open: the
    outage event is recorded whatever happens here."""
    # A new stretch replaces this problem's own window (a tail left by the
    # resolve it came back from); a repeat occurrence leaves it alone.
    restart = began is not None
    try:
        async with conn.transaction():
            if began is None:
                began = await conn.fetchval(
                    "SELECT first_seen_at FROM problems WHERE id = $1::uuid", problem_id
                )
            end = _aware(began, now) + OUTAGE_MAX
            if end <= now:
                logger.info("hub_outage_window_capped", problem_id=problem_id, until=end.isoformat())
                return
            await conn.execute(
                "INSERT INTO service_state (subject, subject_kind, state, until_at, set_by, "
                "note, updated_at) VALUES ('*', '*', $1, $5, $2, $3, $4) "
                "ON CONFLICT (subject, subject_kind) DO UPDATE SET state = EXCLUDED.state, "
                "until_at = EXCLUDED.until_at, set_by = EXCLUDED.set_by, note = EXCLUDED.note, "
                "updated_at = EXCLUDED.updated_at "
                f"WHERE ({_TAKE_OVER}) AND ($6 OR service_state.set_by <> EXCLUDED.set_by)",
                OUTAGE_STATE,
                _hold_owner(problem_id),
                "Cluster outage: infra problems are recorded, not raised, until it ends "
                f"or for {int(OUTAGE_MAX.total_seconds() // 3600)} hours at most.",
                now,
                end,
                restart,
            )
    except Exception as exc:  # noqa: BLE001 — the outage event is recorded regardless
        logger.warning("hub_outage_window_failed", problem_id=problem_id, error=error_text(exc))
        return
    logger.info("hub_outage_window", problem_id=problem_id)


async def _hold_services(
    conn: asyncpg.Connection,
    *,
    problem_id: str,
    node: str,
    services: list[str],
    now: datetime,
) -> None:
    """Hold back the services a node that is down explains (#633).

    One `service_state` row per service that had a task on the node when it
    went down, state `outage`, with no end: `(<service>, service)`, set by
    this problem. `_active_suppression` matches it exactly, so a
    `DockerServiceDown`, `ServiceDownProlonged` or crash-loop problem for one
    of them — from alertmanager or the heartbeat, both keyed on the swarm
    service name — is recorded as `suppressed`, and a service on another node
    is not held at all. lam alone carries about 30 pinned services.

    A row is taken over only as `_TAKE_OVER` allows. A service another live
    node problem already holds (its row has no end) stays with that one.

    Not capped like the cluster window: a node that is still down still
    explains its own services. The rows end when the problem resolves
    (`_release_holds`). Savepoint, fail open."""
    subjects = sorted({s for s in (_slug(str(x)) for x in services) if s})
    if not subjects:
        return
    try:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO service_state (subject, subject_kind, state, until_at, set_by, "
                "note, updated_at) "
                "SELECT s, 'service', $1, NULL, $2, $3, $4 FROM unnest($5::text[]) AS s "
                "ON CONFLICT (subject, subject_kind) DO UPDATE SET state = EXCLUDED.state, "
                "until_at = NULL, set_by = EXCLUDED.set_by, note = EXCLUDED.note, "
                "updated_at = EXCLUDED.updated_at "
                f"WHERE ({_TAKE_OVER}) AND service_state.until_at IS NOT NULL",
                OUTAGE_STATE,
                _hold_owner(problem_id),
                f"Node {node or '?'} is down: problems on its services are recorded, "
                "not raised, until it is back.",
                now,
                subjects,
            )
    except Exception as exc:  # noqa: BLE001 — the node event is recorded regardless
        logger.warning("hub_node_hold_failed", problem_id=problem_id, error=error_text(exc))
        return
    logger.info("hub_node_hold", problem_id=problem_id, node=node, services=len(subjects))


async def _release_holds(conn: asyncpg.Connection, problem_id: str, now: datetime) -> None:
    """End the rows a resolving problem set (#630, #633): each gets an end
    `OUTAGE_TAIL` from now instead of going at once, and none is lengthened.
    Services on the returning nodes take a few minutes to converge; once the
    tail passes, `promote_expired_suppressions` opens whatever is still broken
    and the sweep gives it its task and its investigation.

    Only the rows this problem set: an operator's window, or a row another
    live problem took over, is left as it is. Savepoint, fail open."""
    try:
        async with conn.transaction():
            await conn.execute(
                "UPDATE service_state SET until_at = $3, updated_at = $4 "
                "WHERE set_by = $1 AND state = $2 AND (until_at IS NULL OR until_at > $3)",
                _hold_owner(problem_id),
                OUTAGE_STATE,
                now + OUTAGE_TAIL,
                now,
            )
    except Exception as exc:  # noqa: BLE001 — the resolve is recorded regardless
        logger.warning("hub_release_holds_failed", problem_id=problem_id, error=error_text(exc))


async def _resume_holds(conn: asyncpg.Connection, problem_id: str, now: datetime) -> None:
    """A node problem reopened by hand holds its services again: the rows it
    set, still its own, lose the end `_release_holds` gave them. Savepoint,
    fail open."""
    try:
        async with conn.transaction():
            await conn.execute(
                "UPDATE service_state SET until_at = NULL, updated_at = $3 "
                "WHERE set_by = $1 AND state = $2 AND subject <> '*'",
                _hold_owner(problem_id),
                OUTAGE_STATE,
                now,
            )
    except Exception as exc:  # noqa: BLE001 — the reopen is recorded regardless
        logger.warning("hub_resume_holds_failed", problem_id=problem_id, error=error_text(exc))


async def _reap_holds(conn: asyncpg.Connection, now: datetime) -> None:
    """The sweep's safety net for the hub's own rows, run before promotion.

    * A row whose problem is no longer live gets its tail. A resolve ends its
      rows in the same transaction, but a problem can also leave by a merge,
      a close, or a resolve whose release failed, and an open-ended row
      nobody ends would hold its services back for ever.
    * A cluster window from before `OUTAGE_MAX` existed has no end: it gets
      the one it would have had, `first_seen_at + OUTAGE_MAX`.

    Savepoint, fail open: promotion runs either way."""
    try:
        async with conn.transaction():
            await conn.execute(
                "UPDATE service_state s SET until_at = $2, updated_at = $3 "
                "WHERE s.state = $1 AND s.set_by LIKE 'hub:%' "
                "  AND (s.until_at IS NULL OR s.until_at > $2) "
                "  AND NOT EXISTS (SELECT 1 FROM problems p WHERE 'hub:' || p.id::text = s.set_by "
                "                  AND p.closed_at IS NULL AND p.status = ANY($4::text[]))",
                OUTAGE_STATE,
                now + OUTAGE_TAIL,
                now,
                sorted(LIVE_STATUSES),
            )
            await conn.execute(
                "UPDATE service_state s SET until_at = p.first_seen_at + $2::interval, "
                "updated_at = $3 FROM problems p "
                "WHERE s.subject = '*' AND s.subject_kind = '*' AND s.state = $1 "
                "  AND s.until_at IS NULL AND s.set_by = 'hub:' || p.id::text "
                "  AND p.class = $4",
                OUTAGE_STATE,
                OUTAGE_MAX,
                now,
                OUTAGE_CLASS,
            )
    except Exception as exc:  # noqa: BLE001 — promotion matters more
        logger.warning("hub_reap_holds_failed", error=error_text(exc))


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
    deploy, maintenance or outage did not make it go away. Returns the
    promoted ids. The hub's own windows are tidied first (`_reap_holds`)."""
    now = now or _utcnow()
    promoted: list[str] = []
    async with pool.acquire() as conn, conn.transaction():
        await _reap_holds(conn, now)
        # The source of the first occurrence, because an `outage` window holds
        # only some sources back (`_active_suppression`), so "is its window
        # still in force" depends on who raised it.
        rows = await conn.fetch(
            "SELECT p.id::text AS id, p.subject, p.subject_kind, p.severity, "
            "  COALESCE((SELECT e.source FROM problem_events e WHERE e.problem_id = p.id "
            "            AND e.kind = 'occurrence' ORDER BY e.id LIMIT 1), '') AS source "
            "FROM problems p WHERE p.status = 'suppressed' AND p.closed_at IS NULL "
            "FOR UPDATE OF p"
        )
        for row in rows:
            if await _active_suppression(
                conn, row["subject"], row["subject_kind"], now, row["source"]
            ):
                continue
            await conn.execute(
                "UPDATE problems SET status = 'open' WHERE id = $1::uuid", row["id"]
            )
            await record_state_change(
                conn,
                row["id"],
                f"promote:{row['id']}:{now.isoformat()}",
                severity=row["severity"],
                payload={"action": "promote", "status": "open", "reason": "suppression_expired"},
                occurred_at=now,
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
            "SELECT status, severity, class FROM problems WHERE id = $1::uuid "
            "AND closed_at IS NULL FOR UPDATE",
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
        await record_state_change(
            conn,
            problem_id,
            f"{source}:{problem_id}:{status}:{now.isoformat()}",
            severity=row["severity"],
            payload={
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
            occurred_at=now,
        )
        if status == "resolved":
            # Every way a problem resolves retires its cards (#629) and ends
            # the windows it kept (#630, #633): this is the Problems page, a
            # fix that held, a completed task, and an investigation's own
            # verdict.
            await _retire_cards_quietly(conn, problem_id, now)
            await _release_holds(conn, problem_id, now)
        elif reopening and row["class"] == OUTAGE_CLASS:
            await _open_outage_window(conn, problem_id=problem_id, now=now, began=now)
        elif reopening and row["class"] in _NODE_CLASSES:
            await _resume_holds(conn, problem_id, now)
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
        await record_state_change(
            conn,
            problem_id,
            f"mute:{problem_id}:{now.isoformat()}",
            severity=row["severity"],
            payload={
                "action": "mute",
                "until": until.isoformat(),
                "by": by,
                "reason": reason[:300],
            },
            occurred_at=now,
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

    # An `aegis_class` label names the hub class outright, before the alertname
    # (#630). It lets two rules that mean the same failure meet on one problem:
    # Prometheus' `ServiceDownProlonged` is the two-hour escalation of
    # `DockerServiceDown`, and with `aegis_class: DockerServiceDown` it joins
    # that problem instead of opening a second one with a second card and task.
    # Normalised like an alertname (`correlation_key` slugs both).
    klass = str(labels.get("aegis_class") or "").strip() or alertname
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
        # `hostname` is what Prometheus' node rules carry (#633): the swarm
        # exporter's `SwarmNodeNotReady` (with `aegis_class: NodeDown`) and
        # node-exporter's `NodeDown`. It is the `docker node ls` hostname, the
        # heartbeat's own NodeDown subject, so all three meet on one
        # `nodedown:node:<host>` problem. Another class keeps its old key: a
        # CPU or disk alert has an instance, and re-keying it would split its
        # live problem in two.
        node = str(
            labels.get("node") or labels.get("nodename") or labels.get("hostname") or ""
        ).strip()
        if node and (_slug(klass) in _NODE_CLASSES or not alert.get("service")):
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
    if _slug(klass) == OUTAGE_CLASS:
        # An outage is the cluster's, never one service's or node's. Whatever
        # labels a rule carries, both producers must land on the one key.
        subject, subject_kind = "", ""

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
    payload: dict[str, Any] = {
        "fingerprint": fingerprint,
        "labels": labels,
        "description": str(alert.get("description") or "")[:2000],
    }
    services = alert.get("services")
    if isinstance(services, list) and services:
        # The swarm services that had a task on a node that went down: the
        # heartbeat's NodeDown carries them, and the hub holds them back
        # while the node is down (`_hold_services`, #633).
        payload["services"] = sorted({str(s).strip() for s in services if str(s).strip()})
    return Event(
        source=source,
        external_id=external_id,
        kind="resolved" if resolved else "occurrence",
        title=str(alert.get("title") or alertname or "Alert").strip(),
        subject=subject,
        subject_kind=subject_kind,
        klass=klass,
        severity=str(alert.get("severity") or "warning"),
        payload=payload,
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
    """Fold ``merge_id`` into ``keep_id``: its events and links move, its
    occurrences count on the kept problem, and it closes with a `problem` link
    back so the history reads both ways. Its `work_sessions` rows are
    re-pointed too, but nothing reads `work_sessions.problem_id` — sessions are
    listed by task, so they stay on the merged problem's task.

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


# Sources whose problems the infra digest leaves out, told apart by the source
# of their first occurrence. `feeds` is Raphael's (#511): a broken feed is a
# `#feeds` task he owns, not an infra incident.
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
        # task's question — are not problems the infra digest reports (#513);
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
    """One problem with everything hanging off it: its events, its links, its
    sessions and the window suppressing it, if any."""
    from aegis.services import work_sessions

    problem = await get_problem(pool, problem_id)
    if problem is None:
        return None
    async with pool.acquire() as conn:
        # Who raised it decides whether an outage window covers it.
        source = await conn.fetchval(
            "SELECT source FROM problem_events WHERE problem_id = $1::uuid "
            "AND kind = 'occurrence' ORDER BY id LIMIT 1",
            problem_id,
        )
        window = await _active_suppression(
            conn, problem["subject"], problem["subject_kind"], _utcnow(), source or ""
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
