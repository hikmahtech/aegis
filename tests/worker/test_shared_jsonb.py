"""`decode_jsonb` and the two policies its callers layer on top of it.

The helper itself decodes; it does not decide what a string that will not
parse means. That is the difference the five decoders it replaced actually
had, so it is what this pins.
"""

from __future__ import annotations

import pytest
from aegis_worker.activities.alerts import _decode_metadata
from aegis_worker.activities.channels import _decode_config
from aegis_worker.activities.review import _decode_counts
from aegis_worker.shared.jsonb import decode_jsonb


@pytest.mark.parametrize("empty", [{}, []])
def test_nothing_decodes_to_the_callers_empty(empty):
    assert decode_jsonb(None, empty) == empty
    assert decode_jsonb("", empty) == empty


def test_an_object_passes_straight_through():
    """The codec-registered path, which is every row in production."""
    row = {"last_cursor": "2026-09-16"}
    assert decode_jsonb(row, {}) is row
    caps = ["infra"]
    assert decode_jsonb(caps, []) is caps


def test_a_string_is_parsed():
    """The double-encoded legacy row, and any pool with no jsonb codec."""
    assert decode_jsonb('{"a": 1}', {}) == {"a": 1}
    assert decode_jsonb('["infra"]', []) == ["infra"]


def test_junk_raises_rather_than_reading_as_empty():
    """The whole point of the split: the helper never decides to degrade."""
    with pytest.raises(ValueError):
        decode_jsonb("not json", {})


def test_a_channel_config_raises_and_is_a_copy():
    """A config that will not parse must not read as `{}`: the updater writes
    this dict back, so an empty one would wipe the row's other keys."""
    with pytest.raises(ValueError):
        _decode_config("not json")
    row = {"last_cursor": "2026-09-16"}
    decoded = _decode_config(row)
    decoded["last_cursor"] = "edited"
    assert row["last_cursor"] == "2026-09-16"


def test_the_degrading_callers_degrade():
    """A digest line and an alert's routing would each rather lose one row
    than fail the run."""
    assert _decode_counts("not json") == {}
    assert _decode_counts(["not", "a", "dict"]) == {}
    assert _decode_counts('{"tasks": 3}') == {"tasks": 3}
    assert _decode_metadata({"metadata": "not json"}) == {}
    assert _decode_metadata({"metadata": None}) == {}
    assert _decode_metadata({"metadata": {"github_repo": "a/b"}}) == {"github_repo": "a/b"}
