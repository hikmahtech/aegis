"""The weekly journal backfill's schedule carries its window (#514, audit).

`since_days` comes from the `activities` row through the FlowSpec builder, and
the seed row ships it, so the scheduled run never rereads the pre-vault rows."""

from __future__ import annotations

from pathlib import Path

import yaml
from aegis_worker.flows.notes_backfill import NotesBackfillFlow
from aegis_worker.registry import FLOWS

SEED = Path(__file__).resolve().parents[2] / "config" / "seed" / "activities.yaml"


def _builder():
    return next(s for s in FLOWS if s.flow is NotesBackfillFlow).schedule_config


def test_the_builder_carries_limit_since_days_and_batch_from_the_row():
    cfg = _builder()({"agent_id": "raphael", "config": {"limit": 500, "since_days": 14, "batch": 10}})
    assert (cfg.agent_id, cfg.limit, cfg.since_days, cfg.batch) == ("raphael", 500, 14, 10)


def test_a_row_without_the_window_takes_every_row():
    cfg = _builder()({"agent_id": "raphael", "config": {}})
    assert (cfg.limit, cfg.since_days, cfg.batch) == (1000, 0, 50)


def test_the_seed_row_ships_a_two_week_window():
    rows = yaml.safe_load(SEED.read_text("utf-8"))["activities"]
    row = next(r for r in rows if r["slug"] == "notes-backfill-weekly")
    assert row["config"] == {"limit": 1000, "since_days": 14, "batch": 50}
    cfg = _builder()({"agent_id": row["agent_id"], "config": row["config"]})
    assert cfg.since_days == 14


def test_the_sync_row_carries_the_index_cut():
    from aegis_worker.flows.notes_sync import NotesSyncFlow

    build = next(s for s in FLOWS if s.flow is NotesSyncFlow).schedule_config
    cfg = build({"agent_id": "raphael", "config": {"max_files": 20, "index_max_chars": 5000}})
    assert (cfg.max_files, cfg.index_max_chars) == (20, 5000)
    assert build({"agent_id": "raphael", "config": {}}).index_max_chars == 100_000
    rows = yaml.safe_load(SEED.read_text("utf-8"))["activities"]
    row = next(r for r in rows if r["slug"] == "notes-sync-hourly")
    assert row["config"] == {"max_files": 300, "index_max_chars": 100000}
