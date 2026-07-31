"""Guard against future duplicate migration numeric prefixes.

migrations/NNN_*.sql files are applied in filename order and recorded in
the `schema_migrations` table by exact filename (see
aegis.db.run_migrations). Two files sharing a numeric prefix is almost
always a merge accident — CI should catch it before it ships, not after
someone's deploy applies both in whatever order `glob` happens to return.

One pair already exists in the repo: 006_infra_coding.sql and
006_social_metrics.sql. Both have already run in real deployments and are
tracked by their exact filename in `schema_migrations`, so renaming either
one is NOT a fix — it would desync the tracking table from the file on
disk and cause the migration to be (re)applied under a "new" name on next
boot. That pair is therefore grandfathered by exact filename below, and
must stay that way. Do not "clean this up" by renaming.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent
MIGRATIONS_DIR = REPO_ROOT / "migrations"

# Grandfathered duplicate prefixes: prefix -> exact set of filenames that
# are allowed to share it. Anything not listed here that shares a prefix
# with another file is a real bug. See module docstring for why 006 exists.
GRANDFATHERED_DUPLICATES: dict[str, list[str]] = {
    "006": ["006_infra_coding.sql", "006_social_metrics.sql"],
}

_PREFIX_RE = re.compile(r"^(\d+)_")


def find_duplicate_migration_numbers(
    migrations_dir: Path,
    allowlist: dict[str, list[str]] | None = None,
) -> dict[str, list[str]]:
    """Return {prefix: [filenames, ...]} for every leading numeric prefix
    used by more than one *.sql file directly under `migrations_dir`.

    A prefix is excluded from the result only if `allowlist` has an entry
    for it whose filename set is an EXACT match for the files found on
    disk — an extra file reusing an already-grandfathered prefix (e.g. a
    third 006_*.sql) still fails.
    """
    if allowlist is None:
        allowlist = GRANDFATHERED_DUPLICATES

    by_prefix: dict[str, list[str]] = {}
    for path in migrations_dir.glob("*.sql"):
        match = _PREFIX_RE.match(path.name)
        if not match:
            continue
        by_prefix.setdefault(match.group(1), []).append(path.name)

    offenders: dict[str, list[str]] = {}
    for prefix, names in by_prefix.items():
        if len(names) <= 1:
            continue
        allowed = allowlist.get(prefix)
        if allowed is not None and sorted(names) == sorted(allowed):
            continue
        offenders[prefix] = sorted(names)
    return offenders


def test_real_migrations_dir_has_no_unexpected_duplicates():
    """Acceptance criterion 1: passes today against the real migrations/
    dir, with only the documented 006 pair grandfathered in."""
    offenders = find_duplicate_migration_numbers(MIGRATIONS_DIR)
    assert offenders == {}, (
        f"Unexpected duplicate migration number(s): {offenders}. "
        "If this is a genuine new migration, renumber it to a free prefix. "
        "If you are trying to 'fix' the 006 pair by renaming, don't — see "
        "this test's module docstring."
    )


def test_grandfathered_pair_is_present_and_exempted():
    """Sanity check that the allowlist actually matches what's on disk
    today, so the grandfather entry doesn't silently go stale."""
    names = {p.name for p in MIGRATIONS_DIR.glob("006_*.sql")}
    assert names == set(GRANDFATHERED_DUPLICATES["006"])


def test_third_file_on_an_already_used_prefix_fails(tmp_path: Path):
    """Acceptance criterion 2: a tmp fixture dir, NOT the real migrations/
    dir. Two files already share prefix 002; adding a third with the same
    prefix must be flagged."""
    (tmp_path / "001_baseline.sql").write_text("-- noop\n")
    (tmp_path / "002_first.sql").write_text("-- noop\n")
    (tmp_path / "002_second.sql").write_text("-- noop\n")

    offenders = find_duplicate_migration_numbers(tmp_path, allowlist={})

    assert offenders == {"002": ["002_first.sql", "002_second.sql"]}


def test_new_duplicate_pair_not_in_allowlist_fails(tmp_path: Path):
    """Acceptance criterion 3: a brand-new duplicate pair elsewhere in the
    sequence (not the grandfathered 006 prefix) must fail even when the
    real-world allowlist is in effect."""
    (tmp_path / "001_baseline.sql").write_text("-- noop\n")
    (tmp_path / "003_alpha.sql").write_text("-- noop\n")
    (tmp_path / "003_beta.sql").write_text("-- noop\n")
    (tmp_path / "006_infra_coding.sql").write_text("-- noop\n")
    (tmp_path / "006_social_metrics.sql").write_text("-- noop\n")

    offenders = find_duplicate_migration_numbers(
        tmp_path, allowlist=GRANDFATHERED_DUPLICATES
    )

    # The 006 pair matches the allowlist exactly and is exempted; the new
    # 003 pair is not in the allowlist and must still be reported.
    assert offenders == {"003": ["003_alpha.sql", "003_beta.sql"]}


def test_extra_file_on_grandfathered_prefix_still_fails(tmp_path: Path):
    """A THIRD file reusing the grandfathered 006 prefix is not covered by
    the exact-match allowlist entry and must still fail."""
    (tmp_path / "006_infra_coding.sql").write_text("-- noop\n")
    (tmp_path / "006_social_metrics.sql").write_text("-- noop\n")
    (tmp_path / "006_oops.sql").write_text("-- noop\n")

    offenders = find_duplicate_migration_numbers(
        tmp_path, allowlist=GRANDFATHERED_DUPLICATES
    )

    assert offenders == {
        "006": ["006_infra_coding.sql", "006_oops.sql", "006_social_metrics.sql"]
    }


def test_no_duplicates_is_clean(tmp_path: Path):
    """Baseline: a fixture dir with unique prefixes reports no offenders."""
    (tmp_path / "001_a.sql").write_text("-- noop\n")
    (tmp_path / "002_b.sql").write_text("-- noop\n")
    (tmp_path / "003_c.sql").write_text("-- noop\n")

    assert find_duplicate_migration_numbers(tmp_path, allowlist={}) == {}
