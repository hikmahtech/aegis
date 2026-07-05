"""Issue #36 — GmailIngestFlow's financial fan-out is addressed to whichever
agent holds the `finance` tag (resolved once per run), not the literal "maou".
When no agent holds `finance`, the MoneyProcessFlow fan-out is skipped.
"""

from __future__ import annotations

import uuid

import pytest
from aegis_worker.flows.gmail_ingest import GmailIngestFlow, GmailIngestInput
from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

# NOTE: this module is re-imported inside the Temporal workflow sandbox to
# validate _FakeMoneyProcessFlow, so it must NOT import sandbox-restricted
# modules at top level (e.g. aegis_worker.activities.gmail → googleapiclient).
# The fake child runs in the sandbox, so we observe the agent_id it received
# via its RETURN VALUE (module globals mutated inside the sandbox don't escape).


@workflow.defn(name="MoneyProcessFlow")
class _FakeMoneyProcessFlow:
    @workflow.run
    async def run(self, inp) -> dict:
        # inp is the MoneyProcessInput dataclass deserialized to a dict.
        agent_id = inp["agent_id"] if isinstance(inp, dict) else inp.agent_id
        return {"agent_id": agent_id}


def _stubs(resolve_map):
    @activity.defn(name="list_active_channels")
    async def list_channels(kind):
        return [
            {"identifier": "acct@example.com", "config": {"label": "acct", "last_cursor_ts": None}}
        ]

    @activity.defn(name="resolve_agents")
    async def resolve_agents(tags):
        return {t: resolve_map.get(t) for t in tags}

    @activity.defn(name="fetch_emails")
    async def fetch_emails(*a, **k):
        # Temporal coerces this dict into the activity's result_type
        # (FetchEmailsResult) on the flow side.
        return {
            "messages": [
                {"id": "m1", "thread_id": "", "subject": "Receipt", "sender": "shop@x.com"}
            ],
            "latest_internal_date_ms": 0,
        }

    @activity.defn(name="ingest_idempotency_claim")
    async def claim(source, ext_id):
        return True  # fresh

    @activity.defn(name="classify_email")
    async def classify(msg, thread):
        return {
            "category": "informational",
            "confidence": 0.9,
            "tags": ["financial"],
            "source": "cache",
        }

    @activity.defn(name="record_triage_outcome")
    async def record_triage(*a, **k):
        return None

    @activity.defn(name="update_channel_config_key")
    async def update_key(*a, **k):
        return None

    @activity.defn(name="apply_label")
    async def apply_label(*a, **k):
        return {"ok": True}

    return [
        list_channels,
        resolve_agents,
        fetch_emails,
        claim,
        classify,
        record_triage,
        update_key,
        apply_label,
    ]


async def _run(resolve_map, agent_id):
    """Run the flow over one financial email; return the fan-out child's
    received agent_id, or None if no MoneyProcessFlow child was spawned."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        client: Client = env.client
        async with Worker(
            client,
            task_queue=f"gmail-{uuid.uuid4()}",
            workflows=[GmailIngestFlow, _FakeMoneyProcessFlow],
            activities=_stubs(resolve_map),
        ) as w:
            await client.execute_workflow(
                GmailIngestFlow.run,
                GmailIngestInput(agent_id=agent_id),
                id=f"gmail-{uuid.uuid4()}",
                task_queue=w.task_queue,
            )
            # The money fan-out is a fire-and-forget ABANDON child, so the
            # parent completing doesn't wait for it. Drive it to completion (if
            # one was spawned) and read the agent_id it received.
            try:
                res = await client.get_workflow_handle("money-process-m1").result()
                return res["agent_id"]
            except Exception:
                return None  # no child spawned (the skip case)


@pytest.mark.asyncio
async def test_financial_fanout_uses_resolved_finance_agent():
    """A renamed finance agent (not 'maou') receives the MoneyProcessFlow spawn."""
    spawned_agent = await _run({"finance": "money-agent"}, agent_id="sebas")
    assert spawned_agent == "money-agent"


@pytest.mark.asyncio
async def test_financial_fanout_skipped_when_no_finance_agent():
    """No agent holds `finance` → the money fan-out is skipped entirely."""
    spawned_agent = await _run({}, agent_id="sebas")
    assert spawned_agent is None
