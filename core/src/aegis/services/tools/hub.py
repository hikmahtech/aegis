"""Chat tools over the problem hub (`services/hub.py`).

`set_service_state` is how an operator tells the hub that a service is being
deployed, is in maintenance, or is back — from chat, or from their own Claude
Code session over the operator MCP mount. It is withheld from coding runs
(`routes/mcp_server.py::_UNSERVED_TOOLS`): a run that could open a maintenance
window could silence the alert about itself.
"""

from __future__ import annotations

from typing import Literal

import asyncpg

from aegis.services.hub import list_service_states, set_service_state
from aegis.services.tools.base import ToolContext
from aegis.services.tools.registry import aegis_tool


def _fmt_until(row: dict) -> str:
    until = row.get("until_at")
    return f"until {until:%Y-%m-%d %H:%M} UTC" if until else "until cleared"


@aegis_tool
async def _exec_set_service_state(
    pool: asyncpg.Pool,
    ctx: ToolContext,
    *,
    subject: str,
    state: Literal["deploying", "maintenance", "degraded", "ok"],
    minutes: int = 30,
    note: str = "",
) -> str:
    """Declare a swarm service or node deploying, in maintenance, degraded, or ok again. While a subject is deploying or in maintenance the problem hub records what it sees there but raises nothing; `ok` ends the window early.

    Args:
        subject: the swarm service (`stack_service`) or node name, or `*` for everything.
        state: deploying | maintenance | degraded | ok.
        minutes: how long the window lasts; ignored for ok.
        note: why — shown on every problem the window suppresses.
    """
    subject = (subject or "").strip()
    kind = "*" if subject == "*" else "service"
    try:
        row = await set_service_state(
            pool,
            subject,
            state,
            subject_kind=kind,
            minutes=minutes if state != "ok" else None,
            set_by=f"chat:{ctx.agent_id or 'unknown'}",
            note=note,
        )
    except ValueError as exc:
        return f"Refused: {exc}"
    if state == "ok":
        head = (
            f"{row['subject']}: window cleared."
            if row.get("cleared")
            else f"{row['subject']}: no window was set."
        )
    else:
        head = f"{row['subject']}: {row['state']} {_fmt_until(row)} (set by {row['set_by']})."
    active = await list_service_states(pool)
    if not active:
        return head + " No windows in force."
    lines = [f"- {r['subject']} ({r['subject_kind']}): {r['state']} {_fmt_until(r)}" for r in active]
    return head + " Windows in force:\n" + "\n".join(lines)
