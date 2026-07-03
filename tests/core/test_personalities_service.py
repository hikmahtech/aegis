"""aegis.services.personalities — DB-first persona store.

Covers: DB-first reads, starter-file fallback when an agent has no rows,
import-on-first-boot semantics (never overwrites DB rows), kind validation,
and cache invalidation on write. Real Postgres (:25432) via db_pool.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from aegis.db import run_migrations
from aegis.services import personalities as p

AGENT = "zzpersona-svc"


@pytest_asyncio.fixture(loop_scope="function")
async def agent_pool(db_pool):
    await run_migrations(db_pool)
    await db_pool.execute("DELETE FROM agents WHERE id = $1", AGENT)
    await db_pool.execute(
        "INSERT INTO agents (id, name, role, system_prompt_path, active) "
        "VALUES ($1, 'Z', 'r', '', true)",
        AGENT,
    )
    p.invalidate()
    yield db_pool
    # agent_personalities rows cascade with the agent row.
    await db_pool.execute("DELETE FROM agents WHERE id = $1", AGENT)
    p.invalidate()


def _write_starter_files(tmp_path: Path, agent_id: str, kinds: dict[str, str]) -> None:
    agent_dir = tmp_path / agent_id
    agent_dir.mkdir(parents=True)
    names = {"soul": "SOUL.md", "agents": "AGENTS.md", "user": "USER.md", "memory": "MEMORY.md"}
    for kind, content in kinds.items():
        (agent_dir / names[kind]).write_text(content)


async def test_get_personality_db_first(agent_pool):
    await agent_pool.execute(
        "INSERT INTO agent_personalities (agent_id, kind, content) VALUES ($1, 'soul', 'DB soul')",
        AGENT,
    )
    out = await p.get_personality(agent_pool, AGENT, use_cache=False)
    assert out == {"soul": "DB soul", "agents": "", "user": "", "memory": ""}


async def test_get_personality_falls_back_to_files_when_no_rows(agent_pool, tmp_path, monkeypatch):
    _write_starter_files(tmp_path, AGENT, {"soul": "file soul", "memory": "file memory"})
    monkeypatch.setenv("AEGIS_PERSONALITY_DIR", str(tmp_path))

    out = await p.get_personality(agent_pool, AGENT, use_cache=False)
    assert out["soul"] == "file soul"
    assert out["memory"] == "file memory"
    assert out["agents"] == "" and out["user"] == ""


async def test_db_rows_win_over_files_even_when_empty_content(agent_pool, tmp_path, monkeypatch):
    """Any row for the agent disables the file fallback — a deliberately
    cleared kind must not resurrect from the starter file."""
    _write_starter_files(tmp_path, AGENT, {"soul": "file soul"})
    monkeypatch.setenv("AEGIS_PERSONALITY_DIR", str(tmp_path))
    await agent_pool.execute(
        "INSERT INTO agent_personalities (agent_id, kind, content) VALUES ($1, 'user', 'U')",
        AGENT,
    )

    out = await p.get_personality(agent_pool, AGENT, use_cache=False)
    assert out["soul"] == ""  # not read from file — DB has rows for this agent
    assert out["user"] == "U"


async def test_import_personality_files_once(agent_pool, tmp_path, monkeypatch):
    _write_starter_files(tmp_path, AGENT, {"soul": "file soul", "user": "file user"})
    monkeypatch.setenv("AEGIS_PERSONALITY_DIR", str(tmp_path))

    assert await p.import_personality_files(agent_pool, AGENT) == 2

    # UI edit after the import…
    await p.set_personality(agent_pool, AGENT, {"soul": "edited soul"})
    # …a re-import (every boot) must not clobber it, and imports nothing new.
    assert await p.import_personality_files(agent_pool, AGENT) == 0

    out = await p.get_personality(agent_pool, AGENT, use_cache=False)
    assert out["soul"] == "edited soul"
    assert out["user"] == "file user"


async def test_import_skips_kind_cleared_in_ui(agent_pool, tmp_path, monkeypatch):
    _write_starter_files(tmp_path, AGENT, {"memory": "file memory"})
    monkeypatch.setenv("AEGIS_PERSONALITY_DIR", str(tmp_path))

    await p.import_personality_files(agent_pool, AGENT)
    await p.set_personality(agent_pool, AGENT, {"memory": ""})  # cleared in UI
    assert await p.import_personality_files(agent_pool, AGENT) == 0

    out = await p.get_personality(agent_pool, AGENT, use_cache=False)
    assert out["memory"] == ""


async def test_set_personality_rejects_unknown_kind(agent_pool):
    with pytest.raises(ValueError, match="unknown personality kind"):
        await p.set_personality(agent_pool, AGENT, {"vibe": "x"})


async def test_set_personality_rejects_non_string(agent_pool):
    with pytest.raises(ValueError, match="must be a string"):
        await p.set_personality(agent_pool, AGENT, {"soul": {"nested": "dict"}})


async def test_cache_invalidated_on_write(agent_pool):
    await p.set_personality(agent_pool, AGENT, {"soul": "v1"})
    assert (await p.get_personality(agent_pool, AGENT))["soul"] == "v1"  # cached
    await p.set_personality(agent_pool, AGENT, {"soul": "v2"})
    assert (await p.get_personality(agent_pool, AGENT))["soul"] == "v2"


async def test_seed_loader_imports_shipped_personas(db_pool):
    """load_seeds → agent_personalities has the shipped starter content for sebas."""
    from aegis.seed import load_seeds

    await run_migrations(db_pool)
    seed_dir = Path(__file__).parent.parent.parent / "config" / "seed"
    await load_seeds(db_pool, seed_dir)

    rows = await db_pool.fetch("SELECT kind FROM agent_personalities WHERE agent_id = 'sebas'")
    kinds = {r["kind"] for r in rows}
    # SOUL.md / USER.md / MEMORY.md ship as examples; AGENTS.md doesn't.
    assert {"soul", "user", "memory"} <= kinds
