"""`config/skills/*/SKILL.md` — the runbooks mounted into a claude agent run.

These files are copied verbatim into `<worktree>/.claude/skills` by
`RemoteScriptConnector.start_kimi_run`, and nothing downstream validates them:
a SKILL.md with broken or missing frontmatter is silently ignored by the CLI,
so the run simply never loads the runbook and nobody finds out. That silence is
what these tests exist to break.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

SKILLS_DIR = pathlib.Path(__file__).resolve().parents[2] / "config" / "skills"

# The set the connector ships. Losing one is a regression, not a refactor —
# these are referenced by name from docs and from the skills themselves.
EXPECTED = {"aegis-tools", "gtd-audit", "investigation", "subscription-audit"}


def _skill_files() -> list[pathlib.Path]:
    return sorted(SKILLS_DIR.glob("*/SKILL.md"))


def _frontmatter(path: pathlib.Path) -> dict:
    """Parse the leading `---` YAML block. Raises if there isn't one."""
    text = path.read_text()
    assert text.startswith("---\n"), f"{path}: no YAML frontmatter"
    _, block, _ = text.split("---\n", 2)
    data = yaml.safe_load(block)
    assert isinstance(data, dict), f"{path}: frontmatter is not a mapping"
    return data


def test_expected_skills_are_present():
    """Superset, not equality: adding a runbook is free, losing one is not."""
    assert {p.parent.name for p in _skill_files()} >= EXPECTED


@pytest.mark.parametrize("path", _skill_files(), ids=lambda p: p.parent.name)
def test_skill_frontmatter_is_loadable(path: pathlib.Path):
    """`name` must match the directory (that is how a skill is addressed) and
    `description` must be a single line — it is what an agent reads to decide
    whether to load the skill, and a multi-line value breaks that listing."""
    data = _frontmatter(path)
    assert data.get("name") == path.parent.name
    description = data.get("description") or ""
    assert description.strip(), f"{path}: empty description"
    assert "\n" not in description.strip(), f"{path}: description must be one line"


@pytest.mark.parametrize("path", _skill_files(), ids=lambda p: p.parent.name)
def test_skill_has_a_body(path: pathlib.Path):
    """Frontmatter alone is a stub. Guidance thin enough to fit in a few lines
    belongs in the description, not in a file a run pays context to load."""
    body = path.read_text().split("---\n", 2)[2]
    assert len(body.splitlines()) >= 20, f"{path}: body too thin to be a runbook"
