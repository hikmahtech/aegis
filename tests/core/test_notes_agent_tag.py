"""The journal block's tag names the agent that wrote it (spec 2026-09-22 §1).

The journal changed hands within two weeks of shipping, so a tag that names one
agent splits one kind of block across two names. `{agent}` is the one
placeholder `entry.tag` takes, resolved once when the block is built.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime

import pytest
from aegis.services import notes
from aegis.services import vault_layout as vl

NOW = datetime(2026, 9, 22, 19, 5)
NESTED = replace(vl.DEFAULT_LAYOUT, entry_tag="#aegis/{agent}")


def test_the_placeholder_takes_the_writing_agents_id():
    assert NESTED.tag_for("sebas") == "#aegis/sebas"


def test_an_id_that_is_not_tag_safe_is_slugged():
    assert NESTED.tag_for("Pandora's Actor") == "#aegis/pandora-s-actor"


def test_with_no_agent_the_placeholder_and_its_slash_drop():
    """A write nobody is named for is still AEGIS's, so it keeps the parent tag
    rather than writing `{agent}` into the user's note."""
    assert NESTED.tag_for("") == "#aegis"


def test_a_literal_tag_is_returned_unchanged():
    assert replace(vl.DEFAULT_LAYOUT, entry_tag="#raphael").tag_for("sebas") == "#raphael"


def test_the_block_carries_the_agents_tag():
    block = notes.journal_block("daylog:2026-09-22", "day log", "The day.", NESTED, "sebas")
    assert block.startswith("- #aegis/sebas day log %% aegis:daylog:2026-09-22 %%\n")


def test_journal_append_carries_the_agent_to_the_block():
    ap = notes.journal_append(
        "daily", date(2026, 9, 22), "2026-09-22", "The day.", NOW, NESTED, agent="sebas"
    )
    assert ap.agent == "sebas"
    assert notes.journal_block(ap.key, ap.label, ap.body, ap.layout, ap.agent).startswith(
        "- #aegis/sebas day log"
    )


@pytest.mark.parametrize("tag", ["#aegis/{agent}", "#aegis", ""])
def test_validate_accepts_a_nested_tag_and_the_placeholder(tag):
    assert vl.validate({"entry": {"tag": tag}})["entry"]["tag"] == tag


@pytest.mark.parametrize("bad", ["aegis/{agent}", "#aegis {agent}", "#"])
def test_validate_still_refuses_a_bad_tag(bad):
    with pytest.raises(ValueError, match="^entry.tag"):
        vl.validate({"entry": {"tag": bad}})
