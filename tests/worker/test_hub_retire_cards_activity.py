"""`HubActivities.retire_cards` and `promoted_investigations` (#629, #630).

`retire_cards` is where a retired card stops looking alive: its Slack message
is edited (never deleted) and the `InteractionFlow` waiting on it is signalled
with the answer that ends its run. Real test database; the comms edit and the
Temporal signal are recorded by fakes.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from aegis.services import hub_cards
from aegis.services.hub import Event, get_problem, ingest_event, set_status
from aegis_worker.activities.hub import HubActivities
from temporalio.service import RPCError, RPCStatusCode
from temporalio.testing import ActivityEnvironment

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
_PREFIX = "gate2-fp-"


@pytest_asyncio.fixture(autouse=True, loop_scope="function")
async def _clean_cards(db_pool):
    yield
    await db_pool.execute("DELETE FROM interactions WHERE flow_run_id LIKE $1", _PREFIX + "%")


class _Delivery:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.edits: list[tuple[dict | None, str]] = []

    async def edit_card(self, delivery_ref, text):
        self.edits.append((delivery_ref, text))
        return {"ok": self.ok}


class _Temporal:
    """Records every signal; a run id in `gone` answers NOT_FOUND, as Temporal
    does for a workflow that already closed."""

    def __init__(self, gone: set[str] | None = None) -> None:
        self.gone = gone or set()
        self.signals: list[tuple[str, str, dict]] = []

    def get_workflow_handle(self, wid):
        outer = self

        class _H:
            async def signal(self, name, arg):
                if wid in outer.gone:
                    raise RPCError("workflow execution already completed", RPCStatusCode.NOT_FOUND, b"")
                outer.signals.append((wid, name, arg))

        return _H()


async def _problem(pool, source: str = "heartbeat") -> str:
    subject = f"svc_{uuid.uuid4().hex[:8]}"
    r = await ingest_event(
        pool,
        Event(
            source=source,
            external_id=f"{source}:{subject}@1",
            kind="occurrence",
            title=f"Service {subject} down",
            klass="DockerServiceDown",
            subject=subject,
            severity="critical",
            payload={
                "fingerprint": f"aegis-heartbeat:DockerServiceDown:{subject}",
                "labels": {"alertname": "DockerServiceDown", "service_name": subject},
                "description": "below desired",
            },
            occurred_at=NOW,
        ),
        now=NOW,
    )
    return r.problem_id


async def _card(pool, problem_id: str, ref: dict | None = None) -> tuple[str, str]:
    run = f"investigate-{problem_id}-{uuid.uuid4().hex[:4]}"
    await ingest_event(
        pool,
        Event(
            source="investigation",
            external_id=f"{run}:gate2",
            kind="investigation",
            title="Decision card posted.",
            problem_id=problem_id,
            occurred_at=NOW,
        ),
        now=NOW,
    )
    flow_run_id = f"{_PREFIX}{run}"
    iid = await pool.fetchval(
        "INSERT INTO interactions (flow_run_id, agent_id, kind, origin, prompt, status, "
        "timeout_policy, delivery_ref) VALUES ($1, 'pandoras-actor', 'choice', $2, $3, "
        "'pending', 'archive', $4) RETURNING id::text",
        flow_run_id,
        hub_cards.GATE_ORIGIN,
        "<b>Investigation — actionable</b>\nProposed: restart it",
        ref if ref is not None else {"adapter": "slack", "channel": "C1", "ts": "1.5"},
    )
    return iid, flow_run_id


async def test_a_newer_card_retires_edits_and_ends_the_older_ones(db_pool):
    pid = await _problem(db_pool)
    older, older_run = await _card(db_pool, pid)
    delivery, temporal = _Delivery(), _Temporal()
    acts = HubActivities(db_pool=db_pool, delivery=delivery, temporal_client=temporal)

    out = await ActivityEnvironment().run(
        acts.retire_cards,
        {"problem_id": pid, "reason": "superseded", "exclude_run": "investigate-new-1"},
    )

    assert out == {"retired": 1, "finished": 1, "waiting": 0}
    # Edited, not deleted: the reason first, then what the card offered.
    [(ref, text)] = delivery.edits
    assert ref == {"adapter": "slack", "channel": "C1", "ts": "1.5"}
    assert text.startswith("⏭ <b>Replaced by a newer card.</b>")
    assert "Proposed: restart it" in text
    # The waiting run is told the value that ends it without acting.
    assert temporal.signals == [
        (older_run, "submit_response", {"value": "superseded", "note": "auto-closed: newer card"})
    ]
    row = await db_pool.fetchrow(
        "SELECT status, metadata FROM interactions WHERE id = $1::uuid", older
    )
    assert row["status"] == "retired" and "done_at" in row["metadata"]["retired"]
    # Done is done: a second pass touches nothing.
    again = await ActivityEnvironment().run(acts.retire_cards, {})
    assert again == {"retired": 0, "finished": 0, "waiting": 0}
    assert len(delivery.edits) == 1 and len(temporal.signals) == 1


async def test_the_sweep_finishes_what_a_resolve_retired(db_pool):
    """A resolve inside the hub only moves the row; the sweep's call with no
    problem does the rest, and the run is told `self_resolved`."""
    pid = await _problem(db_pool)
    iid, run = await _card(db_pool, pid)
    await set_status(db_pool, pid, "resolved", reason="by hand", source="admin", now=NOW)
    delivery, temporal = _Delivery(), _Temporal()
    acts = HubActivities(db_pool=db_pool, delivery=delivery, temporal_client=temporal)

    out = await ActivityEnvironment().run(acts.retire_cards, {})

    assert out["finished"] >= 1
    texts = [t for _, t in delivery.edits if "Proposed: restart it" in t]
    assert texts and texts[0].startswith("✅ <b>Resolved on its own.</b>")
    assert (run, "submit_response", {"value": "self_resolved", "note": "auto-closed: problem resolved"}) in (
        temporal.signals
    )


async def test_a_problem_back_again_is_not_resolved_by_its_old_card(db_pool):
    """Retired as resolved, but live again by the time the sweep gets there:
    `self_resolved` would make the old run record the live problem resolved,
    so it is told `superseded` instead."""
    pid = await _problem(db_pool)
    _, run = await _card(db_pool, pid)
    await set_status(db_pool, pid, "resolved", reason="by hand", source="admin", now=NOW)
    await set_status(db_pool, pid, "open", reason="back", source="admin", now=NOW + timedelta(minutes=1))
    assert (await get_problem(db_pool, pid))["status"] == "open"
    temporal = _Temporal()
    acts = HubActivities(db_pool=db_pool, delivery=_Delivery(), temporal_client=temporal)

    await ActivityEnvironment().run(acts.retire_cards, {"problem_id": pid})

    assert temporal.signals == [
        (run, "submit_response", {"value": "superseded", "note": "auto-closed: newer card"})
    ]


async def test_a_run_already_over_counts_as_ended_and_a_failed_edit_is_retried(db_pool):
    pid = await _problem(db_pool)
    iid, run = await _card(db_pool, pid)
    delivery, temporal = _Delivery(ok=False), _Temporal(gone={run})
    acts = HubActivities(db_pool=db_pool, delivery=delivery, temporal_client=temporal)

    first = await ActivityEnvironment().run(
        acts.retire_cards, {"problem_id": pid, "reason": "superseded"}
    )
    # NOT_FOUND is fine (nothing waits on the card), but Slack said no: again.
    assert first == {"retired": 1, "finished": 0, "waiting": 1}
    delivery.ok = True
    second = await ActivityEnvironment().run(acts.retire_cards, {"problem_id": pid})
    assert second == {"retired": 0, "finished": 1, "waiting": 0}
    assert len(delivery.edits) == 2


async def test_a_card_with_no_slack_message_needs_no_edit(db_pool):
    """The web channel keeps cards in the admin inbox: nothing to edit."""
    from aegis_worker.activities.delivery import DeliveryActivities

    pid = await _problem(db_pool)
    await _card(db_pool, pid, ref={"adapter": "web"})
    acts = HubActivities(
        db_pool=db_pool, delivery=DeliveryActivities(channel="web"), temporal_client=_Temporal()
    )
    out = await ActivityEnvironment().run(
        acts.retire_cards, {"problem_id": pid, "reason": "superseded"}
    )
    assert out == {"retired": 1, "finished": 1, "waiting": 0}


async def test_promoted_problems_get_the_alert_their_producer_would_have_investigated(db_pool):
    infra = await _problem(db_pool, source="heartbeat")
    money = await _problem(db_pool, source="money")
    gone = await _problem(db_pool, source="alertmanager")
    await set_status(db_pool, gone, "resolved", reason="cleared", source="admin", now=NOW)
    acts = HubActivities(db_pool=db_pool)

    alerts = await ActivityEnvironment().run(acts.promoted_investigations, [infra, money, gone])

    assert [a["problem_id"] for a in alerts] == [infra]
    [alert] = alerts
    assert alert["source"] == "aegis-heartbeat"
    assert alert["labels"]["alertname"] == "DockerServiceDown"
    assert alert["fingerprint"].startswith("aegis-heartbeat:DockerServiceDown:")
    assert alert["service"] == alert["labels"]["service_name"]
    assert alert["escalate"] is False
