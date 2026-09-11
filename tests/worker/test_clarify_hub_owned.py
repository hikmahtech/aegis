"""#472 — clarify must not start a second investigation for a task the hub owns.

The problem hub projects its `#alert` tasks into the managed Inbox, which is
the only project clarify reads. In prod the `infra-incident` content route
matched the hub's own titles ("Service aegis_core down", "Swarm node wow
down"), and clarify's `pandora_investigation` branch started a fresh
AlertInvestigationFlow with the task but no problem. Its step 0 ingested a new
event, which created a second problem (and a second decision card) for an
incident the hub was already handling, and linked the task to both.

Real test database throughout: the hub rows are written by the hub's own
functions, the way a producer leaves them.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aegis.services.content_routes import save_content_routes
from aegis.services.hub import Event, ingest_event
from aegis.services.hub_project import FOOTER, link_task
from aegis_worker.activities import clarify as clarify_mod
from aegis_worker.activities.clarify import ClarifyActivities

pytestmark = pytest.mark.asyncio

# The prod route, as `settings.content_routes` held it on 2026-09-11.
_INFRA_ROUTE = {
    "key": "infra-incident",
    "gate": True,
    "match": "regex",
    "value": "(?i)(node|swarm|service).*(down|unreachable|stuck)",
    "assignee": "@pandora",
    "contexts": ["@deep"],
    "alert_overrides": {"source": "todoist-infra", "severity": "critical", "alertname": "NodeDown"},
}


@pytest_asyncio.fixture(autouse=True, loop_scope="function")
async def _infra_route(db_pool):
    clarify_mod._routes_cache.update(routes=None, ts=0.0)
    await save_content_routes(db_pool, [_INFRA_ROUTE])
    clarify_mod._routes_cache.update(routes=None, ts=0.0)
    await db_pool.execute(
        "INSERT INTO settings (key, value) VALUES ('todoist_managed_project_ids', $1) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        {"inbox": "P_INBOX"},
    )
    await db_pool.execute(
        "INSERT INTO todoist_projects (id, name, is_managed, raw) "
        "VALUES ('P_INBOX','Inbox',true,'{}'::jsonb) ON CONFLICT (id) DO NOTHING"
    )
    yield
    await db_pool.execute("DELETE FROM settings WHERE key = 'content_routes'")
    clarify_mod._routes_cache.update(routes=None, ts=0.0)


def _tid() -> str:
    return f"6h{uuid.uuid4().hex[:14]}"


async def _task(
    pool,
    task_id: str,
    *,
    content: str = "Swarm node wow down",
    labels: tuple[str, ...] = ("#alert", "@pandora"),
    source_tag: str | None = "#alert",
    last_clarified_at: datetime | None = None,
) -> dict:
    """The mirror row TodoistSyncFlow writes, and the dict clarify passes on."""
    assignee = "@pandora" if "@pandora" in labels else None
    await pool.execute(
        "INSERT INTO todoist_tasks (id, project_id, content, labels, assignee_label, source_tag, "
        "is_completed, raw, last_clarified_at) "
        "VALUES ($1, 'P_INBOX', $2, $3, $4, $5, false, '{}'::jsonb, $6)",
        task_id,
        content,
        list(labels),
        assignee,
        source_tag,
        last_clarified_at,
    )
    return {
        "id": task_id,
        "content": content,
        "description": "",
        "labels": list(labels),
        "source_tag": source_tag,
        "latest_user_note": None,
    }


async def _hub_problem(pool, task_id: str, *, closed: bool = False) -> str:
    """A problem that owns `task_id`, left behind the way the heartbeat's
    ingest and the projector leave it."""
    now = datetime.now(UTC)
    node = f"zznode{uuid.uuid4().hex[:6]}"
    result = await ingest_event(
        pool,
        Event(
            source="heartbeat",
            external_id=f"aegis-heartbeat:NodeDown:{node}@{now.isoformat()}",
            kind="occurrence",
            title=f"Swarm node {node} down",
            klass="NodeDown",
            subject=node,
            subject_kind="node",
            severity="critical",
            occurred_at=now,
        ),
        now=now,
    )
    assert await link_task(pool, result.problem_id, task_id)
    if closed:
        await pool.execute(
            "UPDATE problems SET status = 'closed', closed_at = now() WHERE id = $1::uuid",
            result.problem_id,
        )
    return result.problem_id


def _acts(db_pool):
    connector = AsyncMock()
    connector.commands = AsyncMock(
        return_value={"ok": True, "data": {"sync_status": {}, "temp_id_mapping": {}}}
    )
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=connector, llm_client=AsyncMock())
    return acts, connector


# --- classify_one: the guard ---------------------------------------------------


async def test_the_hubs_own_alert_task_is_not_investigated_again(db_pool):
    """The prod path: a hub task carries `@pandora` (the projector assigns the
    infra agent), matches the route, and no investigation id names it — so
    the retry branch used to call it a crashed investigation and start one."""
    acts, _ = _acts(db_pool)
    task_id = _tid()
    task = await _task(db_pool, task_id)
    await _hub_problem(db_pool, task_id)
    decision = await acts.classify_one(task)
    assert decision["classification"] == "hub_owned"
    assert decision["llm_model"] == "rules"


async def test_a_task_the_hub_adopted_is_owned_too(db_pool):
    """No `#alert` tag — a hand-captured task whose first investigation gave it
    a problem. Ownership is the problem, found by `find_problem_for_task`."""
    acts, _ = _acts(db_pool)
    task_id = _tid()
    task = await _task(db_pool, task_id, labels=("@pandora",), source_tag="#chat")
    await _hub_problem(db_pool, task_id)
    assert (await acts.classify_one(task))["classification"] == "hub_owned"


async def test_an_alert_task_is_owned_before_its_id_is_linked(db_pool):
    """A task created through the outbox is linked by its temp id until the
    projector swaps in the real one, so `#alert` is the signal meanwhile."""
    acts, _ = _acts(db_pool)
    task = await _task(db_pool, _tid())
    assert (await acts.classify_one(task))["classification"] == "hub_owned"


async def test_a_task_the_hub_does_not_own_still_gets_its_investigation(db_pool):
    acts, _ = _acts(db_pool)
    task = await _task(db_pool, _tid(), labels=("@pandora",), source_tag="#chat")
    assert (await acts.classify_one(task))["classification"] == "pandora_investigation"


async def test_a_hub_task_without_the_agent_label_gets_no_gate_card(db_pool):
    """Without `@pandora` the route would ask "want Pandora to investigate?"
    about an incident the hub is already investigating."""
    acts, _ = _acts(db_pool)
    task_id = _tid()
    task = await _task(db_pool, task_id, labels=("#alert",))
    await _hub_problem(db_pool, task_id)
    assert (await acts.classify_one(task))["classification"] == "hub_owned"


async def test_a_fresh_task_still_gets_its_gate_card(db_pool):
    acts, _ = _acts(db_pool)
    task = await _task(db_pool, _tid(), labels=(), source_tag="#chat")
    assert (await acts.classify_one(task))["classification"] == "pandora_gate"


# --- apply_outcome: hub_owned leaves clarify correctly -------------------------


async def test_hub_owned_starts_nothing_and_lands_next(db_pool):
    acts, connector = _acts(db_pool)
    task_id = _tid()
    task = await _task(db_pool, task_id)
    out = await acts.apply_outcome(task, await acts.classify_one(task))
    assert out["applied"] is True  # so the flow bumps the watermark
    assert out["interaction_spawned"] is False
    assert out["interaction_payload"] is None
    (cmd,) = connector.commands.await_args.args[0]
    assert cmd["type"] == "item_update"
    # The projector created it with `#alert` and the agent label only, so
    # clarify owes it the GTD state and nothing else.
    assert sorted(cmd["args"]["labels"]) == ["#alert", "@next", "@pandora"]


async def test_hub_owned_keeps_a_state_the_task_already_has(db_pool):
    """A hub task the investigation parked on a decision card is `@waiting`.
    Adding `@next` would give it two states, which is as unanswerable as none."""
    acts, connector = _acts(db_pool)
    task = await _task(db_pool, _tid(), labels=("#alert", "@pandora", "@waiting"))
    out = await acts.apply_outcome(task, await acts.classify_one(task))
    assert out["applied"] is True
    assert out["commands_sent"] == 0
    connector.commands.assert_not_awaited()


# --- a human follow-up investigates the task's OWN problem --------------------


async def _followup(db_pool, task_id: str, **kw) -> dict:
    acts, _ = _acts(db_pool)
    task = await _task(db_pool, task_id, **kw)
    task["latest_user_note"] = "still down after the reboot?"
    task["last_note_at"] = datetime.now(UTC)
    decision = await acts.classify_one(task)
    assert decision["classification"] == "pandora_followup"
    out = await acts.apply_outcome(task, decision)
    assert out["applied"] is True and out["interaction_spawned"] is True
    return out["interaction_payload"]["alert"]


async def test_a_followup_on_a_hub_task_names_its_problem(db_pool):
    """With `problem_id` the flow's step 0 skips ingest, so the comment is
    investigated on the problem the task already belongs to."""
    task_id = _tid()
    problem_id = await _hub_problem(db_pool, task_id)
    alert = await _followup(db_pool, task_id)
    assert alert["problem_id"] == problem_id
    assert alert["todoist_task_id"] == task_id
    assert "still down after the reboot?" in alert["description"]


async def test_a_followup_on_an_unowned_task_names_no_problem(db_pool):
    alert = await _followup(db_pool, _tid(), labels=("@pandora",), source_tag="#chat")
    assert "problem_id" not in alert


async def test_a_followup_on_a_closed_problem_starts_fresh(db_pool):
    """A closed problem is history; the investigation gets a problem of its
    own (the `ensure_problem_for_task` rule), keyed on the task."""
    task_id = _tid()
    await _hub_problem(db_pool, task_id, closed=True)
    alert = await _followup(db_pool, task_id)
    assert "problem_id" not in alert


# --- find_unclassified_items: no loop, no hourly re-entry ----------------------


async def _eligible(db_pool) -> dict[str, dict]:
    acts, _ = _acts(db_pool)
    return {r["id"]: r for r in await acts.find_unclassified_items(max_items=1000)}


async def _note(pool, task_id: str, content: str, posted_at: datetime) -> None:
    await pool.execute(
        "INSERT INTO todoist_notes (id, item_id, content, posted_at) VALUES ($1, $2, $3, $4)",
        f"n{uuid.uuid4().hex[:12]}",
        task_id,
        content,
        posted_at,
    )
    await pool.execute(
        "UPDATE todoist_tasks SET last_note_at = GREATEST(COALESCE(last_note_at, $2), $2) "
        "WHERE id = $1",
        task_id,
        posted_at,
    )


async def test_the_hubs_own_comment_does_not_wake_clarify(db_pool):
    """Every projector comment carries `Workflow run: problem-hub`. Without the
    loop guard each one would read as a user follow-up and start a new
    investigation, which would comment, which would start another."""
    now = datetime.now(UTC)
    task_id = _tid()
    await _task(db_pool, task_id, last_clarified_at=now - timedelta(minutes=10))
    await _hub_problem(db_pool, task_id)
    hub_comment = "⚠️ 3 more occurrences (4 in total)." + FOOTER
    await _note(db_pool, task_id, hub_comment, now - timedelta(minutes=5))
    assert task_id not in await _eligible(db_pool)

    await _note(db_pool, task_id, "still down after the reboot?", now - timedelta(minutes=1))
    row = (await _eligible(db_pool))[task_id]
    assert row["latest_user_note"] == "still down after the reboot?"


async def test_the_retry_surface_skips_a_hub_task(db_pool):
    """The retry branch re-admits a `@pandora` route task every hour until an
    investigation naming it completes. The hub's investigations are named for
    the problem, so a hub task was re-admitted for as long as it was open."""
    two_hours_ago = datetime.now(UTC) - timedelta(hours=2)
    # Projected and linked: the ordinary hub task.
    owned = _tid()
    await _task(db_pool, owned, last_clarified_at=two_hours_ago)
    await _hub_problem(db_pool, owned)
    # Projected, link still on the outbox temp id: only the tag says so.
    tagged = _tid()
    await _task(db_pool, tagged, last_clarified_at=two_hours_ago)
    # Hand-captured, adopted by a problem: only the problem says so.
    adopted = _tid()
    await _task(
        db_pool, adopted, labels=("@pandora",), source_tag="#chat", last_clarified_at=two_hours_ago
    )
    await _hub_problem(db_pool, adopted)
    unowned = _tid()
    await _task(
        db_pool, unowned, labels=("@pandora",), source_tag="#chat", last_clarified_at=two_hours_ago
    )
    eligible = await _eligible(db_pool)
    assert unowned in eligible  # the retry surface itself still works
    assert owned not in eligible
    assert tagged not in eligible
    assert adopted not in eligible
