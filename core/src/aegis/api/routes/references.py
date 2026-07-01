"""Reference library — raphael's KS-backed reading corpus + failure lane.

Surfaces three views:

- ``GET /api/references`` — the library: KS content with
  ``source_type=reference``, filtered/searched in the UI.
- ``GET /api/references/{content_id}`` — detail view (status + chunks).
- ``GET /api/references/failures`` — tasks demoted to ``@to-read`` because
  KS ingest gave up. Sourced from the ``todoist_tasks`` projection.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from aegis.api.auth import verify_auth
from aegis.api.deps import get_knowledge_connector as _get_connector
from aegis.clarify_note import CLARIFY_NOTE_PREFIX

router = APIRouter(prefix="/api/references", dependencies=[Depends(verify_auth)])


def _get_pool(request: Request):
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="Database not available")
    return pool


def _extract_source_tag(item: dict) -> str | None:
    """Resolve a reference's source_tag.

    Newer ingests carry it in ``metadata.source_tag``. Legacy ingests (pre
    references-as-knowledge) only embedded it as a ``tags[]`` entry alongside
    ``gtd:reference``. Fall back to the first ``#``-prefixed tag so the
    Library filter chip still works for older rows.
    """
    meta = item.get("metadata") or {}
    explicit = meta.get("source_tag")
    if explicit:
        return explicit
    for tag in item.get("tags") or []:
        if isinstance(tag, str) and tag.startswith("#"):
            return tag
    return None


@router.get("")
async def list_references(
    request: Request,
    limit: int = Query(200, ge=1, le=500),
    source_tag: str | None = Query(
        None, description="Filter by source tag (e.g. #research, #email)"
    ),
    q: str | None = Query(None, description="Semantic search query"),
) -> list[dict[str, Any]]:
    """List filed references.

    When ``q`` is set, runs a semantic search scoped to references.
    Otherwise lists recent KS content items, filtered to
    ``source_type=reference`` (and optionally a single ``source_tag``).
    """
    connector = _get_connector(request)

    if q:
        results = await connector.search(q, limit=limit, source_type="reference")
    else:
        results = await connector.list_content_items(limit=max(limit, 500), source_type="reference")
        results = list(results or [])

    if source_tag:
        results = [r for r in results if _extract_source_tag(r) == source_tag]
    return results[:limit]


@router.get("/failures")
async def list_failures(request: Request, limit: int = Query(100, ge=1, le=500)):
    """List tasks demoted to a reading lane because KS ingest gave up.

    Pulls open ``todoist_tasks`` carrying the ``@to-read`` label. Each row
    includes the demotion comment when available so the UI can surface
    the reason raphael couldn't file it.
    """
    pool = _get_pool(request)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                t.id,
                t.content AS title,
                t.description,
                t.labels,
                t.source_tag,
                t.updated_at,
                t.last_clarified_at,
                (
                    SELECT content FROM todoist_notes
                    WHERE item_id = t.id
                      AND content LIKE $2
                    ORDER BY posted_at DESC
                    LIMIT 1
                ) AS demotion_note
            FROM todoist_tasks t
            WHERE NOT t.is_completed
              AND '@to-read' = ANY(t.labels)
            ORDER BY t.updated_at DESC
            LIMIT $1
            """,
            limit,
            f"{CLARIFY_NOTE_PREFIX}ref-demote]%",
        )
    out: list[dict[str, Any]] = []
    for row in rows:
        record = dict(row)
        record["labels"] = list(record.get("labels") or [])
        out.append(record)
    return out


@router.get("/{content_id}")
async def get_reference(request: Request, content_id: str) -> dict[str, Any]:
    """Reference detail — status metadata + chunked body.

    Returns ``{"content": <status-dict>, "chunks": [<chunk>, ...]}`` so the
    UI can render a single round-trip detail view. 404 only when KS itself
    returns 404; transient KS failures bubble up as 5xx.
    """
    connector = _get_connector(request)
    try:
        status = await connector.get_content_status(content_id)
    except Exception as exc:  # noqa: BLE001
        # Translate httpx 4xx into FastAPI 404; let other errors propagate.
        import httpx

        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404:
            raise HTTPException(status_code=404, detail="Reference not found") from exc
        raise
    chunks: list[dict[str, Any]] = []
    try:
        chunks = await connector.get_content_chunks(content_id)
    except Exception:  # noqa: BLE001
        pass
    return {"content": status, "chunks": chunks}
