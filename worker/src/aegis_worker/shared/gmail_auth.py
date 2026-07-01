"""Shared Gmail/Calendar OAuth auth-expiry detection."""

from __future__ import annotations


def is_auth_expired(exc: BaseException) -> bool:
    """Walk the exception cause chain to detect a gmail_auth_expired sentinel.

    Temporal wraps the worker-side ApplicationError inside an ActivityError
    (message = "Activity task failed"), so `str(exc)` alone won't contain the
    sentinel string. We walk __cause__ until we find it or exhaust the chain.

    Shared by GmailIngestFlow, CalendarIngestFlow and ReceiptIngestFlow —
    calendar fetches raise GmailAuthExpiredError (shared with Gmail OAuth).
    """
    current: BaseException | None = exc
    while current is not None:
        if "gmail_auth_expired" in str(current):
            return True
        current = current.__cause__
    return False
