"""The hub's settle windows as operator-editable config (PR #559).

#537 put the numbers in the database and then left `psql` as the only way to
set them — which is not "configurable" in a system built to be forked. Read is
lenient so a bad row cannot stop an alert being handled; write is strict so a
typo cannot save with a 200 and then silently do nothing, which is the worse
failure because the operator believes they changed something.
"""

from __future__ import annotations

import pytest
from aegis.services import hub_settle
from aegis.services.hub import SETTLE_SETTINGS_KEY, verify_seconds_for

pytestmark = pytest.mark.asyncio


def test_read_is_lenient():
    """A malformed entry is dropped, never raised: every consumer of this row is
    on the path of handling a live alert."""
    out = hub_settle.merge({"NodeDown": "90", "junk": "soon", "": 5, "*": 0})
    assert out == {"nodedown": 90, "*": 0}
    assert hub_settle.merge(None) == {}
    assert hub_settle.merge("not an object") == {}
    # Out-of-range values are clamped on read rather than dropped, so an
    # existing row written before the cap still means something sensible.
    assert hub_settle.merge({"x": 99999}) == {"x": hub_settle.MAX_SECONDS}
    assert hub_settle.merge({"x": -5}) == {"x": 0}


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        ("nope", "must be an object"),
        ({"": 1}, "cannot be empty"),
        ({"nodedown": "soon"}, "not a whole number"),
        ({"nodedown": -1}, "cannot be negative"),
        ({"nodedown": 99999}, "is a mute"),
    ],
)
def test_write_is_strict(bad, message):
    """Falsifiable: make `validate` fall back like `merge` and each of these
    saves a 200 that does nothing."""
    with pytest.raises(ValueError, match=message):
        hub_settle.validate(bad)


def test_write_normalises_the_class_the_same_way_the_reader_does():
    """An operator types a class as the Problems page shows it, or with spaces.
    Both must land on the key `verify_seconds_for` will look up."""
    assert hub_settle.validate({"Docker Service Down": 120}) == {"docker-service-down": 120}
    assert hub_settle.validate({"dockerservicedown": 120}) == {"dockerservicedown": 120}
    assert hub_settle.validate({"*": 0}) == {"*": 0}


async def test_saving_then_reading_changes_what_the_hub_waits(db_pool):
    """The round trip that matters: what the admin page writes is what the
    projector and the investigation delay then read.

    Falsifiable: point `save_settle_seconds` at a different key and the window
    below stays at its code default.
    """
    await db_pool.execute("DELETE FROM settings WHERE key = $1", SETTLE_SETTINGS_KEY)
    try:
        assert await verify_seconds_for(db_pool, "DockerServiceDown") == 300

        out = await hub_settle.save_settle_seconds(db_pool, {"DockerServiceDown": 45})
        assert out["overrides"] == {"dockerservicedown": 45}
        assert await verify_seconds_for(db_pool, "DockerServiceDown") == 45

        # An empty object removes the row rather than storing `{}`, so the
        # effective config is the code default and nothing suggests an
        # override that is not there.
        out = await hub_settle.save_settle_seconds(db_pool, {})
        assert out["overrides"] == {}
        assert await db_pool.fetchval(
            "SELECT count(*) FROM settings WHERE key = $1", SETTLE_SETTINGS_KEY
        ) == 0
        assert await verify_seconds_for(db_pool, "DockerServiceDown") == 300
    finally:
        await db_pool.execute("DELETE FROM settings WHERE key = $1", SETTLE_SETTINGS_KEY)


async def test_the_read_shows_the_defaults_it_sits_on(db_pool):
    """A blank field has to be shown as what it means. Without the defaults the
    page would imply every unlisted class waits zero seconds."""
    await db_pool.execute("DELETE FROM settings WHERE key = $1", SETTLE_SETTINGS_KEY)
    out = await hub_settle.get_settle_seconds(db_pool)
    assert out["overrides"] == {}
    assert out["defaults"]["nodedown"] == 300
    assert out["default_seconds"] == 180
    assert out["wildcard"] == "*"
