"""Knowledge endpoints — native pgvector subsystem (search/ask/ingest/seed)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel

from aegis.api.auth import verify_auth
from aegis.api.deps import get_knowledge_connector as _get_connector
from aegis.api.deps import get_settings
from aegis.config import Settings

router = APIRouter(prefix="/api/knowledge", dependencies=[Depends(verify_auth)])

# Extensions the folder/upload seeders will try to extract.
_TEXT_EXTS = {".txt", ".md", ".markdown", ".html", ".htm", ".pdf", ".json", ".csv", ".rst"}


@router.get("/search")
async def search(
    request: Request,
    q: str = Query(..., description="Search query"),
    limit: int = Query(10, ge=1, le=100),
    source_type: str | None = None,
    tags: list[str] | None = Query(None),
) -> list[dict[str, Any]]:
    """Semantic search across ingested content."""
    connector = _get_connector(request)
    return await connector.search(q, limit=limit, source_type=source_type, tags=tags)


class AskRequest(BaseModel):
    question: str
    max_sources: int = 5
    min_confidence: float = 0.0


@router.post("/ask")
async def ask(request: Request, body: AskRequest) -> dict[str, Any]:
    """RAG-powered Q&A with sources and confidence."""
    connector = _get_connector(request)
    return await connector.ask(
        body.question,
        max_sources=body.max_sources,
        min_confidence=body.min_confidence,
    )


class IngestRequest(BaseModel):
    url: str
    title: str | None = None
    source_type: str | None = None
    summary: str | None = None
    raw_text: str | None = None
    tags: list[str] | None = None
    knowledge: list[dict] | None = None
    metadata: dict | None = None


@router.post("/ingest")
async def ingest(request: Request, body: IngestRequest) -> dict[str, Any]:
    """Ingest content. If no raw_text is given and the url is http(s), the page
    is fetched and its readable text extracted before embedding."""
    connector = _get_connector(request)
    raw_text = body.raw_text
    title = body.title
    if not raw_text and body.url.startswith(("http://", "https://")):
        from aegis.services.content_extract import fetch_and_extract

        text, extracted_title = await fetch_and_extract(body.url, body.source_type)
        raw_text = text or None
        title = title or extracted_title
    return await connector.ingest_content(
        url=body.url,
        title=title or body.url,
        source_type=body.source_type or "content",
        summary=body.summary,
        raw_text=raw_text,
        tags=body.tags,
        knowledge=body.knowledge,
        metadata=body.metadata,
    )


@router.post("/upload")
async def upload(
    request: Request,
    file: UploadFile = File(...),
    source_type: str = Form("upload"),
    tags: str = Form(""),  # comma-separated
) -> dict[str, Any]:
    """Seed knowledge from an uploaded file (pdf / html / txt / md / …)."""
    from aegis.services.content_extract import extract_bytes

    connector = _get_connector(request)
    data = await file.read()
    text, extracted_title = extract_bytes(data, file.content_type or "", file.filename or "")
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    return await connector.ingest_content(
        url=f"upload://{file.filename}",
        title=extracted_title or file.filename or "upload",
        source_type=source_type,
        raw_text=text,
        tags=tag_list,
        metadata={"filename": file.filename, "via": "upload"},
    )


class FolderIngestRequest(BaseModel):
    path: str
    source_type: str = "upload"
    tags: list[str] | None = None
    recursive: bool = True


@router.post("/ingest-folder")
async def ingest_folder(request: Request, body: FolderIngestRequest) -> dict[str, Any]:
    """Seed knowledge from a server-readable folder of documents.

    Walks `path` (server filesystem — the core container's view), extracts each
    supported file, and ingests it. Returns counts. NOTE: Google Drive / cloud
    folders are a separate follow-up (need the Drive API wired like Gmail); this
    handles local/mounted paths only.
    """
    import os
    from pathlib import Path

    from aegis.services.content_extract import extract_bytes

    connector = _get_connector(request)
    root = Path(body.path)
    if not root.is_dir():
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail=f"not a directory: {body.path}")

    ingested, skipped, errors = 0, 0, 0
    walk = os.walk(root) if body.recursive else [(str(root), [], [f.name for f in root.iterdir()])]
    for dirpath, _dirs, files in walk:
        for name in files:
            fp = Path(dirpath) / name
            if fp.suffix.lower() not in _TEXT_EXTS:
                skipped += 1
                continue
            try:
                text, extracted_title = extract_bytes(fp.read_bytes(), "", name)
                if not text:
                    skipped += 1
                    continue
                await connector.ingest_content(
                    url=f"file://{fp}",
                    title=extracted_title or name,
                    source_type=body.source_type,
                    raw_text=text,
                    tags=body.tags,
                    metadata={"path": str(fp), "via": "folder"},
                )
                ingested += 1
            except Exception:  # noqa: BLE001 — one bad file shouldn't abort the sweep
                errors += 1
    return {"ingested": ingested, "skipped": skipped, "errors": errors}


class DriveIngestRequest(BaseModel):
    folder_id: str
    account: str  # gmail account label whose token to use (gmail_token_dir/<account>.json)
    source_type: str = "drive"
    tags: list[str] | None = None


@router.post("/ingest-drive")
async def ingest_drive(
    request: Request,
    body: DriveIngestRequest,
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Seed knowledge from a Google Drive folder.

    Reuses the Gmail OAuth token for `account` — it must be re-authorized with
    the Drive scope (see gmail_reauth). Lists the folder, extracts each file
    (Google Docs exported to text; pdf/txt/md/… downloaded), and ingests.
    """
    from aegis.services.drive import ingest_drive_folder

    connector = _get_connector(request)
    token_path = Path(settings.gmail_token_dir) / f"{body.account}.json"
    try:
        return await ingest_drive_folder(
            connector, token_path, body.folder_id, body.source_type, body.tags
        )
    except Exception as exc:  # noqa: BLE001
        # Most likely: token missing or lacks the Drive scope (needs re-auth).
        raise HTTPException(
            status_code=400,
            detail=(
                f"drive_ingest_failed: {exc!s} — ensure account '{body.account}' is "
                "re-authorized with Drive read access (Admin → Gmail re-auth)."
            ),
        ) from exc


@router.get("/content")
async def list_content(
    request: Request,
    limit: int = Query(200, ge=1, le=500),
) -> list[dict[str, Any]]:
    """List recently ingested content items."""
    connector = _get_connector(request)
    return await connector.list_content_items(limit=limit)


@router.get("/content/{content_id}")
async def get_content(request: Request, content_id: str) -> dict[str, Any]:
    """Get ingestion status + metadata for a single content item.

    Merges the per-id status payload (job state) with title/url/source_type
    from the recent-items list — knowledge-service does not expose a per-id
    metadata endpoint, but the bulk listing is fast (~50ms for 2000 items).
    """
    connector = _get_connector(request)
    status = await connector.get_content_status(content_id)
    try:
        items = await connector.list_content_items(limit=2000)
        match = next((it for it in items if it.get("id") == content_id), None)
        if match:
            for key in ("title", "url", "source_type", "tags", "ingested_at"):
                if key not in status and key in match:
                    status[key] = match[key]
    except Exception:
        # Title enrichment is best-effort — never block the status response.
        pass
    return status


@router.get("/content/{content_id}/chunks")
async def get_content_chunks(request: Request, content_id: str) -> list[dict[str, Any]]:
    """List semantic chunks extracted from a content item."""
    connector = _get_connector(request)
    return await connector.get_content_chunks(content_id)


@router.get("/jobs")
async def list_jobs(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
) -> list[dict[str, Any]]:
    """Recent ingestion jobs."""
    connector = _get_connector(request)
    return await connector.get_recent_jobs(limit=limit)


@router.get("/jobs/{content_id}")
async def get_job_status(request: Request, content_id: str) -> dict[str, Any]:
    """Ingestion job status for a content item."""
    connector = _get_connector(request)
    return await connector.get_content_status(content_id)


@router.get("/health")
async def knowledge_health(request: Request) -> dict[str, Any]:
    """Aggregated knowledge health metrics."""
    pool = request.app.state.db_pool
    connector = getattr(request.app.state, "knowledge_connector", None)

    kg_stats: dict[str, Any] = {}
    if connector:
        try:
            kg_stats = await connector.get_stats()
        except Exception:
            kg_stats = {"error": "unavailable"}

    source_rows = await pool.fetch("SELECT * FROM knowledge_source_quality ORDER BY source_type")
    source_quality = [dict(r) for r in source_rows]
    injection_stats = await pool.fetchval(
        "SELECT COUNT(*) FROM knowledge_injection_log WHERE created_at > now() - interval '30 days'"
    )

    return {
        "kg_stats": kg_stats,
        "source_quality": source_quality,
        "injection_log_30d": injection_stats or 0,
    }
