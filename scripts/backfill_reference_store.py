"""One-shot backfill: push tasks already in the 🔖 Reference Todoist project
into knowledge-service via ingest_reference_to_ks.

Phase 5 added the reference store, but items routed to 🔖 Reference BEFORE
that change shipped don't have a corresponding KS entry with
source_type='reference'. This script iterates every non-completed task in
the Reference project and calls the same ingest path the live ClarifyFlow
uses. KS dedupes by URL, so re-running this is safe.

Usage:
    set -a; source .env.claude; set +a
    python scripts/backfill_reference_store.py --dry-run
    python scripts/backfill_reference_store.py --apply [--limit N]
"""

from __future__ import annotations

import argparse
import asyncio
import os

import structlog
from aegis.config import Settings
from aegis.connectors.knowledge import KnowledgeConnector
from aegis.db.pool import create_pool

logger = structlog.get_logger()


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report what would be ingested; no KS writes")
    parser.add_argument("--apply", action="store_true", help="actually call KS ingest")
    parser.add_argument("--limit", type=int, default=None, help="cap number of tasks processed")
    args = parser.parse_args()

    if not (args.dry_run or args.apply):
        parser.error("pass --dry-run or --apply")

    settings = Settings()
    pool = await create_pool(
        f"postgresql://{os.environ['PGUSER']}:{os.environ['PGPASSWORD']}"
        f"@{os.environ['PGHOST']}:{os.environ['PGPORT']}/{os.environ['PGDATABASE']}"
    )

    async with pool.acquire() as conn:
        ref_proj = await conn.fetchval(
            "SELECT value->>'reference' FROM settings "
            "WHERE key='todoist_managed_project_ids'"
        )
        if not ref_proj:
            print("ERROR: no reference project id in settings — bootstrap first")
            await pool.close()
            return 1

        rows = await conn.fetch(
            "SELECT id, content, description, source_tag, labels "
            "FROM todoist_tasks WHERE project_id=$1 AND NOT is_completed "
            "ORDER BY updated_at DESC " + (f"LIMIT {int(args.limit)}" if args.limit else ""),
            ref_proj,
        )
    print(f"Found {len(rows)} non-completed tasks in 🔖 Reference (project_id={ref_proj}).")

    if args.dry_run:
        for r in rows[:20]:
            print(f"  [{r['id']}] source_tag={r['source_tag']} content={(r['content'] or '')[:60]}")
        if len(rows) > 20:
            print(f"  ... and {len(rows) - 20} more")
        await pool.close()
        return 0

    # apply mode
    kc = KnowledgeConnector(
        base_url=getattr(settings, "knowledge_url", None) or "http://knowledge:8000",
        api_key=getattr(settings, "knowledge_api_key", "") or "",
    )

    # Reuse the same logic as ClarifyActivities.ingest_reference_to_ks.
    # Inlined here (not imported from worker) so the script doesn't pull
    # the worker package — it's a Core-side ops tool.
    import re

    def _extract_first_url(text: str) -> str | None:
        if not text:
            return None
        m = re.search(r"https?://[^\s)<>\"']+", text)
        if not m:
            return None
        return m.group(0).rstrip(".,;:")

    ok = err = skipped = 0
    for r in rows:
        task_id = r["id"]
        content = r["content"] or ""
        description = r["description"] or ""
        source_tag = r["source_tag"] or ""
        url = _extract_first_url(f"{content} {description}")
        title = content[:200] or "(untitled reference)"
        tags = ["gtd:reference"]
        if source_tag:
            tags.append(source_tag)
        metadata = {
            "todoist_task_id": task_id,
            "source_tag": source_tag,
            "captured_via": "gtd_clarify_backfill",
        }
        try:
            if url:
                result = await kc.ingest_content(
                    url=url, title=title, source_type="reference",
                    tags=tags, metadata=metadata,
                )
            else:
                raw_text = f"{content}\n\n{description}".strip()[:50_000]
                if not raw_text:
                    skipped += 1
                    continue
                result = await kc.ingest_content(
                    url=f"aegis://reference/{task_id}",
                    title=title,
                    source_type="reference",
                    raw_text=raw_text,
                    tags=tags,
                    metadata=metadata,
                )
            print(
                f"  OK [{task_id}] content_id={result.get('content_id')} "
                f"status={result.get('status')} url={url or '(synthetic)'}"
            )
            ok += 1
        except Exception as exc:
            print(f"  ERR [{task_id}] {type(exc).__name__}: {str(exc)[:120]}")
            err += 1

    print(f"\nBackfill summary: ok={ok}, err={err}, skipped={skipped}, total={len(rows)}")
    await kc.close()
    await pool.close()
    return 0 if err == 0 else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
