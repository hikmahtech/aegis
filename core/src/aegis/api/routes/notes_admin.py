"""The vault layout — where the journal goes, what an entry looks like.

``GET/PUT /api/admin/notes/layout`` is the validating write path for the
``vault_layout`` settings row (``services/vault_layout.py``): the read path is
lenient so a bad row never stops the daylog, and that same leniency at the
write boundary would let a typo'd folder save with a 200 and quietly move the
journal — so the PUT 400s on it, as the email and meeting rules do.

``/layout/preview`` renders what the layout does for one date, with the real
code (`vault_layout.preview`), so the admin page shows the writer's paths and
not a reimplementation of them. GET previews the saved layout; POST previews a
candidate without saving it.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from aegis.api.auth import verify_auth
from aegis.api.settings_routes import settings_row_routes
from aegis.services import notes
from aegis.services import vault_layout as vl
from aegis.services.agents import resolve_tag

router = APIRouter(
    prefix="/api/admin/notes",
    tags=["notes"],
    dependencies=[Depends(verify_auth)],
)


settings_row_routes(
    router,
    "/layout",
    get=vl.get_layout_value,
    save=vl.save_layout,
    view=lambda _pool, layout: {
        "layout": layout,
        "defaults": vl.merge({}),
        "options": _options(),
    },
    doc=(
        "The effective vault layout (the stored row merged over the defaults), the defaults "
        "themselves and the vocabularies the page's selects need. The PUT replaces the layout "
        "and answers 400 — not a silent drop — on any bad key; the layout in force before a "
        "change is kept as `previous`, so a day written under it is still recognised and never "
        "written twice."
    ),
)


@router.get("/layout/preview")
async def preview_layout_route(
    request: Request, date: str | None = Query(default=None)
) -> dict[str, Any]:
    """What the SAVED layout renders for `date` (today by default)."""
    layout = await vl.get_layout(request.app.state.db_pool)
    return _preview(layout, date, await _journal_owner(request))


@router.post("/layout/preview")
async def preview_candidate_route(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """What a candidate layout (`body.layout`, not saved) renders for
    `body.date`. 400 with the first bad key when it does not validate."""
    try:
        merged = vl.validate(body.get("layout") if isinstance(body, dict) else None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _preview(
        vl.layout_from(merged),
        body.get("date") if isinstance(body, dict) else None,
        await _journal_owner(request),
    )


async def _journal_owner(request: Request) -> str:
    """Whose block the sample shows: the journal's owner, so the preview's tag
    is the one the nightly run will write. Never raises — an unresolved tag
    previews with no agent rather than failing the page."""
    try:
        return await resolve_tag(request.app.state.db_pool, notes.JOURNAL_OWNER_TAG) or ""
    except Exception:  # noqa: BLE001 — a preview must not 500 on a lookup
        return ""


def _preview(layout: vl.Layout, when: str | None, agent: str = "") -> dict[str, Any]:
    try:
        d = date.fromisoformat(when) if when else datetime.now().date()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD") from exc
    out = vl.preview(layout, d)
    out["sample_block"] = notes.journal_block(
        notes.journal_key("daily", d.isoformat()),
        layout.daily.label,
        "The first paragraph of the day, as one line.\n\nA second paragraph.\n\n"
        f"{layout.word('tasks')}\n  - an item under that label",
        layout,
        agent,
    )
    return out


def _options() -> dict[str, list[str]]:
    return {
        "locale": list(vl.LOCALES),
        "week_start": list(vl.WEEK_STARTS),
        "week_numbering": list(vl.WEEK_NUMBERINGS),
        "indent": list(vl.INDENTS),
        "kinds": list(vl.KINDS),
        "language_keys": list(vl.DEFAULT_LANGUAGE),
    }
