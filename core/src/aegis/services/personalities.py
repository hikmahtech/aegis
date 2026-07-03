"""Agent personalities — DB-first persona store (agent_personalities table).

Each agent's persona is four markdown documents ("kinds"):

  soul    — identity / voice            (starter file: personalities/<id>/SOUL.md)
  agents  — operational boundaries      (starter file: personalities/<id>/AGENTS.md)
  user    — user context                (starter file: personalities/<id>/USER.md)
  memory  — long-term memory document   (starter file: personalities/<id>/MEMORY.md)

The DB is the source of truth (edited via the admin UI / PUT
/api/admin/agents/{id}/personality). The markdown files under personalities/
are import-on-first-boot starter examples: the seed loader calls
import_personality_files() per agent, which only inserts kinds that have a
file but no row yet — existing rows (including deliberately-emptied ones) are
never overwritten by files.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import asyncpg

KINDS = ("soul", "agents", "user", "memory")

_KIND_FILES = {
    "soul": "SOUL.md",
    "agents": "AGENTS.md",
    "user": "USER.md",
    "memory": "MEMORY.md",
}

# Short TTL cache: chat builds a system prompt per message; the persona only
# changes on an explicit UI save (which invalidates in-process immediately —
# other processes converge within the TTL).
_CACHE_TTL_SECONDS = 30.0
_cache: dict[str, tuple[float, dict[str, str]]] = {}


def personality_dir() -> Path:
    """Where personalities/<id>/ starter files live — env override, repo dir,
    or the container path."""
    env = os.environ.get("AEGIS_PERSONALITY_DIR")
    if env and Path(env).is_dir():
        return Path(env)
    repo = Path(__file__).resolve().parents[4] / "personalities"
    if repo.is_dir():
        return repo
    return Path("/app/personalities")


def read_personality_files(agent_id: str) -> dict[str, str]:
    """kind → content for the kinds that have a non-empty starter .md file."""
    agent_dir = personality_dir() / agent_id
    out: dict[str, str] = {}
    for kind, filename in _KIND_FILES.items():
        f = agent_dir / filename
        if f.exists():
            text = f.read_text().strip()
            if text:
                out[kind] = text
    return out


def invalidate(agent_id: str | None = None) -> None:
    """Drop the cached persona for one agent (or all when agent_id is None)."""
    if agent_id is None:
        _cache.clear()
    else:
        _cache.pop(agent_id, None)


async def get_personality(
    pool: asyncpg.Pool, agent_id: str, *, use_cache: bool = True
) -> dict[str, str]:
    """All four persona kinds for `agent_id`, DB-first ('' for absent kinds).

    Falls back to the starter files only when the agent has NO rows at all
    (pre-first-seed boot window, or an agent that was never imported).
    """
    if use_cache:
        hit = _cache.get(agent_id)
        if hit and hit[0] > time.monotonic():
            return dict(hit[1])

    rows = await pool.fetch(
        "SELECT kind, content FROM agent_personalities WHERE agent_id = $1",
        agent_id,
    )
    data: dict[str, str] = dict.fromkeys(KINDS, "")
    if rows:
        for r in rows:
            data[r["kind"]] = r["content"] or ""
    else:
        data.update(read_personality_files(agent_id))

    _cache[agent_id] = (time.monotonic() + _CACHE_TTL_SECONDS, dict(data))
    return data


async def set_personality(
    pool: asyncpg.Pool, agent_id: str, updates: dict[str, str]
) -> dict[str, str]:
    """Upsert the given kinds for `agent_id`. Returns the full updated persona.

    Raises ValueError on an unknown kind or a non-string value.
    """
    unknown = set(updates) - set(KINDS)
    if unknown:
        raise ValueError(f"unknown personality kind(s): {', '.join(sorted(unknown))}")
    for kind, content in updates.items():
        if content is not None and not isinstance(content, str):
            raise ValueError(f"personality '{kind}' content must be a string")

    async with pool.acquire() as conn:
        for kind, content in updates.items():
            await conn.execute(
                """
                INSERT INTO agent_personalities (agent_id, kind, content)
                VALUES ($1, $2, $3)
                ON CONFLICT (agent_id, kind) DO UPDATE
                SET content = EXCLUDED.content, updated_at = now()
                """,
                agent_id,
                kind,
                content or "",
            )
    invalidate(agent_id)
    return await get_personality(pool, agent_id, use_cache=False)


async def import_personality_files(pool: asyncpg.Pool, agent_id: str) -> int:
    """First-boot import: insert file-backed kinds that have no DB row yet.

    Never overwrites an existing row — the DB owns the content once a row
    exists (a kind cleared in the UI keeps its empty row and is NOT
    re-imported). Returns the number of kinds imported.
    """
    files = read_personality_files(agent_id)
    if not files:
        return 0
    imported = 0
    async with pool.acquire() as conn:
        for kind, content in files.items():
            status = await conn.execute(
                "INSERT INTO agent_personalities (agent_id, kind, content) "
                "VALUES ($1, $2, $3) ON CONFLICT (agent_id, kind) DO NOTHING",
                agent_id,
                kind,
                content,
            )
            if status == "INSERT 0 1":
                imported += 1
    if imported:
        invalidate(agent_id)
    return imported
