"""GmailIngestFlow — a `meeting`-tagged classification starts MeetingNotesFlow
and skips the truncated email copy; an untagged one does neither."""

from __future__ import annotations

import pytest
from temporalio import activity, workflow
from temporalio.client import WorkflowHandle
from temporalio.service import RPCError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.gmail import FetchEmailsInput, FetchEmailsResult
    from aegis_worker.flows.gmail_ingest import GmailIngestFlow, GmailIngestInput
    from aegis_worker.flows.meeting_notes import MeetingNotesFlow

MSG = {
    "id": "gm-fan-1",
    "thread_id": "t1",
    "sender": "notes@example.com",
    "subject": "Notes: standup",
    "to": "",
    "date": "",
    "snippet": "notes",
    "internal_date_ms": 1_788_000_000_000,
    "labels": [],
    "lane": "own",
}
_calls: dict[str, list] = {"ingest_email_to_kg": [], "fetch_meeting_document": []}


def _reset():
    for v in _calls.values():
        v.clear()


def _stubs(tags: list[str]):
    @activity.defn(name="list_active_channels")
    async def list_active_channels(kind: str) -> list[dict]:
        return [{"identifier": "me@example.com", "config": {"label": "acct"}}]

    @activity.defn(name="resolve_agents")
    async def resolve_agents(tags_: list[str]) -> dict:
        return {"finance": None}

    @activity.defn(name="fetch_emails")
    async def fetch_emails(input: FetchEmailsInput) -> FetchEmailsResult:
        return FetchEmailsResult(messages=[MSG], latest_internal_date_ms=MSG["internal_date_ms"])

    @activity.defn(name="ingest_idempotency_claim")
    async def claim(source_type: str, external_id: str) -> bool:
        return True

    @activity.defn(name="fetch_thread")
    async def fetch_thread(account_label: str, thread_id: str, max_chars: int = 2000) -> str:
        return "Notes from the standup " * 20

    @activity.defn(name="classify_email")
    async def classify_email(msg: dict, thread_content: str = "") -> dict:
        return {
            "category": "important_read",
            "confidence": 1.0,
            "tags": tags,
            "reason": "",
            "summary": "",
            "lane": "own",
            "source": "override",
        }

    @activity.defn(name="record_triage_outcome")
    async def record_triage_outcome(
        email_id: str, predicted: str, labels: list, account_label: str = ""
    ) -> dict:
        return {"recorded": True}

    @activity.defn(name="enrich_people_from_email")
    async def enrich(msg: dict) -> dict:
        return {}

    @activity.defn(name="link_email_to_task")
    async def link(msg: dict, body: str = "") -> dict:
        return {"applied": False}

    @activity.defn(name="apply_label")
    async def apply_label(account_label: str, message_id: str, label: str) -> dict:
        # The real activity returns {"ok": bool}; the flow reads `.get("ok")`.
        return {"ok": True}

    @activity.defn(name="ingest_email_to_kg")
    async def ingest_email_to_kg(msg: dict, thread_content: str, classification: dict) -> dict:
        _calls["ingest_email_to_kg"].append(msg["id"])
        return {"ingested": True}

    @activity.defn(name="is_message_unread")
    async def unread(account_label: str, message_id: str) -> bool:
        return True

    @activity.defn(name="gather_email_context")
    async def ctx(subject: str, sender: str, exclude_url: str = "") -> str:
        return ""

    @activity.defn(name="update_channel_config_key")
    async def update_key(kind: str, identifier: str, key: str, value) -> None:
        return None

    @activity.defn(name="recheck_triage_outcomes")
    async def recheck(account_label: str, limit: int = 50) -> dict:
        return {}

    @activity.defn(name="send_system_event")
    async def send_event(message: str, chat_id: int = 0) -> dict:
        return {}

    @activity.defn(name="capture_to_inbox")
    async def capture(
        source_tag: str, external_id: str, title: str, description: str | None = None
    ) -> str | None:
        return None

    # MeetingNotesFlow's own activities — it runs ABANDONED alongside the parent.
    @activity.defn(name="fetch_meeting_document")
    async def fetch_doc(account_label: str, msg: dict) -> dict:
        _calls["fetch_meeting_document"].append(msg["id"])
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

    return [
        list_active_channels,
        resolve_agents,
        fetch_emails,
        claim,
        fetch_thread,
        classify_email,
        record_triage_outcome,
        enrich,
        link,
        apply_label,
        ingest_email_to_kg,
        unread,
        ctx,
        update_key,
        recheck,
        send_event,
        capture,
        fetch_doc,
    ]


async def _run(tags: list[str], wf_id: str):
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=[GmailIngestFlow, MeetingNotesFlow],
            activities=_stubs(tags),
        ),
    ):
        result = await env.client.execute_workflow(
            GmailIngestFlow.run,
            GmailIngestInput(agent_id="sebas"),
            id=wf_id,
            task_queue="tq",
        )
        handle: WorkflowHandle = env.client.get_workflow_handle(f"meeting-notes-{MSG['id']}")
        try:
            desc = await handle.describe()
            child_started = desc is not None
        except RPCError:
            child_started = False
        return result, child_started


@pytest.mark.asyncio
async def test_meeting_tag_starts_child_and_skips_email_copy():
    _reset()
    result, child_started = await _run(["meeting"], "gi-meeting")
    assert result["processed"] == 1
    assert child_started is True
    assert _calls["ingest_email_to_kg"] == []


@pytest.mark.asyncio
async def test_without_meeting_tag_email_copy_is_filed_and_no_child_starts():
    _reset()
    result, child_started = await _run(["work"], "gi-plain")
    assert result["processed"] == 1
    assert child_started is False
    assert _calls["ingest_email_to_kg"] == ["gm-fan-1"]
