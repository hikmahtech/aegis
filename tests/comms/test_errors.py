"""`aegis_comms.errors.error_text`, comms' copy of `aegis.errors` (#575)."""

from __future__ import annotations

import httpx
from aegis_comms.errors import error_text


def test_an_exception_with_no_message_is_named_by_its_type():
    assert error_text(httpx.ReadTimeout("")) == "ReadTimeout"
    assert error_text(TimeoutError()) == "TimeoutError"


def test_a_message_follows_the_type():
    assert error_text(RuntimeError("slack down")) == "RuntimeError: slack down"


def test_a_whitespace_message_counts_as_empty_and_a_message_is_stripped():
    assert error_text(ValueError("   \n")) == "ValueError"
    assert error_text(ValueError("  bad value \n")) == "ValueError: bad value"


def test_the_limit_covers_the_whole_string():
    assert len(error_text(RuntimeError("x" * 500))) == 200
    assert error_text(RuntimeError("x" * 500), 20) == "RuntimeError: xxxxxx"


def test_it_matches_the_core_copy():
    # The two copies must say the same thing; the CI job installs core too.
    from aegis.errors import error_text as core_error_text

    for exc in (httpx.ReadTimeout(""), RuntimeError(" boom "), ValueError("y" * 400)):
        for limit in (200, 30, 500):
            assert error_text(exc, limit) == core_error_text(exc, limit)
