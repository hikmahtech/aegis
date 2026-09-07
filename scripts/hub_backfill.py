"""Backfill the problem hub from the open `#alert` Todoist tasks.

One-time, run once after PR 3b deploys (spec §13, step 3). Every open task
AEGIS created for an alert becomes a `problems` row that owns that task, so
the next occurrence of the same alert attaches to the existing task instead of
minting a ninth copy (#341), and a `resolved` closes it (#279).

Dry-run by default: prints what it would do. `--apply` writes.

    python scripts/hub_backfill.py --database-url postgresql://... [--apply]

Reads, when present, the retired `alert_dedup_index` for recurrence counts —
the table is dropped by a later migration, after this has run. Duplicate tasks
for one problem (same correlation key) are completed through the outbox; the
oldest stays as the problem's task.

Run it in the WORKER container, not core: it imports `aegis_worker` for the
same `extract_service_name` the coding lane uses, and the core image does not
carry that package.

    docker exec <aegis_worker> python /tmp/hub_backfill.py [--apply]

A task whose class and subject cannot be read gets an EMPTY key and therefore
its own problem. That is deliberate: a shared fallback key would merge
unrelated alerts, and the hub's own rule is that creating a duplicate is
recoverable while attaching to the wrong problem hides an outage.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from datetime import UTC, datetime

_HEARTBEAT = re.compile(r"^aegis-heartbeat:([^:]+):(.*)$")
_NODE_DOWN = re.compile(r"\bnode\b.*\bdown\b", re.I)
_SERVICE_DOWN = re.compile(r"prolonged|degraded|fewer tasks|below desired|\bdown\b|unhealthy", re.I)


def classify(title: str, fingerprint: str, service_from_title: str) -> tuple[str, str, str]:
    """(class, subject, subject_kind) for a legacy alert task."""
    m = _HEARTBEAT.match(fingerprint or "")
    if m:
        klass, subject = m.group(1), m.group(2)
        kind = "node" if klass.lower() == "nodedown" else "service"
        if klass.lower() == "servicedownprolonged":
            klass = "DockerServiceDown"  # one problem per service, PR 3b
        return klass, subject, kind
    if _NODE_DOWN.search(title):
        node = re.search(r"\bnode\s+([\w.-]+)", title, re.I)
        return "NodeDown", (node.group(1) if node else ""), "node"
    if service_from_title and _SERVICE_DOWN.search(title):
        return "DockerServiceDown", service_from_title, "service"
    if service_from_title:
        return "manual", service_from_title, "service"
    # Neither a class nor a subject could be read out of this task. Returning
    # `manual` with no subject would key it `manual::` — a key that matches no
    # producer's, and that EVERY other unparseable task also matches, so a run
    # folds a dozen unrelated alerts into one problem. Two empty strings key it
    # `''` instead, which `correlation_key` defines as "creates, never
    # attaches". Found in production on 2026-09-08: two nodes' overlay alerts
    # were merged into one problem by this line.
    return "", "", ""


async def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--database-url", default=os.getenv("AEGIS_DATABASE_URL", ""))
    ap.add_argument("--apply", action="store_true", help="write; default is a dry run")
    args = ap.parse_args(argv)
    if not args.database_url:
        print("need --database-url or AEGIS_DATABASE_URL", file=sys.stderr)
        return 2

    from aegis.connectors.todoist import TodoistConnector
    from aegis.db import create_pool
    from aegis.services.hub import Event, correlation_key, get_problem
    from aegis_worker.activities.agent_task import extract_service_name

    pool = await create_pool(args.database_url, min_size=1, max_size=2)
    try:
        tasks = await pool.fetch(
            "SELECT t.id, t.content, t.assignee_label, ci.external_id, ci.captured_at "
            "FROM todoist_tasks t "
            "LEFT JOIN todoist_capture_idempotency ci ON ci.todoist_task_ref = t.id "
            "  AND ci.source_tag = '#alert' "
            "WHERE t.source_tag = '#alert' AND NOT t.is_completed "
            "  AND NOT EXISTS (SELECT 1 FROM problems p WHERE p.todoist_task_id = t.id) "
            "ORDER BY ci.captured_at NULLS LAST, t.updated_at"
        )
        has_index = await pool.fetchval("SELECT to_regclass('public.alert_dedup_index') IS NOT NULL")
        counts: dict[str, tuple[int, datetime | None]] = {}
        if has_index:
            for r in await pool.fetch(
                "SELECT task_id, occurrence_count, last_seen_at FROM alert_dedup_index"
            ):
                counts[r["task_id"]] = (int(r["occurrence_count"] or 1), r["last_seen_at"])

        now = datetime.now(UTC)
        created = merged = 0
        for t in tasks:
            ext = str(t["external_id"] or "")
            fp = ext[len("alert-") :] if ext.startswith("alert-") else ""
            title = str(t["content"] or "")
            klass, subject, kind = classify(title, fp, extract_service_name(title))
            key = correlation_key(
                Event(source="manual", external_id="x", kind="occurrence", title=title,
                      klass=klass, subject=subject, subject_kind=kind)
            )
            occurrences, last_seen = counts.get(t["id"], (1, None))
            first_seen = t["captured_at"] or now
            last_seen = last_seen or first_seen
            existing = (
                await pool.fetchrow(
                    "SELECT id::text AS id, todoist_task_id FROM problems "
                    "WHERE correlation_key = $1 AND closed_at IS NULL",
                    key,
                )
                if key
                else None
            )
            if existing is not None:
                # A second task for a problem another task already owns: the
                # duplicate the hub exists to prevent. Close it.
                merged += 1
                print(f"MERGE  {t['id']}  {title[:60]!r}  -> problem {existing['id']} (task {existing['todoist_task_id']})")
                if args.apply:
                    await pool.execute(
                        "UPDATE problems SET occurrences = occurrences + $2, "
                        "last_seen_at = GREATEST(last_seen_at, $3) WHERE id = $1::uuid",
                        existing["id"], occurrences, last_seen,
                    )
                    if t["assignee_label"] != "@me":
                        await pool.execute(
                            "INSERT INTO todoist_outbox (temp_id, command, status) "
                            "VALUES ($1, $2, 'pending') ON CONFLICT (temp_id) DO NOTHING",
                            f"problem-close-{t['id']}",
                            TodoistConnector.build_item_complete_command(t["id"]),
                        )
                        await pool.execute(
                            "UPDATE todoist_tasks SET is_completed = true, updated_at = now() WHERE id = $1",
                            t["id"],
                        )
                continue

            created += 1
            print(f"CREATE {t['id']}  {title[:60]!r}  key={key or '(none)'}  x{occurrences}")
            if not args.apply:
                continue
            async with pool.acquire() as conn, conn.transaction():
                pid = await conn.fetchval(
                    "INSERT INTO problems (correlation_key, class, subject, subject_kind, title, "
                    "severity, status, first_seen_at, last_seen_at, occurrences, todoist_task_id, "
                    "metadata) VALUES ($1, $2, $3, $4, $5, 'warning', 'open', $6, $7, $8, $9, $10) "
                    "RETURNING id::text",
                    key, klass.lower() or "manual", subject.lower(), kind, title[:500],
                    first_seen, last_seen, occurrences, t["id"], {"backfilled": True},
                )
                eid = await conn.fetchval(
                    "INSERT INTO problem_events (problem_id, source, external_id, kind, severity, "
                    "payload, occurred_at) VALUES ($1::uuid, 'manual', $2, 'occurrence', 'warning', "
                    "$3, $4) RETURNING id",
                    pid, f"backfill:{t['id']}", {"fingerprint": fp, "backfill": True}, first_seen,
                )
                # The task already tells the story: nothing is replayed as comments.
                await conn.execute(
                    "UPDATE problems SET metadata = $2 WHERE id = $1::uuid",
                    pid, {"backfilled": True, "projected_event_id": int(eid), "pending_occurrences": 0},
                )
                await conn.execute(
                    "INSERT INTO problem_links (problem_id, link_kind, ref) VALUES ($1::uuid, 'todoist_task', $2) "
                    "ON CONFLICT DO NOTHING",
                    pid, t["id"],
                )
                await conn.execute(
                    "INSERT INTO todoist_capture_idempotency (source_tag, external_id, todoist_task_ref) "
                    "VALUES ('#alert', $1, $2) ON CONFLICT (source_tag, external_id) DO NOTHING",
                    f"problem-{pid}", t["id"],
                )
            assert await get_problem(pool, pid)
        print(f"\n{'applied' if args.apply else 'dry run'}: {created} problems created, {merged} duplicate tasks merged")
        return 0
    finally:
        await pool.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
