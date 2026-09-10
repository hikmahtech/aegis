"""One unusable `activities.config` row must cost only that row (#373).

The mapper runs inside `sync_schedules`' loop with nothing catching it, so a
`ValueError` out of `int("")` propagated out of the whole function. Both callers
catch it with a bare `except Exception` and log a warning, so the worker did not
crash — it just stopped reconciling after the bad row, skipped every activity
that sorted after it, skipped orphan pruning, and did the same thing again 300
seconds later until someone fixed the config by hand.
"""

from __future__ import annotations

import pytest
from aegis_worker.registry import _int
from aegis_worker.schedule_sync import _ACTIVITY_TYPE_MAP, sync_schedules

_BROKEN = "zz-broken-config-test"
_ORPHAN = "zz-orphan-test"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", 50),  # the admin page's "cleared field"
        (None, 50),  # an explicit JSON null
        ("not a number", 50),
        ("12", 12),  # a number that arrived as a string still counts
        (7, 7),
        (0, 0),  # zero is a value, not an absence
    ],
)
def test_a_numeric_config_field_falls_back_only_when_it_is_not_a_number(value, expected):
    assert _int({"max_per_account": value}, "max_per_account", 50) == expected


def test_the_missing_key_still_takes_the_default():
    assert _int({}, "max_per_account", 50) == 50


def test_the_repro_from_the_issue_no_longer_raises():
    """`{"max_per_account": ""}` on any row whose flow maps that key — the exact
    config in the issue, through the real mapper rather than a stand-in."""
    mapper = _ACTIVITY_TYPE_MAP["ReceiptIngestFlow"]
    _, config = mapper(
        {
            "agent_id": "maou",
            "config": {"max_per_account": ""},
            "_settings": {"aegis_ui_url": "", "comms_url": ""},
        }
    )
    assert config.max_per_account == 50


class _Handle:
    def __init__(self, schedule_id: str, deleted: list[str]):
        self._id, self._deleted = schedule_id, deleted

    async def describe(self):
        # Nothing exists yet, so every row takes the create path.
        raise RuntimeError("no such schedule")

    async def delete(self) -> None:
        self._deleted.append(self._id)


class _Listing:
    def __init__(self, ids):
        self._ids = list(ids)

    def __aiter__(self):
        async def gen():
            for schedule_id in self._ids:
                yield type("S", (), {"id": schedule_id})()

        return gen()


class _FakeClient:
    def __init__(self, live_ids):
        self.created: list[str] = []
        self.deleted: list[str] = []
        self._live = live_ids

    def get_schedule_handle(self, schedule_id: str) -> _Handle:
        return _Handle(schedule_id, self.deleted)

    async def create_schedule(self, schedule_id: str, _schedule) -> None:
        self.created.append(schedule_id)

    async def list_schedules(self) -> _Listing:
        return _Listing(self._live)


@pytest.mark.asyncio
async def test_one_unusable_config_does_not_cost_the_rest_of_the_tick(db_pool, monkeypatch):
    """Two properties, and neither depends on where the bad row sorts.

    Orphan pruning runs at the END of the loop, so an orphan actually being
    deleted is proof the loop reached the end — which is precisely what the
    escaping exception used to prevent. And the broken row's own schedule must
    survive: an id missing from `expected_ids` IS an orphan, so claiming it
    before the mapper is what stops one bad field tearing down a schedule that
    is still running fine on the last config that mapped.
    """

    def _raises(_act):
        raise ValueError("invalid literal for int() with base 10: ''")

    monkeypatch.setitem(_ACTIVITY_TYPE_MAP, "TodoistSyncFlow", _raises)
    await db_pool.execute("DELETE FROM activities WHERE slug = $1", _BROKEN)
    await db_pool.execute(
        "INSERT INTO activities (slug, workflow_type, agent_id, schedule_cron, config, active) "
        "VALUES ($1, 'TodoistSyncFlow', 'sebas', '*/5 * * * *', '{}'::jsonb, TRUE)",
        _BROKEN,
    )
    try:
        client = _FakeClient([_BROKEN, _ORPHAN])
        registered = await sync_schedules(client, db_pool, "aegis-main", settings=None)
    finally:
        await db_pool.execute("DELETE FROM activities WHERE slug = $1", _BROKEN)

    assert _ORPHAN in client.deleted, "the loop never reached orphan pruning"
    assert _BROKEN not in client.deleted, "an unusable config tore down a live schedule"
    assert _BROKEN not in client.created
    assert registered >= 1, "no other activity was reconciled"
