"""Assertions on config/seed/todoist.yaml — the source of truth for the
managed Todoist label set used by bootstrap and clarify rules."""

from pathlib import Path

import yaml

_SEED_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "seed" / "todoist.yaml"
)


def _load_seed() -> dict:
    return yaml.safe_load(_SEED_PATH.read_text())


def test_todoist_seed_area_labels_are_now_projects():
    """After the GTD restructure, life-areas/work-streams are real Todoist
    projects, not labels — so only @area/acme remains as a label (the pandora
    investigation flow still tags Jira-linked tasks with it)."""
    data = _load_seed()
    all_label_names = {
        entry["name"]
        for group in data["labels"].values()
        for entry in group
    }
    assert "@area/acme" in all_label_names
    # The rest became projects and must no longer be seeded as labels.
    retired = {"@area/business", "@area/aegis", "@area/finance", "@area/family", "@area/admin"}
    assert not (retired & all_label_names), f"area labels that should be projects now: {retired & all_label_names}"


def test_todoist_seed_preserves_existing_context_labels():
    """Adding area labels must not displace existing GTD context labels."""
    data = _load_seed()
    # Context labels remain in 'contexts' group; @waiting moved to gtd_state
    context_names = {entry["name"] for entry in data["labels"]["contexts"]}
    expected_legacy = {
        "@5min", "@deep", "@email", "@phone", "@code",
        "@errand", "@home", "@office", "@reading",
    }
    missing = expected_legacy - context_names
    assert not missing, f"existing context labels lost: {missing}"


def test_managed_projects_are_empty():
    """Todoist restructure (2026-07): Next/Someday are now the @next /
    @someday LABELS, not managed projects — AEGIS only adopts the native
    Inbox (not listed here), so there are no AEGIS-created managed
    projects at all."""
    data = _load_seed()
    assert data["managed_projects"] == []


def test_someday_and_next_labels_present_other_state_labels_present():
    """@next / @someday are GTD-state labels now (replacing the old
    Next / Someday-Later managed projects)."""
    data = _load_seed()
    all_label_names = {
        L["name"]
        for group in data["labels"].values()
        for L in group
    }
    assert "@waiting" in all_label_names      # delegation state, kept
    assert "@reference" in all_label_names    # kept
    assert "@someday" in all_label_names      # Someday is now a label
    assert "@next" in all_label_names         # Next is now a label


def test_someday_maybe_filter_removed():
    data = _load_seed()
    filter_names = {f["name"] for f in data["filters"]}
    assert "💭 Someday / Maybe" not in filter_names
