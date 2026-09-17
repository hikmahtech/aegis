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

Three rows live on this page: ``email_triage_rules``, ``meeting_rules`` (who
"you" are in a transcript) and ``email_task_links`` (mail that closes or
unblocks a task AEGIS already tracks). The last had no validating write at all
until #337 — its rules were edited through the generic editor, where a typo'd
action saved with a 200 and then did nothing forever.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from aegis.api.auth import verify_auth
from aegis.api.deps import get_pool
from aegis.api.settings_routes import settings_row_routes
from aegis.observability import log_audit
from aegis.services import email_task_links
from aegis.services.email_rules import (
    CATEGORIES,
    get_email_rules,
    known_senders,
    save_email_rules,
)
from aegis.services.meeting_rules import get_meeting_rules, save_meeting_rules

router = APIRouter(
    prefix="/api/admin/email",
    tags=["email-triage"],
    dependencies=[Depends(verify_auth)],
)


async def _triage_view(pool: Any, rules: dict[str, Any]) -> dict[str, Any]:
    """The rules plus the sender reputations they can be written against."""
    return {
        "categories": list(CATEGORIES),
        **rules,
        "known_senders": await known_senders(pool),
    }


settings_row_routes(
    router,
    "/triage-rules",
    get=get_email_rules,
    save=save_email_rules,
    view=_triage_view,
    doc=(
        "Your per-sender verdicts and extra notification markers "
        "(`settings.email_triage_rules`), with the sender reputations a rule can be written "
        "against. The PUT replaces the rules and answers 400 — not a silent drop — on an "
        "invalid category or marker."
    ),
)

settings_row_routes(
    router,
    "/meeting-rules",
    get=get_meeting_rules,
    save=save_meeting_rules,
    audit=lambda request, rules: log_audit(
        get_pool(request),
        actor="admin",
        action="meeting_rules_saved",
        target_type="settings",
        target_id="meeting_rules",
        details=rules,
    ),
    doc=(
        "`settings.meeting_rules` — who \"you\" are in a meeting transcript. Empty means the "
        "notes are filed and the self-analysis is skipped. 400 on a malformed self_names."
    ),
)

def _links_body(body: dict[str, Any]) -> Any:
    """The rules out of the request body. A body that does not carry `links` is
    a mistake, not an empty list: `{"link": [...]}` or `{"links": null}` would
    otherwise save `[]` with a 200 and wipe every rule the operator had."""
    links = body.get("links")
    if links is None:
        raise ValueError('links is required - send {"links": [...]}, or [] to remove every rule')
    return links


settings_row_routes(
    router,
    "/task-links",
    get=email_task_links.get_email_task_links,
    save=email_task_links.save_email_task_links,
    body=_links_body,
    view=lambda _pool, links: {"actions": list(email_task_links.ACTIONS), "links": links},
    audit=lambda request, links: log_audit(
        get_pool(request),
        actor="admin",
        action="email_task_links_saved",
        target_type="settings",
        target_id=email_task_links.SETTINGS_KEY,
        details={"links": links},
    ),
    doc=(
        "Mail that changes a task AEGIS already tracks (`settings.email_task_links`, #337): "
        "an ordered, first-match-wins list of {key, subject_re, body_re, action}. The PUT "
        "compiles both regexes server-side and answers 400 on a bad one or an unknown action, "
        "because the read path drops a malformed rule silently."
    ),
)
