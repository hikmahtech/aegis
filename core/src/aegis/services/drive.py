"""Google Drive folder ingestion into the native knowledge subsystem.

Reuses the Gmail OAuth token (same per-account token file under
`gmail_token_dir/<account>.json`) — it just needs the `drive.readonly` scope,
which is added to `gmail_reauth._SCOPES`. Re-authorize the account once after
deploying for Drive reads to work.

Google Docs/Sheets/Slides are exported to text; ordinary files (pdf/txt/md/…)
are downloaded; each is run through the shared `content_extract` then ingested.
The sync Drive API is run in a worker thread so the async route isn't blocked.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio
import structlog

from aegis.services.content_extract import extract_bytes

logger = structlog.get_logger()

# Google-native MIME → (export MIME, filename suffix for the extractor)
_GOOGLE_EXPORT = {
    "application/vnd.google-apps.document": ("text/plain", ".txt"),
    "application/vnd.google-apps.spreadsheet": ("text/csv", ".csv"),
    "application/vnd.google-apps.presentation": ("text/plain", ".txt"),
}
_SUPPORTED_EXTS = {".pdf", ".txt", ".md", ".markdown", ".html", ".htm", ".csv", ".json", ".rst"}


def _build_drive_service(token_path: Path):
    """Build a Drive v3 client from the gmail token. Raises on missing/invalid token."""
    from google.auth.exceptions import RefreshError
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    if not token_path.exists():
        raise RefreshError(f"token_missing:{token_path}")
    creds = Credentials.from_authorized_user_file(str(token_path))
    if creds.expired and creds.refresh_token:
        from google.auth.transport.requests import Request as GoogleRequest

        creds.refresh(GoogleRequest())
        token_path.write_text(creds.to_json())
    return build("drive", "v3", credentials=creds, cache_discovery=False)


_FOLDER_MIME = "application/vnd.google-apps.folder"


def _list_folder(svc, folder_id: str, recurse: bool = False) -> list[dict]:
    """List ingestable files in a folder (subfolders excluded). With recurse=True,
    descends into subfolders. Each file dict has id, name, mimeType, modifiedTime."""
    files: list[dict] = []
    subfolders: list[str] = []
    page_token = None
    while True:
        resp = (
            svc.files()
            .list(
                q=f"'{folder_id}' in parents and trashed=false",
                fields="nextPageToken, files(id, name, mimeType, modifiedTime)",
                pageSize=100,
                pageToken=page_token,
            )
            .execute()
        )
        for f in resp.get("files", []):
            if f.get("mimeType") == _FOLDER_MIME:
                subfolders.append(f["id"])
            else:
                files.append(f)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    if recurse:
        for sub in subfolders:
            files.extend(_list_folder(svc, sub, recurse=True))
    return files


def _download(svc, f: dict) -> bytes:
    mime = f["mimeType"]
    if mime in _GOOGLE_EXPORT:
        export_mime = _GOOGLE_EXPORT[mime][0]
        data = svc.files().export(fileId=f["id"], mimeType=export_mime).execute()
    else:
        data = svc.files().get_media(fileId=f["id"]).execute()
    return data if isinstance(data, bytes) else str(data).encode("utf-8")


async def ingest_drive_folder(
    store: Any,
    token_path: Path,
    folder_id: str,
    source_type: str = "drive",
    tags: list[str] | None = None,
    recurse: bool = False,
    skip_unchanged: dict[str, str] | None = None,
) -> dict:
    """List a Drive folder, extract each supported file, ingest. Returns counts.

    `recurse` descends into subfolders. `skip_unchanged` is an optional
    {content_id: drive_modified_time} map — a file whose modifiedTime matches is
    skipped (incremental sync, so unchanged docs aren't re-embedded each run).
    """
    from aegis.services.knowledge import _content_id_for

    svc = await anyio.to_thread.run_sync(_build_drive_service, token_path)
    files = await anyio.to_thread.run_sync(_list_folder, svc, folder_id, recurse)

    ingested = skipped = unchanged = errors = 0
    for f in files:
        mime = f["mimeType"]
        name = f.get("name", f["id"])
        is_google = mime in _GOOGLE_EXPORT
        if not is_google and Path(name).suffix.lower() not in _SUPPORTED_EXTS:
            skipped += 1
            continue
        url = f"gdrive://{f['id']}"
        mtime = f.get("modifiedTime", "")
        if skip_unchanged is not None and mtime and skip_unchanged.get(_content_id_for(url)) == mtime:
            unchanged += 1
            continue
        try:
            data = await anyio.to_thread.run_sync(_download, svc, f)
            if is_google:
                ct, suffix = _GOOGLE_EXPORT[mime]
                text, title = extract_bytes(data, ct, name + suffix)
            else:
                text, title = extract_bytes(data, "", name)
            if not text:
                skipped += 1
                continue
            await store.ingest_content(
                url=url,
                title=title or name,
                source_type=source_type,
                raw_text=text,
                tags=tags,
                metadata={
                    "drive_file_id": f["id"],
                    "drive_name": name,
                    "drive_modified_time": mtime,
                    "via": "drive",
                },
            )
            ingested += 1
        except Exception:  # noqa: BLE001 — one bad file shouldn't abort the sweep
            errors += 1
            logger.warning("drive_file_ingest_failed", file=name)
    return {
        "ingested": ingested,
        "skipped": skipped,
        "unchanged": unchanged,
        "errors": errors,
    }
