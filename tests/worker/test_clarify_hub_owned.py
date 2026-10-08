"""#472 — clarify leaves the problem hub's tasks to the hub.

The hub projects its `#alert` tasks into the managed Inbox, which is the only
project clarify reads. Clarify must never trash, refile or re-route one: the
hub decides what happens to it. The infra lane that used to investigate them
moved to the DevOps vertical (a2-devops); the guard stays for v1's own
producers.

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

# A route whose regex matches the hub's own titles ("Flow x failing").
_ROUTE = {
    "key": "failing-things",
    "match": "regex",
    "value": "(?i)(flow|service).*(failing|down)",
    "assignee": "@raphael",
    "contexts": ["@deep"],
}


@pytest_asyncio.fixture(autouse=True, loop_scope="function")
async def _route(db_pool):
    clarify_mod._routes_cache.update(routes=None, ts=0.0)
    await save_content_routes(db_pool, [_ROUTE])
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
    content: str = "Flow TodoistSyncFlow failing",
    labels: tuple[str, ...] = ("#alert", "@sebas"),
    source_tag: str | None = "#alert",
    last_clarified_at: datetime | None = None,
) -> dict:
    """The mirror row TodoistSyncFlow writes, and the dict clarify passes on."""
    assignee = next((lab for lab in labels if lab.startswith("@") and lab != "@next"), None)
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


async def _hub_problem(pool, task_id: str) -> str:
    """A problem that owns `task_id`, left the way a watchdog's ingest and the
    projector leave it."""
    now = datetime.now(UTC)
    flow = f"zzflow{uuid.uuid4().hex[:6]}"
    result = await ingest_event(
        pool,
        Event(
            source="flow_health",
            external_id=f"flow_health:flow_failing:{flow}@{now.isoformat()}",
            kind="occurrence",
            title=f"Flow {flow} failing",
            klass="flow_failing",
            subject=flow,
            subject_kind="flow",
            severity="critical",
            occurred_at=now,
        ),
        now=now,
    )
    assert await link_task(pool, result.problem_id, task_id)
    return result.problem_id


def _acts(db_pool):
    connector = AsyncMock()
    connector.commands = AsyncMock(
        return_value={"ok": True, "data": {"sync_status": {}, "temp_id_mapping": {}}}
    )
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=connector, llm_client=AsyncMock())
    return acts, connector


# --- classify_one: the guard ---------------------------------------------------


async def test_the_hubs_own_alert_task_is_left_to_the_hub(db_pool):
    """A hub task matches the route, but the route never re-labels it and the
    classifier never sees it."""
    acts, _ = _acts(db_pool)
    task_id = _tid()
    task = await _task(db_pool, task_id)
    await _hub_problem(db_pool, task_id)
    decision = await acts.classify_one(task)
    assert decision["classification"] == "hub_owned"
    assert decision["llm_model"] == "rules"
    acts.llm_client.think.assert_not_awaited()


async def test_an_alert_task_is_owned_before_its_id_is_linked(db_pool):
    """A task created through the outbox is linked by its temp id until the
    projector swaps in the real one, so `#alert` is the signal meanwhile."""
    acts, _ = _acts(db_pool)
    task = await _task(db_pool, _tid(), content="Something unrouted")
    assert (await acts.classify_one(task))["classification"] == "hub_owned"
    acts.llm_client.think.assert_not_awaited()


async def test_a_task_the_hub_adopted_is_not_re_routed(db_pool):
    """No `#alert` tag — a hand-captured task a problem holds. Ownership is the
    problem, found by `find_problem_for_task`."""
    acts, _ = _acts(db_pool)
    task_id = _tid()
    task = await _task(db_pool, task_id, labels=(), source_tag="#chat")
    await _hub_problem(db_pool, task_id)
    assert (await acts.classify_one(task))["classification"] == "hub_owned"


async def test_a_task_the_hub_does_not_own_takes_the_route(db_pool):
    acts, _ = _acts(db_pool)
    task = await _task(db_pool, _tid(), labels=(), source_tag="#chat")
    decision = await acts.classify_one(task)
    assert decision["classification"] == "route_apply"
    assert decision["assignee"] == "@raphael"


async def test_a_comment_on_a_hub_task_goes_to_its_owner(db_pool):
    """The comment channel sits above the hub check: a question on the task is
    its owner's to answer."""
    acts, _ = _acts(db_pool)
    task = await _task(db_pool, _tid(), content="Something unrouted")
    task["latest_user_note"] = "why did this start?"
    decision = await acts.classify_one(task)
    assert decision["classification"] == "sebas_followup"


# --- a money task that falls back to the Inbox --------------------------------
#
# A money problem's task lands in the Inbox only when `books_todoist_projects`
# names no `personal` project. A `trash` verdict would complete it, and the hub
# then reads that completion back as the user acknowledging the finding: the
# system grading its own work.

_MONEY_LABELS = ("#money", "@maou", "@next")


async def test_a_money_task_is_the_hubs_and_never_reaches_the_classifier(db_pool):
    acts, connector = _acts(db_pool)
    task = await _task(
        db_pool, _tid(), content="3 unmatched rows on axis-cc-1313",
        labels=_MONEY_LABELS, source_tag="#money",
    )
    decision = await acts.classify_one(task)
    assert decision["classification"] == "hub_owned"
    # A title the route matches still never reaches the route.
    routed = await _task(
        db_pool, _tid(), content="Service charge down 2 rows on axis-cc-1313",
        labels=_MONEY_LABELS, source_tag="#money",
    )
    assert (await acts.classify_one(routed))["classification"] == "hub_owned"
    acts.llm_client.think.assert_not_awaited()

    # It already has its state, so clarify sends nothing: no complete, no move.
    out = await acts.apply_outcome(task, decision)
    assert out["applied"] is True and out["commands_sent"] == 0
    connector.commands.assert_not_awaited()


async def test_a_comment_on_a_money_task_still_reaches_maou(db_pool):
    acts, _ = _acts(db_pool)
    task = await _task(
        db_pool, _tid(), content="3 unmatched rows on axis-cc-1313",
        labels=_MONEY_LABELS, source_tag="#money",
    )
    task["latest_user_note"] = "the 12 July one was rent, the rest are groceries"
    decision = await acts.classify_one(task)
    assert decision["classification"] == "maou_followup"
    assert decision["assignee"] == "@maou"


# --- apply_outcome: hub_owned leaves clarify correctly -------------------------


async def test_hub_owned_starts_nothing_and_lands_next(db_pool):
    acts, connector = _acts(db_pool)
    task = await _task(db_pool, _tid())
    out = await acts.apply_outcome(task, await acts.classify_one(task))
    assert out["applied"] is True  # so the flow bumps the watermark
    assert out["interaction_spawned"] is False
    assert out["interaction_payload"] is None
    (cmd,) = connector.commands.await_args.args[0]
    assert cmd["type"] == "item_update"
    # The projector created it with `#alert` and the owner's label only, so
    # clarify owes it the GTD state and nothing else.
    assert sorted(cmd["args"]["labels"]) == ["#alert", "@next", "@sebas"]


async def test_hub_owned_keeps_a_state_the_task_already_has(db_pool):
    """Adding `@next` to a `@waiting` task would give it two states."""
    acts, connector = _acts(db_pool)
    task = await _task(db_pool, _tid(), labels=("#alert", "@sebas", "@waiting"))
    out = await acts.apply_outcome(task, await acts.classify_one(task))
    assert out["applied"] is True
    assert out["commands_sent"] == 0
    connector.commands.assert_not_awaited()


# --- find_unclassified_items: no loop ------------------------------------------


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
    loop guard each one would read as a user follow-up and start a reply,
    which would comment, which would start another."""
    now = datetime.now(UTC)
    task_id = _tid()
    await _task(db_pool, task_id, last_clarified_at=now - timedelta(minutes=10))
    await _hub_problem(db_pool, task_id)
    hub_comment = "⚠️ 3 more occurrences (4 in total)." + FOOTER
    await _note(db_pool, task_id, hub_comment, now - timedelta(minutes=5))
    assert task_id not in await _eligible(db_pool)

    await _note(db_pool, task_id, "still failing after the fix?", now - timedelta(minutes=1))
    row = (await _eligible(db_pool))[task_id]
    assert row["latest_user_note"] == "still failing after the fix?"
