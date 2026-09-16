"""`aegis.errors.error_text`: a caught exception as text that is never empty (#575)."""

from __future__ import annotations

import httpx
from aegis.errors import error_text, logged_failure


def test_an_exception_with_no_message_is_named_by_its_type():
    # The case that started it: httpx raises its timeouts with no message.
    assert str(httpx.ReadTimeout("")) == ""
    assert error_text(httpx.ReadTimeout("")) == "ReadTimeout"
    assert error_text(httpx.ConnectTimeout("")) == "ConnectTimeout"
    assert error_text(TimeoutError()) == "TimeoutError"


def test_a_message_follows_the_type():
    assert error_text(RuntimeError("db down")) == "RuntimeError: db down"


def test_a_whitespace_message_counts_as_empty_and_a_message_is_stripped():
    assert error_text(ValueError("   \n")) == "ValueError"
    assert error_text(ValueError("  bad value \n")) == "ValueError: bad value"


def test_the_limit_covers_the_whole_string():
    assert len(error_text(RuntimeError("x" * 500))) == 200
    assert error_text(RuntimeError("x" * 500), 20) == "RuntimeError: xxxxxx"
    assert len(error_text(RuntimeError("x" * 900), 500)) == 500
    # A short message is not padded or cut.
    assert error_text(KeyError("k"), 500) == "KeyError: 'k'"


# --------------------------------------------------------------- logged_failure


class _Recorder:
    """A logger that keeps what it was handed, unrendered."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def warning(self, *args) -> None:
        self.calls.append(args)


def test_logged_failure_swallows_an_exception_and_names_the_event():
    log = _Recorder()
    with logged_failure("gather_calendar_failed", logger=log):
        raise RuntimeError("db down")
    # The rendered line is what the hand-written handler produced, unchanged.
    fmt, *rest = log.calls[0]
    assert fmt % tuple(rest) == "gather_calendar_failed err=RuntimeError: db down"


def test_the_field_label_is_the_call_sites_own():
    log = _Recorder()
    with logged_failure("gather_calendar_failed", logger=log, field="error"):
        raise RuntimeError("db down")
    fmt, *rest = log.calls[0]
    assert fmt % tuple(rest) == "gather_calendar_failed error=RuntimeError: db down"


def test_a_block_that_succeeds_logs_nothing():
    log = _Recorder()
    done = []
    with logged_failure("never", logger=log):
        done.append(1)
    assert done == [1] and log.calls == []


def test_a_cancellation_is_not_a_degradation():
    """`BaseException` goes straight through: a cancelled activity must not
    read as a step that quietly did nothing."""
    log = _Recorder()
    try:
        with logged_failure("never", logger=log):
            raise KeyboardInterrupt
    except KeyboardInterrupt:
        pass
    else:  # pragma: no cover — the assert below says what went wrong
        raise AssertionError("logged_failure swallowed a BaseException")
    assert log.calls == []
