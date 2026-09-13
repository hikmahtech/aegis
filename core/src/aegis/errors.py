"""A caught exception as text that is never empty.

httpx raises its timeout exceptions (`ReadTimeout`, `ConnectTimeout`, ...) with
no message, so `str(exc)` is "" and a log line reads `error=` with nothing after
it (#573, #575). `error_text` always names the exception's type, and adds its
message when there is one.

The worker imports this module. comms has no aegis-core dependency, so it keeps
its own copy in `aegis_comms/errors.py`; change the two together.
"""

from __future__ import annotations


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
