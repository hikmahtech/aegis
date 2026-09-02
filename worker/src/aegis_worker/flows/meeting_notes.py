"""MeetingNotesFlow — one note-taker email → notes in the knowledge store,
a self-review, and speaking metrics in life.observations.

Spawned by GmailIngestFlow (fire-and-forget, ParentClosePolicy.ABANDON) when a
triage classification carries the `meeting` tag — the tag comes from a
sender-override rule on the Email triage page, so any vendor works.

  fetch_meeting_document(account, msg)   # Gmail body → Docs link → Drive export
    → ingest_content  source_type=meeting          (notes only, never transcript)
    → analyse_meeting(doc)                          (stats in code, one LLM review)
    → ingest_content  source_type=meeting_review

Every downgrade is a normal result, never a failure: the notes are filed
before the analysis runs, and the analysis is best-effort.
"""

from __future__ import annotations

from dataclasses import dataclass

from temporalio import workflow
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.content import ContentActivities
    from aegis_worker.shared.retry import (
        NO_RETRY,
        RETRY_ONCE,
        TIMEOUT_LLM,
        TIMEOUT_LONG,
        TIMEOUT_STANDARD,
    )

# Below this many characters of notes with no fetched doc there is nothing
# worth filing (a Read.ai "sign in to view" nag is ~180 chars).
_MIN_BODY = 200
_NOTES_CAP = 16_000


@dataclass
class MeetingNotesInput:
    agent_id: str
    msg: dict
    account_label: str


@workflow.defn(name="MeetingNotesFlow")
class MeetingNotesFlow:
    @workflow.run
    async def run(self, input: MeetingNotesInput) -> dict:
        msg_id = str(input.msg.get("id") or "")
        step = "fetch_meeting_document"
        try:
            doc = await workflow.execute_activity(
                "fetch_meeting_document",
                args=[input.account_label, input.msg],
                start_to_close_timeout=TIMEOUT_LONG,
                retry_policy=RETRY_ONCE,
            )
            doc["message_id"] = msg_id
            doc["account"] = input.account_label
            doc_status = doc.get("doc_status") or "fetch_failed"
            notes = (doc.get("notes") or "")[:_NOTES_CAP]
            if doc_status != "ok" and len(notes) < _MIN_BODY:
                return {"status": "skipped", "reason": "nothing_usable", "doc_status": doc_status}

            step = "ingest_meeting"
            url = (
                f"gdoc://{doc['doc_id']}"
                if doc_status == "ok"
                else input.msg.get("permalink") or f"https://mail.google.com/mail/u/0/#inbox/{msg_id}"
            )
            ingested = await workflow.execute_activity_method(
                ContentActivities.ingest_content,
                args=[
                    {
                        "url": url,
                        "title": doc.get("title") or input.msg.get("subject") or "Meeting notes",
                        "source_type": "meeting",
                        "raw_text": notes,
                        "tags": ["meeting"],
                        "metadata": {
                            "doc_id": doc.get("doc_id") or "",
                            "doc_url": doc.get("doc_url") or "",
                            "doc_status": doc_status,
                            "message_id": msg_id,
                            "account": input.account_label,
                            "meeting_date": doc.get("meeting_date") or "",
                            "speakers": list(doc.get("speakers") or []),
                        },
                    }
                ],
                start_to_close_timeout=TIMEOUT_STANDARD,
                retry_policy=RETRY_ONCE,
            )
            status = (ingested or {}).get("status")
            if status != "ok":
                return {
                    "status": "skipped",
                    "reason": f"ingest_{status or 'no_result'}",
                    "doc_status": doc_status,
                }
            content_id = (ingested or {}).get("content_id")

            step = "analyse_meeting"
            try:
                analysis = await workflow.execute_activity(
                    "analyse_meeting",
                    args=[doc],
                    start_to_close_timeout=TIMEOUT_LLM,
                    retry_policy=NO_RETRY,
                )
            except Exception as exc:  # noqa: BLE001 — notes are filed; review is best-effort
                workflow.logger.warning("meeting_analyse_failed msg_id=%s err=%s", msg_id, str(exc)[:200])
                analysis = {"skipped": "analysis_failed"}
            if not analysis or analysis.get("skipped"):
                return {
                    "status": "stored_no_analysis",
                    "analysis": (analysis or {}).get("skipped") or "no_result",
                    "doc_status": doc_status,
                    "content_id": content_id,
                    "url": url,
                }

            step = "ingest_review"
            review_key = doc.get("doc_id") or msg_id
            review_ingested = await workflow.execute_activity_method(
                ContentActivities.ingest_content,
                args=[
                    {
                        "url": f"aegis://meeting-review/{review_key}",
                        "title": f"Meeting review: {doc.get('title') or msg_id}",
                        "source_type": "meeting_review",
                        "raw_text": analysis.get("rendered") or "",
                        "tags": ["meeting_review"],
                        "metadata": {
                            "review": analysis.get("review") or {},
                            "stats": analysis.get("stats") or {},
                            "self_matched": bool(analysis.get("self_matched")),
                            "meeting_url": url,
                            "meeting_date": doc.get("meeting_date") or "",
                            "title": doc.get("title") or "",
                            "message_id": msg_id,
                            "account": input.account_label,
                        },
                    }
                ],
                start_to_close_timeout=TIMEOUT_STANDARD,
                retry_policy=RETRY_ONCE,
            )
            return {
                "status": "stored",
                "doc_status": doc_status,
                "content_id": content_id,
                "review_content_id": (review_ingested or {}).get("content_id"),
                "observations": analysis.get("observations", 0),
                "url": url,
            }
        except ApplicationError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ApplicationError(
                f"meeting_notes_failed at step={step}: {exc!r}", non_retryable=True
            ) from exc
