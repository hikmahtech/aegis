"""Assertions on config/seed/todoist.yaml — the source of truth for the
managed Todoist label set used by bootstrap and clarify rules."""

from pathlib import Path

import yaml

_SEED_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "seed" / "todoist.yaml"
)


def _load_seed() -> dict:
    return yaml.safe_load(_SEED_PATH.read_text())


def test_todoist_seed_includes_area_labels():
    """The 6 curated @area/* labels must be present for the migration."""
    data = _load_seed()
    # Area labels moved to dedicated 'areas' group in GTD restructure
    all_label_names = {
        entry["name"]
        for group in data["labels"].values()
        for entry in group
    }
    expected_areas = {
        "@area/acme",
        "@area/business",
        "@area/aegis",
        "@area/finance",
        "@area/family",
        "@area/admin",
    }
    missing = expected_areas - all_label_names
    assert not missing, f"missing area labels in seed YAML: {missing}"


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


def test_managed_projects_are_next_and_someday_only():
    """Projects-as-workstreams removed: containers are next + someday only
    (inbox is adopted natively, not listed here)."""
    data = _load_seed()
    keys = {p["key"] for p in data["managed_projects"]}
    assert keys == {"next", "someday"}
    names = {p["name"] for p in data["managed_projects"]}
    assert "Next" in names
    assert "Someday / Later" in names
    # Old project-container keys are gone.
    assert "projects" not in keys
    assert "single_actions" not in keys


def test_someday_label_removed_other_state_labels_present():
    data = _load_seed()
    all_label_names = {
        L["name"]
        for group in data["labels"].values()
        for L in group
    }
    assert "@waiting" in all_label_names      # delegation state, kept
    assert "@reference" in all_label_names    # kept
    assert "@someday" not in all_label_names  # Someday is now a project


def test_someday_maybe_filter_removed():
    data = _load_seed()
    filter_names = {f["name"] for f in data["filters"]}
    assert "💭 Someday / Maybe" not in filter_names
