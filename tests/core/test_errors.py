"""`aegis.errors.error_text`: a caught exception as text that is never empty (#575)."""

from __future__ import annotations

import httpx
from aegis.errors import error_text


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
