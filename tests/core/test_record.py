"""The owner's record compiled into each agent's `user` document (vault record
spec §5, §19 "Compile"). Made-up agents and notes; local bare repos."""

from __future__ import annotations

import pytest
import pytest_asyncio
from aegis.db import run_migrations
from aegis.services import personalities as p
from aegis.services import record
from aegis.services import vault_layout as vl
from aegis.services.notes import RecordFiles
from structlog.testing import capture_logs

from tests.notes_vault import CIPHER, device_commit, make_vault, needs_git

AGENTS = {"zzrec-gtd": ["gtd"], "zzrec-fin": ["finance"], "zzrec-res": ["research"]}
MAP = {"enabled": True, "shared": ["about"], "by_tag": {"finance": ["money"], "research": ["interests"]}}


def _layout(**record_keys) -> vl.Layout:
    return vl.layout_from({"record": {**MAP, **record_keys}})


def test_notes_for_is_shared_then_by_tag_then_unclaimed_for_the_generalist():
    lay = _layout()
    present = ["about", "money", "interests", "cars"]
    assert record.notes_for(["gtd"], lay, present) == ["about", "cars"]
    assert record.notes_for(["finance"], lay, present) == ["about", "money"]
    assert record.notes_for(["infra"], lay, present) == ["about"]
    assert record.notes_for(["finance"], lay, ["money"]) == ["money"]


def test_render_names_each_note_and_drops_frontmatter_comments_and_ciphertext():
    doc, cut = record.render(
        [
            ("about", f"---\ntags: [me]\n---\n# About\n- Lives in Exampletown %% hidden %%\n{CIPHER}\n"),
            ("empty", "---\na: 1\n---\n%% only a comment %%\n"),
        ],
        _layout(),
    )
    assert not cut
    assert doc.startswith("From me/about.md:\n# About\n- Lives in Exampletown")
    assert "hidden" not in doc and "tags:" not in doc and "c2VjcmV0" not in doc
    assert "me/empty.md" not in doc


def test_render_cuts_at_max_chars_and_says_so():
    doc, cut = record.render([("about", "- " + "x" * 2000)], _layout(max_chars=600))
    assert cut and len(doc) <= 600 and "Cut at 600 characters" in doc


@pytest_asyncio.fixture(loop_scope="function")
async def record_pool(db_pool):
    await run_migrations(db_pool)
    was_active = [r["id"] for r in await db_pool.fetch("SELECT id FROM agents WHERE active")]
    await db_pool.execute("UPDATE agents SET active = false")
    for aid, tags in AGENTS.items():
        await db_pool.execute(
            "INSERT INTO agents (id, name, role, system_prompt_path, active, capabilities) "
            "VALUES ($1, $1, 'r', '', true, $2) "
            "ON CONFLICT (id) DO UPDATE SET active = true, capabilities = EXCLUDED.capabilities",
            aid, tags,
        )
    await db_pool.execute("DELETE FROM settings WHERE key IN ('vault_layout', 'notes_record_state')")
    vl.invalidate_cache()
    p.invalidate()
    yield db_pool
    await db_pool.execute("DELETE FROM agents WHERE id = ANY($1::text[])", list(AGENTS))
    await db_pool.execute("UPDATE agents SET active = true WHERE id = ANY($1::text[])", was_active)
    await db_pool.execute("DELETE FROM settings WHERE key IN ('vault_layout', 'notes_record_state')")
    vl.invalidate_cache()
    p.invalidate()


async def _revisions(pool) -> int:
    return await pool.fetchval(
        "SELECT count(*) FROM agent_profile_revisions WHERE source = 'vault_record' AND agent_id = ANY($1::text[])",
        list(AGENTS),
    )


async def _row_stamps(pool) -> dict:
    rows = await pool.fetch(
        "SELECT agent_id, updated_at FROM agent_personalities "
        "WHERE kind = 'user' AND agent_id = ANY($1::text[])",
        list(AGENTS),
    )
    return {r["agent_id"]: r["updated_at"] for r in rows}


@needs_git
async def test_with_the_record_off_nothing_happens(record_pool, tmp_path):
    v = make_vault(tmp_path, {"me/about.md": "# About\n- Lives in Exampletown\n"})
    assert await record.compile_all(record_pool, v["cfg"], _layout(enabled=False)) == {"status": "off"}
    assert await _revisions(record_pool) == 0
    assert await record.get_state(record_pool) == {}


@needs_git
async def test_a_missing_folder_skips_and_changes_no_row(record_pool, tmp_path):
    v = make_vault(tmp_path, {"knowledge/a.md": "# A\n"})
    await p.set_personality(record_pool, "zzrec-gtd", {"user": "Hand-written context."})
    res = await record.compile_all(record_pool, v["cfg"], _layout())
    assert res == {"status": "no_folder", "folder": "me"}
    assert (await p.get_personality(record_pool, "zzrec-gtd", use_cache=False))["user"] == "Hand-written context."


@needs_git
async def test_compile_writes_each_agent_once_and_accepts_a_shrink(record_pool, tmp_path):
    v = make_vault(tmp_path, {
        "me/about.md": "# About\n- Lives in Exampletown\n",
        "me/money.md": "# Money\n- Two current accounts\n",
        "me/cars.md": "# Cars\n- Rides a bicycle\n",
        "me/interests.draft.md": "# Interests\n- a draft line\n",
    })
    await p.set_personality(record_pool, "zzrec-gtd", {"user": "y" * 3000})
    res = await record.compile_all(record_pool, v["cfg"], _layout())
    assert res["status"] == "ok" and sorted(res["written"]) == sorted(AGENTS)
    gtd = (await p.get_personality(record_pool, "zzrec-gtd", use_cache=False))["user"]
    fin = (await p.get_personality(record_pool, "zzrec-fin", use_cache=False))["user"]
    assert "From me/about.md:" in gtd and "From me/cars.md:" in gtd and "money" not in gtd.lower()
    assert "Two current accounts" in fin and "bicycle" not in fin
    assert "a draft line" not in gtd + fin, "a draft is never compiled"
    state = await record.get_state(record_pool)
    assert state["agents"]["zzrec-gtd"]["notes"] == ["me/about.md", "me/cars.md"]
    assert state["drafts"] == ["me/interests.draft.md"] and state["commit"]
    before = await _revisions(record_pool)
    again = await record.compile_all(record_pool, v["cfg"], _layout())
    assert again["written"] == [] and await _revisions(record_pool) == before


@needs_git
async def test_a_second_compile_over_an_unchanged_folder_writes_nothing(record_pool, tmp_path):
    # The row must hold exactly the compiled text: if anything between the
    # compile and the stored row changed it, every hourly run would rewrite
    # every row and log a false overwrite.
    v = make_vault(tmp_path, {
        "me/about.md": "---\ntags: [me]\n---\n# About\n- Lives in Exampletown  \n\n\n\n- trailing space above\n",
        "me/money.md": "# Money\n- Two current accounts\n",
    })
    first = await record.compile_all(record_pool, v["cfg"], _layout())
    assert sorted(first["written"]) == sorted(AGENTS)
    stamps, revisions = await _row_stamps(record_pool), await _revisions(record_pool)
    with capture_logs() as logs:
        again = await record.compile_all(record_pool, v["cfg"], _layout())
    assert again["status"] == "ok" and again["written"] == [] and again["overwritten"] == []
    assert await _revisions(record_pool) == revisions
    assert await _row_stamps(record_pool) == stamps, "no persona row is written again"
    assert not [e for e in logs if e["event"] == "record_cache_overwritten"]


@needs_git
async def test_an_agent_the_record_gives_nothing_gets_an_empty_document(record_pool, tmp_path):
    from aegis.services.chat import _build_agent_system_prompt

    v = make_vault(tmp_path, {"me/money.md": "# Money\n- Two current accounts\n"})
    await p.set_personality(record_pool, "zzrec-res", {"user": "Hand-written context.", "soul": "A helper."})
    res = await record.compile_all(record_pool, v["cfg"], _layout(shared=[]))
    assert res["status"] == "ok" and "zzrec-res" in res["written"]
    persona = await p.get_personality(record_pool, "zzrec-res", use_cache=False)
    assert persona["user"] == ""
    prompt = _build_agent_system_prompt("zzrec-res", "fallback", persona=persona)
    assert "A helper." in prompt and "User Context" not in prompt
    state = await record.get_state(record_pool)
    assert state["agents"]["zzrec-res"] == {**state["agents"]["zzrec-res"], "chars": 0, "notes": []}
    again = await record.compile_all(record_pool, v["cfg"], _layout(shared=[]))
    assert again["written"] == []


@needs_git
async def test_a_hand_edited_row_is_overwritten_and_logged(record_pool, tmp_path):
    v = make_vault(tmp_path, {"me/about.md": "# About\n- Lives in Exampletown\n"})
    await record.compile_all(record_pool, v["cfg"], _layout())
    await p.set_personality(record_pool, "zzrec-fin", {"user": "typed by hand"})
    with capture_logs() as logs:
        res = await record.compile_all(record_pool, v["cfg"], _layout())
    assert res["overwritten"] == ["zzrec-fin"]
    assert any(e["event"] == "record_cache_overwritten" and e["agent_id"] == "zzrec-fin" for e in logs)
    assert "typed by hand" not in (await p.get_personality(record_pool, "zzrec-fin", use_cache=False))["user"]


@needs_git
async def test_a_vault_edit_reaches_the_row(record_pool, tmp_path):
    v = make_vault(tmp_path, {"me/about.md": "# About\n- Lives in Exampletown\n"})
    await record.compile_all(record_pool, v["cfg"], _layout())
    device_commit(v, {"me/about.md": "# About\n- Moved to Othertown\n"})
    res = await record.compile_all(record_pool, v["cfg"], _layout())
    assert "zzrec-gtd" in res["written"]
    assert "Othertown" in (await p.get_personality(record_pool, "zzrec-gtd", use_cache=False))["user"]


@needs_git
async def test_the_switch_is_refused_while_the_generalist_would_be_emptied(record_pool, tmp_path):
    v = make_vault(tmp_path, {"me/about.draft.md": "# About\n- a draft\n"})
    await p.set_personality(record_pool, "zzrec-gtd", {"user": "The real document."})
    with pytest.raises(ValueError, match="^record.enabled: zzrec-gtd's user document"):
        await record.check_switch(record_pool, v["cfg"], _layout())
    device_commit(v, {"me/about.md": "# About\n- accepted\n"})
    await record.check_switch(record_pool, v["cfg"], _layout())  # now allowed


async def test_the_switch_is_allowed_when_the_generalist_row_is_empty(record_pool, tmp_path):
    from aegis.services.notes import NotesConfig

    await p.set_personality(record_pool, "zzrec-gtd", {"user": ""})
    empty = RecordFiles(head="", notes={})
    assert record.document_for(["gtd"], _layout(), empty)[0] == ""
    # No vault is needed when there is nothing to protect.
    await record.check_switch(record_pool, NotesConfig(path=tmp_path / "none"), _layout())
