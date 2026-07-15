"""ClarifyFlow orchestration — uses ActivityEnvironment + worker time-skip."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
from aegis_worker.flows.clarify import ClarifyConfig, ClarifyFlow
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker


@dataclass
class _StubActs:
    """Mirror ClarifyActivities's surface with @activity.defn shims."""

    async def find_unclassified_items(self, max_items: int = 20) -> list[dict]:
        return [
            {
                "id": "T_A",
                "content": "hi",
                "source_tag": "#email",
                "labels": ["#email"],
                "description": None,
                "latest_user_note": None,
                "last_note_at": None,
            }
        ]

    async def classify_one(self, task: dict) -> dict:
        return {
            "classification": "next_action",
            "confidence": 0.9,
            "assignee": "@sebas",
            "contexts": ["@email", "@5min"],
            "reason": "test",
            "llm_model": "qwen3:14b",
            "source_tag": "#email",
        }

    async def apply_outcome(self, task: dict, decision: dict, pass_n: int = 1) -> dict:
        return {"applied": True, "interaction_spawned": False, "commands_sent": 3}

    async def log_classification(
        self,
        task_id: str,
        decision: dict,
        applied: bool,
        pass_n: int = 1,
        user_hint: str | None = None,
        bump_watermark: bool = True,
    ) -> None:
        return None


@pytest.mark.asyncio
async def test_clarify_flow_processes_one_task() -> None:
    async with await WorkflowEnvironment.start_time_skipping() as env:
        client: Client = env.client

        from temporalio import activity

        stub = _StubActs()
        # Lock in the symmetric pair of the watermark invariant: on the
        # canonical applied-true / no-interaction path, log_classification
        # MUST be invoked with bump_watermark=True. Without this assertion a
        # regression that defaults bump_watermark=False on the happy path
        # would silently cost a full re-classify per tick.
        bumps_seen: list[bool] = []

        @activity.defn(name="find_unclassified_items")
        async def find_unclassified_items(max_items: int = 20):
            return await stub.find_unclassified_items(max_items)

        @activity.defn(name="classify_one")
        async def classify_one(task: dict):
            return await stub.classify_one(task)

        @activity.defn(name="apply_outcome")
        async def apply_outcome(task: dict, decision: dict, pass_n: int = 1):
            return await stub.apply_outcome(task, decision, pass_n)

        @activity.defn(name="log_classification")
        async def log_classification(
            task_id: str,
            decision: dict,
            applied: bool,
            pass_n: int = 1,
            user_hint: str | None = None,
            bump_watermark: bool = True,
        ):
            bumps_seen.append(bump_watermark)
            return await stub.log_classification(
                task_id, decision, applied, pass_n, user_hint, bump_watermark
            )

        async with Worker(
            client,
            task_queue="aegis-clarify-test",
            workflows=[ClarifyFlow],
            activities=[
                find_unclassified_items,
                classify_one,
                apply_outcome,
                log_classification,
            ],
        ):
            result = await client.execute_workflow(
                ClarifyFlow.run,
                ClarifyConfig(agent_id="sebas", max_items=10),
                id=f"clarify-{uuid.uuid4()}",
                task_queue="aegis-clarify-test",
            )
            assert result["found"] == 1
            assert result["applied"] == 1
            assert result["interactions"] == 0
            assert bumps_seen == [True], (
                f"applied=True happy path must bump watermark, got: {bumps_seen}"
            )


@pytest.mark.asyncio
async def test_clarify_flow_no_tasks_returns_zero_counts() -> None:
    async with await WorkflowEnvironment.start_time_skipping() as env:
        client = env.client
        from temporalio import activity

        @activity.defn(name="find_unclassified_items")
        async def find_unclassified_items(max_items: int = 20):
            return []

        @activity.defn(name="classify_one")
        async def classify_one(task: dict):
            raise AssertionError("must not be called")

        @activity.defn(name="apply_outcome")
        async def apply_outcome(task: dict, decision: dict, pass_n: int = 1):
            raise AssertionError("must not be called")

        @activity.defn(name="log_classification")
        async def log_classification(*a, **kw):
            raise AssertionError("must not be called")

        async with Worker(
            client,
            task_queue="aegis-clarify-test",
            workflows=[ClarifyFlow],
            activities=[
                find_unclassified_items,
                classify_one,
                apply_outcome,
                log_classification,
            ],
        ):
            result = await client.execute_workflow(
                ClarifyFlow.run,
                ClarifyConfig(agent_id="sebas", max_items=10),
                id=f"clarify-{uuid.uuid4()}",
                task_queue="aegis-clarify-test",
            )
            assert result == {"found": 0, "applied": 0, "interactions": 0}


# --- Phase 4: child InteractionFlow spawn ---


@pytest.mark.asyncio
async def test_clarify_flow_spawns_interaction_when_payload_returned() -> None:
    """When apply_outcome returns interaction_payload, ClarifyFlow starts
    InteractionFlow as a fire-and-forget abandoned child."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        client: Client = env.client
        from aegis_worker.flows.interaction import InteractionFlow
        from temporalio import activity

        spawn_calls: list[dict] = []

        # When ClarifyFlow starts the child workflow, the child needs the
        # InteractionFlow's activities registered. Stub them.
        @activity.defn(name="insert_interaction")
        async def insert_interaction(input):
            spawn_calls.append({"insert": input})
            return {"interaction_id": "11111111-1111-1111-1111-111111111111"}

        @activity.defn(name="send_interaction_card")
        async def send_interaction_card(*a, **kw):
            return {"ok": True, "message_id": 0}

        @activity.defn(name="resolve_interaction")
        async def resolve_interaction(input):
            return {"already_resolved": False}

        @activity.defn(name="apply_interaction_timeout")
        async def apply_interaction_timeout(input):
            return None

        @activity.defn(name="find_unclassified_items")
        async def find_unclassified_items(max_items: int = 20):
            return [
                {
                    "id": "T_LOW",
                    "content": "ambiguous",
                    "source_tag": "#email",
                    "labels": ["#email"],
                    "description": None,
                    "latest_user_note": None,
                    "last_note_at": None,
                }
            ]

        @activity.defn(name="classify_one")
        async def classify_one(task: dict):
            return {
                "classification": "someday",
                "confidence": 0.4,
                "assignee": "@me",
                "contexts": ["@reading"],
                "reason": "unclear",
                "llm_model": "claude-sonnet",
                "source_tag": "#email",
            }

        @activity.defn(name="apply_outcome")
        async def apply_outcome(task: dict, decision: dict, pass_n: int = 1):
            return {
                "applied": False,
                "interaction_spawned": True,
                "interaction_payload": {
                    "flavor": "low_conf",
                    "prompt": "Review: ambiguous",
                    "options": {"confirm": "C", "trash": "T", "leave": "L"},
                    "decision": decision,
                    "pass_n": pass_n,
                },
                "commands_sent": 1,
                "outbox_queued": 0,
            }

        @activity.defn(name="log_classification")
        async def log_classification(*a, **kw):
            return None

        async with Worker(
            client,
            task_queue="aegis-clarify-spawn-test",
            workflows=[ClarifyFlow, InteractionFlow],
            activities=[
                find_unclassified_items,
                classify_one,
                apply_outcome,
                log_classification,
                insert_interaction,
                send_interaction_card,
                resolve_interaction,
                apply_interaction_timeout,
            ],
        ):
            result = await client.execute_workflow(
                ClarifyFlow.run,
                ClarifyConfig(agent_id="sebas", max_items=10),
                id=f"clarify-spawn-{uuid.uuid4()}",
                task_queue="aegis-clarify-spawn-test",
            )
            assert result["found"] == 1
            assert result["applied"] == 0
            # Interactions counter == 1 means start_child_workflow returned
            # without error, i.e. the spawn was issued. ABANDON means the
            # child runs independently of this worker context, so we don't
            # block on it here — InteractionFlow's own behavior is covered
            # by tests/worker/test_interaction_flow.py.
            assert result["interactions"] == 1


@pytest.mark.asyncio
async def test_clarify_flow_no_spawn_when_applied_true() -> None:
    """Normal next_action path applies the commands and does NOT spawn a child."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        client = env.client
        from aegis_worker.flows.interaction import InteractionFlow
        from temporalio import activity

        spawn_counter = {"n": 0}

        @activity.defn(name="insert_interaction")
        async def insert_interaction(*a, **kw):
            spawn_counter["n"] += 1
            return {"interaction_id": "x"}

        @activity.defn(name="send_interaction_card")
        async def send_card(*a, **kw):
            return {"ok": True, "message_id": 0}

        @activity.defn(name="resolve_interaction")
        async def resolve(*a, **kw):
            return {"already_resolved": False}

        @activity.defn(name="apply_interaction_timeout")
        async def timeout(*a, **kw):
            return None

        @activity.defn(name="find_unclassified_items")
        async def find(max_items: int = 20):
            return [
                {
                    "id": "T_NORMAL",
                    "content": "x",
                    "source_tag": "#email",
                    "labels": ["#email"],
                    "description": None,
                    "latest_user_note": None,
                    "last_note_at": None,
                }
            ]

        @activity.defn(name="classify_one")
        async def classify(task: dict):
            return {
                "classification": "next_action",
                "confidence": 0.9,
                "assignee": "@sebas",
                "contexts": ["@email"],
                "reason": "x",
                "llm_model": "qwen3:14b",
                "source_tag": "#email",
            }

        @activity.defn(name="apply_outcome")
        async def apply(task: dict, decision: dict, pass_n: int = 1):
            return {
                "applied": True,
                "interaction_spawned": False,
                "interaction_payload": None,
                "commands_sent": 2,
                "outbox_queued": 0,
            }

        @activity.defn(name="log_classification")
        async def log(*a, **kw):
            return None

        async with Worker(
            client,
            task_queue="aegis-clarify-nospawn-test",
            workflows=[ClarifyFlow, InteractionFlow],
            activities=[
                find,
                classify,
                apply,
                log,
                insert_interaction,
                send_card,
                resolve,
                timeout,
            ],
        ):
            result = await client.execute_workflow(
                ClarifyFlow.run,
                ClarifyConfig(agent_id="sebas", max_items=10),
                id=f"clarify-nospawn-{uuid.uuid4()}",
                task_queue="aegis-clarify-nospawn-test",
            )
            assert result["applied"] == 1
            assert result["interactions"] == 0
            assert spawn_counter["n"] == 0


@pytest.mark.asyncio
async def test_clarify_flow_spawns_alert_investigation_for_pandora_path() -> None:
    """APP-<n>: detection path: apply_outcome returns spawn_kind=
    pandora_investigation; ClarifyFlow fires AlertInvestigationFlow as
    an abandoned child instead of InteractionFlow.

    The child's check_dedup is stubbed to return is_duplicate=True so
    AlertInvestigationFlow returns early — we only care that the spawn
    issued successfully (counter increment).
    """
    async with await WorkflowEnvironment.start_time_skipping() as env:
        client: Client = env.client
        from aegis_worker.flows.alert_investigation import AlertInvestigationFlow
        from temporalio import activity

        spawned_alerts: list[dict] = []

        @activity.defn(name="check_dedup")
        async def check_dedup(fingerprint: str, hours: int):
            # Capture the alert that was passed and short-circuit the
            # child workflow so we don't need to register every activity.
            return {"is_duplicate": True}

        @activity.defn(name="send_system_event")
        async def send_system_event(msg: str):
            return None

        @activity.defn(name="find_unclassified_items")
        async def find_unclassified_items(max_items: int = 20):
            return [
                {
                    "id": "T_APP_JIRA",
                    "content": "APP-12345: Portfolio valuation off by 0.05",
                    "source_tag": "#manual",
                    "labels": ["#manual"],
                    "description": "Spotted on 2026-05-20",
                    "latest_user_note": None,
                    "last_note_at": None,
                }
            ]

        @activity.defn(name="classify_one")
        async def classify_one(task: dict):
            return {
                "classification": "pandora_investigation",
                "confidence": 1.0,
                "assignee": "@pandora",
                "contexts": ["@deep", "@code"],
                "reason": "APP- prefix",
                "llm_model": "rules",
                "source_tag": "#manual",
            }

        @activity.defn(name="apply_outcome")
        async def apply_outcome(task: dict, decision: dict, pass_n: int = 1):
            spawned_alerts.append(task)
            return {
                "applied": True,
                "interaction_spawned": True,
                "interaction_payload": {
                    "spawn_kind": "pandora_investigation",
                    "alert": {
                        "title": task["content"],
                        "description": task.get("description") or "",
                        "source": "todoist-jira",
                        "service": "acme",
                        "severity": "normal",
                        "fingerprint": f"jira-{task['id']}",
                        "labels": {"alertname": task["content"], "service": "acme"},
                        "requires_approval": False,
                        "todoist_task_id": task["id"],
                        "resource_tag_filter": ["acme"],
                    },
                },
                "commands_sent": 1,
                "outbox_queued": 0,
            }

        @activity.defn(name="log_classification")
        async def log_classification(*a, **kw):
            return None

        async with Worker(
            client,
            task_queue="aegis-clarify-pandora-test",
            workflows=[ClarifyFlow, AlertInvestigationFlow],
            activities=[
                find_unclassified_items,
                classify_one,
                apply_outcome,
                log_classification,
                check_dedup,
                send_system_event,
            ],
        ):
            result = await client.execute_workflow(
                ClarifyFlow.run,
                ClarifyConfig(agent_id="sebas", max_items=10),
                id=f"clarify-pandora-spawn-{uuid.uuid4()}",
                task_queue="aegis-clarify-pandora-test",
            )
            assert result["found"] == 1
            assert result["applied"] == 1
            # The pandora spawn issued — counter increments.
            assert result["interactions"] == 1
            assert spawned_alerts[0]["id"] == "T_APP_JIRA"


@pytest.mark.asyncio
async def test_clarify_flow_pandora_unapplied_does_not_bump_watermark() -> None:
    """H4 regression — when apply_outcome returns interaction_spawned=True with
    spawn_kind=pandora_investigation BUT applied=False (e.g. non-retryable
    Todoist rejection against a stale projection), the flow must:

    1. Skip the AlertInvestigationFlow spawn (existing guard at clarify.py:147)
    2. Pass bump_watermark=False to log_classification so the task stays
       eligible for re-clarification on the next tick (otherwise the user's
       followup comment is silently consumed forever).
    """
    bumps_seen: list[bool] = []

    async with await WorkflowEnvironment.start_time_skipping() as env:
        client = env.client
        from temporalio import activity

        @activity.defn(name="find_unclassified_items")
        async def find_unclassified_items(max_items: int = 20):
            return [{
                "id": "T_GHOST_APP",
                "content": "APP-9999: bug",
                "source_tag": None,
                "labels": ["@pandora"],
                "description": None,
                "latest_user_note": "follow up please",
                "last_note_at": "2026-05-23T10:00:00+00:00",
            }]

        @activity.defn(name="classify_one")
        async def classify_one(task: dict):
            return {
                "classification": "pandora_followup",
                "confidence": 1.0,
                "assignee": "@pandora",
                "contexts": ["@deep", "@code"],
                "reason": "user comment on @pandora APP-",
                "llm_model": "rules",
            }

        @activity.defn(name="apply_outcome")
        async def apply_outcome(task: dict, decision: dict, pass_n: int = 1):
            # Simulate a non-retryable Todoist rejection: note posted against
            # a stale projection row → applied=False but the followup payload
            # is still set because the classifier wanted to spawn.
            return {
                "applied": False,
                "interaction_spawned": True,
                "interaction_payload": {
                    "spawn_kind": "pandora_investigation",
                    "alert": {
                        "title": task["content"],
                        "todoist_task_id": task["id"],
                        "fingerprint": "jira-T_GHOST_APP-followup-x",
                    },
                },
                "commands_sent": 1,
                "outbox_queued": 0,
            }

        @activity.defn(name="log_classification")
        async def log_classification(
            task_id: str,
            decision: dict,
            applied: bool,
            pass_n: int = 1,
            user_hint: str | None = None,
            bump_watermark: bool = True,
        ):
            bumps_seen.append(bump_watermark)
            return None

        async with Worker(
            client,
            task_queue="aegis-clarify-watermark-test",
            workflows=[ClarifyFlow],
            activities=[
                find_unclassified_items,
                classify_one,
                apply_outcome,
                log_classification,
            ],
        ):
            result = await client.execute_workflow(
                ClarifyFlow.run,
                ClarifyConfig(agent_id="sebas", max_items=10),
                id=f"clarify-unapplied-pandora-{uuid.uuid4()}",
                task_queue="aegis-clarify-watermark-test",
            )

    # Flow ran the one task, but didn't spawn (applied=False guard) and
    # didn't bump the watermark either (H4 invariant).
    assert result["found"] == 1
    assert result["applied"] == 0
    assert result["interactions"] == 0
    assert bumps_seen == [False], (
        f"watermark must stay NULL on unapplied pandora followup; got {bumps_seen}"
    )


@pytest.mark.asyncio
async def test_reference_verdict_demotes_when_complete_permanently_fails() -> None:
    """ingest_reference_to_ks returns ok BUT complete_reference_task is
    rejected permanently → flow falls through to reclassify_reference_to_reading
    so the user sees the task move to @to-read instead of being orphaned.

    Regression for the silent-failure path where ``complete_reference_task``
    returned ``{"completed": False, ...}`` and the dispatch helper ignored it
    (the task stayed open with the ``@reference`` label forever, while KS
    held the content).
    """
    async with await WorkflowEnvironment.start_time_skipping() as env:
        client = env.client
        from temporalio import activity

        calls: dict[str, list] = {"complete": [], "reclassify": []}

        @activity.defn(name="find_unclassified_items")
        async def find(max_items: int = 20):
            return [
                {
                    "id": "T_REF",
                    "content": "Some reference article",
                    "source_tag": "#chat",
                    "labels": ["@reference"],
                    "description": "https://example.com/article",
                    "latest_user_note": None,
                    "last_note_at": None,
                }
            ]

        @activity.defn(name="classify_one")
        async def classify(task: dict):
            return {
                "classification": "reference",
                "confidence": 0.95,
                "assignee": "@raphael",
                "contexts": [],
                "reason": "looks like a saved article",
                "llm_model": "qwen3:14b",
                "source_tag": "#chat",
            }

        @activity.defn(name="apply_outcome")
        async def apply(task: dict, decision: dict, pass_n: int = 1):
            return {
                "applied": True,
                "interaction_spawned": False,
                "interaction_payload": None,
                "commands_sent": 2,
                "outbox_queued": 0,
            }

        @activity.defn(name="log_classification")
        async def log(*a, **kw):
            return None

        @activity.defn(name="ingest_reference_to_ks")
        async def ingest(
            task_id: str,
            task_content: str,
            task_description: str,
            source_tag: str,
            latest_user_note: str | None,
        ):
            return {
                "status": "ok",
                "content_id": "c-123",
                "url": "https://example.com/article",
            }

        @activity.defn(name="complete_reference_task")
        async def complete(
            task_id: str,
            title: str,
            source_tag: str,
            content_id: str | None,
            url: str | None,
        ):
            calls["complete"].append(task_id)
            # Simulate the production silent-failure: Sync API rejected the
            # complete (e.g. ITEM_NOT_FOUND from a stale projection). NOT
            # retryable, so the dispatch helper must fall through to demote.
            return {
                "completed": False,
                "reason": "ITEM_NOT_FOUND",
                "retryable": False,
            }

        @activity.defn(name="reclassify_reference_to_reading")
        async def reclassify(
            task_id: str,
            title: str,
            source_tag: str,
            existing_labels: list[str],
            reason: str,
        ):
            calls["reclassify"].append((task_id, reason))
            return {"reclassified": True, "labels": ["@to-read"]}

        async with Worker(
            client,
            task_queue="aegis-clarify-ref-fallback",
            workflows=[ClarifyFlow],
            activities=[
                find,
                classify,
                apply,
                log,
                ingest,
                complete,
                reclassify,
            ],
        ):
            await client.execute_workflow(
                ClarifyFlow.run,
                ClarifyConfig(agent_id="sebas", max_items=10),
                id=f"clarify-ref-fallback-{uuid.uuid4()}",
                task_queue="aegis-clarify-ref-fallback",
            )

    assert calls["complete"] == ["T_REF"], (
        f"complete_reference_task must be tried first; got {calls['complete']}"
    )
    assert len(calls["reclassify"]) == 1, (
        f"permanent complete failure must trigger demotion; got {calls['reclassify']}"
    )
    assert calls["reclassify"][0][0] == "T_REF"
    assert "ITEM_NOT_FOUND" in calls["reclassify"][0][1]
