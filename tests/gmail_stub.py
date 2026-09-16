"""One stand-in for the googleapiclient Gmail `build()` result.

`FakeGmailService` serves both callers. Hand it a flat list of message dicts,
or a `{label: [messages]}` map plus a `configure(label)` call before each read
— which is how the ingest e2e drives one service across two mailboxes.

`get()` answers a message fetch and a thread fetch from the same payload: the
message itself, wrapped in the `{"messages": [...]}` shape a thread read
expects. `modify_calls` keeps every `messages().modify()` kwargs verbatim, so a
test can assert the wire payload rather than merely that a call happened, and
`raise_auth` makes every request fail the way an expired refresh token does.
"""

from __future__ import annotations

from typing import Any


class FakeGmailRequest:
    def __init__(self, payload: Any, raise_auth: bool = False):
        self._payload = payload
        self._raise_auth = raise_auth

    def execute(self):
        if self._raise_auth:
            from google.auth.exceptions import RefreshError

            raise RefreshError("invalid_grant")
        return self._payload


class FakeLabelsEndpoint:
    """Stand-in for svc.users().labels()."""

    def __init__(self, labels: list[dict]):
        self._labels = labels

    def list(self, **kwargs):
        return FakeGmailRequest({"labels": self._labels})


class FakeGmailService:
    def __init__(
        self,
        messages: list[dict] | dict[str, list[dict]],
        raise_auth: bool = False,
        labels: list[dict] | None = None,
    ):
        self._messages = messages
        self._raise_auth = raise_auth
        # A test opts in to lane derivation by setting `labelIds:
        # ["Label_forwarded_acme"]` on a message and supplying a matching
        # label dict here.
        self._labels = labels or []
        self._current_label: str | None = None
        self.modify_calls: list[dict] = []

    def configure(self, label: str) -> None:
        """Pick which mailbox the next read answers from (map form only)."""
        self._current_label = label

    def _current(self) -> list[dict]:
        if isinstance(self._messages, dict):
            return self._messages.get(self._current_label, [])
        return self._messages

    def users(self):
        return self

    def messages(self):
        return self

    def threads(self):
        return self

    def labels(self):
        return FakeLabelsEndpoint(self._labels)

    def list(self, **kwargs):
        return FakeGmailRequest(
            {"messages": [{"id": m["id"]} for m in self._current()]},
            self._raise_auth,
        )

    def get(self, id="", **kwargs):  # noqa: A002 — the API's own parameter name
        match = next((m for m in self._current() if m["id"] == id), None)
        if match is None:
            return FakeGmailRequest({"messages": []}, self._raise_auth)
        return FakeGmailRequest({**match, "messages": [match]}, self._raise_auth)

    def modify(self, **kwargs):
        self.modify_calls.append(kwargs)
        return FakeGmailRequest({"id": kwargs.get("id", "")}, self._raise_auth)
