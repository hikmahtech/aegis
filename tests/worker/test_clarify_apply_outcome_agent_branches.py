"""apply_outcome tests for sebas_followup / raphael_followup / maou_followup."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from aegis_worker.activities.clarify import ClarifyActivities


def _task(labels: list[str], content: str = "Title here") -> dict:
    return {
        "id": "task-z",
        "content": content,
        "description": "Some description",
        "labels": labels,
        "source_tag": "#manual",
        "latest_user_note": "Comment from user.",
        "last_note_at": None,
    }


@pytest.mark.parametrize(
    "classification, target_agent",
    [
        ("sebas_followup", "sebas"),
        ("raphael_followup", "raphael"),
        ("maou_followup", "maou"),
    ],
)
@pytest.mark.asyncio
async def test_apply_outcome_agent_followup_returns_spawn_payload(
    db_pool, classification, target_agent
):
    """Each per-agent branch returns:
    - applied=True (no Todoist commands to fail)
    - interaction_spawned=True
    - interaction_payload with spawn_kind="agent_chat_reply" + target_agent
    - thread_id=`todoist-task-<id>`
    - no commands sent to Todoist
    """
    todoist = AsyncMock()  # MUST NOT be called
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=todoist)
    decision = {
        "classification": classification,
        "confidence": 1.0,
        "assignee": f"@{target_agent}",
        "contexts": ["@deep"],
        "reason": "test",
        "llm_model": "rules",
    }

    outcome = await acts.apply_outcome(_task([f"@{target_agent}", "#manual"]), decision)

    assert outcome["applied"] is True
    assert outcome["interaction_spawned"] is True
    payload = outcome["interaction_payload"]
    assert payload["spawn_kind"] == "agent_chat_reply"
    assert payload["target_agent"] == target_agent
    assert payload["task_id"] == "task-z"
    assert payload["thread_id"] == "todoist-task-task-z"
    assert "Comment from user." in payload["synthetic_input"]
    assert "Title here" in payload["synthetic_input"]
    assert outcome["commands_sent"] == 0
    assert outcome["outbox_queued"] == 0
    todoist.commands.assert_not_called()


@pytest.mark.asyncio
async def test_apply_outcome_pandora_chat_followup_returns_spawn_payload(db_pool):
    """pandora_chat_followup routes through the shared agent_chat_reply
    spawn but maps to the personality id `pandoras-actor` (not `pandora`,
    which is just a label prefix). The label on the task stays @pandora
    — only the spawn payload's target_agent differs.
    """
    todoist = AsyncMock()  # MUST NOT be called
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=todoist)
    decision = {
        "classification": "pandora_chat_followup",
        "confidence": 1.0,
        "assignee": "@pandora",
        "contexts": ["@deep"],
        "reason": "user comment on @pandora non-APP task",
        "llm_model": "rules",
    }

    outcome = await acts.apply_outcome(_task(["@pandora", "#manual"]), decision)

    assert outcome["applied"] is True
    assert outcome["interaction_spawned"] is True
    payload = outcome["interaction_payload"]
    assert payload["spawn_kind"] == "agent_chat_reply"
    assert payload["target_agent"] == "pandoras-actor"
    assert payload["task_id"] == "task-z"
    assert payload["thread_id"] == "todoist-task-task-z"
    assert "Comment from user." in payload["synthetic_input"]
    assert "Title here" in payload["synthetic_input"]
    assert outcome["commands_sent"] == 0
    assert outcome["outbox_queued"] == 0
    todoist.commands.assert_not_called()


@pytest.mark.asyncio
async def test_apply_outcome_threads_recent_comments_into_synthetic_input(db_pool):
    """Pin (2026-05-27): synthetic_input must include the recent comment
    thread on the Todoist task — both user notes AND prior agent
    replies — so the spawned chat reply can see what it already said
    and avoid repeating itself. Without this, pandora generated
    near-duplicate replies every clarify tick.
    """
    import datetime as _dt
    from unittest.mock import patch

    fetched = [
        {
            "posted_at": _dt.datetime(2026, 5, 27, 9, 40, tzinfo=_dt.UTC),
            "content": "Look into the bcp double-suffix bug @pandora",
        },
        {
            "posted_at": _dt.datetime(2026, 5, 27, 16, 15, tzinfo=_dt.UTC),
            "content": (
                "[Agent reply @ 16:15 UTC agent=pandoras-actor]\n"
                "Looked at the code; the fix is in repo_screener/models.py "
                "— let me try via remote kimi."
            ),
        },
        {
            "posted_at": _dt.datetime(2026, 5, 27, 16, 30, tzinfo=_dt.UTC),
            "content": "Comment from user.",
        },
    ]

    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=AsyncMock())
    decision = {
        "classification": "pandora_chat_followup",
        "confidence": 1.0,
        "assignee": "@pandora",
        "contexts": ["@deep"],
        "reason": "user comment on @pandora non-APP task",
        "llm_model": "rules",
    }

    async def fake_fetch(self, task_id, limit=15):
        assert task_id == "task-z"
        return fetched

    with patch.object(ClarifyActivities, "_fetch_recent_task_notes", new=fake_fetch):
        outcome = await acts.apply_outcome(_task(["@pandora", "#manual"]), decision)

    synthetic = outcome["interaction_payload"]["synthetic_input"]
    # Header still present
    assert "User commented on Todoist task task-z" in synthetic
    # Transcript section appears, oldest-first
    assert "Recent comment thread on this task" in synthetic
    assert "Look into the bcp double-suffix bug" in synthetic
    # Prior agent reply visible to the agent (with its prefix preserved
    # so it can identify its own past turns)
    assert "agent=pandoras-actor" in synthetic
    assert "let me try via remote kimi" in synthetic
    # Latest user comment still highlighted at the end
    assert synthetic.rstrip().endswith("Comment from user.")
    # Oldest comes before newest in the transcript
    assert synthetic.index("Look into the bcp") < synthetic.index("agent=pandoras-actor")


@pytest.mark.asyncio
async def test_apply_outcome_synthetic_input_works_when_no_recent_notes(db_pool):
    """Edge case: _fetch_recent_task_notes returns [] (DB error or no
    notes). The synthetic input must still build cleanly without a
    transcript section.
    """
    from unittest.mock import patch

    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=AsyncMock())
    decision = {
        "classification": "sebas_followup",
        "confidence": 1.0,
        "assignee": "@sebas",
        "contexts": ["@deep"],
        "reason": "",
        "llm_model": "rules",
    }

    async def fake_fetch_empty(self, task_id, limit=15):
        return []

    with patch.object(ClarifyActivities, "_fetch_recent_task_notes", new=fake_fetch_empty):
        outcome = await acts.apply_outcome(_task(["@sebas", "#manual"]), decision)

    synthetic = outcome["interaction_payload"]["synthetic_input"]
    assert "User commented on Todoist task task-z" in synthetic
    assert "Comment from user." in synthetic
    # No transcript section when there are no notes
    assert "Recent comment thread" not in synthetic


@pytest.mark.asyncio
async def test_apply_outcome_raphael_pre_fetches_ks_context(db_pool):
    """Raphael's branch calls _maybe_attach_ks_context; on success the
    synthetic_input includes 'Existing knowledge:'."""
    from unittest.mock import patch

    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=AsyncMock())
    decision = {
        "classification": "raphael_followup",
        "confidence": 1.0,
        "assignee": "@raphael",
        "contexts": ["@deep"],
        "reason": "",
        "llm_model": "rules",
    }

    async def fake_attach_ks(synthetic, task):
        return synthetic + "\n\nExisting knowledge:\n- prior note 1\n"

    with patch.object(ClarifyActivities, "_maybe_attach_ks_context", side_effect=fake_attach_ks):
        outcome = await acts.apply_outcome(_task(["@raphael", "#manual"]), decision)

    assert "Existing knowledge:" in outcome["interaction_payload"]["synthetic_input"]


@pytest.mark.asyncio
async def test_apply_outcome_maou_pre_fetches_transaction_context(db_pool):
    from unittest.mock import patch

    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=AsyncMock())
    decision = {
        "classification": "maou_followup",
        "confidence": 1.0,
        "assignee": "@maou",
        "contexts": ["@deep"],
        "reason": "",
        "llm_model": "rules",
    }

    async def fake_attach_tx(synthetic, task):
        return synthetic + "\n\nRecent receipts:\n- 2026-05-01 Vercel $20\n"

    with patch.object(
        ClarifyActivities, "_maybe_attach_transaction_context", side_effect=fake_attach_tx
    ):
        outcome = await acts.apply_outcome(_task(["@maou", "#receipt"]), decision)

    assert "Recent receipts:" in outcome["interaction_payload"]["synthetic_input"]


@pytest.mark.asyncio
async def test_apply_outcome_sebas_has_no_prefetch_hook(db_pool):
    """Sebas branch does NOT call any pre-fetch — his context IS the task."""
    from unittest.mock import patch

    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=AsyncMock())
    decision = {
        "classification": "sebas_followup",
        "confidence": 1.0,
        "assignee": "@sebas",
        "contexts": ["@deep"],
        "reason": "",
        "llm_model": "rules",
    }

    with (
        patch.object(ClarifyActivities, "_maybe_attach_ks_context") as mk_ks,
        patch.object(ClarifyActivities, "_maybe_attach_transaction_context") as mk_tx,
    ):
        outcome = await acts.apply_outcome(_task(["@sebas", "#manual"]), decision)

    mk_ks.assert_not_called()
    mk_tx.assert_not_called()
    assert "Comment from user." in outcome["interaction_payload"]["synthetic_input"]


@pytest.mark.asyncio
async def test_apply_outcome_maou_real_hook_returns_unchanged_when_no_receipts():
    """When finance.receipt_email returns no rows, the real hook returns the
    synthetic_input unchanged (no 'Recent receipts:' section). Uses a
    mock pool whose connection returns an empty list so the test is
    independent of real DB state.
    """
    from unittest.mock import AsyncMock as _AsyncMock
    from unittest.mock import MagicMock

    # Build a mock pool whose acquire() context manager returns a conn
    # whose fetch() returns an empty list.
    mock_conn = _AsyncMock()
    mock_conn.fetch = _AsyncMock(return_value=[])
    mock_cm = MagicMock()
    mock_cm.__aenter__ = _AsyncMock(return_value=mock_conn)
    mock_cm.__aexit__ = _AsyncMock(return_value=None)
    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=mock_cm)

    acts = ClarifyActivities(db_pool=mock_pool, todoist_connector=AsyncMock())
    decision = {
        "classification": "maou_followup",
        "confidence": 1.0,
        "assignee": "@maou",
        "contexts": ["@deep"],
        "reason": "",
        "llm_model": "rules",
    }
    # Use source_tag="#receipt" so the hook reaches the DB query path
    # (rather than returning early on the source_tag guard). The mocked
    # conn returns [] so no "Recent receipts:" section is appended.
    task = {
        "id": "task-z",
        "content": "Title here",
        "description": "Some description",
        "labels": ["@maou", "#receipt"],
        "source_tag": "#receipt",
        "latest_user_note": "Comment from user.",
        "last_note_at": None,
    }

    outcome = await acts.apply_outcome(task, decision)

    # No prefetch section appended when query returns 0 rows.
    assert "Recent receipts:" not in outcome["interaction_payload"]["synthetic_input"]
    # But the synthetic_input is still well-formed.
    assert "Comment from user." in outcome["interaction_payload"]["synthetic_input"]


@pytest.mark.asyncio
async def test_maybe_attach_ks_context_calls_real_search_method():
    """Regression: clarify.py:678 called `knowledge_connector.search_knowledge(...)`
    but `KnowledgeConnector` only has `.search(...)`. The bug was hidden
    because the wrapper-level `_maybe_attach_ks_context` was the patched
    surface in every other test, so the inner method name was never
    exercised. The AttributeError was then swallowed by the broad-except
    soft-fail path, leaving raphael's reply with no KS context grounding.

    Found in prod 2026-05-26 — log line:
        WARNING ... agent_chat_ks_prefetch_failed task_id=6ghxpGJ7q2j5P6mv
        err='KnowledgeConnector' object has no attribute 'search_knowledge'

    `spec=KnowledgeConnector` pins this — any wrong method name raises
    AttributeError instead of being absorbed by MagicMock auto-attrs.
    """
    from unittest.mock import MagicMock

    from aegis.services.knowledge import KnowledgeStore

    kc = MagicMock(spec=KnowledgeStore)
    kc.search = AsyncMock(
        return_value=[
            {"title": "Indo-European Religion Notes", "url": "http://ks/123"},
            {"title": "Aryan Migration Overview", "url": "http://ks/456"},
        ]
    )

    acts = ClarifyActivities(
        db_pool=None,
        todoist_connector=AsyncMock(),
        knowledge_connector=kc,
    )
    task = {
        "id": "task-x",
        "content": "Do investigation about religion of indo europeans",
        "labels": ["@raphael"],
    }

    result = await acts._maybe_attach_ks_context("base input", task)

    kc.search.assert_called_once_with(
        query="Do investigation about religion of indo europeans",
        limit=3,
    )
    assert "Existing knowledge:" in result, (
        "real KS search succeeded but the rendered section is missing — "
        "either the method name regressed or the rendering path broke"
    )
    assert "Indo-European Religion Notes" in result
    assert "Aryan Migration Overview" in result


@pytest.mark.asyncio
async def test_maybe_attach_ks_context_returns_input_when_connector_unset():
    """None-guard branch — no connector means no prefetch attempt, no error."""
    acts = ClarifyActivities(
        db_pool=None,
        todoist_connector=AsyncMock(),
        knowledge_connector=None,
    )
    task = {"id": "t", "content": "anything", "labels": ["@raphael"]}
    result = await acts._maybe_attach_ks_context("base input", task)
    assert result == "base input"
