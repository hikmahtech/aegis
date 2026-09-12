"""NotesActivities — Raphael's Obsidian vault, on the worker (#514).

Every write goes through `aegis.services.notes`, the only module that touches
the vault (append-only, pushed or reported, never force-pushed); the chat
writes go through `aegis.services.notes_write`, the same code the tool
validated against. Spec: `docs/superpowers/specs/2026-09-12-raphael-notes-design.md`.

Four jobs:

* `notes_write` — one chat write, for `NotesWriteFlow`.
* `notes_journal_write` — the daylog's entry into the journal. Never raises:
  anything but `written`/`exists` sends the daylog back to its knowledge row,
  so a vault problem never loses a day.
* `notes_index_vault` — the incremental index behind `NotesSyncFlow`. It
  leaves out `UNINDEXED_PREFIXES`.
* `notes_backfill_journal` — the daylog's knowledge rows into the journal,
  for the weekly `NotesBackfillFlow`.

A time written into a note (a new note's template placeholders) is on the
user's clock (`user_timezone`), not the container's UTC one.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date
from typing import Any

from aegis.services import notes
from aegis.services import notes_write as nw
from aegis.services.knowledge import _content_id_for
from aegis.services.user_time import user_now
from temporalio import activity

from aegis_worker.activities.daylog import _stitch

INDEX_STATE_KEY = "notes_index_state"
# Files per indexing run; the rest wait for the next run.
DEFAULT_INDEX_BATCH = 300
# Journal entries per backfill commit.
BACKFILL_BATCH = 50
# Notes the index leaves out. `raphael/questions/` holds ResearchFlow's answers,
# which the flow also keeps in the knowledge store (`aegis://research/<hash>`),
# so indexing the note put every answer in retrieval twice. A row an earlier
# run made for one is dropped on the next run.
UNINDEXED_PREFIXES = (f"{notes.RAPHAEL_DIR}/questions/",)

# Newest first: a weekly run is for the recent days whose vault write failed
# and fell back to their knowledge row; the old rows went in on the first run.
_BACKFILL_SQL = """
SELECT c.source_type, c.metadata,
       array_agg(k.chunk_text ORDER BY k.chunk_index)
         FILTER (WHERE k.chunk_text IS NOT NULL) AS chunks
  FROM knowledge_content c
  LEFT JOIN knowledge_chunks k ON k.content_id = c.content_id
 WHERE c.source_type IN ('daylog', 'daylog_rollup')
 GROUP BY c.content_id, c.source_type, c.metadata
 ORDER BY COALESCE(c.metadata->>'date', c.metadata->>'start') DESC
 LIMIT $1
"""


@dataclass
class NotesActivities:
    settings: Any = None
    db_pool: Any = None
    knowledge_connector: Any = None

    def _cfg(self) -> notes.NotesConfig:
        return notes.config_from_settings(self.settings)

    # ------------------------------------------------------------ writes

    @activity.defn
    async def notes_write(self, op: str, payload: dict) -> dict:
        """One vault write on behalf of a chat tool. `{"ok", "message"}`; a
        refusal comes back as `ok: False`, never as a raise, because a raise
        here is a Temporal retry of a write that was deliberately turned down."""
        cfg = self._cfg()
        if not cfg.configured:
            return {
                "ok": False,
                "message": "error: the vault is not configured on the worker, so the write "
                "could not run. Nothing was written.",
            }
        return await nw.perform_write(op, payload, cfg)

    @activity.defn
    async def notes_journal_write(self, entry: dict) -> dict:
        """The daylog's entry for one day, week or month, appended to the
        journal note. `{"status": written | exists | not_configured | error,
        "path", "error"}`.

        `entry`: `kind` (daily / weekly / monthly), `day` (the day; the ISO
        week's Monday; the month's first day), `label` (the daylog's own label)
        and `text`.
        """
        cfg = self._cfg()
        if not cfg.configured:
            return {"status": "not_configured"}
        try:
            ap = notes.journal_append(
                str(entry["kind"]),
                date.fromisoformat(str(entry["day"])),
                str(entry["label"]),
                str(entry.get("text") or ""),
                await user_now(self.db_pool),
            )
            res = await notes.write(cfg, [ap], f"raphael: journal {entry['label']}")
        except notes.NotesDisabled:
            return {"status": "not_configured"}
        except (notes.NotesError, KeyError, ValueError) as exc:
            activity.logger.warning("notes_journal_write_failed err=%s", str(exc)[:300])
            return {"status": "error", "error": str(exc)[:300]}
        # The note it actually went to: the filed path, or the day's live note
        # at the journal root when the user had one open.
        outcomes = res.get("outcomes") or [{}]
        return {"status": res["status"], "path": outcomes[0].get("path") or ap.rel}

    # ------------------------------------------------------------- index

    async def _state(self) -> dict:
        value = await self.db_pool.fetchval(
            "SELECT value FROM settings WHERE key = $1", INDEX_STATE_KEY
        )
        return value if isinstance(value, dict) else {}

    async def _save_state(self, state: dict) -> None:
        await self.db_pool.execute(
            "INSERT INTO settings (key, value) VALUES ($1, $2) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            INDEX_STATE_KEY,
            state,
        )

    async def _delete(self, content_id: str, what: str) -> int:
        try:
            return int(bool(await self.knowledge_connector.delete_content(content_id)))
        except Exception as exc:  # noqa: BLE001 — a stale row is retried next pass
            activity.logger.warning("notes_unindex_failed path=%s err=%s", what, str(exc)[:200])
            return 0

    async def _unindex(self, rel: str) -> int:
        return await self._delete(_content_id_for(notes.note_url(rel)), rel)

    async def _drop_unindexed(self) -> int:
        """Remove the index rows of notes the index leaves out, which runs
        before `UNINDEXED_PREFIXES` existed made. One small query a run; after
        the first run it finds nothing."""
        rows = await self.db_pool.fetch(
            "SELECT content_id, url FROM knowledge_content "
            "WHERE source_type = 'note' AND url LIKE ANY($1::text[])",
            [notes.note_url(prefix) + "%" for prefix in UNINDEXED_PREFIXES],
        )
        removed = 0
        for r in rows:
            removed += await self._delete(r["content_id"], r["url"])
        return removed

    @activity.defn
    async def notes_index_vault(self, max_files: int = DEFAULT_INDEX_BATCH) -> dict:
        """Index what changed in the vault since the last full pass.

        A pass is the list of notes changed between the last indexed commit and
        HEAD (every note on the first run). It is kept in
        `settings.notes_index_state` and worked through `max_files` at a time,
        so the first pass over ~1,000 notes spreads over a few runs. A note that
        fails to index is retried at the start of the next pass; a deleted or
        emptied note leaves the index. Encrypted blocks are stripped by the
        read, before anything is embedded. Notes under `UNINDEXED_PREFIXES` are
        never indexed.
        """
        cfg = self._cfg()
        if not cfg.configured:
            return {"status": "not_configured"}
        if self.knowledge_connector is None or self.db_pool is None:
            return {"status": "no_knowledge_store"}
        batch_size = max(1, int(max_files or DEFAULT_INDEX_BATCH))
        state = await self._state()

        removed = await self._drop_unindexed()
        if state.get("target") and isinstance(state.get("todo"), list):
            # A pass in progress is pinned to the commit it started from and
            # finishes even while HEAD moves (the nightly daylog commit, the
            # phone's obsidian-git every few minutes); what changed after it is
            # the NEXT pass, diffed from `target`. Restarting on every new HEAD
            # meant a first pass over ~1,000 notes never got past one batch.
            target = str(state["target"])
            full = bool(state.get("full"))
            todo = [p for p in state["todo"] if isinstance(p, str)]
            done = int(state.get("done") or 0)
            retry = [p for p in state.get("retry") or [] if isinstance(p, str)]
        else:
            changes = await asyncio.to_thread(notes.vault_changes_sync, cfg, state.get("commit"))
            target, full = changes.head, changes.full
            # A new pass: last pass's failures first, then what changed. Deleted
            # notes leave the index now, once per pass.
            prior = [p for p in state.get("retry") or [] if isinstance(p, str)]
            todo = list(dict.fromkeys([*prior, *changes.changed]))
            done = 0
            retry = []
            for rel in changes.deleted:
                removed += await self._unindex(rel)

        batch = todo[done : done + batch_size]
        wanted = [rel for rel in batch if not rel.startswith(UNINDEXED_PREFIXES)]
        texts = await asyncio.to_thread(notes.read_many_sync, cfg, wanted) if wanted else {}
        indexed = 0
        for rel in wanted:
            text = texts.get(rel)
            if text is None or not text.strip():
                removed += await self._unindex(rel)
                continue
            try:
                await self.knowledge_connector.ingest_content(
                    url=notes.note_url(rel),
                    title=notes.note_title(rel),
                    source_type="note",
                    summary=text[:500],
                    raw_text=text[: notes.INDEX_MAX_CHARS],
                    tags=["note", notes.top_folder(rel) or "root"],
                    metadata={"path": rel, "folder": notes.top_folder(rel), "commit": target},
                )
                indexed += 1
            except Exception as exc:  # noqa: BLE001 — one note must not stop the pass
                retry.append(rel)
                activity.logger.warning("notes_index_failed path=%s err=%s", rel, str(exc)[:200])

        done += len(batch)
        remaining = max(0, len(todo) - done)
        if remaining == 0:
            # The pass is done: the next one diffs from where this one started.
            await self._save_state({"commit": target, "retry": retry})
        else:
            await self._save_state(
                {
                    "commit": state.get("commit"),
                    "target": target,
                    "full": full,
                    "todo": todo,
                    "done": done,
                    "retry": retry,
                }
            )
        return {
            "status": "ok",
            "head": target[:12],
            "full": full,
            "indexed": indexed,
            "removed": removed,
            "failed": len(retry),
            "remaining": remaining,
        }

    # ---------------------------------------------------------- backfill

    @activity.defn
    async def notes_backfill_journal(self, limit: int = 1000) -> dict:
        """Write the daylog's knowledge rows into the matching journal notes,
        newest first. The markers are the ones the live daylog uses, so a day
        already in the journal is left alone and a run with nothing missing
        writes nothing."""
        cfg = self._cfg()
        if not cfg.configured:
            return {"status": "not_configured"}
        if self.db_pool is None:
            return {"status": "no_database"}
        rows = await self.db_pool.fetch(_BACKFILL_SQL, max(1, int(limit)))
        appends: list[notes.Append] = []
        skipped = 0
        now = await user_now(self.db_pool)
        for r in rows:
            meta = r["metadata"] if isinstance(r["metadata"], dict) else {}
            text = _stitch(list(r["chunks"] or []))
            try:
                if r["source_type"] == "daylog":
                    label = str(meta["date"])
                    appends.append(
                        notes.journal_append("daily", date.fromisoformat(label), label, text, now)
                    )
                else:
                    kind = str(meta.get("period") or "")
                    appends.append(
                        notes.journal_append(
                            kind, date.fromisoformat(str(meta["start"])), str(meta["label"]),
                            text, now,
                        )
                    )
            except (KeyError, ValueError, notes.NotesError):
                skipped += 1
        written = 0
        existed = 0
        for i in range(0, len(appends), BACKFILL_BATCH):
            chunk = appends[i : i + BACKFILL_BATCH]
            res = await notes.write(cfg, chunk, f"raphael: journal backfill ({len(chunk)})")
            # Per append, not per path: a day may go to its live root note
            # instead of the filed one, so `ap.rel` is not where it landed.
            outcomes = res.get("outcomes") or []
            written += sum(1 for o in outcomes if o.get("changed"))
            existed += sum(1 for o in outcomes if not o.get("changed"))
        return {
            "status": "ok",
            "entries": len(appends),
            "written": written,
            "already_there": existed,
            "skipped": skipped,
        }
