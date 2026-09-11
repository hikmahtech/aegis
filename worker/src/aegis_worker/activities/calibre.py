"""CalibreActivities — keep the knowledge store's index of the library (#510).

Calibre is the record and the knowledge store only indexes it: one
`source_type='book'` row per book, holding its metadata and description,
never its text. `sync_calibre_library` makes the index match the library —
it adds new books, refreshes changed ones, skips the unchanged (by
fingerprint, so a book is not re-embedded every day) and removes the rows of
books that left Calibre, saying which.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aegis.services import library
from aegis.services.connector_health import record_connector_health
from aegis.services.knowledge import _content_id_for
from temporalio import activity

# A catalogue this much smaller than the index is not trusted to delete from
# it: a calibre-web answering with half a page (a bad library mount, a user
# whose visibility was narrowed) must not wipe the index. The run reports
# `removal_withheld` instead, and the rows stay until a full catalogue says
# the books really went.
_REMOVAL_FLOOR = 0.5


@dataclass
class CalibreActivities:
    knowledge_connector: Any = None
    db_pool: Any = None
    settings: Any = None

    @activity.defn
    async def sync_calibre_library(self) -> dict:
        """Make the book index match the library. Returns counts, never book text.

        Not configured is a reported no-op, not an error, so the schedule can
        ship active and inert. A calibre-web that cannot be read raises, after
        recording connector health, so the run fails visibly.
        """
        conn, reason = library.connector_or_reason(self.settings)
        if conn is None:
            return {"status": reason}
        if self.knowledge_connector is None or self.db_pool is None:
            return {"status": "no_knowledge_store"}
        try:
            books = await conn.catalog(use_cache=False)
        except Exception as exc:
            await record_connector_health(
                self.db_pool, self.settings, "calibre", ok=False, error=str(exc)
            )
            raise
        await record_connector_health(self.db_pool, self.settings, "calibre", ok=True)

        rows = await self.db_pool.fetch(
            "SELECT content_id, title, metadata FROM knowledge_content WHERE source_type = $1",
            library.BOOK_SOURCE_TYPE,
        )
        indexed = {r["content_id"]: r for r in rows}

        added = updated = unchanged = failed = 0
        seen: set[str] = set()
        for book in books:
            doc = library.book_document(book)
            content_id = _content_id_for(doc["url"])
            seen.add(content_id)
            existing = indexed.get(content_id)
            md = (existing["metadata"] or {}) if existing is not None else {}
            if existing is not None and md.get("fingerprint") == doc["metadata"]["fingerprint"]:
                unchanged += 1
                continue
            try:
                await self.knowledge_connector.ingest_content(
                    url=doc["url"],
                    title=doc["title"],
                    source_type=library.BOOK_SOURCE_TYPE,
                    summary=doc["summary"],
                    raw_text=doc["raw_text"],
                    tags=doc["tags"],
                    metadata=doc["metadata"],
                )
            except Exception as exc:  # noqa: BLE001 — one book must not cost the rest
                failed += 1
                activity.logger.warning(
                    "calibre_book_index_failed id=%s err=%s", book.get("id"), str(exc)[:200]
                )
                continue
            if existing is None:
                added += 1
            else:
                updated += 1

        gone = [cid for cid in indexed if cid not in seen]
        summary: dict[str, Any] = {
            "status": "ok",
            "books": len(books),
            "added": added,
            "updated": updated,
            "unchanged": unchanged,
            "failed": failed,
        }
        if gone:
            if not books or len(books) < _REMOVAL_FLOOR * len(indexed):
                summary["removal_withheld"] = len(gone)
                activity.logger.warning(
                    "calibre_removal_withheld catalogue=%d indexed=%d", len(books), len(indexed)
                )
            else:
                removed: list[str] = []
                for cid in gone:
                    if await self.knowledge_connector.delete_content(cid):
                        removed.append(indexed[cid]["title"] or cid)
                summary["removed"] = len(removed)
                summary["removed_titles"] = removed[:20]
        return summary
