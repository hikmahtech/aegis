"""fetch_thread's max_chars budget.

The default keeps the classifier's prompt small. `email_task_links` rules need
the whole message: Jira front-loads ~2400 chars of tracking URLs before the
field table that says whether the ticket was resolved, so at the old fixed
600-per-message cap those rules could never match anything.
"""

from __future__ import annotations

import pytest
from aegis_worker.activities.gmail import GmailActivities
from temporalio.testing import ActivityEnvironment

BOILERPLATE = "https://stocky.atlassian.net/browse/APP-1234?atlOrigin=eyJpIjoi\n" * 60
PAYLOAD = BOILERPLATE + "Resolution : DoneStatus : Deployed\n"


class _FakeSvc:
    """Minimal stand-in for the googleapiclient chain fetch_thread walks."""

    def users(self):
        return self

    def threads(self):
        return self

    def get(self, **_kw):
        return self

    def execute(self):
        return {
            "messages": [
                {"payload": {"mimeType": "text/plain", "body": {"data": _b64(PAYLOAD)}}}
            ]
        }


def _b64(text: str) -> str:
    import base64

    return base64.urlsafe_b64encode(text.encode()).decode()


@pytest.fixture
def acts(monkeypatch, tmp_path):
    (tmp_path / "acct.json").write_text("{}")
    monkeypatch.setattr(
        "aegis_worker.activities.gmail._build_gmail_service", lambda *_a: _FakeSvc()
    )
    return GmailActivities(gmail_credentials_file="c.json", gmail_token_dir=str(tmp_path))


@pytest.mark.asyncio
async def test_default_budget_still_truncates_for_the_classifier(acts):
    body = await ActivityEnvironment().run(acts.fetch_thread, "acct", "th-1")
    assert len(body) <= 2000
    # The discriminator is out of reach at the default budget — by design.
    assert "Resolution" not in body


@pytest.mark.asyncio
async def test_raised_budget_reaches_the_field_table(acts):
    body = await ActivityEnvironment().run(acts.fetch_thread, "acct", "th-1", 20000)
    assert "Resolution : DoneStatus : Deployed" in body
    assert body.index("Resolution") > 2000, "sample must reproduce the real burial depth"


# Issue #339: the char budget must be spent on the newest message first.


class _FakeThreadSvc:
    def __init__(self, messages):
        self._messages = messages

    def users(self):
        return self

    def threads(self):
        return self

    def get(self, **_kw):
        return self

    def execute(self):
        return {"messages": self._messages}


OLD_LINE = "OLD-FILLER " * 120  # long enough that text[:per_message] is ~666 chars
NEWEST_MARKER = "NEWEST: card declined for order 4711"


@pytest.mark.asyncio
async def test_default_budget_keeps_the_newest_message(monkeypatch, tmp_path):
    old_messages = [
        {"payload": {"mimeType": "text/plain", "body": {"data": _b64(OLD_LINE)}}}
        for _ in range(4)
    ]
    newest = {
        "payload": {
            "mimeType": "text/plain",
            "body": {"data": _b64(NEWEST_MARKER + " " + OLD_LINE)},
        }
    }
    messages = old_messages + [newest]
    (tmp_path / "acct.json").write_text("{}")
    monkeypatch.setattr(
        "aegis_worker.activities.gmail._build_gmail_service",
        lambda *_a: _FakeThreadSvc(messages),
    )
    local_acts = GmailActivities(
        gmail_credentials_file="c.json", gmail_token_dir=str(tmp_path)
    )
    body = await ActivityEnvironment().run(local_acts.fetch_thread, "acct", "th-1")
    assert NEWEST_MARKER in body
    assert len(body) <= 2000
    if "OLD-FILLER" in body:
        assert body.index("OLD-FILLER") < body.index(NEWEST_MARKER)
