"""MeetingSweepFlow — notes mail the hourly triage never saw, filed anyway.

`GmailIngestFlow` fetches `is:unread`. Notes read on a phone before the hourly
run are never triaged, never fan out, and nothing notices. This flow ignores
both the read state and the account cursor, and files whatever has no `meeting`
row yet.
"""

from __future__ import annotations

import pytest
from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.gmail import FetchEmailsInput, FetchEmailsResult
    from aegis_worker.flows.meeting_notes import MeetingNotesFlow
    from aegis_worker.flows.meeting_sweep import MeetingSweepFlow, MeetingSweepInput

SENDERS = ["notes-vendor.example", "notes@other.example"]
CHANNELS = [
    {"identifier": "a@example.com", "config": {"label": "acct-a"}},
    {"identifier": "b@example.com", "config": {"label": "acct-b"}},
]
_calls: dict[str, list] = {"senders": [], "channels": [], "fetch": [], "unstored": []}


def _reset():
    for v in _calls.values():
        v.clear()


def _msg(mid: str) -> dict:
    return {
        "id": mid,
        "subject": f"Notes: {mid}",
        "snippet": "notes",
        "internal_date_ms": 1_788_000_000_000,
    }


@workflow.defn(name="ParkedFlow")
class ParkedFlow:
    """Squats on a workflow id and never finishes, so the sweep's child start
    hits the real server-side collision instead of a mocked one."""

    @workflow.run
    async def run(self) -> str:
        await workflow.wait_condition(lambda: False)
        return "never"


def _stubs(senders: list[str], per_account: dict[str, list[str]], stored=(), fail=()):
    @activity.defn(name="meeting_sender_addresses")
    async def sender_addresses() -> list[str]:
        _calls["senders"].append(True)
        return list(senders)

    @activity.defn(name="list_active_channels")
    async def list_channels(kind: str) -> list[dict]:
        _calls["channels"].append(kind)
        return CHANNELS

    @activity.defn(name="fetch_emails")
    async def fetch_emails(input: FetchEmailsInput) -> FetchEmailsResult:
        _calls["fetch"].append(
            {
                "account": input.account_label,
                "query": input.query,
                "since": input.since_cursor_ts,
                "max": input.max_results,
            }
        )
        if input.account_label in fail:
            raise ApplicationError("gmail token expired", non_retryable=True)
        return FetchEmailsResult(
            messages=[_msg(m) for m in per_account.get(input.account_label, [])],
            latest_internal_date_ms=0,
        )

    @activity.defn(name="unstored_meeting_messages")
    async def unstored(message_ids: list[str]) -> list[str]:
        _calls["unstored"].append(list(message_ids))
        return [m for m in message_ids if m not in set(stored)]

    # MeetingNotesFlow's own first activity — the children run ABANDONED
    # alongside the sweep, so keep them cheap and terminal.
    @activity.defn(name="fetch_meeting_document")
    async def fetch_doc(account_label: str, msg: dict) -> dict:
        return {
            "title": "x",
            "meeting_date": "",
            "doc_id": "",
            "doc_url": "",
            "doc_modified_time": "",
            "notes": "tiny",
            "transcript": [],
            "speakers": [],
            "doc_status": "no_link",
        }

    return [sender_addresses, list_channels, fetch_emails, unstored, fetch_doc]


async def _started(client, msg_id: str) -> bool:
    try:
        await client.get_workflow_handle(f"meeting-notes-{msg_id}").describe()
    except RPCError:
        return False
    return True


async def _run(
    wf_id: str,
    *,
    senders=SENDERS,
    per_account: dict[str, list[str]] | None = None,
    stored=(),
    fail=(),
    parked=(),
    check=(),
):
    per_account = per_account or {}
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=[MeetingSweepFlow, MeetingNotesFlow, ParkedFlow],
            activities=_stubs(senders, per_account, stored, fail),
        ),
    ):
        for wid in parked:
            await env.client.start_workflow(ParkedFlow.run, id=wid, task_queue="tq")
        result = await env.client.execute_workflow(
            MeetingSweepFlow.run,
            MeetingSweepInput(agent_id="sebas"),
            id=wf_id,
            task_queue="tq",
        )
        children = {mid: await _started(env.client, mid) for mid in check}
        return result, children


@pytest.mark.asyncio
async def test_no_tagged_sender_stops_before_it_reads_a_mailbox():
    """Inert on a fresh install: no rule carries the `meeting` tag, so the
    flow must not even look up the accounts, let alone query Gmail."""
    _reset()
    result, _ = await _run("msw-none", senders=[])
    assert result == {"status": "skipped", "reason": "no_meeting_senders"}
    assert _calls["channels"] == [] and _calls["fetch"] == []


@pytest.mark.asyncio
async def test_the_query_ignores_read_state_and_already_filed_notes_are_skipped():
    _reset()
    result, children = await _run(
        "msw-two-accounts",
        per_account={"acct-a": ["gm-1", "gm-2"], "acct-b": ["gm-3"]},
        stored=("gm-2",),
        check=("gm-1", "gm-2", "gm-3"),
    )

    assert result["status"] == "ok"
    assert result["senders"] == 2 and result["fetched"] == 3
    assert result["filed"] == 2 and result["already_running"] == 0
    assert children == {"gm-1": True, "gm-2": False, "gm-3": True}

    # One fetch per account, both with the same sender-derived query.
    assert [f["account"] for f in _calls["fetch"]] == ["acct-a", "acct-b"]
    query = _calls["fetch"][0]["query"]
    assert query.startswith("from:(")
    for sender in SENDERS:
        assert sender in query
    assert "newer_than:7d" in query
    # The whole point: read state and the account cursor are both ignored.
    assert "is:unread" not in query
    assert all(f["since"] is None for f in _calls["fetch"])
    assert all(f["max"] == 50 for f in _calls["fetch"])

    # The already-filed message was filtered out by the DB check, not by luck.
    assert _calls["unstored"] == [["gm-1", "gm-2"], ["gm-3"]]
    assert result["accounts"] == [
        {"account": "acct-a", "fetched": 2, "filed": 1, "already_running": 0},
        {"account": "acct-b", "fetched": 1, "filed": 1, "already_running": 0},
    ]


@pytest.mark.asyncio
async def test_one_dead_account_does_not_cost_the_others_their_sweep():
    _reset()
    result, children = await _run(
        "msw-one-dead",
        per_account={"acct-a": ["gm-4"], "acct-b": ["gm-5"]},
        fail=("acct-a",),
        check=("gm-4", "gm-5"),
    )

    assert result["status"] == "ok" and result["filed"] == 1
    assert children == {"gm-4": False, "gm-5": True}
    failed, ok = result["accounts"]
    assert failed["account"] == "acct-a" and failed["fetched"] == 0
    assert "gmail token expired" in failed["error"]
    assert ok == {"account": "acct-b", "fetched": 1, "filed": 1, "already_running": 0}


@pytest.mark.asyncio
async def test_a_child_the_hourly_path_already_started_is_counted_not_an_error():
    """The expected collision: the hourly run spawned `meeting-notes-<id>`
    seconds earlier and it is still going. That is the sweep working as
    designed, not a failure."""
    _reset()
    result, _ = await _run(
        "msw-already",
        per_account={"acct-a": ["gm-6", "gm-7"]},
        parked=("meeting-notes-gm-6",),
    )

    assert result["status"] == "ok"
    assert result["already_running"] == 1 and result["filed"] == 1
    assert result["accounts"][0] == {
        "account": "acct-a",
        "fetched": 2,
        "filed": 1,
        "already_running": 1,
    }


def test_registry_declares_the_flow_on_a_schedule_and_serves_its_activities():
    from pathlib import Path

    import aegis_worker.__main__ as main_mod  # noqa: F401 — import must not raise
    import yaml
    from aegis_worker.activities.meeting import MeetingActivities
    from aegis_worker.registry import FLOWS

    spec = next(s for s in FLOWS if s.flow is MeetingSweepFlow)
    assert spec.schedule_config is not None and spec.feature_flag is None
    cfg = spec.schedule_config({"agent_id": "sebas", "config": {}, "_settings": {}})
    assert cfg == MeetingSweepInput(agent_id="sebas", lookback_days=7, max_per_account=50)

    seed = Path(__file__).resolve().parents[3] / "config" / "seed" / "activities.yaml"
    rows = yaml.safe_load(seed.read_text())["activities"]
    assert any(r["workflow_type"] == "MeetingSweepFlow" for r in rows)

    assert hasattr(MeetingActivities, "meeting_sender_addresses")
    assert hasattr(MeetingActivities, "unstored_meeting_messages")
