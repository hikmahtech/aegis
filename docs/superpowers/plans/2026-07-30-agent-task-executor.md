# Agent Task Executor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the loop on AEGIS's own triage output — 80 agent-assigned Todoist tasks that have sat untouched since 2026-07-01 — by executing each according to its `source_tag`.

**Architecture:** Two Temporal workflows mirroring the existing `SentryPollFlow` → `AlertInvestigationFlow` split. `AgentTaskSweepFlow` runs on a 15-minute schedule, selects at most 3 eligible tasks (oldest first, 6h per-task cooldown), and spawns one **abandoned** `AgentTaskFlow` child per task. Each child resolves a verb from the task's `source_tag`, runs a read-only investigation freely, and requires an `InteractionFlow` approval card before any write. Every child ends by either completing the task or parking it at `@waiting` — and because eligibility excludes `@waiting`, that removal from the pool is what prevents an infinite slow loop.

**Tech Stack:** Python 3.12, Temporal (`temporalio`), asyncpg over Postgres 16, pytest with `pytest-asyncio` (`asyncio_mode=auto`) and `WorkflowEnvironment.start_time_skipping()`.

## Global Constraints

- **`agent_id` MUST be the first field** of every flow config dataclass — `WorkflowRunRecorderInterceptor` reads it to populate `workflow_runs.agent_id`.
- **`todoist_task_id` MUST be a field on `AgentTaskFlowInput`** — `interceptors._extract_todoist_task_ref` (`worker/src/aegis_worker/interceptors.py:119`) reads exactly that attribute name to populate `workflow_runs.todoist_task_ref`, which Task 1's cooldown query depends on.
- **Every agent-authored Todoist comment MUST contain the literal string `Workflow run:`.** Clarify's eligibility filter (`activities/clarify.py:371-373`) excludes AEGIS-authored notes by matching `[ClarifyFlow @`, `[Agent reply @`, and `%Workflow run:%`. A comment without one of these reads as fresh user input and re-spawns the flow every 15 minutes. This loop has shipped and been fixed twice (2026-05-21, 2026-05-27).
- **Never resolve an agent by literal id.** Use `AgentRegistryActivities.resolve_agents(tags)`; zero holders ⇒ skip with a warning, never crash.
- **Never apply the active-work guard in this flow.** It keys on due/overdue Todoist tasks naming the repo — which is the task being executed — and would suppress every run.
- **Do NOT run `ruff format` on `core/src/aegis/services/chat.py`** — it has local-ruff-version drift and rewrites the whole ~4000-line file. Run `ruff check` (must pass) and write already-formatted edits.
- Lint gate CI enforces: `ruff check worker/src/ tests/worker/`.
- Test command (per-file parallel; the suite deadlocks without it): `PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/ -n auto --dist loadfile --timeout=300`
- From a git worktree you **must** set `PYTHONPATH=core/src:worker/src:comms/src` or a bare `pytest` silently tests the main checkout's code.

---

## File Structure

| File | Responsibility |
|---|---|
| `worker/src/aegis_worker/activities/agent_task.py` | **Create.** Eligibility query, verb resolution, task-context lookup, terminal-state writers, per-verb executor activities. |
| `worker/src/aegis_worker/flows/agent_task.py` | **Create.** `AgentTaskSweepFlow` (scheduled dispatcher) + `AgentTaskFlow` (per-task). |
| `worker/src/aegis_worker/activities/infra_ops.py` | **Create.** Activity wrappers over `HomelabConnector.service_ps` / `restart_service` — these exist only as chat tools today, and workflows cannot reach the DB or connectors directly. |
| `worker/src/aegis_worker/__main__.py` | **Modify.** Construct the activity classes and register both flows + all activities in **FOUR** lists (see the Registration note below). |
| `worker/src/aegis_worker/schedule_sync.py` | **Modify.** Add `AgentTaskSweepFlow` to `_ACTIVITY_TYPE_MAP`. |
| `config/seed/activities.yaml` | **Modify.** Seed the `agent-task-15min` schedule row. |
| `tests/worker/activities/test_agent_task_eligibility.py` | **Create.** Real-DB tests for the selection query. |
| `tests/worker/activities/test_agent_task_verbs.py` | **Create.** Verb resolution + context extraction (pure). |
| `tests/worker/flows/test_agent_task_flow.py` | **Create.** Flow-level, time-skipping. |
| `tests/worker/activities/test_agent_task_terminal.py` | **Create.** Terminal-state table test (the anti-infinite-loop guard). |

### Registration note — read before Task 3

Activity classes are constructed **inline in `main()` in `__main__.py`**, not in `bootstrap.py`
(`bootstrap.py` only builds `deps`, exposing `deps.pool`, `deps.connectors`, `deps.llm`,
`deps.settings`). Connectors come from `connectors.get("homelab" | "remote_script" | "social" |
"knowledge")`; `todoist_connector` is a local variable already in scope by line ~400.

`__main__.py` has **FOUR** registration lists and every new flow/activity must be added to the right
ones — a past prod boot regression came from updating only some:

| Location | Purpose |
|---|---|
| `WORKFLOWS` (module level, ~line 106) | import-time tests |
| `ACTIVITIES` (module level, ~line 131) | import-time tests; uses `_stub_*` instances built with `db_pool=None` |
| `workflows = [...]` inside `main()` (~line 601) | the live worker |
| `activities = [...]` inside `main()` (~line 452) | the live worker |

So each new activity needs a stub instance entry **and** a live bound-method entry.

Tasks 4–7 (one per verb) are independent of each other. Task 3 is the first shippable increment — it proves the sweep, spawn, comment, and terminal-state plumbing end to end with no verb logic.

---

### Task 1: Eligibility query and backlog brake

**Files:**
- Create: `worker/src/aegis_worker/activities/agent_task.py`
- Test: `tests/worker/activities/test_agent_task_eligibility.py`

**Interfaces:**
- Consumes: nothing (first task).
- Produces: `AgentTaskActivities(db_pool)` with
  `find_actionable_tasks(max_tasks: int = 3, cooldown_hours: int = 6, max_coding: int = 1) -> list[dict]`.
  Each dict has keys `id`, `content`, `description`, `labels` (`list[str]`), `source_tag` (`str | None`),
  `project_id`, `assignee_label`, `updated_at`.

- [ ] **Step 1: Write the failing test**

```python
# tests/worker/activities/test_agent_task_eligibility.py
"""AgentTaskActivities.find_actionable_tasks — eligibility + backlog brake."""

from __future__ import annotations

import pytest_asyncio
from aegis_worker.activities.agent_task import AgentTaskActivities

_IDS = tuple(f"tt-{n}" for n in range(1, 10))


@pytest_asyncio.fixture(loop_scope="function")
async def _seed(db_pool):
    await db_pool.execute("DELETE FROM todoist_tasks WHERE id = ANY($1::text[])", list(_IDS))
    await db_pool.execute(
        """
        INSERT INTO todoist_tasks
            (id, content, labels, source_tag, assignee_label, is_completed, updated_at)
        VALUES
          ('tt-1','alert oldest',   ARRAY['@pandora'],           '#alert',  '@pandora', false, now() - interval '9 days'),
          ('tt-2','email mid',      ARRAY['@sebas'],             '#email',  '@sebas',   false, now() - interval '8 days'),
          ('tt-3','receipt newer',  ARRAY['@maou'],              '#receipt','@maou',    false, now() - interval '7 days'),
          ('tt-4','someday',        ARRAY['@pandora','@someday'],'#alert',  '@pandora', false, now() - interval '10 days'),
          ('tt-5','waiting',        ARRAY['@pandora','@waiting'],'#alert',  '@pandora', false, now() - interval '10 days'),
          ('tt-6','done',           ARRAY['@pandora'],           '#alert',  '@pandora', true,  now() - interval '10 days'),
          ('tt-7','no assignee',    ARRAY['@next'],              '#alert',  NULL,       false, now() - interval '10 days'),
          ('tt-8','user code task', ARRAY['@pandora','@code'],   NULL,      '@pandora', false, now() - interval '6 days'),
          ('tt-9','dateless alert', ARRAY['@pandora'],           '#alert',  '@pandora', false, now() - interval '5 days')
        """
    )
    yield
    await db_pool.execute("DELETE FROM todoist_tasks WHERE id = ANY($1::text[])", list(_IDS))
    await db_pool.execute("DELETE FROM workflow_runs WHERE todoist_task_ref = ANY($1::text[])", list(_IDS))


async def test_selects_dateless_agent_tasks_oldest_first(db_pool, _seed):
    """No due date is required — none of the 80 real tasks has one."""
    act = AgentTaskActivities(db_pool=db_pool)
    rows = await act.find_actionable_tasks(max_tasks=3)
    assert [r["id"] for r in rows] == ["tt-1", "tt-2", "tt-3"]


async def test_excludes_someday_waiting_completed_and_unassigned(db_pool, _seed):
    act = AgentTaskActivities(db_pool=db_pool)
    ids = {r["id"] for r in await act.find_actionable_tasks(max_tasks=50)}
    assert "tt-4" not in ids  # @someday
    assert "tt-5" not in ids  # @waiting — the parking state must exit the pool
    assert "tt-6" not in ids  # completed
    assert "tt-7" not in ids  # no assignee label


async def test_cap_respected(db_pool, _seed):
    act = AgentTaskActivities(db_pool=db_pool)
    assert len(await act.find_actionable_tasks(max_tasks=2)) == 2


async def test_cooldown_excludes_recently_run_task(db_pool, _seed):
    await db_pool.execute(
        """
        INSERT INTO workflow_runs (run_id, workflow_id, workflow_type, status, started_at, todoist_task_ref)
        VALUES ('r1','agent-task-tt-1','AgentTaskFlow','completed', now() - interval '1 hour', 'tt-1')
        """
    )
    act = AgentTaskActivities(db_pool=db_pool)
    ids = [r["id"] for r in await act.find_actionable_tasks(max_tasks=3)]
    assert "tt-1" not in ids


async def test_cooldown_expired_task_is_eligible_again(db_pool, _seed):
    await db_pool.execute(
        """
        INSERT INTO workflow_runs (run_id, workflow_id, workflow_type, status, started_at, todoist_task_ref)
        VALUES ('r2','agent-task-tt-1','AgentTaskFlow','completed', now() - interval '7 hours', 'tt-1')
        """
    )
    act = AgentTaskActivities(db_pool=db_pool)
    assert "tt-1" in [r["id"] for r in await act.find_actionable_tasks(max_tasks=3)]


async def test_at_most_one_coding_task_per_batch(db_pool, _seed):
    """Coding runs take minutes and the tmux window cap is 10."""
    await db_pool.execute(
        """
        INSERT INTO todoist_tasks (id, content, labels, source_tag, assignee_label, is_completed, updated_at)
        VALUES ('tt-10','code b', ARRAY['@pandora','@code'], NULL, '@pandora', false, now() - interval '11 days')
        """
    )
    try:
        act = AgentTaskActivities(db_pool=db_pool)
        rows = await act.find_actionable_tasks(max_tasks=5, max_coding=1)
        coding = [r for r in rows if r["source_tag"] is None and "@code" in r["labels"]]
        assert len(coding) == 1
    finally:
        await db_pool.execute("DELETE FROM todoist_tasks WHERE id = 'tt-10'")


async def test_no_pool_degrades_to_empty():
    assert await AgentTaskActivities(db_pool=None).find_actionable_tasks() == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/activities/test_agent_task_eligibility.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'aegis_worker.activities.agent_task'`

- [ ] **Step 3: Write minimal implementation**

```python
# worker/src/aegis_worker/activities/agent_task.py
"""AgentTaskActivities — execute AEGIS's own agent-assigned Todoist tasks.

Every one of the 80 agent-assigned tasks in prod is AEGIS's own triage output
(source_tag #alert/#email/#receipt), not a user delegation, and NONE has a due
date. So eligibility deliberately does not require one — requiring a date would
keep this flow permanently idle. The brake is instead: a small cap per tick,
oldest first, and a per-task cooldown.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from temporalio import activity

# Assignee labels this flow will act on. @me is deliberately absent: a task the
# user has claimed is theirs to handle.
ADDRESSABLE_ASSIGNEES = ["@sebas", "@raphael", "@maou", "@pandora"]

# Reaching either of these removes a task from the eligible pool. Without that,
# the cooldown becomes an infinite slow loop over the same tasks.
PARK_LABEL = "@waiting"
EXCLUDED_LABELS = ["@someday", PARK_LABEL]


@dataclass
class AgentTaskActivities:
    db_pool: Any = None
    todoist_connector: Any = None
    remote_script: Any = None
    homelab_connector: Any = None
    gmail_accounts: list[str] = field(default_factory=list)

    @activity.defn
    async def find_actionable_tasks(
        self, max_tasks: int = 3, cooldown_hours: int = 6, max_coding: int = 1
    ) -> list[dict]:
        """Eligible agent-assigned tasks, oldest first, cooldown-filtered.

        `max_coding` caps coding tasks (source_tag IS NULL + @code) within the
        batch — a kimi run takes minutes and the coding host's tmux window cap
        is 10, so an uncapped fan-out would wedge it.
        """
        if self.db_pool is None:
            return []
        rows = await self.db_pool.fetch(
            """
            SELECT t.id, t.content, t.description, t.labels, t.source_tag,
                   t.project_id, t.assignee_label, t.updated_at
            FROM todoist_tasks t
            WHERE NOT t.is_completed
              AND t.assignee_label = ANY($1::text[])
              AND NOT (t.labels && $2::text[])
              AND NOT EXISTS (
                  SELECT 1 FROM workflow_runs wr
                  WHERE wr.workflow_type = 'AgentTaskFlow'
                    AND wr.todoist_task_ref = t.id
                    AND wr.started_at > now() - make_interval(hours => $3)
              )
            ORDER BY t.updated_at ASC
            LIMIT $4
            """,
            ADDRESSABLE_ASSIGNEES,
            EXCLUDED_LABELS,
            cooldown_hours,
            # Over-fetch so the coding cap can drop rows without shrinking the batch.
            max_tasks * 4,
        )

        out: list[dict] = []
        coding_seen = 0
        for row in rows:
            task = dict(row)
            task["labels"] = list(task["labels"] or [])
            is_coding = task["source_tag"] is None and "@code" in task["labels"]
            if is_coding:
                if coding_seen >= max_coding:
                    continue
                coding_seen += 1
            out.append(task)
            if len(out) >= max_tasks:
                break
        return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/activities/test_agent_task_eligibility.py -v`
Expected: PASS — 7 passed

- [ ] **Step 5: Lint and commit**

```bash
ruff check worker/src/ tests/worker/
git add worker/src/aegis_worker/activities/agent_task.py tests/worker/activities/test_agent_task_eligibility.py
git commit -m "feat(agent-task): eligibility query with per-task cooldown and coding cap"
```

---

### Task 2: Verb resolution and task context

**Files:**
- Modify: `worker/src/aegis_worker/activities/agent_task.py`
- Test: `tests/worker/activities/test_agent_task_verbs.py`

**Interfaces:**
- Consumes: `AgentTaskActivities` from Task 1.
- Produces:
  - `resolve_verb(task: dict) -> str` — module-level pure function returning `"infra" | "finance" | "email" | "coding" | "unknown"`.
  - `extract_service_name(title: str) -> str` — module-level pure function; `""` when no service is named.
  - `AgentTaskActivities.load_task_context(task_id: str) -> dict` — activity returning
    `{"external_id": str, "fingerprint": str, "gmail_message_id": str}` (empty strings when absent).

- [ ] **Step 1: Write the failing test**

```python
# tests/worker/activities/test_agent_task_verbs.py
"""Verb resolution and task-context extraction."""

from __future__ import annotations

import pytest
import pytest_asyncio
from aegis_worker.activities.agent_task import (
    AgentTaskActivities,
    extract_service_name,
    resolve_verb,
)


@pytest.mark.parametrize(
    ("source_tag", "labels", "expected"),
    [
        ("#alert", ["@pandora"], "infra"),
        ("#receipt", ["@maou"], "finance"),
        ("#email", ["@sebas"], "email"),
        (None, ["@pandora", "@code"], "coding"),
        # source_tag wins over a stray @code label. Clarify put @code on a real
        # #email task in prod; running a coding agent on an email is nonsense.
        ("#email", ["@sebas", "@code"], "email"),
        (None, ["@pandora"], "unknown"),
        ("#chat", ["@pandora"], "unknown"),
    ],
)
def test_resolve_verb(source_tag, labels, expected):
    assert resolve_verb({"source_tag": source_tag, "labels": labels}) == expected


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("PROLONGED: clickhouse_clickhouse degraded for over 2 hours", "clickhouse_clickhouse"),
        ("PROLONGED: postiz_postiz-postgres degraded for over 2 hours", "postiz_postiz-postgres"),
        ("Service ollama_ollama has fewer tasks than desired", "ollama_ollama"),
        ("Loki is down", "loki"),
        ("PostgreSQL is down", "postgresql"),
        ("AttributeError: 'MongoRepository' object has no attribute 'db'", ""),
    ],
)
def test_extract_service_name(title, expected):
    assert extract_service_name(title) == expected


@pytest_asyncio.fixture(loop_scope="function")
async def _ctx_seed(db_pool):
    await db_pool.execute("DELETE FROM todoist_capture_idempotency WHERE todoist_task_ref LIKE 'ct-%'")
    await db_pool.execute(
        """
        INSERT INTO todoist_capture_idempotency (source_tag, external_id, todoist_task_ref)
        VALUES ('#alert','alert-a2827e4213f4dae4','ct-1'),
               ('#email','gmail-19f761cbfd89d8c8','ct-2')
        """
    )
    yield
    await db_pool.execute("DELETE FROM todoist_capture_idempotency WHERE todoist_task_ref LIKE 'ct-%'")


async def test_load_task_context_alert_fingerprint(db_pool, _ctx_seed):
    ctx = await AgentTaskActivities(db_pool=db_pool).load_task_context("ct-1")
    assert ctx["fingerprint"] == "a2827e4213f4dae4"
    assert ctx["gmail_message_id"] == ""


async def test_load_task_context_gmail_message_id(db_pool, _ctx_seed):
    ctx = await AgentTaskActivities(db_pool=db_pool).load_task_context("ct-2")
    assert ctx["gmail_message_id"] == "19f761cbfd89d8c8"
    assert ctx["fingerprint"] == ""


async def test_load_task_context_missing_row_is_empty(db_pool, _ctx_seed):
    ctx = await AgentTaskActivities(db_pool=db_pool).load_task_context("ct-absent")
    assert ctx == {"external_id": "", "fingerprint": "", "gmail_message_id": ""}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/activities/test_agent_task_verbs.py -v`
Expected: FAIL — `ImportError: cannot import name 'resolve_verb'`

- [ ] **Step 3: Write minimal implementation**

Add to `worker/src/aegis_worker/activities/agent_task.py` — the two pure functions at module level (after `EXCLUDED_LABELS`), and the activity inside the class:

```python
import re

# source_tag → verb. source_tag is PRIMARY; @code is consulted only when
# source_tag IS NULL (i.e. the task is user-authored). Clarify put a stray
# @code label on a real #email task in prod, and treating that as "run a
# coding agent on this email" would be nonsense.
_VERB_BY_SOURCE_TAG = {
    "#alert": "infra",
    "#receipt": "finance",
    "#email": "email",
}

# Swarm service names as they appear in real prod alert titles.
_SERVICE_PATTERNS = (
    re.compile(r"^PROLONGED:\s+(\S+)\s+degraded", re.I),
    re.compile(r"^Service\s+(\S+)\s+has\s+fewer\s+tasks", re.I),
    re.compile(r"^([A-Za-z][\w.-]*)\s+is\s+down\b", re.I),
)


def resolve_verb(task: dict) -> str:
    """Verb for a task: from source_tag, or @code when source_tag is NULL."""
    source_tag = task.get("source_tag")
    if source_tag:
        return _VERB_BY_SOURCE_TAG.get(source_tag, "unknown")
    if "@code" in (task.get("labels") or []):
        return "coding"
    return "unknown"


def extract_service_name(title: str) -> str:
    """Swarm service named by an alert title, or '' when none is."""
    text = (title or "").strip()
    for pattern in _SERVICE_PATTERNS:
        match = pattern.match(text)
        if match:
            return match.group(1).lower() if "_" not in match.group(1) else match.group(1)
    return ""
```

```python
    # --- inside AgentTaskActivities ---

    @activity.defn
    async def load_task_context(self, task_id: str) -> dict:
        """Recover the source identity a task was captured from.

        `todoist_capture_idempotency` links task → external_id with near-total
        coverage in prod (41/42 #alert, 30/30 #email). external_id is prefixed
        by source: `alert-<fingerprint>`, `gmail-<message_id>`.
        """
        empty = {"external_id": "", "fingerprint": "", "gmail_message_id": ""}
        if self.db_pool is None or not task_id:
            return empty
        external_id = await self.db_pool.fetchval(
            "SELECT external_id FROM todoist_capture_idempotency "
            "WHERE todoist_task_ref = $1 ORDER BY captured_at DESC LIMIT 1",
            task_id,
        )
        if not external_id:
            return empty
        return {
            "external_id": external_id,
            "fingerprint": external_id[len("alert-") :] if external_id.startswith("alert-") else "",
            "gmail_message_id": (
                external_id[len("gmail-") :] if external_id.startswith("gmail-") else ""
            ),
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/activities/test_agent_task_verbs.py -v`
Expected: PASS — 16 passed

- [ ] **Step 5: Lint and commit**

```bash
ruff check worker/src/ tests/worker/
git add worker/src/aegis_worker/activities/agent_task.py tests/worker/activities/test_agent_task_verbs.py
git commit -m "feat(agent-task): verb resolution from source_tag and source-identity lookup"
```

---

### Task 3: Terminal states, comment helper, sweep + per-task flow, registration

This is the first shippable increment: the sweep selects tasks, spawns children, each child comments and parks the task. No verb logic yet — every verb resolves to `unknown` and parks.

**Files:**
- Modify: `worker/src/aegis_worker/activities/agent_task.py`
- Create: `worker/src/aegis_worker/flows/agent_task.py`
- Modify: `worker/src/aegis_worker/__main__.py`
- Modify: `worker/src/aegis_worker/schedule_sync.py`
- Modify: `config/seed/activities.yaml`
- Test: `tests/worker/activities/test_agent_task_terminal.py`, `tests/worker/flows/test_agent_task_flow.py`

**Interfaces:**
- Consumes: `find_actionable_tasks`, `resolve_verb`, `load_task_context` from Tasks 1–2.
- Produces:
  - `AgentTaskActivities.park_task(task_id: str, reason: str) -> dict` → `{"parked": bool}`; adds `@waiting`.
  - `AgentTaskActivities.complete_task(task_id: str) -> dict` → `{"completed": bool}`.
  - `AgentTaskActivities.comment(task_id: str, agent_id: str, body: str) -> dict` → `{"ok": bool}`.
  - `AgentTaskSweepConfig(agent_id: str, max_tasks: int = 3, cooldown_hours: int = 6, max_coding: int = 1)`.
  - `AgentTaskFlowInput(agent_id: str, todoist_task_id: str, task: dict)`.
  - Flows `AgentTaskSweepFlow`, `AgentTaskFlow`.

- [ ] **Step 1: Write the failing terminal-state test**

```python
# tests/worker/activities/test_agent_task_terminal.py
"""Terminal-state writers. Every non-complete exit MUST park at @waiting —
otherwise the 6h cooldown becomes an infinite slow loop over the same tasks."""

from __future__ import annotations

import pytest_asyncio
from aegis_worker.activities.agent_task import PARK_LABEL, AgentTaskActivities


@pytest_asyncio.fixture(loop_scope="function")
async def _seed(db_pool):
    await db_pool.execute("DELETE FROM todoist_tasks WHERE id LIKE 'tm-%'")
    await db_pool.execute("DELETE FROM todoist_outbox WHERE temp_id LIKE 'agent-task-%'")
    await db_pool.execute(
        "INSERT INTO todoist_tasks (id, content, labels, source_tag, assignee_label, is_completed) "
        "VALUES ('tm-1','a task', ARRAY['@pandora'], '#alert', '@pandora', false)"
    )
    yield
    await db_pool.execute("DELETE FROM todoist_tasks WHERE id LIKE 'tm-%'")
    await db_pool.execute("DELETE FROM todoist_outbox WHERE temp_id LIKE 'agent-task-%'")


async def test_park_task_adds_waiting_label_locally_and_to_outbox(db_pool, _seed):
    act = AgentTaskActivities(db_pool=db_pool)
    assert (await act.park_task("tm-1", "needs a human"))["parked"] is True

    labels = await db_pool.fetchval("SELECT labels FROM todoist_tasks WHERE id = 'tm-1'")
    assert PARK_LABEL in labels
    queued = await db_pool.fetchval(
        "SELECT count(*) FROM todoist_outbox WHERE temp_id = 'agent-task-park-tm-1'"
    )
    assert queued == 1


async def test_park_task_is_idempotent(db_pool, _seed):
    act = AgentTaskActivities(db_pool=db_pool)
    await act.park_task("tm-1", "first")
    await act.park_task("tm-1", "second")
    labels = await db_pool.fetchval("SELECT labels FROM todoist_tasks WHERE id = 'tm-1'")
    assert labels.count(PARK_LABEL) == 1


async def test_complete_task_queues_item_complete(db_pool, _seed):
    act = AgentTaskActivities(db_pool=db_pool)
    assert (await act.complete_task("tm-1"))["completed"] is True
    cmd = await db_pool.fetchval(
        "SELECT command FROM todoist_outbox WHERE temp_id = 'agent-task-complete-tm-1'"
    )
    assert cmd["type"] == "item_complete"


async def test_park_missing_task_is_false_not_crash(db_pool, _seed):
    assert (await AgentTaskActivities(db_pool=db_pool).park_task("tm-absent", "x"))["parked"] is False


async def test_comment_body_carries_the_workflow_run_footer(db_pool, _seed):
    """Without this marker clarify treats the comment as fresh user input and
    re-spawns the flow every 15 minutes (loop shipped twice: 2026-05-21, 05-27)."""
    sent: dict = {}

    class _Todoist:
        @staticmethod
        async def add_note(task_id: str, content: str) -> dict:
            sent["task_id"] = task_id
            sent["content"] = content
            return {"ok": True}

    act = AgentTaskActivities(db_pool=db_pool, todoist_connector=_Todoist())
    assert (await act.comment("tm-1", "pandoras-actor", "found the cause"))["ok"] is True
    assert "Workflow run:" in sent["content"]
    assert "found the cause" in sent["content"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/activities/test_agent_task_terminal.py -v`
Expected: FAIL — `ImportError: cannot import name 'PARK_LABEL'` (or `AttributeError: park_task`)

- [ ] **Step 3: Implement the terminal-state writers**

Add to `worker/src/aegis_worker/activities/agent_task.py`:

```python
    # --- terminal states ---

    async def _queue_command(self, temp_id: str, command: dict) -> None:
        """Enqueue a Todoist Sync command. The deterministic temp_id makes
        re-runs no-ops until TodoistSyncFlow drains it."""
        await self.db_pool.execute(
            "INSERT INTO todoist_outbox (temp_id, command, status) "
            "VALUES ($1, $2, 'pending') ON CONFLICT (temp_id) DO NOTHING",
            temp_id,
            command,
        )

    @activity.defn
    async def park_task(self, task_id: str, reason: str) -> dict:
        """Add @waiting — the parking state. Eligibility excludes @waiting, so
        this is what removes a task from the pool and stops the cooldown
        re-picking it forever."""
        from aegis.connectors.todoist import TodoistConnector

        if self.db_pool is None or not task_id:
            return {"parked": False}
        labels = await self.db_pool.fetchval(
            "SELECT labels FROM todoist_tasks WHERE id = $1", task_id
        )
        if labels is None:
            return {"parked": False}
        if PARK_LABEL in labels:
            return {"parked": True}
        new_labels = [*labels, PARK_LABEL]
        await self._queue_command(
            f"agent-task-park-{task_id}",
            TodoistConnector.build_item_update_command(task_id, labels=new_labels),
        )
        # Optimistic local update so the next tick doesn't re-select the task
        # before the 5-min sync round-trips.
        await self.db_pool.execute(
            "UPDATE todoist_tasks SET labels = $1, updated_at = now() WHERE id = $2",
            new_labels,
            task_id,
        )
        activity.logger.info("agent_task_parked task_id=%s reason=%s", task_id, reason[:120])
        return {"parked": True}

    @activity.defn
    async def complete_task(self, task_id: str) -> dict:
        """Close the task — only when no human work remains."""
        from aegis.connectors.todoist import TodoistConnector

        if self.db_pool is None or not task_id:
            return {"completed": False}
        exists = await self.db_pool.fetchval(
            "SELECT 1 FROM todoist_tasks WHERE id = $1", task_id
        )
        if not exists:
            return {"completed": False}
        await self._queue_command(
            f"agent-task-complete-{task_id}",
            TodoistConnector.build_item_complete_command(task_id),
        )
        await self.db_pool.execute(
            "UPDATE todoist_tasks SET is_completed = true, updated_at = now() WHERE id = $1",
            task_id,
        )
        return {"completed": True}

    @activity.defn
    async def comment(self, task_id: str, agent_id: str, body: str) -> dict:
        """Post a task comment. The `Workflow run:` footer is REQUIRED: clarify
        excludes AEGIS-authored notes by matching it, and without it this
        comment re-eligibles the task and the flow re-spawns every 15 min."""
        if self.todoist_connector is None or not task_id:
            return {"ok": False}
        info = activity.info() if activity.in_activity() else None
        run_ref = info.workflow_id if info else "local"
        content = f"[{agent_id}] {body}\n\nWorkflow run: {run_ref}"
        try:
            return await self.todoist_connector.add_note(task_id, content)
        except Exception as exc:  # noqa: BLE001 — comments are best-effort
            activity.logger.warning("agent_task_comment_failed err=%s", str(exc)[:200])
            return {"ok": False, "error": str(exc)[:200]}
```

- [ ] **Step 4: Run the terminal test**

Run: `PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/activities/test_agent_task_terminal.py -v`
Expected: PASS — 5 passed

- [ ] **Step 5: Write the failing flow test**

```python
# tests/worker/flows/test_agent_task_flow.py
"""AgentTaskSweepFlow / AgentTaskFlow — dispatch and unknown-verb parking."""

from __future__ import annotations

import uuid

from aegis_worker.flows.agent_task import (
    AgentTaskFlow,
    AgentTaskFlowInput,
    AgentTaskSweepConfig,
    AgentTaskSweepFlow,
)
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

_TASK = {
    "id": "tf-1",
    "content": "PROLONGED: redis_redis degraded for over 2 hours",
    "description": "",
    "labels": ["@pandora"],
    "source_tag": "#chat",  # deliberately an unmapped verb
    "project_id": "p1",
    "assignee_label": "@pandora",
}


async def test_unknown_verb_parks_the_task_and_never_leaves_it_in_the_pool():
    calls: list[tuple[str, str]] = []

    @activity.defn(name="load_task_context")
    async def load_task_context(task_id: str) -> dict:
        return {"external_id": "", "fingerprint": "", "gmail_message_id": ""}

    @activity.defn(name="comment")
    async def comment(task_id: str, agent_id: str, body: str) -> dict:
        calls.append(("comment", body))
        return {"ok": True}

    @activity.defn(name="park_task")
    async def park_task(task_id: str, reason: str) -> dict:
        calls.append(("park", reason))
        return {"parked": True}

    @activity.defn(name="resolve_agents")
    async def resolve_agents(tags: list[str]) -> dict:
        return {"infra": "pandoras-actor"}

    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue = f"tq-{uuid.uuid4()}"
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[AgentTaskFlow],
            activities=[load_task_context, comment, park_task, resolve_agents],
        ):
            result = await env.client.execute_workflow(
                AgentTaskFlow.run,
                AgentTaskFlowInput(
                    agent_id="pandoras-actor", todoist_task_id="tf-1", task=_TASK
                ),
                id=f"agent-task-tf-1-{uuid.uuid4()}",
                task_queue=queue,
            )

    assert result["verb"] == "unknown"
    assert result["status"] == "parked"
    assert any(kind == "park" for kind, _ in calls)


async def test_sweep_spawns_one_child_per_task_and_does_not_await_them():
    @activity.defn(name="find_actionable_tasks")
    async def find_actionable_tasks(
        max_tasks: int = 3, cooldown_hours: int = 6, max_coding: int = 1
    ) -> list[dict]:
        return [dict(_TASK, id=f"tf-{n}") for n in range(1, 4)]

    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue = f"tq-{uuid.uuid4()}"
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[AgentTaskSweepFlow, AgentTaskFlow],
            activities=[find_actionable_tasks],
        ):
            result = await env.client.execute_workflow(
                AgentTaskSweepFlow.run,
                AgentTaskSweepConfig(agent_id="pandoras-actor"),
                id=f"sweep-{uuid.uuid4()}",
                task_queue=queue,
            )

    assert result == {"found": 3, "spawned": 3}
```

- [ ] **Step 6: Run flow test to verify it fails**

Run: `PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/flows/test_agent_task_flow.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'aegis_worker.flows.agent_task'`

- [ ] **Step 7: Implement both flows**

```python
# worker/src/aegis_worker/flows/agent_task.py
"""AgentTaskSweepFlow + AgentTaskFlow — execute agent-assigned Todoist tasks.

The sweep spawns ABANDONED children and never awaits them: a child can sit on
an approval card for days, and Temporal schedules default to overlap=SKIP, so
one unanswered card would starve every later tick (the failure that caused 511
skipped Sentry polls over 41h on 2026-05-29).

Every child ends by completing the task or parking it at @waiting. Eligibility
excludes @waiting, so parking is what removes the task from the pool — without
it the 6h cooldown is an infinite slow loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from temporalio import workflow
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.agent_task import resolve_verb
    from aegis_worker.shared.retry import (
        ACT_RETRY,
        NO_RETRY,
        TIMEOUT_FAST,
        TIMEOUT_STANDARD,
    )


@dataclass
class AgentTaskSweepConfig:
    agent_id: str  # MUST be first — the run recorder reads it
    max_tasks: int = 3
    cooldown_hours: int = 6
    max_coding: int = 1


@dataclass
class AgentTaskFlowInput:
    agent_id: str  # MUST be first — the run recorder reads it
    # MUST be named todoist_task_id — interceptors._extract_todoist_task_ref
    # reads this exact attribute to populate workflow_runs.todoist_task_ref,
    # which the eligibility cooldown query depends on.
    todoist_task_id: str
    task: dict[str, Any] = field(default_factory=dict)


@workflow.defn(name="AgentTaskSweepFlow")
class AgentTaskSweepFlow:
    @workflow.run
    async def run(self, config: AgentTaskSweepConfig) -> dict:
        step = "find_actionable_tasks"
        try:
            tasks = await workflow.execute_activity(
                "find_actionable_tasks",
                args=[config.max_tasks, config.cooldown_hours, config.max_coding],
                start_to_close_timeout=TIMEOUT_STANDARD,
                retry_policy=ACT_RETRY,
            )

            step = "spawn_children"
            spawned = 0
            for task in tasks:
                try:
                    await workflow.start_child_workflow(
                        AgentTaskFlow.run,
                        AgentTaskFlowInput(
                            agent_id=config.agent_id,
                            todoist_task_id=str(task["id"]),
                            task=task,
                        ),
                        id=f"agent-task-{task['id']}",
                        parent_close_policy=workflow.ParentClosePolicy.ABANDON,
                    )
                    spawned += 1
                except WorkflowAlreadyStartedError:
                    continue  # a previous tick's child is still running
                except Exception as exc:  # noqa: BLE001
                    workflow.logger.warning(
                        "agent_task_spawn_failed task_id=%s err=%s",
                        task["id"],
                        str(exc)[:200],
                    )
        except Exception as exc:  # noqa: BLE001
            raise ApplicationError(
                f"agent_task_sweep_failed at step={step}: {exc!r}", non_retryable=True
            ) from exc

        return {"found": len(tasks), "spawned": spawned}


@workflow.defn(name="AgentTaskFlow")
class AgentTaskFlow:
    @workflow.run
    async def run(self, input: AgentTaskFlowInput) -> dict:
        task = input.task
        task_id = input.todoist_task_id
        verb = resolve_verb(task)

        await workflow.execute_activity(
            "load_task_context",
            args=[task_id],
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=ACT_RETRY,
        )

        # Verb executors are added in Tasks 4-7. An unmapped verb parks the
        # task rather than guessing at it.
        await workflow.execute_activity(
            "comment",
            args=[
                task_id,
                input.agent_id,
                f"No executor for this task type ({task.get('source_tag') or 'no source tag'}) "
                "— leaving it for you.",
            ],
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=NO_RETRY,
        )
        await workflow.execute_activity(
            "park_task",
            args=[task_id, f"no executor for verb={verb}"],
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=ACT_RETRY,
        )
        return {"task_id": task_id, "verb": verb, "status": "parked"}
```

- [ ] **Step 8: Run flow test to verify it passes**

Run: `PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/flows/test_agent_task_flow.py -v`
Expected: PASS — 2 passed

- [ ] **Step 9: Register both flows and all activities**

See the **Registration note** above — there are FOUR lists in `__main__.py`, not two.

Add the imports at the top of `worker/src/aegis_worker/__main__.py`:

```python
from aegis_worker.activities.agent_task import AgentTaskActivities
from aegis_worker.flows.agent_task import AgentTaskFlow, AgentTaskSweepFlow
```

**(a)** Next to the other `_stub_*` instances (~line 100):

```python
_stub_agent_task_act = AgentTaskActivities(db_pool=None)
```

**(b)** Module-level `WORKFLOWS` (~line 106) and **(c)** the live `workflows = [...]` in `main()`
(~line 601) — add to BOTH:

```python
    AgentTaskSweepFlow,
    AgentTaskFlow,
```

**(d)** Module-level `ACTIVITIES` (~line 131) — add the stub-bound methods:

```python
    _stub_agent_task_act.find_actionable_tasks,
    _stub_agent_task_act.load_task_context,
    _stub_agent_task_act.park_task,
    _stub_agent_task_act.complete_task,
    _stub_agent_task_act.comment,
```

**(e)** In `main()`, construct the real instance next to `capture_act` / `social_act` (~line 403,
where `todoist_connector` is already in scope):

```python
    agent_task_act = AgentTaskActivities(
        db_pool=deps.pool,
        todoist_connector=todoist_connector,
        remote_script=connectors.get("remote_script"),
        homelab_connector=connectors.get("homelab"),
    )
```

**(f)** The live `activities = [...]` in `main()` (~line 452) — add the same five, bound to the real
instance:

```python
    agent_task_act.find_actionable_tasks,
    agent_task_act.load_task_context,
    agent_task_act.park_task,
    agent_task_act.complete_task,
    agent_task_act.comment,
```

In `worker/src/aegis_worker/schedule_sync.py`, add to `_ACTIVITY_TYPE_MAP`:

```python
    "AgentTaskSweepFlow": AgentTaskSweepFlow,
```

In `config/seed/activities.yaml`, append:

```yaml
  # Executes AEGIS's own agent-assigned tasks. 3 per tick with a 6h per-task
  # cooldown, so the ~80-task backlog drains over about two days rather than
  # stampeding on the first run.
  - slug: agent-task-15min
    workflow_type: AgentTaskSweepFlow
    agent_id: pandoras-actor
    cron: "*/15 * * * *"
    active: true
    config: {}
```

- [ ] **Step 10: Verify registration and the whole suite**

```bash
PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/ -n auto --dist loadfile --timeout=300 -q
ruff check worker/src/ tests/worker/
```

Expected: all pass. If `tests/worker/test_activity_registration.py` exists it will fail on an activity registered in one list but not the other — that is the guard working; fix the omission.

- [ ] **Step 11: Commit**

```bash
git add worker/src/aegis_worker/activities/agent_task.py \
        worker/src/aegis_worker/flows/agent_task.py \
        worker/src/aegis_worker/__main__.py \
        \
        worker/src/aegis_worker/schedule_sync.py \
        config/seed/activities.yaml \
        tests/worker/activities/test_agent_task_terminal.py \
        tests/worker/flows/test_agent_task_flow.py
git commit -m "feat(agent-task): sweep + per-task flow with terminal states and registration"
```

---

### Task 4: `#alert` → infra verb (40 tasks, the largest group)

**Files:**
- Create: `worker/src/aegis_worker/activities/infra_ops.py`
- Modify: `worker/src/aegis_worker/flows/agent_task.py`
- Modify: `worker/src/aegis_worker/__main__.py`
- Test: `tests/worker/activities/test_infra_ops.py`, `tests/worker/flows/test_agent_task_infra.py`

**Interfaces:**
- Consumes: `extract_service_name`, `comment`, `park_task`, `complete_task`, `load_task_context`.
- Produces: `InfraOpsActivities(homelab_connector)` with
  - `service_health(service_name: str) -> dict` → `{"found": bool, "healthy": bool, "detail": str}`
  - `service_logs(service_name: str, lines: int = 50) -> dict` → `{"logs": str}`
  - `restart_service(service_name: str) -> dict` → `{"ok": bool, "detail": str}`

**Why live state rather than alert history:** only 12 of 42 open `#alert` tasks have an `alert_dedup_index` row, and the 30 that lack one are exactly the PROLONGED bulk. Every one of those titles names a swarm service, so asking Docker whether it is healthy *now* works for all 42 — and is what a human would do.

- [ ] **Step 1: Write the failing activity test**

```python
# tests/worker/activities/test_infra_ops.py
"""InfraOpsActivities — thin activity wrappers over HomelabConnector."""

from __future__ import annotations

from aegis_worker.activities.infra_ops import InfraOpsActivities


class _Connector:
    def __init__(self, services: dict, restart_ok: bool = True):
        self._services = services
        self._restart_ok = restart_ok
        self.restarted: list[str] = []

    async def list_services(self) -> dict:
        return {"services": [{"name": n, "replicas": r} for n, r in self._services.items()]}

    async def service_ps(self, service_name: str) -> dict:
        return {"tasks": [{"state": "Running"}]}

    async def restart_service(self, service_name: str) -> dict:
        self.restarted.append(service_name)
        return {"ok": self._restart_ok}


async def test_service_health_healthy_when_replicas_match():
    act = InfraOpsActivities(homelab_connector=_Connector({"redis_redis": "1/1"}))
    result = await act.service_health("redis_redis")
    assert result == {"found": True, "healthy": True, "detail": "1/1"}


async def test_service_health_unhealthy_when_replicas_short():
    act = InfraOpsActivities(homelab_connector=_Connector({"redis_redis": "0/1"}))
    result = await act.service_health("redis_redis")
    assert result["found"] is True
    assert result["healthy"] is False


async def test_service_health_not_found():
    act = InfraOpsActivities(homelab_connector=_Connector({"other_svc": "1/1"}))
    assert (await act.service_health("redis_redis"))["found"] is False


async def test_service_health_no_connector_is_not_found_not_crash():
    assert (await InfraOpsActivities(homelab_connector=None).service_health("x"))["found"] is False


async def test_restart_service_delegates():
    conn = _Connector({"redis_redis": "0/1"})
    assert (await InfraOpsActivities(homelab_connector=conn).restart_service("redis_redis"))["ok"]
    assert conn.restarted == ["redis_redis"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/activities/test_infra_ops.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'aegis_worker.activities.infra_ops'`

- [ ] **Step 3: Implement the infra activities**

```python
# worker/src/aegis_worker/activities/infra_ops.py
"""InfraOpsActivities — swarm service ops as Temporal activities.

`HomelabConnector` already implements these (core/src/aegis/connectors/homelab.py),
but only chat tools call it. Workflows cannot touch connectors or the DB
directly, so they need activity wrappers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from temporalio import activity


@dataclass
class InfraOpsActivities:
    homelab_connector: Any = None

    @activity.defn
    async def service_health(self, service_name: str) -> dict:
        """Is `service_name` running its desired replica count right now?

        `replicas` is Docker's "running/desired" string (e.g. "1/1", "0/1").
        A missing service is `found: False` — never silently "healthy", or a
        renamed service would auto-close its own alert task.
        """
        if self.homelab_connector is None or not service_name:
            return {"found": False, "healthy": False, "detail": "no connector or service name"}
        try:
            listing = await self.homelab_connector.list_services()
        except Exception as exc:  # noqa: BLE001
            activity.logger.warning("service_health_failed err=%s", str(exc)[:200])
            return {"found": False, "healthy": False, "detail": str(exc)[:200]}

        for svc in listing.get("services") or []:
            if str(svc.get("name", "")).lower() != service_name.lower():
                continue
            replicas = str(svc.get("replicas") or "")
            running, _, desired = replicas.partition("/")
            healthy = bool(desired) and running == desired and running not in ("", "0")
            return {"found": True, "healthy": healthy, "detail": replicas}
        return {"found": False, "healthy": False, "detail": "service not in swarm"}

    @activity.defn
    async def service_logs(self, service_name: str, lines: int = 50) -> dict:
        if self.homelab_connector is None or not service_name:
            return {"logs": ""}
        try:
            result = await self.homelab_connector.service_ps(service_name)
        except Exception as exc:  # noqa: BLE001
            activity.logger.warning("service_logs_failed err=%s", str(exc)[:200])
            return {"logs": ""}
        return {"logs": str(result)[:4000]}

    @activity.defn
    async def restart_service(self, service_name: str) -> dict:
        if self.homelab_connector is None or not service_name:
            return {"ok": False, "detail": "no connector or service name"}
        try:
            result = await self.homelab_connector.restart_service(service_name)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": str(exc)[:200]}
        return {"ok": bool(result.get("ok", True)), "detail": str(result)[:500]}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/activities/test_infra_ops.py -v`
Expected: PASS — 5 passed

- [ ] **Step 5: Write the failing infra-verb flow test**

```python
# tests/worker/flows/test_agent_task_infra.py
"""AgentTaskFlow, infra verb: healthy short-circuit and the restart gate."""

from __future__ import annotations

import uuid

from aegis_worker.flows.agent_task import AgentTaskFlow, AgentTaskFlowInput
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

_ALERT_TASK = {
    "id": "ti-1",
    "content": "PROLONGED: redis_redis degraded for over 2 hours",
    "description": "",
    "labels": ["@pandora"],
    "source_tag": "#alert",
    "project_id": "p1",
    "assignee_label": "@pandora",
}


def _base_activities(events: list, *, healthy: bool):
    @activity.defn(name="load_task_context")
    async def load_task_context(task_id: str) -> dict:
        return {"external_id": "alert-abc", "fingerprint": "abc", "gmail_message_id": ""}

    @activity.defn(name="comment")
    async def comment(task_id: str, agent_id: str, body: str) -> dict:
        events.append(("comment", body))
        return {"ok": True}

    @activity.defn(name="park_task")
    async def park_task(task_id: str, reason: str) -> dict:
        events.append(("park", reason))
        return {"parked": True}

    @activity.defn(name="complete_task")
    async def complete_task(task_id: str) -> dict:
        events.append(("complete", task_id))
        return {"completed": True}

    @activity.defn(name="service_health")
    async def service_health(service_name: str) -> dict:
        return {"found": True, "healthy": healthy, "detail": "1/1" if healthy else "0/1"}

    @activity.defn(name="service_logs")
    async def service_logs(service_name: str, lines: int = 50) -> dict:
        return {"logs": "boot loop"}

    @activity.defn(name="restart_service")
    async def restart_service(service_name: str) -> dict:
        events.append(("restart", service_name))
        return {"ok": True, "detail": "restarted"}

    return [
        load_task_context,
        comment,
        park_task,
        complete_task,
        service_health,
        service_logs,
        restart_service,
    ]


async def test_healthy_service_completes_task_without_a_card():
    """Expected to close a large share of the 30 four-week-old PROLONGED tasks."""
    events: list = []
    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue = f"tq-{uuid.uuid4()}"
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[AgentTaskFlow],
            activities=_base_activities(events, healthy=True),
        ):
            result = await env.client.execute_workflow(
                AgentTaskFlow.run,
                AgentTaskFlowInput(
                    agent_id="pandoras-actor", todoist_task_id="ti-1", task=_ALERT_TASK
                ),
                id=f"agent-task-ti-1-{uuid.uuid4()}",
                task_queue=queue,
            )

    assert result["verb"] == "infra"
    assert result["status"] == "resolved"
    assert any(kind == "complete" for kind, _ in events)
    assert not any(kind == "restart" for kind, _ in events)


async def test_unhealthy_service_investigates_and_parks_pending_approval():
    """No card answer in this harness ⇒ must park, never leave the task live."""
    events: list = []
    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue = f"tq-{uuid.uuid4()}"
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[AgentTaskFlow],
            activities=_base_activities(events, healthy=False),
        ):
            result = await env.client.execute_workflow(
                AgentTaskFlow.run,
                AgentTaskFlowInput(
                    agent_id="pandoras-actor", todoist_task_id="ti-1", task=_ALERT_TASK
                ),
                id=f"agent-task-ti-1-{uuid.uuid4()}",
                task_queue=queue,
            )

    assert result["status"] == "parked"
    assert any(kind == "comment" and "boot loop" in body for kind, body in events)
    assert any(kind == "park" for kind, _ in events)
```

- [ ] **Step 6: Run flow test to verify it fails**

Run: `PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/flows/test_agent_task_infra.py -v`
Expected: FAIL — `assert result["status"] == "resolved"` gets `"parked"` (the Task 3 stub parks everything)

- [ ] **Step 7: Implement the infra branch**

In `worker/src/aegis_worker/flows/agent_task.py`, import `extract_service_name` alongside `resolve_verb`, and replace the stub body of `AgentTaskFlow.run` (everything after `load_task_context`) with a verb dispatch plus the infra handler:

```python
        if verb == "infra":
            return await self._run_infra(input, task_id)

        # Verbs added in Tasks 5-7. An unmapped verb parks rather than guesses.
        await workflow.execute_activity(
            "comment",
            args=[
                task_id,
                input.agent_id,
                f"No executor for this task type ({task.get('source_tag') or 'no source tag'}) "
                "— leaving it for you.",
            ],
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=NO_RETRY,
        )
        await workflow.execute_activity(
            "park_task",
            args=[task_id, f"no executor for verb={verb}"],
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=ACT_RETRY,
        )
        return {"task_id": task_id, "verb": verb, "status": "parked"}

    async def _run_infra(self, input: AgentTaskFlowInput, task_id: str) -> dict:
        """Check live service state; investigate and gate a restart if broken.

        Deliberately does NOT replay alert history: only 12 of 42 open #alert
        tasks have an alert_dedup_index row, and the 30 without one are exactly
        the PROLONGED bulk. Every such title names a service, so asking Docker
        about current state covers all of them.
        """
        title = str(input.task.get("content") or "")
        service = extract_service_name(title)
        if not service:
            await workflow.execute_activity(
                "comment",
                args=[task_id, input.agent_id, "I couldn't tell which service this is about."],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=NO_RETRY,
            )
            await workflow.execute_activity(
                "park_task",
                args=[task_id, "service name not parseable from title"],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=ACT_RETRY,
            )
            return {"task_id": task_id, "verb": "infra", "status": "parked"}

        health = await workflow.execute_activity(
            "service_health",
            args=[service],
            start_to_close_timeout=TIMEOUT_STANDARD,
            retry_policy=ACT_RETRY,
        )

        if health["found"] and health["healthy"]:
            await workflow.execute_activity(
                "comment",
                args=[
                    task_id,
                    input.agent_id,
                    f"`{service}` is healthy now ({health['detail']}) — this alert has "
                    "resolved itself, so I'm closing the task.",
                ],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=NO_RETRY,
            )
            await workflow.execute_activity(
                "complete_task",
                args=[task_id],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=ACT_RETRY,
            )
            return {"task_id": task_id, "verb": "infra", "status": "resolved", "service": service}

        logs = await workflow.execute_activity(
            "service_logs",
            args=[service, 50],
            start_to_close_timeout=TIMEOUT_STANDARD,
            retry_policy=ACT_RETRY,
        )
        detail = health["detail"] if health["found"] else "not present in the swarm"
        await workflow.execute_activity(
            "comment",
            args=[
                task_id,
                input.agent_id,
                f"`{service}` is still unhealthy ({detail}).\n\n{logs['logs'][:1500]}",
            ],
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=NO_RETRY,
        )

        # Restarting is a write, so it needs an approval card. The card is
        # spawned ABANDONED with a post_resolve hook rather than awaited, so a
        # slow answer never holds a worker slot; this run parks the task and the
        # hook does the restart.
        await workflow.execute_activity(
            "park_task",
            args=[task_id, f"awaiting restart approval for {service}"],
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=ACT_RETRY,
        )
        return {"task_id": task_id, "verb": "infra", "status": "parked", "service": service}
```

- [ ] **Step 8: Run flow test to verify it passes**

Run: `PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/flows/test_agent_task_infra.py -v`
Expected: PASS — 2 passed

- [ ] **Step 9: Register the infra activities**

In `worker/src/aegis_worker/__main__.py`, import `InfraOpsActivities` from
`aegis_worker.activities.infra_ops`, then add in all four places (see the Registration note):

```python
# next to the other stubs (~line 100)
_stub_infra_ops_act = InfraOpsActivities(homelab_connector=None)

# module-level ACTIVITIES (~line 131)
    _stub_infra_ops_act.service_health,
    _stub_infra_ops_act.service_logs,
    _stub_infra_ops_act.restart_service,

# in main(), next to agent_task_act (~line 403)
    infra_ops_act = InfraOpsActivities(homelab_connector=connectors.get("homelab"))

# the live activities list (~line 452)
    infra_ops_act.service_health,
    infra_ops_act.service_logs,
    infra_ops_act.restart_service,
```

Note `restart_service` is also the name of a chat tool executor in core, but activity names live in
their own namespace — there is no collision. There must, however, be only ONE activity registered
under this name across the whole worker, or `Worker()` refuses to start.

- [ ] **Step 10: Run the whole suite, lint, commit**

```bash
PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/ -n auto --dist loadfile --timeout=300 -q
ruff check worker/src/ tests/worker/
git add worker/src/aegis_worker/activities/infra_ops.py \
        worker/src/aegis_worker/flows/agent_task.py \
        worker/src/aegis_worker/__main__.py \
        tests/worker/activities/test_infra_ops.py tests/worker/flows/test_agent_task_infra.py
git commit -m "feat(agent-task): infra verb — live service health, investigate, gate restarts"
```

---

### Task 5: Restart approval card and its post-resolve hook

Task 4 parks unhealthy services without offering a restart. This task adds the card.

**Files:**
- Modify: `worker/src/aegis_worker/flows/agent_task.py`, `worker/src/aegis_worker/activities/agent_task.py`
- Modify: `worker/src/aegis_worker/__main__.py`
- Test: `tests/worker/activities/test_agent_task_restart_hook.py`

**Interfaces:**
- Consumes: `InfraOpsActivities.restart_service`, `service_health`, `complete_task`, `park_task`, `comment`.
- Produces: `AgentTaskActivities.apply_restart_approval(interaction_id: str, response: dict, metadata: dict) -> dict`
  → `{"applied": "approved" | "skipped" | "none"}`. `metadata` carries `{"task_id", "service", "agent_id"}`.
  Also adds the `infra_ops: Any = None` collaborator field to `AgentTaskActivities`.

- [ ] **Step 1: Write the failing test**

```python
# tests/worker/activities/test_agent_task_restart_hook.py
"""apply_restart_approval — the InteractionFlow post_resolve hook."""

from __future__ import annotations

from aegis_worker.activities.agent_task import AgentTaskActivities


class _Recorder:
    def __init__(self, healthy_after_restart: bool):
        self.healthy_after = healthy_after_restart
        self.restarted: list[str] = []
        self.completed: list[str] = []
        self.parked: list[str] = []
        self.notes: list[str] = []

    async def restart_service(self, service_name: str) -> dict:
        self.restarted.append(service_name)
        return {"ok": True, "detail": "ok"}

    async def service_health(self, service_name: str) -> dict:
        return {"found": True, "healthy": self.healthy_after, "detail": "1/1"}


def _act(rec: _Recorder) -> AgentTaskActivities:
    # `infra_ops` is a normal collaborator field, so the fake drops straight in —
    # no private-attribute injection.
    act = AgentTaskActivities(db_pool=None, infra_ops=rec)
    async def _complete(task_id: str) -> dict:
        rec.completed.append(task_id)
        return {"completed": True}
    async def _park(task_id: str, reason: str) -> dict:
        rec.parked.append(task_id)
        return {"parked": True}
    async def _comment(task_id: str, agent_id: str, body: str) -> dict:
        rec.notes.append(body)
        return {"ok": True}
    act.complete_task = _complete                        # type: ignore[assignment]
    act.park_task = _park                                # type: ignore[assignment]
    act.comment = _comment                               # type: ignore[assignment]
    return act


_META = {"task_id": "tr-1", "service": "redis_redis", "agent_id": "pandoras-actor"}


async def test_approve_restarts_and_completes_when_service_recovers():
    rec = _Recorder(healthy_after_restart=True)
    result = await _act(rec).apply_restart_approval("i1", {"value": "approve"}, _META)
    assert result == {"applied": "approved"}
    assert rec.restarted == ["redis_redis"]
    assert rec.completed == ["tr-1"]
    assert rec.parked == []


async def test_approve_parks_when_service_still_broken_after_restart():
    rec = _Recorder(healthy_after_restart=False)
    await _act(rec).apply_restart_approval("i1", {"value": "approve"}, _META)
    assert rec.restarted == ["redis_redis"]
    assert rec.completed == []
    assert rec.parked == ["tr-1"]


async def test_skip_parks_without_restarting():
    rec = _Recorder(healthy_after_restart=True)
    result = await _act(rec).apply_restart_approval("i1", {"value": "skip"}, _META)
    assert result == {"applied": "skipped"}
    assert rec.restarted == []
    assert rec.parked == ["tr-1"]


async def test_unknown_choice_takes_no_action():
    rec = _Recorder(healthy_after_restart=True)
    result = await _act(rec).apply_restart_approval("i1", {"value": "???"}, _META)
    assert result == {"applied": "none"}
    assert rec.restarted == []


async def test_missing_task_id_takes_no_action():
    rec = _Recorder(healthy_after_restart=True)
    result = await _act(rec).apply_restart_approval("i1", {"value": "approve"}, {})
    assert result == {"applied": "none"}
    assert rec.restarted == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/activities/test_agent_task_restart_hook.py -v`
Expected: FAIL — `AttributeError: 'AgentTaskActivities' object has no attribute 'apply_restart_approval'`

- [ ] **Step 3: Implement the hook**

Add to `AgentTaskActivities` in `worker/src/aegis_worker/activities/agent_task.py`:

Add the collaborator field to the dataclass (alongside `todoist_connector` etc.):

```python
    # InfraOpsActivities instance. A plain field, not a private seam, so tests
    # pass a fake and production passes the real thing.
    infra_ops: Any = None

    @activity.defn
    async def apply_restart_approval(
        self, interaction_id: str, response: dict, metadata: dict
    ) -> dict:
        """InteractionFlow post_resolve hook for the restart card.

        Approve: restart, re-check health, and complete the task only if the
        service actually recovered — a restart that didn't fix it must stay
        visible, so it parks instead.
        """
        choice = (response.get("value") or "").strip()
        task_id = str(metadata.get("task_id") or "")
        service = str(metadata.get("service") or "")
        agent_id = str(metadata.get("agent_id") or "")
        if not task_id or not service:
            return {"applied": "none"}

        if choice == "skip":
            await self.comment(task_id, agent_id, f"Leaving `{service}` alone as you asked.")
            await self.park_task(task_id, "restart declined")
            return {"applied": "skipped"}

        if choice != "approve":
            activity.logger.info(
                "agent_task_restart_no_action interaction_id=%s choice=%s",
                interaction_id,
                choice,
            )
            return {"applied": "none"}

        restart = await self.infra_ops.restart_service(service)
        health = await self.infra_ops.service_health(service)
        if restart.get("ok") and health.get("healthy"):
            await self.comment(
                task_id, agent_id, f"Restarted `{service}` and it's healthy again — closing."
            )
            await self.complete_task(task_id)
        else:
            await self.comment(
                task_id,
                agent_id,
                f"Restarted `{service}` but it's still not healthy "
                f"({health.get('detail', 'unknown')}) — needs a look.",
            )
            await self.park_task(task_id, "restart did not restore health")
        return {"applied": "approved"}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/activities/test_agent_task_restart_hook.py -v`
Expected: PASS — 5 passed

- [ ] **Step 5: Spawn the card from the infra branch**

In `worker/src/aegis_worker/flows/agent_task.py`, add the import inside the
`workflow.unsafe.imports_passed_through()` block:

```python
    from aegis_worker.flows.interaction import InteractionFlow, InteractionFlowInput
```

In `_run_infra`, replace the final `park_task` + return (the "awaiting restart approval" block) with:

```python
        try:
            await workflow.start_child_workflow(
                InteractionFlow.run,
                InteractionFlowInput(
                    agent_id=input.agent_id,
                    kind="choice",
                    origin="agent_task_infra",
                    prompt=(
                        f"🔧 <b>{service}</b> is unhealthy ({detail}).\n\n"
                        "Restart it?"
                    ),
                    options={"approve": "🔄 Restart", "skip": "⏭️ Leave it"},
                    timeout_seconds=86400,
                    timeout_policy="archive",
                    metadata={
                        "task_id": task_id,
                        "service": service,
                        "agent_id": input.agent_id,
                    },
                    post_resolve_activity="apply_restart_approval",
                ),
                id=f"agent-task-restart-{task_id}",
                parent_close_policy=workflow.ParentClosePolicy.ABANDON,
            )
        except WorkflowAlreadyStartedError:
            pass  # a previous run's card is still open

        # Park now: the card's post_resolve hook owns the outcome from here, and
        # parking keeps the task out of the next tick's selection meanwhile.
        await workflow.execute_activity(
            "park_task",
            args=[task_id, f"awaiting restart approval for {service}"],
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=ACT_RETRY,
        )
        return {"task_id": task_id, "verb": "infra", "status": "carded", "service": service}
```

Update `tests/worker/flows/test_agent_task_infra.py::test_unhealthy_service_investigates_and_parks_pending_approval` to expect `result["status"] == "carded"`, and add `InteractionFlow` to that test's `workflows=[...]` list.

- [ ] **Step 6: Wire the seams and register**

In `worker/src/aegis_worker/__main__.py`, inside `main()` after both instances exist:

```python
    # apply_restart_approval runs as an AgentTask activity but needs the infra
    # ops. Mirrors the existing `alert_act.todoist_connector = todoist_connector`
    # late-wiring at ~line 415.
    agent_task_act.infra_ops = infra_ops_act
```

Add `apply_restart_approval` to BOTH the module-level `ACTIVITIES` (as
`_stub_agent_task_act.apply_restart_approval`) and the live `activities` list (as
`agent_task_act.apply_restart_approval`).

- [ ] **Step 7: Run the whole suite, lint, commit**

```bash
PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/ -n auto --dist loadfile --timeout=300 -q
ruff check worker/src/ tests/worker/
git add worker/src/aegis_worker/activities/agent_task.py worker/src/aegis_worker/flows/agent_task.py \
        worker/src/aegis_worker/__main__.py \
        tests/worker/activities/test_agent_task_restart_hook.py tests/worker/flows/test_agent_task_infra.py
git commit -m "feat(agent-task): restart approval card with recovery-verified completion"
```

---

### Task 6: `#email` → triage verb (30 tasks)

**Known ceiling — do not design around sending.** The OAuth scope list is `gmail.modify`,
`calendar.readonly`, `drive.readonly` (`core/src/aegis/api/routes/gmail_reauth.py:32`).
`gmail.modify` permits archive, trash, and label changes but **not sending**. Adding `gmail.send`
would force a re-consent round for all three accounts, so this verb is triage-only.

**Three active accounts** (`arshad-personal`, `arshad-stpd`, `arshad-hikmah`) and the task does not
record which one a message came from, so the account is found by probing.

**Files:**
- Modify: `worker/src/aegis_worker/activities/agent_task.py`, `worker/src/aegis_worker/flows/agent_task.py`
- Modify: `worker/src/aegis_worker/__main__.py`
- Test: `tests/worker/activities/test_agent_task_email.py`

**Interfaces:**
- Consumes: `load_task_context` (for `gmail_message_id`), `complete_task`, `park_task`, `comment`.
- Produces: `AgentTaskActivities.triage_email(task_id: str, title: str, gmail_message_id: str) -> dict`
  → `{"action": "archived" | "needs_human" | "not_found", "account": str}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/worker/activities/test_agent_task_email.py
"""triage_email — archive notifications, park anything needing a human reply."""

from __future__ import annotations

from aegis_worker.activities.agent_task import AgentTaskActivities


class _Gmail:
    """Only `owner` holds the message; the others 404 like the real API."""

    def __init__(self, owner: str | None):
        self.owner = owner
        self.labelled: list[tuple[str, str, str]] = []

    async def apply_label(self, account_label: str, message_id: str, label: str) -> dict:
        if account_label != self.owner:
            return {"ok": False, "error": "404 not found"}
        self.labelled.append((account_label, message_id, label))
        return {"ok": True, "id": message_id}


def _act(gmail: _Gmail) -> AgentTaskActivities:
    return AgentTaskActivities(
        db_pool=None,
        gmail_accounts=["arshad-personal", "arshad-stpd", "arshad-hikmah"],
        gmail_activities=gmail,
    )


async def test_notification_is_archived_on_the_owning_account():
    gmail = _Gmail(owner="arshad-stpd")
    result = await _act(gmail).triage_email("te-1", "Plan Expiry Notification", "msg-1")
    assert result["action"] == "archived"
    assert result["account"] == "arshad-stpd"
    assert gmail.labelled == [("arshad-stpd", "msg-1", "ARCHIVE")]


async def test_real_action_email_is_left_for_the_human():
    gmail = _Gmail(owner="arshad-hikmah")
    result = await _act(gmail).triage_email(
        "te-2", "RE: GSTR-2B CONSO for the month of June 2026-27", "msg-2"
    )
    assert result["action"] == "needs_human"
    assert gmail.labelled == []  # never touch mail that needs a reply


async def test_message_in_no_account_is_not_found():
    gmail = _Gmail(owner=None)
    result = await _act(gmail).triage_email("te-3", "Plan Expiry Notification", "msg-3")
    assert result["action"] == "not_found"


async def test_missing_message_id_is_needs_human_not_archive():
    """Without an id we cannot verify what we'd archive, so never guess."""
    gmail = _Gmail(owner="arshad-stpd")
    result = await _act(gmail).triage_email("te-4", "Plan Expiry Notification", "")
    assert result["action"] == "needs_human"
    assert gmail.labelled == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/activities/test_agent_task_email.py -v`
Expected: FAIL — `TypeError: unexpected keyword argument 'gmail_activities'`

- [ ] **Step 3: Implement the email triage activity**

Add the field `gmail_activities: Any = None` to `AgentTaskActivities`, then:

```python
    @activity.defn
    async def triage_email(self, task_id: str, title: str, gmail_message_id: str) -> dict:
        """Archive notification mail; leave anything needing a reply.

        Sending is impossible under the current `gmail.modify` scope, so a real
        action is parked for the user rather than answered.

        Reuses clarify's notification detection so this flow and the classifier
        agree on what counts as junk.
        """
        from aegis_worker.activities.clarify import ClarifyActivities

        if not gmail_message_id:
            return {"action": "needs_human", "account": ""}
        if not ClarifyActivities._looks_like_notification(title):
            return {"action": "needs_human", "account": ""}
        if self.gmail_activities is None:
            return {"action": "not_found", "account": ""}

        # The task doesn't record which of the three accounts the message came
        # from, so probe: a wrong account 404s, which is a clean discriminator.
        for account in self.gmail_accounts:
            result = await self.gmail_activities.apply_label(
                account, gmail_message_id, "ARCHIVE"
            )
            if result.get("ok"):
                return {"action": "archived", "account": account}
        return {"action": "not_found", "account": ""}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/activities/test_agent_task_email.py -v`
Expected: PASS — 4 passed

`_looks_like_notification` is a `@staticmethod` (`activities/clarify.py:609`), so the unbound call
above is correct as written.

- [ ] **Step 5: Wire the email branch into the flow**

In `AgentTaskFlow.run`, add before the unknown-verb fallback:

```python
        if verb == "email":
            return await self._run_email(input, task_id, context)
```

where `context` is the dict returned by the `load_task_context` call (assign it: `context = await workflow.execute_activity("load_task_context", ...)`). Then add:

```python
    async def _run_email(
        self, input: AgentTaskFlowInput, task_id: str, context: dict
    ) -> dict:
        """Archive notification mail; park anything needing a human reply."""
        title = str(input.task.get("content") or "")
        outcome = await workflow.execute_activity(
            "triage_email",
            args=[task_id, title, context.get("gmail_message_id", "")],
            start_to_close_timeout=TIMEOUT_STANDARD,
            retry_policy=ACT_RETRY,
        )

        if outcome["action"] == "archived":
            await workflow.execute_activity(
                "comment",
                args=[
                    task_id,
                    input.agent_id,
                    "This is an automated notification, not an action — archived it "
                    f"in {outcome['account']} and closing the task.",
                ],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=NO_RETRY,
            )
            await workflow.execute_activity(
                "complete_task",
                args=[task_id],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=ACT_RETRY,
            )
            return {"task_id": task_id, "verb": "email", "status": "archived"}

        reason = (
            "needs a reply, and I can't send mail (scope is gmail.modify)"
            if outcome["action"] == "needs_human"
            else "I couldn't find this message in any connected account"
        )
        await workflow.execute_activity(
            "comment",
            args=[task_id, input.agent_id, f"Leaving this one for you — {reason}."],
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=NO_RETRY,
        )
        await workflow.execute_activity(
            "park_task",
            args=[task_id, f"email {outcome['action']}"],
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=ACT_RETRY,
        )
        return {"task_id": task_id, "verb": "email", "status": "parked"}
```

- [ ] **Step 6: Register and wire**

In `worker/src/aegis_worker/__main__.py`, inside `main()` after `gmail_act` is constructed
(~line 308) and `agent_task_act` exists, late-wire both:

```python
    agent_task_act.gmail_activities = gmail_act
    # Active email channels are the Gmail accounts to probe. Read them from the
    # channels table (kind='email', active) — config->>'label' is the account
    # label apply_label expects. In prod: arshad-personal, arshad-stpd,
    # arshad-hikmah.
    agent_task_act.gmail_accounts = [
        r["label"]
        for r in await deps.pool.fetch(
            "SELECT config->>'label' AS label FROM channels "
            "WHERE kind = 'email' AND active AND config->>'label' IS NOT NULL"
        )
    ]
```

Add `triage_email` to BOTH the module-level `ACTIVITIES` (as `_stub_agent_task_act.triage_email`) and
the live `activities` list (as `agent_task_act.triage_email`).

- [ ] **Step 7: Run the whole suite, lint, commit**

```bash
PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/ -n auto --dist loadfile --timeout=300 -q
ruff check worker/src/ tests/worker/
git add worker/src/aegis_worker/activities/agent_task.py worker/src/aegis_worker/flows/agent_task.py \
        worker/src/aegis_worker/__main__.py \
        tests/worker/activities/test_agent_task_email.py
git commit -m "feat(agent-task): email verb — archive notifications, park real actions"
```

---

### Task 7: `#receipt` → finance verb (7 tasks)

These are questions ("Anomaly: ? Eleven Labs"), not work — a human decides whether a charge is
legitimate. The value is the assembled context, not an autonomous decision, so this verb gathers
prior charges for the merchant and puts up a decision card. No autonomous write.

**Files:**
- Modify: `worker/src/aegis_worker/activities/agent_task.py`, `worker/src/aegis_worker/flows/agent_task.py`
- Modify: `worker/src/aegis_worker/__main__.py`
- Test: `tests/worker/activities/test_agent_task_finance.py`

**Interfaces:**
- Consumes: `comment`, `park_task`, `complete_task`.
- Produces:
  - `AgentTaskActivities.merchant_history(title: str, limit: int = 6) -> dict`
    → `{"merchant": str, "charges": list[dict], "summary": str}`
  - `AgentTaskActivities.apply_finance_decision(interaction_id: str, response: dict, metadata: dict) -> dict`
    → `{"applied": "expected" | "investigate" | "none"}`

- [ ] **Step 1: Write the failing test**

```python
# tests/worker/activities/test_agent_task_finance.py
"""merchant_history + apply_finance_decision."""

from __future__ import annotations

import pytest
import pytest_asyncio
from aegis_worker.activities.agent_task import AgentTaskActivities, extract_merchant


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Anomaly: ? Eleven Labs", "Eleven Labs"),
        ("Anomaly: 8100.00 INR Mahavitaran (MSEDCL)", "Mahavitaran (MSEDCL)"),
        ("Renewal in 19.6 days: Mahavitaran (MSEDCL) (810000 INR)", "Mahavitaran (MSEDCL)"),
        ("Something unrelated", ""),
    ],
)
def test_extract_merchant(title, expected):
    assert extract_merchant(title) == expected


@pytest_asyncio.fixture(loop_scope="function")
async def _charges(db_pool):
    await db_pool.execute("DELETE FROM finance.recurring_charge WHERE vendor_name = 'Eleven Labs'")
    await db_pool.execute(
        "INSERT INTO finance.recurring_charge "
        "  (account, sender_label, vendor_name, amount_cents, currency, last_seen_at) "
        "VALUES ('a','s','Eleven Labs', 2200, 'USD', now() - interval '30 days'), "
        "       ('a','s','Eleven Labs', 2200, 'USD', now() - interval '60 days')"
    )
    yield
    await db_pool.execute("DELETE FROM finance.recurring_charge WHERE vendor_name = 'Eleven Labs'")


async def test_merchant_history_returns_prior_charges(db_pool, _charges):
    act = AgentTaskActivities(db_pool=db_pool)
    result = await act.merchant_history("Anomaly: ? Eleven Labs")
    assert result["merchant"] == "Eleven Labs"
    assert len(result["charges"]) == 2
    assert "22" in result["summary"]


async def test_merchant_history_unknown_merchant_is_empty_not_error(db_pool):
    result = await AgentTaskActivities(db_pool=db_pool).merchant_history("Something unrelated")
    assert result == {"merchant": "", "charges": [], "summary": ""}


def _act_recording(calls: list) -> AgentTaskActivities:
    """AgentTaskActivities with its three terminal-state writers recorded."""
    act = AgentTaskActivities(db_pool=None)

    async def _complete(task_id: str) -> dict:
        calls.append(("complete", task_id))
        return {"completed": True}

    async def _park(task_id: str, reason: str) -> dict:
        calls.append(("park", task_id))
        return {"parked": True}

    async def _comment(task_id: str, agent_id: str, body: str) -> dict:
        return {"ok": True}

    act.complete_task = _complete   # type: ignore[assignment]
    act.park_task = _park           # type: ignore[assignment]
    act.comment = _comment          # type: ignore[assignment]
    return act


async def test_finance_decision_expected_completes_the_task():
    calls: list = []
    meta = {"task_id": "tfin-1", "agent_id": "maou", "merchant": "Eleven Labs"}
    result = await _act_recording(calls).apply_finance_decision(
        "i1", {"value": "expected"}, meta
    )
    assert result == {"applied": "expected"}
    assert ("complete", "tfin-1") in calls


async def test_finance_decision_investigate_parks_the_task():
    calls: list = []
    meta = {"task_id": "tfin-2", "agent_id": "maou", "merchant": "Eleven Labs"}
    result = await _act_recording(calls).apply_finance_decision(
        "i1", {"value": "investigate"}, meta
    )
    assert result == {"applied": "investigate"}
    assert ("park", "tfin-2") in calls
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/activities/test_agent_task_finance.py -v`
Expected: FAIL — `ImportError: cannot import name 'extract_merchant'`

Column names are verified against `migrations/001_baseline.sql:45-59`: the table is
`finance.recurring_charge` with `vendor_name` (not `merchant`), `amount_cents` (integer cents, not a
decimal `amount`), `currency`, `last_seen_at`, and NOT NULL `account` + `sender_label`.

- [ ] **Step 3: Implement merchant history and the decision hook**

Add the pure function at module level:

```python
_MERCHANT_PATTERNS = (
    re.compile(r"^Anomaly:\s*\?\s*(.+?)\s*$", re.I),
    re.compile(r"^Anomaly:\s*[\d.,]+\s+\w+\s+(.+?)\s*$", re.I),
    re.compile(r"^Renewal in [\d.]+ days:\s*(.+?)\s*\([^)]*\)\s*$", re.I),
)


def extract_merchant(title: str) -> str:
    """Merchant named by a #receipt task title, or '' when none is."""
    text = (title or "").strip()
    for pattern in _MERCHANT_PATTERNS:
        match = pattern.match(text)
        if match:
            return match.group(1).strip()
    return ""
```

And to `AgentTaskActivities`:

```python
    @activity.defn
    async def merchant_history(self, title: str, limit: int = 6) -> dict:
        """Prior charges for the merchant this task names.

        The value of this verb is assembled context, not an autonomous
        decision — whether a charge is legitimate is the user's call.
        """
        merchant = extract_merchant(title)
        if not merchant or self.db_pool is None:
            return {"merchant": "", "charges": [], "summary": ""}
        rows = await self.db_pool.fetch(
            "SELECT amount_cents, currency, last_seen_at FROM finance.recurring_charge "
            "WHERE vendor_name = $1 ORDER BY last_seen_at DESC LIMIT $2",
            merchant,
            limit,
        )
        charges = [
            {
                # amount_cents is an integer number of cents (migration 001).
                "amount": (r["amount_cents"] or 0) / 100.0,
                "currency": r["currency"] or "",
                "last_seen_at": r["last_seen_at"].isoformat() if r["last_seen_at"] else "",
            }
            for r in rows
        ]
        summary = (
            "; ".join(f"{c['amount']:g} {c['currency']} on {c['last_seen_at'][:10]}" for c in charges)
            or "no prior charges on record"
        )
        return {"merchant": merchant, "charges": charges, "summary": summary}

    @activity.defn
    async def apply_finance_decision(
        self, interaction_id: str, response: dict, metadata: dict
    ) -> dict:
        """InteractionFlow post_resolve hook for the anomaly decision card."""
        choice = (response.get("value") or "").strip()
        task_id = str(metadata.get("task_id") or "")
        agent_id = str(metadata.get("agent_id") or "")
        merchant = str(metadata.get("merchant") or "this charge")
        if not task_id:
            return {"applied": "none"}

        if choice == "expected":
            await self.comment(task_id, agent_id, f"You confirmed {merchant} is expected — closing.")
            await self.complete_task(task_id)
            return {"applied": "expected"}
        if choice == "investigate":
            await self.comment(
                task_id, agent_id, f"Flagged {merchant} for you to investigate."
            )
            await self.park_task(task_id, "finance anomaly needs investigation")
            return {"applied": "investigate"}

        activity.logger.info(
            "agent_task_finance_no_action interaction_id=%s choice=%s", interaction_id, choice
        )
        return {"applied": "none"}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/activities/test_agent_task_finance.py -v`
Expected: PASS — 8 passed

- [ ] **Step 5: Wire the finance branch into the flow**

In `AgentTaskFlow.run`, add before the unknown-verb fallback:

```python
        if verb == "finance":
            return await self._run_finance(input, task_id)
```

```python
    async def _run_finance(self, input: AgentTaskFlowInput, task_id: str) -> dict:
        """Gather merchant context and put the decision to the user."""
        title = str(input.task.get("content") or "")
        history = await workflow.execute_activity(
            "merchant_history",
            args=[title, 6],
            start_to_close_timeout=TIMEOUT_STANDARD,
            retry_policy=ACT_RETRY,
        )
        if not history["merchant"]:
            await workflow.execute_activity(
                "comment",
                args=[task_id, input.agent_id, "I couldn't tell which merchant this is about."],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=NO_RETRY,
            )
            await workflow.execute_activity(
                "park_task",
                args=[task_id, "merchant not parseable from title"],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=ACT_RETRY,
            )
            return {"task_id": task_id, "verb": "finance", "status": "parked"}

        await workflow.execute_activity(
            "comment",
            args=[
                task_id,
                input.agent_id,
                f"Prior charges for {history['merchant']}: {history['summary']}",
            ],
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=NO_RETRY,
        )
        try:
            await workflow.start_child_workflow(
                InteractionFlow.run,
                InteractionFlowInput(
                    agent_id=input.agent_id,
                    kind="choice",
                    origin="agent_task_finance",
                    prompt=(
                        f"💳 <b>{history['merchant']}</b>\n\n{title}\n\n"
                        f"History: {history['summary']}\n\nIs this expected?"
                    ),
                    options={"expected": "✅ Expected", "investigate": "🔍 Investigate"},
                    timeout_seconds=86400,
                    timeout_policy="archive",
                    metadata={
                        "task_id": task_id,
                        "agent_id": input.agent_id,
                        "merchant": history["merchant"],
                    },
                    post_resolve_activity="apply_finance_decision",
                ),
                id=f"agent-task-finance-{task_id}",
                parent_close_policy=workflow.ParentClosePolicy.ABANDON,
            )
        except WorkflowAlreadyStartedError:
            pass

        await workflow.execute_activity(
            "park_task",
            args=[task_id, "awaiting finance decision"],
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=ACT_RETRY,
        )
        return {"task_id": task_id, "verb": "finance", "status": "carded"}
```

- [ ] **Step 6: Register**

Add both to the module-level `ACTIVITIES` (`_stub_agent_task_act.merchant_history`,
`_stub_agent_task_act.apply_finance_decision`) and to the live `activities` list
(`agent_task_act.merchant_history`, `agent_task_act.apply_finance_decision`).

- [ ] **Step 7: Run the whole suite, lint, commit**

```bash
PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/ -n auto --dist loadfile --timeout=300 -q
ruff check worker/src/ tests/worker/
git add worker/src/aegis_worker/activities/agent_task.py worker/src/aegis_worker/flows/agent_task.py \
        worker/src/aegis_worker/__main__.py tests/worker/activities/test_agent_task_finance.py
git commit -m "feat(agent-task): finance verb — merchant context plus a decision card"
```

---

### Task 8: `@code` → coding verb, phase 1 (investigate and plan)

Two-phase by design: a misread task or wrong repo must cost nothing. Phase 1 investigates read-only
and cards the plan. Phase 2 (Task 9) implements on approval.

**Files:**
- Modify: `worker/src/aegis_worker/activities/agent_task.py`, `worker/src/aegis_worker/flows/agent_task.py`
- Modify: `worker/src/aegis_worker/__main__.py`
- Test: `tests/worker/activities/test_agent_task_repo.py`

**Interfaces:**
- Consumes: `comment`, `park_task`.
- Produces:
  - `AgentTaskActivities.resolve_task_repo(task: dict) -> dict`
    → `{"github_repo": str, "repo_path": str, "source": str, "candidates": list[dict]}`
  - `AgentTaskActivities.run_task_investigation(task_id: str, title: str, description: str, repo_path: str, github_repo: str) -> dict`
    → `{"status": str, "transcript": str, "run_id": str}`

- [ ] **Step 1: Write the failing repo-resolution test**

```python
# tests/worker/activities/test_agent_task_repo.py
"""resolve_task_repo — Todoist project name is the strongest repo signal."""

from __future__ import annotations

import pytest_asyncio
from aegis_worker.activities.agent_task import AgentTaskActivities


@pytest_asyncio.fixture(loop_scope="function")
async def _seed(db_pool):
    await db_pool.execute("DELETE FROM todoist_projects WHERE id LIKE 'pr-%'")
    await db_pool.execute("DELETE FROM resources WHERE slug LIKE 'test-repo-%'")
    await db_pool.execute(
        "INSERT INTO todoist_projects (id, name, is_managed, is_archived, order_idx) "
        "VALUES ('pr-bcp','BCP',false,false,1), ('pr-unknown','Nowhere',false,false,2)"
    )
    await db_pool.execute(
        "INSERT INTO resources (slug, kind, title, metadata) VALUES "
        "('test-repo-bcp','repository','Stockopedia/bcp',"
        " '{\"github_repo\": \"Stockopedia/bcp\"}'::jsonb)"
    )
    yield
    await db_pool.execute("DELETE FROM todoist_projects WHERE id LIKE 'pr-%'")
    await db_pool.execute("DELETE FROM resources WHERE slug LIKE 'test-repo-%'")


async def test_project_name_resolves_to_repo(db_pool, _seed):
    act = AgentTaskActivities(db_pool=db_pool)
    result = await act.resolve_task_repo(
        {"id": "x", "content": "Fix the exporter", "project_id": "pr-bcp"}
    )
    assert result["github_repo"] == "Stockopedia/bcp"
    assert result["source"] == "project_map"


async def test_unmapped_project_returns_no_repo_never_a_guess(db_pool, _seed):
    act = AgentTaskActivities(db_pool=db_pool)
    result = await act.resolve_task_repo(
        {"id": "x", "content": "Fix something", "project_id": "pr-unknown"}
    )
    assert result["github_repo"] == ""
    assert result["source"] == "none"


async def test_missing_project_id_returns_no_repo(db_pool, _seed):
    act = AgentTaskActivities(db_pool=db_pool)
    result = await act.resolve_task_repo({"id": "x", "content": "Fix", "project_id": None})
    assert result["github_repo"] == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/activities/test_agent_task_repo.py -v`
Expected: FAIL — `AttributeError: 'AgentTaskActivities' object has no attribute 'resolve_task_repo'`

- [ ] **Step 3: Implement repo resolution**

Add the map and activity. The four pairs below are inferred from the real project names; the map is a
module constant so it is one obvious place to extend (there are 55 registered repositories).

```python
# Todoist project name → GitHub repo. The projects already mirror repos, which
# is a far stronger signal than guessing from a task title.
PROJECT_REPO_MAP = {
    "bcp": "Stockopedia/bcp",
    "aegis": "hikmahtech/aegis",
    "home infra": "hikmahtech/homelab-gitops",
    "drwho": "hikmahtech/drwhome",
}
```

```python
    @activity.defn
    async def resolve_task_repo(self, task: dict) -> dict:
        """Resolve a coding task to a repo. Never guesses.

        Tier 1 is the Todoist project name. An unresolved repo is a hard stop —
        running a coding agent against the wrong checkout is worse than not
        running it.
        """
        empty = {"github_repo": "", "repo_path": "", "source": "none", "candidates": []}
        project_id = task.get("project_id")
        if self.db_pool is None or not project_id:
            return empty
        name = await self.db_pool.fetchval(
            "SELECT name FROM todoist_projects WHERE id = $1", project_id
        )
        if not name:
            return empty
        github_repo = PROJECT_REPO_MAP.get(str(name).strip().lower(), "")
        if not github_repo:
            activity.logger.info("agent_task_repo_unmapped project=%s", name)
            return empty
        # repo_path is the workspace-relative checkout path start_kimi_run needs.
        row = await self.db_pool.fetchrow(
            "SELECT metadata->>'resource_path' AS rpath FROM resources "
            "WHERE kind = 'repository' AND metadata->>'github_repo' = $1 LIMIT 1",
            github_repo,
        )
        return {
            "github_repo": github_repo,
            "repo_path": (row["rpath"] if row and row["rpath"] else github_repo.split("/")[-1]),
            "source": "project_map",
            "candidates": [],
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/activities/test_agent_task_repo.py -v`
Expected: PASS — 3 passed

- [ ] **Step 5: Add the investigation activity**

This reuses the coding-run machinery and the transcript extraction fixed in #150. Read
`worker/src/aegis_worker/activities/alerts.py::run_investigation` (around line 1990) and mirror its
start → poll → extract loop, then:

```python
    @activity.defn
    async def run_task_investigation(
        self,
        task_id: str,
        title: str,
        description: str,
        repo_path: str,
        github_repo: str,
    ) -> dict:
        """Read-only coding-CLI run: understand the task and propose a plan.

        Phase 1 of two. Investigating first means a misread task or wrong repo
        costs nothing. MUST NOT write code — the prompt says so and the plan
        card gates phase 2.
        """
        if self.remote_script is None or not repo_path:
            return {"status": "failed", "transcript": "", "run_id": ""}

        prompt = (
            "You are investigating a task. Do NOT modify any files, do NOT commit, "
            "do NOT create branches.\n\n"
            f"Task: {title}\n\n{description}\n\n"
            "Read the code and report:\n"
            "1. What the task is actually asking for.\n"
            "2. Which files would need to change.\n"
            "3. A short implementation plan.\n"
            "4. Anything that makes this ambiguous or risky.\n\n"
            "End your final message with exactly one of:\n"
            "     STATUS: scoped\n"
            "     STATUS: unactionable: <why>\n"
        )
        settings = await self.remote_script.coding_settings()
        started = await self.remote_script.start_kimi_run(
            repo=repo_path,
            prompt=prompt,
            kimi_binary=settings.get("kimi_binary", ""),
            github_repo=github_repo,
        )
        if started.get("status") != "running":
            return {
                "status": "failed",
                "transcript": started.get("error", "")[:500],
                "run_id": started.get("run_id", ""),
            }
        return {
            "status": "running",
            "transcript": "",
            "run_id": started.get("run_id", ""),
            "output_file": started.get("output_file", ""),
            "host": started.get("host", ""),
            "worktree_path": started.get("worktree_path", ""),
        }
```

- [ ] **Step 6: Wire the coding branch into the flow**

In `AgentTaskFlow.run`, add before the unknown-verb fallback:

```python
        if verb == "coding":
            return await self._run_coding(input, task_id)
```

```python
    async def _run_coding(self, input: AgentTaskFlowInput, task_id: str) -> dict:
        """Phase 1: resolve the repo, investigate read-only, card the plan."""
        repo = await workflow.execute_activity(
            "resolve_task_repo",
            args=[input.task],
            start_to_close_timeout=TIMEOUT_STANDARD,
            retry_policy=ACT_RETRY,
        )
        if not repo["github_repo"]:
            await workflow.execute_activity(
                "comment",
                args=[
                    task_id,
                    input.agent_id,
                    "I couldn't work out which repository this task is about, so I "
                    "haven't touched anything.",
                ],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=NO_RETRY,
            )
            await workflow.execute_activity(
                "park_task",
                args=[task_id, "repo unresolved"],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=ACT_RETRY,
            )
            return {"task_id": task_id, "verb": "coding", "status": "parked"}

        investigation = await workflow.execute_activity(
            "run_task_investigation",
            args=[
                task_id,
                str(input.task.get("content") or ""),
                str(input.task.get("description") or ""),
                repo["repo_path"],
                repo["github_repo"],
            ],
            start_to_close_timeout=TIMEOUT_LONG,
            retry_policy=RETRY_ONCE,
        )

        await workflow.execute_activity(
            "comment",
            args=[
                task_id,
                input.agent_id,
                f"Investigating in `{repo['github_repo']}` "
                f"(run {investigation.get('run_id', '?')}).",
            ],
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=NO_RETRY,
        )
        await workflow.execute_activity(
            "park_task",
            args=[task_id, "coding investigation dispatched"],
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=ACT_RETRY,
        )
        return {
            "task_id": task_id,
            "verb": "coding",
            "status": "investigating",
            "repo": repo["github_repo"],
        }
```

Add `RETRY_ONCE` and `TIMEOUT_LONG` to the retry imports at the top of the file.

- [ ] **Step 7: Register**

Add both to the module-level `ACTIVITIES` (`_stub_agent_task_act.resolve_task_repo`,
`_stub_agent_task_act.run_task_investigation`) and to the live `activities` list
(`agent_task_act.resolve_task_repo`, `agent_task_act.run_task_investigation`).

- [ ] **Step 8: Run the whole suite, lint, commit**

```bash
PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/ -n auto --dist loadfile --timeout=300 -q
ruff check worker/src/ tests/worker/
git add worker/src/aegis_worker/activities/agent_task.py worker/src/aegis_worker/flows/agent_task.py \
        worker/src/aegis_worker/__main__.py tests/worker/activities/test_agent_task_repo.py
git commit -m "feat(agent-task): coding verb phase 1 — repo resolution and read-only investigation"
```

---

### Task 9: `@code` coding verb, phase 2 (plan card, implement, PR)

Task 8 dispatches a read-only investigation and parks. This task consumes the transcript, gates the
plan, implements on approval, and gates the PR.

**Why this verb awaits its cards while the infra/finance verbs abandon theirs:** the sweep already
spawns `AgentTaskFlow` with `ParentClosePolicy.ABANDON`, so *this* workflow blocking on a human costs
nothing — the sweep is unaffected and later ticks still fire. Phase 2 must run strictly after the
plan is approved, and awaiting the card keeps that sequence in one workflow instead of splitting it
across post-resolve hooks.

**Files:**
- Modify: `worker/src/aegis_worker/activities/agent_task.py`, `worker/src/aegis_worker/flows/agent_task.py`
- Modify: `worker/src/aegis_worker/__main__.py`
- Test: `tests/worker/activities/test_agent_task_coding_collect.py`, `tests/worker/flows/test_agent_task_coding.py`

**Interfaces:**
- Consumes: `resolve_task_repo`, `run_task_investigation` (Task 8); `comment`, `park_task`.
- Produces:
  - `AgentTaskActivities.collect_coding_run(output_file: str, host: str, max_polls: int = 40) -> dict`
    → `{"status": "succeeded" | "timed_out" | "failed", "transcript": str}`
  - `AgentTaskActivities.run_task_implementation(task_id: str, title: str, description: str, plan: str, repo_path: str, github_repo: str) -> dict`
    → `{"status": str, "transcript": str, "branch": str, "run_id": str, "output_file": str, "host": str}`

- [ ] **Step 1: Write the failing collect test**

```python
# tests/worker/activities/test_agent_task_coding_collect.py
"""collect_coding_run — poll a coding run to completion and extract its transcript."""

from __future__ import annotations

import json

from aegis_worker.activities.agent_task import AgentTaskActivities


def _assistant(text: str) -> str:
    return json.dumps({"role": "assistant", "content": [{"type": "text", "text": text}]})


class _Remote:
    def __init__(self, responses: list[str | None]):
        self._responses = responses
        self.calls = 0

    async def fetch_kimi_run_output(self, output_file: str, host: str = "") -> str | None:
        value = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return value


async def test_returns_transcript_once_status_footer_appears():
    partial = _assistant("still reading files")
    final = partial + "\n" + _assistant("Plan: fix the parser\nSTATUS: scoped")
    act = AgentTaskActivities(db_pool=None, remote_script=_Remote([partial, final]))

    result = await act.collect_coding_run("/tmp/run.jsonl", "node-a", max_polls=5)
    assert result["status"] == "succeeded"
    assert "Plan: fix the parser" in result["transcript"]
    # Tool-result noise must never reach the transcript (issue fixed in #150).
    assert '"role"' not in result["transcript"]


async def test_times_out_without_status_footer():
    act = AgentTaskActivities(
        db_pool=None, remote_script=_Remote([_assistant("thinking")])
    )
    result = await act.collect_coding_run("/tmp/run.jsonl", "node-a", max_polls=2)
    assert result["status"] == "timed_out"


async def test_empty_output_is_failed_not_a_silent_success():
    act = AgentTaskActivities(db_pool=None, remote_script=_Remote([None]))
    result = await act.collect_coding_run("/tmp/run.jsonl", "node-a", max_polls=2)
    assert result["status"] in {"timed_out", "failed"}
    assert result["transcript"] == ""


async def test_no_connector_is_failed():
    act = AgentTaskActivities(db_pool=None, remote_script=None)
    assert (await act.collect_coding_run("/tmp/x", ""))["status"] == "failed"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/activities/test_agent_task_coding_collect.py -v`
Expected: FAIL — `AttributeError: 'AgentTaskActivities' object has no attribute 'collect_coding_run'`

- [ ] **Step 3: Implement collect and implement activities**

Add to `AgentTaskActivities`:

```python
    @activity.defn
    async def collect_coding_run(
        self, output_file: str, host: str, max_polls: int = 40
    ) -> dict:
        """Poll a coding run until its STATUS footer appears, then extract text.

        Returns the ASSISTANT TRANSCRIPT, never the raw stream-json: handing raw
        jsonl to an LLM is what made every prod verdict confidence=0.0 with
        `{"role":"tool"` in its root_cause (fixed in #150).
        """
        import asyncio

        from aegis_worker.activities.alerts import (
            _extract_kimi_transcript,
            _kimi_output_complete,
        )

        if self.remote_script is None or not output_file:
            return {"status": "failed", "transcript": ""}

        latest = ""
        for _ in range(max_polls):
            raw = await self.remote_script.fetch_kimi_run_output(output_file, host=host)
            if raw:
                latest = raw
                if _kimi_output_complete(raw):
                    return {
                        "status": "succeeded",
                        "transcript": _extract_kimi_transcript(raw)[-8000:],
                    }
            activity.heartbeat()
            await asyncio.sleep(30)

        return {
            "status": "timed_out",
            "transcript": _extract_kimi_transcript(latest)[-8000:] if latest else "",
        }

    @activity.defn
    async def run_task_implementation(
        self,
        task_id: str,
        title: str,
        description: str,
        plan: str,
        repo_path: str,
        github_repo: str,
    ) -> dict:
        """Phase 2: implement the approved plan on a branch. Does NOT open a PR.

        Opening the PR is a separate gated step, so a bad implementation stays
        local and reviewable rather than becoming a PR nobody asked for.
        """
        if self.remote_script is None or not repo_path:
            return {"status": "failed", "transcript": "", "branch": "", "run_id": ""}

        branch = f"aegis-task/{task_id}"
        prompt = (
            "Implement the approved plan below. Commit your work to a new branch "
            f"named exactly `{branch}`. Do NOT open a pull request.\n\n"
            f"Task: {title}\n\n{description}\n\n"
            f"Approved plan:\n{plan}\n\n"
            "End your final message with exactly one of:\n"
            f"     BRANCH: {github_repo.split('/')[-1]}:{branch}\n"
            "     STATUS: implemented\n"
            "   or STATUS: unactionable: <why>\n"
        )
        settings = await self.remote_script.coding_settings()
        started = await self.remote_script.start_kimi_run(
            repo=repo_path,
            prompt=prompt,
            kimi_binary=settings.get("kimi_binary", ""),
            github_repo=github_repo,
        )
        if started.get("status") != "running":
            return {
                "status": "failed",
                "transcript": started.get("error", "")[:500],
                "branch": "",
                "run_id": started.get("run_id", ""),
            }
        return {
            "status": "running",
            "transcript": "",
            "branch": branch,
            "run_id": started.get("run_id", ""),
            "output_file": started.get("output_file", ""),
            "host": started.get("host", ""),
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/activities/test_agent_task_coding_collect.py -v`
Expected: PASS — 4 passed

- [ ] **Step 5: Write the failing flow test**

```python
# tests/worker/flows/test_agent_task_coding.py
"""Coding verb end to end: plan gate, implement, PR gate."""

from __future__ import annotations

import uuid

from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

# Module is `interactions` (PLURAL), and these are imported inside
# imports_passed_through — mirror tests/worker/flows/test_alert_investigation_gates.py:22.
with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.interactions import (
        ApplyTimeoutInput,
        InsertInteractionInput,
        InsertInteractionResult,
        ResolveInteractionInput,
        ResolveInteractionResult,
    )
    from aegis_worker.flows.agent_task import AgentTaskFlow, AgentTaskFlowInput
    from aegis_worker.flows.interaction import InteractionFlow

_CODE_TASK = {
    "id": "tc-1",
    "content": "Fix phantom EPS downgrade",
    "description": "duplicate current=true rows",
    "labels": ["@pandora", "@code"],
    "source_tag": None,
    "project_id": "pr-bcp",
    "assignee_label": "@pandora",
}


def _activities(events: list, *, plan_choice: str):
    @activity.defn(name="load_task_context")
    async def load_task_context(task_id: str) -> dict:
        return {"external_id": "", "fingerprint": "", "gmail_message_id": ""}

    @activity.defn(name="comment")
    async def comment(task_id: str, agent_id: str, body: str) -> dict:
        events.append(("comment", body))
        return {"ok": True}

    @activity.defn(name="park_task")
    async def park_task(task_id: str, reason: str) -> dict:
        events.append(("park", reason))
        return {"parked": True}

    @activity.defn(name="resolve_task_repo")
    async def resolve_task_repo(task: dict) -> dict:
        return {
            "github_repo": "Stockopedia/bcp",
            "repo_path": "Stockopedia/bcp",
            "source": "project_map",
            "candidates": [],
        }

    @activity.defn(name="run_task_investigation")
    async def run_task_investigation(
        task_id: str, title: str, description: str, repo_path: str, github_repo: str
    ) -> dict:
        events.append(("investigate", repo_path))
        return {
            "status": "running",
            "transcript": "",
            "run_id": "r1",
            "output_file": "/tmp/r1.jsonl",
            "host": "node-a",
        }

    @activity.defn(name="collect_coding_run")
    async def collect_coding_run(output_file: str, host: str, max_polls: int = 40) -> dict:
        return {"status": "succeeded", "transcript": "Plan: dedupe the rows\nSTATUS: scoped"}

    @activity.defn(name="run_task_implementation")
    async def run_task_implementation(
        task_id: str,
        title: str,
        description: str,
        plan: str,
        repo_path: str,
        github_repo: str,
    ) -> dict:
        events.append(("implement", plan[:20]))
        return {
            "status": "running",
            "transcript": "",
            "branch": "aegis-task/tc-1",
            "run_id": "r2",
            "output_file": "/tmp/r2.jsonl",
            "host": "node-a",
        }

    # stage_pending_pr returns a PLAIN STRING id, not a dict.
    @activity.defn(name="stage_pending_pr")
    async def stage_pending_pr(inp) -> str:
        return "pr-uuid-stub"

    @activity.defn(name="create_github_pr")
    async def create_github_pr(inp) -> dict:
        events.append(("pr", "opened"))
        return {"pr_url": "https://github.com/Stockopedia/bcp/pull/1", "status": "opened"}

    # InteractionFlow's own activities. Names and input types copied from the
    # canonical stub block in tests/worker/flows/test_alert_investigation_gates.py
    # (~line 215) — read that file and mirror it rather than inventing names.
    @activity.defn(name="insert_interaction")
    async def insert_interaction(inp: InsertInteractionInput) -> InsertInteractionResult:
        return InsertInteractionResult(interaction_id="ia-coding-test")

    @activity.defn(name="send_interaction_card")
    async def send_interaction_card(
        interaction_id: str,
        agent_id: str,
        kind: str,
        prompt: str,
        options,
        allow_hint: bool = False,
    ) -> dict:
        return {"ok": True, "message_id": 1}

    @activity.defn(name="resolve_interaction")
    async def resolve_interaction(inp: ResolveInteractionInput) -> ResolveInteractionResult:
        return ResolveInteractionResult(already_resolved=False)

    @activity.defn(name="apply_interaction_timeout")
    async def apply_interaction_timeout(inp: ApplyTimeoutInput) -> None:
        return None

    @activity.defn(name="update_interaction_delivery_ref")
    async def update_interaction_delivery_ref(*args) -> None:
        return None

    return [
        load_task_context,
        comment,
        park_task,
        resolve_task_repo,
        run_task_investigation,
        collect_coding_run,
        run_task_implementation,
        stage_pending_pr,
        create_github_pr,
        insert_interaction,
        send_interaction_card,
        resolve_interaction,
        apply_interaction_timeout,
        update_interaction_delivery_ref,
    ]


async def test_declined_plan_stops_before_any_implement_run():
    """A misread task or wrong repo must cost nothing beyond the read-only run."""
    events: list = []
    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue = f"tq-{uuid.uuid4()}"
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[AgentTaskFlow, InteractionFlow],
            activities=_activities(events, plan_choice="skip"),
        ):
            result = await env.client.execute_workflow(
                AgentTaskFlow.run,
                AgentTaskFlowInput(
                    agent_id="pandoras-actor", todoist_task_id="tc-1", task=_CODE_TASK
                ),
                id=f"agent-task-tc-1-{uuid.uuid4()}",
                task_queue=queue,
            )

    assert result["verb"] == "coding"
    assert not any(kind == "implement" for kind, _ in events)
    assert not any(kind == "pr" for kind, _ in events)
    assert any(kind == "park" for kind, _ in events)
```

The test above covers the DECLINED path, which needs no card answer (the card times out under
`timeout_policy="archive"` and time-skipping fast-forwards it).

**You must also cover the APPROVED path** — it is the whole point of the verb, and leaving it to live
validation means the first real run is the first test. There is a working precedent for signalling a
child `InteractionFlow`: `tests/worker/flows/test_alert_investigation_gates.py` uses
`WorkflowEnvironment.start_local()` (not `start_time_skipping()`) precisely so the child can be
signalled at the right moment — read its Gate-2 test and mirror the pattern. Add
`test_approved_plan_implements_then_opens_pr_and_parks`, asserting the ordered sequence
`investigate → implement → pr` in `events` and a final status of `pr_opened`.

- [ ] **Step 6: Run flow test to verify it fails**

Run: `PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/flows/test_agent_task_coding.py -v`
Expected: FAIL — `result["status"]` is `"investigating"` (Task 8 parks right after dispatch)

- [ ] **Step 7: Extend `_run_coding` with the two gates**

Replace everything in `_run_coding` after the `run_task_investigation` call with:

```python
        if investigation.get("status") == "failed":
            await workflow.execute_activity(
                "comment",
                args=[task_id, input.agent_id, "I couldn't start a coding run for this."],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=NO_RETRY,
            )
            await workflow.execute_activity(
                "park_task",
                args=[task_id, "coding run failed to start"],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=ACT_RETRY,
            )
            return {"task_id": task_id, "verb": "coding", "status": "parked"}

        collected = await workflow.execute_activity(
            "collect_coding_run",
            args=[investigation.get("output_file", ""), investigation.get("host", "")],
            start_to_close_timeout=TIMEOUT_CLAUDE,
            retry_policy=NO_RETRY,
            heartbeat_timeout=timedelta(minutes=2),
        )
        plan = collected.get("transcript", "")
        if not plan:
            await workflow.execute_activity(
                "comment",
                args=[task_id, input.agent_id, "The investigation produced no usable output."],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=NO_RETRY,
            )
            await workflow.execute_activity(
                "park_task",
                args=[task_id, "empty investigation transcript"],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=ACT_RETRY,
            )
            return {"task_id": task_id, "verb": "coding", "status": "parked"}

        await workflow.execute_activity(
            "comment",
            args=[task_id, input.agent_id, f"Investigation in `{repo['github_repo']}`:\n\n{plan}"],
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=NO_RETRY,
        )

        # AWAIT the plan card. Safe because the sweep spawned this workflow
        # ABANDONED — blocking here cannot starve later ticks.
        plan_card = await workflow.execute_child_workflow(
            InteractionFlow.run,
            InteractionFlowInput(
                agent_id=input.agent_id,
                kind="choice",
                origin="agent_task_coding_plan",
                prompt=(
                    f"🛠 <b>{input.task.get('content')}</b>\n\n"
                    f"Repo: <code>{repo['github_repo']}</code>\n\n{plan[:1200]}\n\n"
                    "Implement this?"
                ),
                options={"approve": "✅ Implement", "skip": "⏭️ Not now"},
                timeout_seconds=172800,
                timeout_policy="archive",
            ),
            id=f"agent-task-plan-{task_id}",
        )
        if (plan_card.response or {}).get("value") != "approve":
            await workflow.execute_activity(
                "park_task",
                args=[task_id, "plan not approved"],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=ACT_RETRY,
            )
            return {"task_id": task_id, "verb": "coding", "status": "plan_declined"}

        implementation = await workflow.execute_activity(
            "run_task_implementation",
            args=[
                task_id,
                str(input.task.get("content") or ""),
                str(input.task.get("description") or ""),
                plan,
                repo["repo_path"],
                repo["github_repo"],
            ],
            start_to_close_timeout=TIMEOUT_LONG,
            retry_policy=NO_RETRY,
        )
        impl_output = await workflow.execute_activity(
            "collect_coding_run",
            args=[implementation.get("output_file", ""), implementation.get("host", "")],
            start_to_close_timeout=TIMEOUT_CLAUDE,
            retry_policy=NO_RETRY,
            heartbeat_timeout=timedelta(minutes=2),
        )
        await workflow.execute_activity(
            "comment",
            args=[
                task_id,
                input.agent_id,
                f"Implementation run finished ({impl_output.get('status')}) on branch "
                f"`{implementation.get('branch', '?')}`.",
            ],
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=NO_RETRY,
        )
        if impl_output.get("status") != "succeeded" or not implementation.get("branch"):
            await workflow.execute_activity(
                "park_task",
                args=[task_id, "implementation did not complete"],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=ACT_RETRY,
            )
            return {"task_id": task_id, "verb": "coding", "status": "parked"}

        pr_card = await workflow.execute_child_workflow(
            InteractionFlow.run,
            InteractionFlowInput(
                agent_id=input.agent_id,
                kind="choice",
                origin="agent_task_coding_pr",
                prompt=(
                    f"📤 Branch <code>{implementation['branch']}</code> is ready in "
                    f"<code>{repo['github_repo']}</code>.\n\nOpen a PR?"
                ),
                options={"approve": "✅ Open PR", "skip": "⏭️ Leave the branch"},
                timeout_seconds=172800,
                timeout_policy="archive",
            ),
            id=f"agent-task-pr-{task_id}",
        )
        if (pr_card.response or {}).get("value") != "approve":
            await workflow.execute_activity(
                "park_task",
                args=[task_id, "PR not approved; branch left in place"],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=ACT_RETRY,
            )
            return {"task_id": task_id, "verb": "coding", "status": "pr_declined"}

        # stage_pending_pr takes StagePendingPrInput and returns a PLAIN STRING
        # pending_pr_id (alert_governance.py:103) — not a dict. `alert_fingerprint`
        # is reused as the correlation key; for a task-driven PR that is task:<id>.
        staged = await workflow.execute_activity(
            "stage_pending_pr",
            StagePendingPrInput(
                alert_fingerprint=f"task:{task_id}",
                repo=repo["github_repo"],
                branch=implementation["branch"],
                title=f"{input.task.get('content')}"[:72],
                body=f"Implements Todoist task {task_id}.\n\n{plan[:2000]}",
            ),
            start_to_close_timeout=TIMEOUT_STANDARD,
            retry_policy=ACT_RETRY,
        )
        pr = await workflow.execute_activity(
            "create_github_pr",
            CreateGithubPrInput(
                pending_pr_id=staged,
                repo=repo["github_repo"],
                branch=implementation["branch"],
                base="main",
                host=implementation.get("host", ""),
                repo_path=repo["repo_path"],
            ),
            start_to_close_timeout=TIMEOUT_LONG,
            retry_policy=RETRY_ONCE,
        )
        # create_github_pr returns {"pr_url", "status", ...} — the key is pr_url.
        await workflow.execute_activity(
            "comment",
            args=[task_id, input.agent_id, f"Opened a PR: {pr.get('pr_url') or 'see the repo'}"],
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=NO_RETRY,
        )
        # @waiting, never complete: the PR still needs your review.
        await workflow.execute_activity(
            "park_task",
            args=[task_id, "PR opened, awaiting review"],
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=ACT_RETRY,
        )
        return {
            "task_id": task_id,
            "verb": "coding",
            "status": "pr_opened",
            "repo": repo["github_repo"],
        }
```

Add to the imports at the top of `flows/agent_task.py`: `from datetime import timedelta` (module
level, outside the `imports_passed_through` block), `TIMEOUT_CLAUDE` to the retry imports, and inside
the `imports_passed_through` block:

```python
    from aegis_worker.activities.alert_governance import (
        CreateGithubPrInput,
        StagePendingPrInput,
    )
```

Both are verified against `worker/src/aegis_worker/activities/alert_governance.py`:
`StagePendingPrInput(alert_fingerprint, repo, branch, title, body)` at line 34, `stage_pending_pr ->
str` at line 103, `CreateGithubPrInput(pending_pr_id, repo, branch, base, host, repo_path)` at line
46, and `create_github_pr -> dict` with a `pr_url` key at line 128.

- [ ] **Step 8: Run flow test to verify it passes**

Run: `PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/flows/test_agent_task_coding.py -v`
Expected: PASS — 1 passed

- [ ] **Step 9: Register**

Add `collect_coding_run` and `run_task_implementation` to BOTH the module-level `ACTIVITIES` (as
`_stub_agent_task_act.*`) and the live `activities` list (as `agent_task_act.*`). `stage_pending_pr`
and `create_github_pr` are already registered by `AlertGovernanceActivities` — do not re-register
them, or `Worker()` will refuse to start on a duplicate activity name.

- [ ] **Step 10: Run the whole suite, lint, commit**

```bash
PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/ -n auto --dist loadfile --timeout=300 -q
ruff check worker/src/ tests/worker/
git add worker/src/aegis_worker/activities/agent_task.py worker/src/aegis_worker/flows/agent_task.py \
        worker/src/aegis_worker/__main__.py \
        tests/worker/activities/test_agent_task_coding_collect.py \
        tests/worker/flows/test_agent_task_coding.py
git commit -m "feat(agent-task): coding verb phase 2 — plan gate, implement, PR gate"
```

---

### Task 10: Deploy and validate

**Files:** none — this is a release plus live verification.

- [ ] **Step 1: Open the PR and wait for green CI**

```bash
git push -u origin worktree-agent-task-executor
gh pr create --title "feat(agent-task): execute agent-assigned Todoist tasks by source_tag" --body "Implements docs/superpowers/specs/2026-07-30-agent-task-executor-design.md. Closes #151."
gh pr checks --watch
```

If "Worker — Test" hangs with no output, that is issue #152 (pre-existing) — but Task 3 added
`-n auto --dist loadfile --timeout=300`, so it should now fail fast with a traceback instead. A real
hang means a new deadlock; investigate rather than retrying.

- [ ] **Step 2: Merge and release**

```bash
gh pr merge --squash
git -C /home/arshad/Workspace/hikmah/aegis checkout main && git -C /home/arshad/Workspace/hikmah/aegis pull --ff-only
cd /home/arshad/Workspace/infrastructure/homelab-gitops/ansible && make aegis-release
```

Expected: `PLAY RECAP` with `failed=0`. `asif` shows `unreachable=1` — that box is down by design.

- [ ] **Step 3: Verify the worker booted with the new flows**

```bash
ssh arshad@10.20.0.103 'docker logs --since 5m $(docker ps -qf name=aegis_worker|head -1) 2>&1 | grep -oE "activities=[0-9]+ flows=[0-9]+|ERROR|CRITICAL" | sort | uniq -c'
```

Expected: the `flows=` count is 2 higher than before (was `flows=30`), no ERROR/CRITICAL.

- [ ] **Step 4: Confirm the schedule reconciled**

```bash
ssh ubuntu@qaf 'CID=$(docker ps -qf name=aegis_temporal.1|head -1); docker exec -i "$CID" sh -c "temporal schedule list --address \$(hostname -i):7233" | grep agent-task'
```

Expected: `agent-task-15min` listed. `schedule_sync` reconciles on boot and every ~300s.

- [ ] **Step 5: Trigger one tick and read the outcome**

```bash
ssh ubuntu@qaf 'CID=$(docker ps -qf name=aegis_temporal.1|head -1); docker exec -i "$CID" sh -c "temporal schedule trigger --schedule-id agent-task-15min --address \$(hostname -i):7233"'
```

Then, after a minute:

```sql
SELECT started_at, status, result_summary
FROM workflow_runs
WHERE workflow_type IN ('AgentTaskSweepFlow','AgentTaskFlow')
ORDER BY started_at DESC LIMIT 10;
```

Expected: one sweep with `{"found": 3, "spawned": 3}` and three `AgentTaskFlow` runs, each with a
`todoist_task_ref` set (this is what makes the cooldown work — verify it is not NULL).

- [ ] **Step 6: Verify the brake and no runaway**

```sql
-- Must be ≤3 per tick, and no task run twice inside 6h.
SELECT todoist_task_ref, count(*), min(started_at), max(started_at)
FROM workflow_runs
WHERE workflow_type = 'AgentTaskFlow' AND started_at > now() - interval '6 hours'
GROUP BY 1 HAVING count(*) > 1;
```

Expected: **zero rows.** Any row means the cooldown is not working — most likely `todoist_task_ref`
is NULL, so re-check that `AgentTaskFlowInput.todoist_task_id` is named exactly that.

- [ ] **Step 7: Verify the loop guard on real comments**

```sql
SELECT item_id, left(content, 80)
FROM todoist_notes
WHERE posted_at > now() - interval '1 hour' AND content NOT LIKE '%Workflow run:%';
```

Expected: **zero rows** from this flow. A comment without the footer will re-eligible its task in
clarify and re-spawn every 15 minutes.

- [ ] **Step 8: Check the infra verb actually closed stale alerts**

```sql
SELECT count(*) FROM todoist_tasks
WHERE source_tag = '#alert' AND NOT is_completed AND assignee_label = '@pandora';
```

Expected: below the starting 42 — the four-week-old PROLONGED tasks name services that have long
since recovered, so the healthy short-circuit should complete a good number of them.

---

## Deferred to follow-up work

Tracked so it isn't silently dropped:

- **Repo resolution tiers 2 and 3** — title matching via `resolve_alert_resource`, and the Gate-0
  repo-confirm card. Task 8 ships tier 1 only, so an unmapped project parks instead of asking.
- **Sending email replies** — needs a `gmail.send` scope and a re-consent round for all three
  accounts.
- **The upstream causes.** This executor drains symptoms; it does not stop the tap. Alerts never
  re-check after firing (which is why PROLONGED tasks accumulate), and clarify classifies
  notifications as actionable (issue #117).
