"""NotesActivities — the user's Obsidian vault, on the worker (#514).

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
  leaves out the layout's `index_skip_prefixes` and its `questions_dir`.
* `notes_backfill_journal` — the daylog's knowledge rows into the journal,
  for the weekly `NotesBackfillFlow`.

Where the notes go is the vault layout (`vault_layout` settings row), read
from the pool on every call so a change on the admin page applies without a
restart. A time written into a note (a new note's template placeholders) is
on the user's clock (`user_timezone`), not the container's UTC one. A commit
is authored by the owning agent — the one the flow ran for, else the holder
of the `research` capability — under its `agents.name`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date
from typing import Any

from aegis.services import notes
from aegis.services import notes_write as nw
from aegis.services.agents import resolve_tag
from aegis.services.knowledge import _content_id_for
from aegis.services.user_time import user_now
from aegis.services.vault_layout import Layout, get_layout
from temporalio import activity

from aegis_worker.activities.daylog import _stitch

INDEX_STATE_KEY = "notes_index_state"
# Files per indexing run; the rest wait for the next run.
DEFAULT_INDEX_BATCH = 300
# Journal entries per backfill commit (`notes-backfill-weekly`'s `batch`).
BACKFILL_BATCH = 50
# The capability whose holder owns a write no agent was named for.
_OWNER_TAG = "research"

# Newest first: a weekly run is for the recent days whose vault write failed
# and fell back to their knowledge row; the old rows went in on the first run.
# `$2` > 0 keeps only rows filed in the last `$2` days. The scheduled run sets
# it, so a block the user deleted from an old journal note stays deleted:
# the pre-vault rows are still in the store, and rereading them every week
# would put the block back. 0 (a run started by hand) takes every row.
_BACKFILL_SQL = """
SELECT c.source_type, c.metadata,
       array_agg(k.chunk_text ORDER BY k.chunk_index)
         FILTER (WHERE k.chunk_text IS NOT NULL) AS chunks
  FROM knowledge_content c
  LEFT JOIN knowledge_chunks k ON k.content_id = c.content_id
 WHERE c.source_type IN ('daylog', 'daylog_rollup')
   AND ($2::int <= 0 OR c.ingested_at > now() - make_interval(days => $2::int))
 GROUP BY c.content_id, c.source_type, c.metadata
 ORDER BY COALESCE(c.metadata->>'date', c.metadata->>'start') DESC
 LIMIT $1
"""


def unindexed_prefixes(layout: Layout) -> tuple[str, ...]:
    """Notes the index leaves out. The questions folder holds ResearchFlow's
    answers, which the flow also keeps in the knowledge store
    (`aegis://research/<hash>`), so indexing the note put every answer in
    retrieval twice. A row an earlier run made for one is dropped on the next
    run."""
    return (f"{layout.questions_dir.strip('/')}/",)


@dataclass
class NotesActivities:
    settings: Any = None
    db_pool: Any = None
    knowledge_connector: Any = None

    def _cfg(self) -> notes.NotesConfig:
        return notes.config_from_settings(self.settings)

    async def _layout(self) -> Layout:
        return await get_layout(self.db_pool)

    async def _author(self, agent_id: str | None) -> notes.Author:
        """The commit's author: the agent named, else the `research` holder,
        under its `agents.name`. Never raises — a failed lookup is AEGIS's
        own identity, not a lost write."""
        if self.db_pool is None:
            return notes.author_for(agent_id)
        try:
            if not agent_id:
                agent_id = await resolve_tag(self.db_pool, _OWNER_TAG)
            if not agent_id:
                return notes.DEFAULT_AUTHOR
            name = await self.db_pool.fetchval("SELECT name FROM agents WHERE id = $1", agent_id)
            return notes.author_for(agent_id, name)
        except Exception as exc:  # noqa: BLE001 — identity is a nicety
            activity.logger.warning("notes_author_lookup_failed err=%s", str(exc)[:200])
            return notes.author_for(agent_id)

    # ------------------------------------------------------------ writes

    @activity.defn
    async def notes_write(self, op: str, payload: dict, agent_id: str = "") -> dict:
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
        return await nw.perform_write(
            op, payload, cfg, layout=await self._layout(), author=await self._author(agent_id)
        )

    @activity.defn
    async def notes_journal_write(self, entry: dict) -> dict:
        """The daylog's entry for one day, week or month, appended to the
        journal note. `{"status": written | exists | not_configured |
        disabled | error, "path", "error"}` — `disabled` when the layout has
        that kind of journal note switched off.

        `entry`: `kind` (daily / weekly / monthly), `day` (the day; the
        week's first day; the month's first day), `label` (the daylog's own
        label), `text` and, optionally, `agent_id` (the flow's owner).
        """
        cfg = self._cfg()
        if not cfg.configured:
            return {"status": "not_configured"}
        layout = await self._layout()
        author = await self._author(str(entry.get("agent_id") or ""))
        try:
            ap = notes.journal_append(
                str(entry["kind"]),
                date.fromisoformat(str(entry["day"])),
                str(entry["label"]),
                str(entry.get("text") or ""),
                await user_now(self.db_pool),
                layout,
            )
            res = await notes.write(
                cfg, [ap], f"{author.prefix}: journal {entry['label']}", author=author
            )
        except notes.NotesDisabled:
            return {"status": "not_configured"}
        except notes.JournalKindDisabled:
            return {"status": "disabled"}
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

    async def _drop_unindexed(self, prefixes: tuple[str, ...]) -> int:
        """Remove the index rows of notes the index leaves out, which runs
        before the skip existed made. One small query a run; after the first
        run it finds nothing."""
        rows = await self.db_pool.fetch(
            "SELECT content_id, url FROM knowledge_content "
            "WHERE source_type = 'note' AND url LIKE ANY($1::text[])",
            [notes.note_url(prefix) + "%" for prefix in prefixes],
        )
        removed = 0
        for r in rows:
            removed += await self._delete(r["content_id"], r["url"])
        return removed

    @activity.defn
    async def notes_index_vault(
        self, max_files: int = DEFAULT_INDEX_BATCH, index_max_chars: int = notes.INDEX_MAX_CHARS
    ) -> dict:
        """Index what changed in the vault since the last full pass.

        A pass is the list of notes changed between the last indexed commit and
        HEAD (every note on the first run). It is kept in
        `settings.notes_index_state` and worked through `max_files` at a time,
        so the first pass over ~1,000 notes spreads over a few runs. A note that
        fails to index is retried at the start of the next pass; a deleted or
        emptied note leaves the index. Encrypted blocks are stripped by the
        read, before anything is embedded. Notes under the layout's skip
        prefixes and its questions folder are never indexed; one note is cut
        at `index_max_chars`.
        """
        cfg = self._cfg()
        if not cfg.configured:
            return {"status": "not_configured"}
        if self.knowledge_connector is None or self.db_pool is None:
            return {"status": "no_knowledge_store"}
        batch_size = max(1, int(max_files or DEFAULT_INDEX_BATCH))
        max_chars = max(1000, int(index_max_chars or notes.INDEX_MAX_CHARS))
        layout = await self._layout()
        skip = unindexed_prefixes(layout)
        state = await self._state()

        removed = await self._drop_unindexed(skip)
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
            changes = await asyncio.to_thread(
                notes.vault_changes_sync, cfg, state.get("commit"), layout
            )
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
        wanted = [rel for rel in batch if not rel.startswith(skip)]
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
                    raw_text=text[:max_chars],
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
    async def notes_backfill_journal(
        self,
        limit: int = 1000,
        since_days: int = 0,
        batch: int = BACKFILL_BATCH,
        agent_id: str = "",
    ) -> dict:
        """Write the daylog's knowledge rows into the matching journal notes,
        newest first. The markers are the ones the live daylog uses, so a day
        already in the journal is left alone and a run with nothing missing
        writes nothing.

        `since_days` > 0 looks only at rows filed in the last that many days —
        the weekly schedule, which must not put back a block the user deleted
        from an old note. 0 takes every row (a run started by hand, and any
        call made before the parameter existed). `batch` is entries per
        commit."""
        cfg = self._cfg()
        if not cfg.configured:
            return {"status": "not_configured"}
        if self.db_pool is None:
            return {"status": "no_database"}
        rows = await self.db_pool.fetch(
            _BACKFILL_SQL, max(1, int(limit)), max(0, int(since_days or 0))
        )
        layout = await self._layout()
        author = await self._author(agent_id)
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
                        notes.journal_append(
                            "daily", date.fromisoformat(label), label, text, now, layout
                        )
                    )
                else:
                    kind = str(meta.get("period") or "")
                    appends.append(
                        notes.journal_append(
                            kind, date.fromisoformat(str(meta["start"])), str(meta["label"]),
                            text, now, layout,
                        )
                    )
            except (KeyError, ValueError, notes.NotesError):
                skipped += 1
        written = 0
        existed = 0
        step = max(1, int(batch or BACKFILL_BATCH))
        for i in range(0, len(appends), step):
            chunk = appends[i : i + step]
            res = await notes.write(
                cfg, chunk, f"{author.prefix}: journal backfill ({len(chunk)})", author=author
            )
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
