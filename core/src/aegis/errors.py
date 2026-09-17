"""A caught exception as text that is never empty.

httpx raises its timeout exceptions (`ReadTimeout`, `ConnectTimeout`, ...) with
no message, so `str(exc)` is "" and a log line reads `error=` with nothing after
it (#573, #575). `error_text` always names the exception's type, and adds its
message when there is one.

The worker imports this module. comms has no aegis-core dependency, so it keeps
its own copy in `aegis_comms/errors.py`; change the two together.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


@contextmanager
def logged_failure(event: str, *, logger: Any, field: str = "err") -> Iterator[None]:
    """Run a block; on any `Exception`, log `<event> <field>=<Type: message>`
    and carry on with whatever the caller already had.

    For the handler whose ENTIRE body is that one log — several hundred of
    them across the fleet, because the idiom here is catch-and-degrade. A
    handler that also writes state, re-raises, branches on the exception or
    says more than the event name keeps its own `except`: what it does is the
    interesting part, and hiding it behind a helper would make two different
    things look the same.

    `BaseException` is deliberately not caught: a cancellation is not a
    degradation.
    """
    try:
        yield
    except Exception as exc:  # noqa: BLE001 — degrading is the whole point
        logger.warning("%s " + field + "=%s", event, error_text(exc))


def error_text(exc: BaseException, limit: int = 200) -> str:
    """`Type: message`, or just `Type` when the message is empty, cut to `limit`.

    The limit covers the whole string, type included. Use it wherever a caught
    exception is reported: a log field, an error envelope returned to a tool or
    a model, a status field, a stored `last_error`. Keep `str(exc)` where the
    message is a contract: code that matches on it later, or text shown to a
    person word for word.
    """
    message = str(exc).strip()
    name = type(exc).__name__
    return (f"{name}: {message}" if message else name)[:limit]
