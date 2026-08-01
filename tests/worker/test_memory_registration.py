"""A3 wiring: the consolidation activity is registered and its config keys are
threaded through the schedule mapper (nothing here is auto-discovered)."""

from __future__ import annotations

from aegis_worker.activities.memory import MemoryActivities
from aegis_worker.schedule_sync import _ACTIVITY_TYPE_MAP


def test_consolidate_is_an_activity_def():
    method = MemoryActivities.consolidate_agent_memories
    assert hasattr(method, "__temporal_activity_definition"), "missing @activity.defn"


def test_worker_registers_consolidate_activity():
    """The ACTIVITIES list is assembled inside main() at runtime, so (as with
    the other registration tests here) assert on the entrypoint source: an
    unregistered activity dies in production with 'activity type not
    registered', and an llm_client-less MemoryActivities is a silent no-op."""
    from pathlib import Path

    import aegis_worker.__main__ as worker_main

    lines = [ln.strip() for ln in Path(worker_main.__file__).read_text().splitlines()]
    live = [ln for ln in lines if not ln.startswith("#")]
    assert "memory_act.consolidate_agent_memories," in live
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
