"""Email triage rules — the user-owned half of Gmail classification.

Mirrors the Todoist gtd-rules / content-routes pair: the repo ships empty
defaults so a fork carries nobody's senders, and the personal rules live in the
``settings`` row ``email_triage_rules``.

Why this exists rather than leaning on the generic ``/api/settings`` editor:
that editor only lists rows that already exist (no create control), and it
stores whatever JSON you type without checking it — while ``email_rules.merge``
deliberately drops entries with an invalid category so a bad row can never stop
mail being classified. Together those two facts mean a typo'd category
disappears with no error anywhere, which is the same silent-degradation shape
this subsystem was fixed for. Here the write path validates loudly (400) and
the read path stays forgiving.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from aegis.api.auth import verify_auth
from aegis.services.email_rules import (
    CATEGORIES,
    get_email_rules,
    known_senders,
    save_email_rules,
)

router = APIRouter(
    prefix="/api/admin/email",
    tags=["email-triage"],
    dependencies=[Depends(verify_auth)],
)


@router.get("/triage-rules")
async def get_triage_rules(request: Request) -> dict[str, Any]:
    """Effective rules + the sender reputations they can be written against."""
    pool = request.app.state.db_pool
    return {
        "categories": list(CATEGORIES),
        **await get_email_rules(pool),
        "known_senders": await known_senders(pool),
    }


@router.put("/triage-rules")
async def put_triage_rules(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """Replace the rules. 400 (not a silent drop) on an invalid category or marker."""
    pool = request.app.state.db_pool
    try:
        rules = await save_email_rules(pool, body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"categories": list(CATEGORIES), **rules, "known_senders": await known_senders(pool)}
