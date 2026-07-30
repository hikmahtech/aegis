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
    result = await _act(gmail).triage_email(
        "te-1", "Arshad, here's a Pulse survey for you", "msg-1"
    )
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
    result = await _act(gmail).triage_email(
        "te-3", "Arshad, here's a Pulse survey for you", "msg-3"
    )
    assert result["action"] == "not_found"


async def test_missing_message_id_is_needs_human_not_archive():
    """Without an id we cannot verify what we'd archive, so never guess."""
    gmail = _Gmail(owner="arshad-stpd")
    result = await _act(gmail).triage_email(
        "te-4", "Arshad, here's a Pulse survey for you", ""
    )
    assert result["action"] == "needs_human"
    assert gmail.labelled == []
