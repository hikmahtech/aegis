"""Wiring: the consolidation activity is registered and its config keys are
threaded through the schedule mapper (nothing here is auto-discovered).

A4 adds the rails to what must be threaded — a `max_ops_pct` that never
reaches the activity would leave the quota running on a default the operator
thinks they changed."""

from __future__ import annotations

from aegis_worker.activities.memory import MemoryActivities
from aegis_worker.schedule_sync import _ACTIVITY_TYPE_MAP


def test_consolidate_is_an_activity_def():
    method = MemoryActivities.consolidate_agent_memories
    assert hasattr(method, "__temporal_activity_definition"), "missing @activity.defn"


def test_worker_registers_consolidate_activity():
    """An unregistered activity dies in production with 'activity type not
    registered', and an llm_client-less MemoryActivities is a silent no-op.

    Registration itself is now derived (registry.collect_activities over the
    instances main() builds, guarded by check_registration at boot), so the
    per-activity line this used to grep for is gone. The constructor wiring is
    still hand-written and still worth pinning to the source.
    """
    from pathlib import Path

    import aegis_worker.__main__ as worker_main
    from aegis_worker.registry import expected_activity_names

    assert "consolidate_agent_memories" in expected_activity_names()

    lines = [ln.strip() for ln in Path(worker_main.__file__).read_text().splitlines()]
    live = [ln for ln in lines if not ln.startswith("#")]
    assert any(ln.startswith("memory_act = MemoryActivities(") for ln in live)
    # The constructor call spans several lines, so match on the joined source
    # rather than a single line.
    source = " ".join(live)
    assert "MemoryActivities( db_pool=deps.pool, llm_client=deps.llm" in source, (
        "MemoryActivities without llm_client is a silent no-op"
    )
    # A4: the environment half of the two-key apply gate. Unwired, the kill
    # switch could never be opened and apply mode would be dead code.
    assert "apply_enabled=bool(getattr(settings, \"memory_consolidation_apply_enabled\"" in source


def test_schedule_mapper_reads_consolidation_config():
    """Non-default values throughout: a mapper that ignored `config` and
    handed back the dataclass defaults would pass a same-as-default check."""
    _, built = _ACTIVITY_TYPE_MAP["MemoryReflectionFlow"](
        {
            "agent_id": "sebas",
            "config": {
                "keep": 12,
                "consolidate": True,
                "dry_run": False,
                "max_ops_pct": 0.1,
                "min_age_hours": 72,
                "retire_grace_days": 45,
            },
        }
    )
    assert (built.keep, built.consolidate, built.dry_run) == (12, True, False)
    assert (built.max_ops_pct, built.min_age_hours, built.retire_grace_days) == (0.1, 72, 45)


def test_schedule_mapper_fails_closed_on_a_legacy_config():
    """A row seeded before A3/A4 (config = {keep: 50}) must stay observe-only,
    on the strictest quota, and must never start hard-deleting retired rows."""
    _, built = _ACTIVITY_TYPE_MAP["MemoryReflectionFlow"](
        {"agent_id": "sebas", "config": {"keep": 50}}
    )
    assert built.consolidate is False
    assert built.dry_run is True
    assert built.max_ops_pct == 0.25
    assert built.min_age_hours == 24
    assert built.retire_grace_days == 0, "a legacy row must not enable the hard purge"
