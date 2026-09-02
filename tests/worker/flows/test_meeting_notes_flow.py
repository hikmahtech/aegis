"""MeetingNotesFlow — fetch → file notes → analyse → file review."""

from __future__ import annotations

import pytest
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from aegis_worker.flows.meeting_notes import MeetingNotesFlow, MeetingNotesInput

_calls: dict[str, list] = {"fetch": [], "ingest": [], "analyse": [], "analyse_agent": []}


def _reset():
    for v in _calls.values():
        v.clear()


MSG = {"id": "gm-flow-1", "subject": "Notes: standup", "snippet": "x", "internal_date_ms": 1_788_000_000_000}
DOC_OK = {
    "title": "Standup – Notes by Gemini", "meeting_date": "2026-09-01T09:00:00+00:00",
    "doc_id": "doc-1", "doc_url": "https://docs.google.com/document/d/doc-1", "doc_modified_time": "m",
    "notes": "Notes " * 100, "transcript": [["Sam Doe", "hi"]], "speakers": ["Sam Doe"], "doc_status": "ok",
}


def _fetch(doc):
    @activity.defn(name="fetch_meeting_document")
    async def fetch(account_label: str, msg: dict) -> dict:
        _calls["fetch"].append((account_label, msg["id"]))
        return doc
    return fetch


@activity.defn(name="ingest_content")
async def ingest(item: dict) -> dict:
    _calls["ingest"].append(item)
    return {"status": "ok", "content_id": f"cid-{item['source_type']}"}


def _analyse(result):
    @activity.defn(name="analyse_meeting")
    async def analyse(doc: dict, agent_id: str = "") -> dict:
        _calls["analyse"].append(doc)
        _calls["analyse_agent"].append(agent_id)
        return result
    return analyse


async def _run(stubs, wf_id):
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(env.client, task_queue="tq", workflows=[MeetingNotesFlow], activities=stubs),
    ):
        return await env.client.execute_workflow(
            MeetingNotesFlow.run,
            MeetingNotesInput(agent_id="sebas", msg=MSG, account_label="acct"),
            id=wf_id,
            task_queue="tq",
        )


@pytest.mark.asyncio
async def test_ok_doc_files_notes_and_review():
    _reset()
    analysis = {"stats": {"self": {"matched": True}}, "observations": 3, "self_matched": True,
                "review": {"commitments": ["x"]}, "rendered": "# Meeting review: Standup"}
    res = await _run([_fetch(DOC_OK), ingest, _analyse(analysis)], "mn-ok")
    assert res["status"] == "stored" and res["doc_status"] == "ok"
    assert res["content_id"] == "cid-meeting" and res["review_content_id"] == "cid-meeting_review"
    assert _calls["fetch"] == [("acct", "gm-flow-1")]
    notes_item, review_item = _calls["ingest"]
    assert notes_item["url"] == "gdoc://doc-1" and notes_item["source_type"] == "meeting"
    assert notes_item["title"] == DOC_OK["title"] and notes_item["tags"] == ["meeting"]
    assert "transcript" not in notes_item["metadata"] and "hi" not in notes_item["raw_text"]
    assert notes_item["metadata"]["doc_status"] == "ok"
    assert notes_item["metadata"]["message_id"] == "gm-flow-1" and notes_item["metadata"]["account"] == "acct"
    assert notes_item["metadata"]["speakers"] == ["Sam Doe"]
    assert review_item["url"] == "aegis://meeting-review/doc-1"
    assert review_item["source_type"] == "meeting_review" and review_item["raw_text"] == "# Meeting review: Standup"
    assert review_item["metadata"]["meeting_url"] == "gdoc://doc-1"
    assert review_item["metadata"]["review"] == {"commitments": ["x"]}
    # The doc handed to analyse carries message_id + account for the observation key.
    assert _calls["analyse"][0]["message_id"] == "gm-flow-1" and _calls["analyse"][0]["account"] == "acct"
    # …and the flow's own agent, so llm_calls bills the review to whoever ran it.
    assert _calls["analyse_agent"] == ["sebas"]


@pytest.mark.asyncio
async def test_a_failed_review_ingest_is_reported_not_called_stored():
    _reset()

    @activity.defn(name="ingest_content")
    async def ingest_review_disabled(item: dict) -> dict:
        _calls["ingest"].append(item)
        if item["source_type"] == "meeting_review":
            return {"status": "disabled"}
        return {"status": "ok", "content_id": f"cid-{item['source_type']}"}

    analysis = {"stats": {}, "observations": 1, "self_matched": True,
                "review": {"commitments": []}, "rendered": "# Meeting review: Standup"}
    res = await _run([_fetch(DOC_OK), ingest_review_disabled, _analyse(analysis)], "mn-review-ingest")
    assert res["status"] == "stored_no_analysis"
    assert res["analysis"] == "review_ingest_disabled"
    assert res["doc_status"] == "ok" and res["content_id"] == "cid-meeting"
    assert res["url"] == "gdoc://doc-1"
    assert "review_content_id" not in res
    assert len(_calls["ingest"]) == 2


@pytest.mark.asyncio
async def test_an_ok_export_with_empty_notes_files_nothing():
    """A title-only row looks like a real meeting in the knowledge store."""
    _reset()
    doc = {**DOC_OK, "notes": "", "transcript": [], "speakers": []}
    res = await _run([_fetch(doc), ingest, _analyse({})], "mn-empty-ok")
    assert res == {"status": "skipped", "reason": "nothing_usable", "doc_status": "ok"}
    assert _calls["ingest"] == [] and _calls["analyse"] == []


@pytest.mark.asyncio
async def test_no_link_files_body_under_the_gmail_permalink_and_skipped_analysis_files_no_review():
    _reset()
    doc = {**DOC_OK, "doc_id": "", "doc_url": "", "doc_status": "no_link", "transcript": [], "speakers": []}
    res = await _run([_fetch(doc), ingest, _analyse({"skipped": "no_self_names", "stats": {}, "observations": 0})], "mn-nolink")
    assert res["status"] == "stored_no_analysis" and res["analysis"] == "no_self_names"
    assert res["doc_status"] == "no_link"
    assert len(_calls["ingest"]) == 1
    item = _calls["ingest"][0]
    assert item["url"] == "https://mail.google.com/mail/u/0/#inbox/gm-flow-1"
    assert item["title"] == DOC_OK["title"]
    assert item["metadata"]["doc_status"] == "no_link"


@pytest.mark.asyncio
async def test_nothing_usable_is_skipped_without_filing():
    _reset()
    doc = {**DOC_OK, "doc_id": "", "doc_status": "fetch_failed", "notes": "tiny", "transcript": []}
    res = await _run([_fetch(doc), ingest, _analyse({})], "mn-thin")
    assert res == {"status": "skipped", "reason": "nothing_usable", "doc_status": "fetch_failed"}
    assert _calls["ingest"] == [] and _calls["analyse"] == []


@pytest.mark.asyncio
async def test_analysis_activity_failure_degrades_to_stored_no_analysis():
    _reset()

    @activity.defn(name="analyse_meeting")
    async def boom(doc: dict) -> dict:
        raise RuntimeError("llm down")

    res = await _run([_fetch(DOC_OK), ingest, boom], "mn-boom")
    assert res["status"] == "stored_no_analysis" and res["analysis"] == "analysis_failed"
    assert len(_calls["ingest"]) == 1


def test_registry_declares_the_flow_and_main_serves_the_activities():
    import aegis_worker.__main__ as main_mod  # noqa: F401 — import must not raise
    from aegis_worker.registry import FLOWS

    assert any(spec.flow is MeetingNotesFlow and spec.schedule_config is None for spec in FLOWS)
    from aegis_worker.activities.meeting import MeetingActivities

    assert hasattr(MeetingActivities, "fetch_meeting_document")
    assert hasattr(MeetingActivities, "analyse_meeting")
