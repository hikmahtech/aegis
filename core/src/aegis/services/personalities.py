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
import uuid as _uuid
from pathlib import Path
from typing import Any

import asyncpg

KINDS = ("soul", "agents", "user", "memory")

# apply_profile_patch refuses a patch that leaves less than this fraction of
# the prior content unless the caller passes allow_shrink=True — an automated
# writer that "summarises" a persona down to a stub is the failure mode this
# guards against.
_MIN_KEEP_FRACTION = 0.5

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


def _validate_updates(updates: dict[str, str]) -> None:
    """Raise ValueError on an unknown kind or a non-string value."""
    unknown = set(updates) - set(KINDS)
    if unknown:
        raise ValueError(f"unknown personality kind(s): {', '.join(sorted(unknown))}")
    for kind, content in updates.items():
        if content is not None and not isinstance(content, str):
            raise ValueError(f"personality '{kind}' content must be a string")


async def _upsert_personality(conn: Any, agent_id: str, updates: dict[str, str]) -> None:
    """Write the kinds on an already-acquired connection.

    Split out of set_personality so apply_profile_patch can run the same
    upsert inside its own transaction, alongside the revision-log insert.
    """
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


async def set_personality(
    pool: asyncpg.Pool, agent_id: str, updates: dict[str, str]
) -> dict[str, str]:
    """Upsert the given kinds for `agent_id`. Returns the full updated persona.

    Raises ValueError on an unknown kind or a non-string value.

    This is the *hand edit* path (admin UI / PUT …/personality) and writes no
    revision row — programmatic writers use apply_profile_patch instead.
    """
    _validate_updates(updates)
    async with pool.acquire() as conn:
        await _upsert_personality(conn, agent_id, updates)
    invalidate(agent_id)
    return await get_personality(pool, agent_id, use_cache=False)


async def apply_profile_patch(
    pool: asyncpg.Pool,
    agent_id: str,
    kind: str,
    new_content: str,
    *,
    source: str,
    interaction_id: str | _uuid.UUID | None = None,
    allow_shrink: bool = False,
) -> dict[str, Any]:
    """Patch ONE persona kind, recording an agent_profile_revisions row.

    The auditable write path for programmatic editors (reflection flows, chat
    tools, approved interactions). Returns
    ``{agent_id, kind, revision_id, source, before_length, after_length}``.

    Safety rail: refuses to leave less than 50% of the prior content unless
    `allow_shrink=True` (ValueError). The guard cannot fire when the kind is
    currently empty — growing from nothing is always allowed.

    The revision row and the persona upsert share ONE transaction, so a failed
    upsert never leaves an orphan revision (and vice versa). A patch is
    recorded even when the content is unchanged: `source` is itself audit
    evidence that a writer ran.
    """
    _validate_updates({kind: new_content})
    after = new_content or ""
    interaction_uuid = (
        interaction_id
        if isinstance(interaction_id, _uuid.UUID) or interaction_id is None
        else _uuid.UUID(str(interaction_id))
    )

    current = await get_personality(pool, agent_id, use_cache=False)
    before = current.get(kind, "") or ""
    # `before and` is belt-and-braces: the multiplication form already can't
    # fire on an empty prior doc, but the ratio form a future rewrite would
    # reach for (len(after) / len(before)) divides by zero. Keep the contract
    # explicit — growing from nothing is always allowed.
    if before and not allow_shrink and len(after) * 2 < len(before):
        raise ValueError(
            f"refusing to shrink personality '{kind}' for agent '{agent_id}' "
            f"from {len(before)} to {len(after)} chars "
            f"(>{int((1 - _MIN_KEEP_FRACTION) * 100)}% loss); pass allow_shrink=True to force"
        )

    async with pool.acquire() as conn, conn.transaction():
        revision_id = await conn.fetchval(
            """
            INSERT INTO agent_profile_revisions
                (agent_id, kind, before_content, after_content, source, interaction_id)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id
            """,
            agent_id,
            kind,
            before,
            after,
            source,
            interaction_uuid,
        )
        await _upsert_personality(conn, agent_id, {kind: after})
    invalidate(agent_id)

    return {
        "agent_id": agent_id,
        "kind": kind,
        "revision_id": int(revision_id),
        "source": source,
        "before_length": len(before),
        "after_length": len(after),
    }


async def list_profile_revisions(
    pool: asyncpg.Pool,
    agent_id: str,
    *,
    kind: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Revision log for `agent_id`, newest first. Optionally one kind only."""
    if kind is not None and kind not in KINDS:
        raise ValueError(f"unknown personality kind(s): {kind}")
    try:
        limit_int = max(1, min(500, int(limit)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"limit must be an integer, got {limit!r}") from exc

    rows = await pool.fetch(
        """
        SELECT id, agent_id, kind, before_content, after_content,
               source, interaction_id, created_at
        FROM agent_profile_revisions
        WHERE agent_id = $1 AND ($2::text IS NULL OR kind = $2)
        ORDER BY created_at DESC, id DESC
        LIMIT $3
        """,
        agent_id,
        kind,
        limit_int,
    )
    return [
        {
            "id": r["id"],
            "agent_id": r["agent_id"],
            "kind": r["kind"],
            "before_content": r["before_content"],
            "after_content": r["after_content"],
            "source": r["source"],
            "interaction_id": str(r["interaction_id"]) if r["interaction_id"] else None,
            "created_at": r["created_at"].isoformat(),
        }
        for r in rows
    ]


async def revert_profile_revision(
    pool: asyncpg.Pool, revision_id: int, *, source: str = "revert"
) -> dict[str, Any]:
    """Restore the `before_content` of `revision_id`, byte-identically.

    The revert is itself a patch, so it lands its own revision row — undoing an
    undo is just another revert. `allow_shrink` is forced on: rolling back an
    edit that grew the doc is exactly the shrink the guard would otherwise
    block. Raises ValueError when the revision id is unknown.
    """
    row = await pool.fetchrow(
        "SELECT agent_id, kind, before_content FROM agent_profile_revisions WHERE id = $1",
        revision_id,
    )
    if row is None:
        raise ValueError(f"unknown profile revision id: {revision_id}")
    return await apply_profile_patch(
        pool,
        row["agent_id"],
        row["kind"],
        row["before_content"] or "",
        source=source,
        allow_shrink=True,
    )


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
