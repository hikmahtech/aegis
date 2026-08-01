"""A3 wiring: the consolidation activity is registered and its config keys are
threaded through the schedule mapper (nothing here is auto-discovered)."""

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
    assert any(
        "MemoryActivities(db_pool=deps.pool, llm_client=deps.llm" in ln for ln in live
    ), "MemoryActivities without llm_client is a silent no-op"


def test_schedule_mapper_reads_consolidation_config():
    _, built = _ACTIVITY_TYPE_MAP["MemoryReflectionFlow"](
        {"agent_id": "sebas", "config": {"keep": 12, "consolidate": True, "dry_run": True}}
    )
    assert (built.keep, built.consolidate, built.dry_run) == (12, True, True)


def test_schedule_mapper_fails_closed_on_a_legacy_config():
    """A row seeded before A3 (config = {keep: 50}) must stay observe-only."""
    _, built = _ACTIVITY_TYPE_MAP["MemoryReflectionFlow"](
        {"agent_id": "sebas", "config": {"keep": 50}}
    )
    assert built.consolidate is False
    assert built.dry_run is True
