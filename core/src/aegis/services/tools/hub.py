"""Chat tools over the problem hub (`services/hub.py`).

`merge_problems` folds a duplicate problem into the one to keep. The session
registry tools (`task_context`, `report_progress`) left with the coding lane,
which moved to the Development vertical (a2-development).
"""

from __future__ import annotations

import uuid

import asyncpg
import structlog

from aegis.services import hub_project
from aegis.services.hub import (
    get_problem,
    merge_problems,
)
from aegis.services.tools.base import ToolContext
from aegis.services.tools.registry import aegis_tool

logger = structlog.get_logger()


def _uuid(value: str) -> str:
    try:
        return str(uuid.UUID(str(value or "").strip()))
    except ValueError:
        return ""


@aegis_tool
async def _exec_merge_problems(
    pool: asyncpg.Pool,
    ctx: ToolContext,
    *,
    keep_id: str,
    merge_id: str,
) -> str:
    """Fold one problem into another when they are the same outage under two names: events and links move to the kept problem, the merged one closes with a link back, and its task is completed with a note. Only do this when you are sure — a wrong merge hides an outage.

    Args:
        keep_id: the problem to keep (uuid).
        merge_id: the duplicate to fold into it (uuid).
    """
    keep = _uuid(keep_id)
    merge = _uuid(merge_id)
    if not keep or not merge:
        return "Refused: keep_id and merge_id must be problem uuids"
    try:
        result = await merge_problems(pool, keep, merge, by=f"chat:{ctx.agent_id or 'unknown'}")
    except ValueError as exc:
        return f"Refused: {exc}"
    kept = await get_problem(pool, keep)
    lines = [
        f"Merged {merge} into {keep}: {result['events_moved']} events moved; "
        f"the kept problem is now {kept['status'] if kept else 'unknown'} with "
        f"{kept['occurrences'] if kept else '?'} occurrences."
    ]
    retired = await hub_project.retire_merged_task(pool, result, settings=ctx.settings)
    if retired is not None:
        lines.append(
            f"Task {result['merged_task_id']} "
            f"{'completed' if retired else 'could not be completed'} with a note."
        )
    return "\n".join(lines)
