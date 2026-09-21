"""Retire a problem's stale decision cards (#629).

An `AlertInvestigationFlow` run posts one Gate-2 card per verdict, and before
this nothing ever took a card back. A problem investigated three times had
three live cards, and a problem that had already resolved still had one. Each
old card's buttons still worked, **Run fix** included: 9 of 33 answered cards
in prod were answered on a card older than the problem's newest, and 5 after
the problem had resolved.

A card is retired in two steps, because they need different things:

1. **The record** — :func:`retire`. The `interactions` row leaves `pending`
   for `retired`, which is all it takes for the resolve endpoint and the
   flow's own `resolve_interaction` to refuse a later click: both only move a
   `pending` row. It is plain SQL, so the hub runs it inside the very
   transaction that resolves a problem (`hub.ingest_event`, `hub.set_status`),
   and every resolve path — the alertmanager webhook, the heartbeat, a
   watchdog's `reconcile_findings`, the alertmanager reconciliation, a fix that
   held, a completed task, the Problems page — retires the cards at once.
2. **The side effects** — :func:`unfinished` and :func:`finish`. The Slack
   message is edited to say why the card is dead (edited, never deleted, so
   what was proposed stays readable), and the waiting `InteractionFlow` is
   signalled so its run ends now rather than after 48 hours. That needs comms
   and Temporal, which only the worker has: `HubActivities.retire_cards` does
   it, from the investigation flow that posts a newer card and from every hub
   sweep.

The signal carries a value the investigation flow already acts on:
``self_resolved`` (the branch the escalating race has always taken) or
``superseded`` (end the run, do nothing).

A problem's cards are found through its own timeline, not a naming
convention: every run that posts a Gate-2 card first records an
``<run id>:gate2`` investigation event on the problem, and the card's workflow
id ends with ``-<run id>``. That holds for runs already waiting when this
shipped, and for a problem whose events a merge moved.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import asyncpg

# The origin of a Gate-2 card (`AlertInvestigationFlow`, step 7.5).
GATE_ORIGIN = "alert_approve_pr"
# The `interactions.status` a retired card moves to.
RETIRED = "retired"
# Why a card was retired.
SUPERSEDED = "superseded"
RESOLVED = "resolved"
REASONS = frozenset({SUPERSEDED, RESOLVED})

# What the edited Slack message says above the card's own text.
_HEADLINE = {
    SUPERSEDED: "⏭ <b>Replaced by a newer card.</b> These buttons no longer do anything.",
    RESOLVED: "✅ <b>Resolved on its own.</b> These buttons no longer do anything.",
}
# The value the waiting `InteractionFlow` is signalled with, which the
# investigation flow reads as the card's answer.
_ANSWER = {SUPERSEDED: "superseded", RESOLVED: "self_resolved"}
# A card whose message could not be edited is tried again on the next sweep,
# this many times in all, then left: the row already refuses a click.
MAX_TRIES = 3

_GATE_SUFFIX = ":gate2"


def headline(reason: str) -> str:
    return _HEADLINE.get(reason, _HEADLINE[SUPERSEDED])


def edit_text(reason: str, prompt: str) -> str:
    """The retired card as it will read: why it is dead, then what it offered."""
    body = (prompt or "").strip()
    return f"{headline(reason)}\n\n{body}" if body else headline(reason)


def answer(reason: str) -> dict[str, str]:
    """The `submit_response` payload that ends the card's waiting flow."""
    value = _ANSWER.get(reason, _ANSWER[SUPERSEDED])
    note = "auto-closed: problem resolved" if reason == RESOLVED else "auto-closed: newer card"
    return {"value": value, "note": note}


async def retire(
    conn: asyncpg.Connection | asyncpg.Pool,
    problem_id: str,
    *,
    reason: str,
    exclude_run: str = "",
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Move every pending Gate-2 card of ``problem_id`` to ``retired``.

    ``exclude_run`` is the investigation run that is about to post the newer
    card, so it never retires its own. Returns the rows it moved (``id``,
    ``flow_run_id``). Idempotent: a row that is no longer pending is left
    alone, so a card a person answered a moment earlier keeps their answer.
    """
    if reason not in REASONS:
        raise ValueError(f"unknown reason {reason!r}")
    now = now or datetime.now(UTC)
    rows = await conn.fetch(
        "UPDATE interactions i SET status = $3, resolved_at = $4, "
        "  response = jsonb_build_object('value', $5::text, 'retired', $2::text), "
        "  metadata = COALESCE(i.metadata, '{}'::jsonb) || jsonb_build_object('retired', "
        "    jsonb_build_object('reason', $2::text, 'problem_id', $1::text, 'at', $4::timestamptz)) "
        "FROM (SELECT DISTINCT left(e.external_id, length(e.external_id) - $6) AS run_id "
        "      FROM problem_events e WHERE e.problem_id = $1::uuid "
        "        AND e.source = 'investigation' AND e.external_id LIKE '%' || $7) g "
        "WHERE i.status = 'pending' AND i.origin = $8 "
        "  AND g.run_id <> '' AND g.run_id <> $9 "
        "  AND right(i.flow_run_id, length(g.run_id) + 1) = '-' || g.run_id "
        "RETURNING i.id::text AS id, i.flow_run_id",
        problem_id,
        reason,
        RETIRED,
        now,
        _ANSWER[reason],
        len(_GATE_SUFFIX),
        _GATE_SUFFIX,
        GATE_ORIGIN,
        exclude_run or "",
    )
    return [dict(r) for r in rows]


async def unfinished(
    pool: asyncpg.Pool, *, problem_id: str = "", limit: int = 50
) -> list[dict[str, Any]]:
    """Retired cards whose message and waiting flow have not been dealt with
    yet, oldest first. ``problem_id`` narrows it to one problem's."""
    rows = await pool.fetch(
        "SELECT id::text AS id, flow_run_id, prompt, delivery_ref, "
        "       metadata->'retired'->>'reason' AS reason, "
        "       metadata->'retired'->>'problem_id' AS problem_id, "
        "       COALESCE((metadata->'retired'->>'tries')::int, 0) AS tries "
        "FROM interactions WHERE status = $1 AND metadata ? 'retired' "
        "  AND NOT (metadata->'retired' ? 'done_at') "
        "  AND ($2 = '' OR metadata->'retired'->>'problem_id' = $2) "
        "ORDER BY resolved_at NULLS FIRST LIMIT $3",
        RETIRED,
        problem_id or "",
        max(1, int(limit)),
    )
    return [dict(r) for r in rows]


async def finish(
    pool: asyncpg.Pool,
    interaction_id: str,
    *,
    edited: bool,
    signalled: bool,
    now: datetime | None = None,
) -> bool:
    """Record one attempt at a retired card's side effects. Done when both
    happened, or after :data:`MAX_TRIES` attempts; otherwise the next sweep
    tries again. Returns whether the card is now done."""
    now = now or datetime.now(UTC)
    row = await pool.fetchrow(
        "UPDATE interactions SET metadata = jsonb_set(metadata, '{retired}', "
        "  (metadata->'retired') || jsonb_build_object("
        "    'tries', COALESCE((metadata->'retired'->>'tries')::int, 0) + 1, "
        "    'edited', $2::boolean, 'signalled', $3::boolean) "
        "  || CASE WHEN ($2 AND $3) "
        "            OR COALESCE((metadata->'retired'->>'tries')::int, 0) + 1 >= $5 "
        "       THEN jsonb_build_object('done_at', $4::timestamptz) ELSE '{}'::jsonb END) "
        "WHERE id = $1::uuid AND status = 'retired' "
        "RETURNING metadata->'retired' ? 'done_at' AS done",
        interaction_id,
        bool(edited),
        bool(signalled),
        now,
        MAX_TRIES,
    )
    return bool(row and row["done"])
