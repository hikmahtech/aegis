"""Nightly episodic diary — what actually happened on one calendar day.

`gather_day_events` reads a single UTC calendar date out of the tables that
already record the day (Todoist completions, ingested calendar events,
resolved interactions, GTD clarify decisions, ingested email, workflow
failures). `distil_daylog` turns that into a short narrative, and
`commit_daylog_state` moves the cursor once the entry is safely filed.

Every source is independently try/excepted: a broken source costs its own
bucket, never the run — a partial day log is worth far more than no day log.
The LLM is optional in exactly the same way: the deterministic bullet
rendering is computed FIRST and is what ships when the model is absent,
failing or truncated (mirrors `BriefingActivities._format_changes_fallback`).

Calendar events are read from `knowledge_content` / `knowledge_chunks` with
`source_type='calendar'` — what `CalendarIngestFlow` actually writes, via
`aegis.services.claims.calendar_event_to_content`. The `settings` rows
matching `calendar_events_%` that `BriefingActivities.gather_calendar_events`
reads are legacy n8n leftovers with NO writer anywhere in this repo, so they
are deliberately NOT a source here.

Day boundaries are UTC. The nightly cron fires at 19:00 UTC = 00:30 IST, so
the run's own UTC date is the IST day that just closed.

A9 adds the period rollups on top: `gather_daylogs` reads a date RANGE of
already-filed day logs back out and `distil_rollup` condenses them into one
`source_type='daylog_rollup'` entry, so retrieval over "last quarter" reads
3 documents instead of 90.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from temporalio import activity

# Ordered so the fallback narrative reads chronologically-ish rather than by
# table name. Each entry has a `_source_<name>` coroutine below.
_SOURCES = ("meetings", "tasks", "decisions", "captures", "email", "failures")

# Per-source row caps. A day log is a summary, not an export.
_LIMIT = 40


def _day_bounds(date: str) -> tuple[datetime, datetime]:
    """[start, end) UTC timestamps for a `YYYY-MM-DD` date."""
    start = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=UTC)
    return start, start + timedelta(days=1)


def _clip(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _bullets(items: list[dict], heading: str, fmt, limit: int = 12) -> list[str]:
    """A heading plus one bullet per item, or nothing at all when empty."""
    lines: list[str] = []
    for item in (items or [])[:limit]:
        text = fmt(item).strip()
        if text:
            lines.append(f"  - {text}")
    return [heading, *lines] if lines else []


def _format_daylog_fallback(events: dict, date: str) -> str:
    """Deterministic rendering — always computed, used whenever the LLM isn't."""
    lines: list[str] = []
    lines += _bullets(events.get("meetings"), "Met / attended:", lambda i: _clip(i.get("title"), 200))
    lines += _bullets(events.get("tasks"), "Completed:", lambda i: _clip(i.get("content"), 200))
    lines += _bullets(
        events.get("decisions"),
        "Decided:",
        lambda i: (
            f"{_clip(i.get('prompt'), 200)}"
            + (f" -> {_clip(i.get('answer'), 120)}" if i.get("answer") else "")
        ),
    )
    lines += _bullets(
        events.get("captures"),
        "Captured / clarified:",
        lambda i: (
            f"{_clip(i.get('content') or i.get('task_id'), 160)}"
            f" [{_clip(i.get('classification'), 40)}]"
        ),
    )
    lines += _bullets(events.get("email"), "Email filed:", lambda i: _clip(i.get("title"), 200))
    lines += _bullets(
        events.get("failures"),
        "Broke:",
        lambda i: f"{_clip(i.get('workflow_type'), 80)}: {_clip(i.get('error'), 160)}",
    )
    if not lines:
        return f"Day log for {date}. Quiet day — nothing was recorded."
    return f"Day log for {date}.\n" + "\n".join(lines)


# --- A9 rollups ---------------------------------------------------------

# Per-entry body clip. A month is 31 entries; at 1200 chars each the whole
# prompt is ~37 KB, which fits the balanced tier with room to spare while
# keeping the Temporal activity payload small.
_ROLLUP_ENTRY_CLIP = 1200
_ROLLUP_PROMPT_CLIP = 40000


def _stitch(chunks: list[str]) -> str:
    """Rejoin `knowledge_chunks` rows into the original body.

    `KnowledgeStore._chunk` slices with a 200-char OVERLAP, so a plain
    concatenation repeats a paragraph at every chunk boundary — visible
    duplication in the rollup that actually gets filed. Drop the longest
    suffix/prefix match instead of hardcoding core's overlap constant.
    """
    out = ""
    for chunk in chunks:
        n = min(len(out), len(chunk))
        while n and not out.endswith(chunk[:n]):
            n -= 1
        out += chunk[n:]
    return out


def _format_rollup_fallback(entries: list[dict], period: str, label: str) -> str:
    """Deterministic concatenation — always computed, used when the LLM isn't."""
    header = f"{period.capitalize()} log {label} — {len(entries)} day(s) recorded."
    blocks = [
        f"{e.get('date') or '?'}\n{(e.get('text') or '').strip()}"
        for e in entries
        if (e.get("text") or "").strip()
    ]
    return header + ("\n\n" + "\n\n".join(blocks) if blocks else "")


_ROLLUP_SYSTEM_PROMPT = (
    "You are the owner's biographer, condensing a run of their daily diary "
    "entries into one summary of the period, in the third person. Write "
    "300-600 words of plain prose (no markdown, no headings, no bullets) "
    "covering the recurring themes, the decisions that were made, what "
    "shipped or broke, and the threads still open at the end of the period. "
    "Name the actual people, projects and items — a future search over this "
    "period must hit the real names. Invent nothing that is not in the "
    "entries. If the entries are mostly empty, say so in one sentence."
)


_SYSTEM_PROMPT = (
    "You are writing one entry in the owner's personal diary, in the third "
    "person, from a JSON record of what their systems observed that day. "
    "Write 200-400 words of plain prose (no markdown, no headings, no "
    "bullets) covering what they did, who they met, what they decided and "
    "what broke. Name the actual items — a future search for this date must "
    "hit the real names. Invent nothing that is not in the JSON. If the day "
    "is empty, say so in one sentence."
)


@dataclass
class DayLogActivities:
    """Gather + distil + file one day of the owner's life."""

    db_pool: Any = None
    llm_client: Any = None
    model: str = "gpt-oss:20b"

    # ------------------------------------------------------------- gathering

    @activity.defn
    async def gather_day_events(self, date: str) -> dict:
        """Everything recorded on the UTC calendar day `date`, bucketed by kind.

        Never raises for a data reason: a failing source degrades to an empty
        bucket and the rest of the day still gets logged.
        """
        start, end = _day_bounds(date)
        out: dict[str, Any] = {"date": date}
        for name in _SOURCES:
            if self.db_pool is None:
                out[name] = []
                continue
            try:
                out[name] = await getattr(self, f"_source_{name}")(start, end)
            except Exception as exc:  # noqa: BLE001 — one bad source must not kill the day
                activity.logger.warning(
                    "daylog_source_failed source=%s date=%s err=%s", name, date, str(exc)[:200]
                )
                out[name] = []
        out["counts"] = {name: len(out[name]) for name in _SOURCES}
        out["quiet"] = not any(out["counts"].values())
        activity.logger.info(
            "daylog_gathered date=%s quiet=%s counts=%s", date, out["quiet"], out["counts"]
        )
        return out

    async def _source_tasks(self, start: datetime, end: datetime) -> list[dict]:
        rows = await self.db_pool.fetch(
            "SELECT content, labels FROM todoist_tasks "
            "WHERE is_completed AND completed_at >= $1 AND completed_at < $2 "
            "ORDER BY completed_at LIMIT $3",
            start,
            end,
            _LIMIT,
        )
        return [
            {"content": _clip(r["content"], 200), "labels": list(r["labels"] or [])} for r in rows
        ]

    async def _source_meetings(self, start: datetime, end: datetime) -> list[dict]:
        """Calendar events whose `Start:` line falls on the day.

        `calendar_event_to_content` writes the start timestamp into the chunk
        text (`Start: 2026-07-14T09:00:00+05:30`); `knowledge_content` itself
        carries no event-time column, so the day filter has to look there.
        """
        day = start.strftime("%Y-%m-%d")
        rows = await self.db_pool.fetch(
            "SELECT DISTINCT c.content_id, c.title FROM knowledge_content c "
            "JOIN knowledge_chunks k ON k.content_id = c.content_id "
            "WHERE c.source_type = 'calendar' AND k.chunk_text LIKE $1 "
            "ORDER BY c.title LIMIT $2",
            f"%Start: {day}%",
            _LIMIT,
        )
        return [{"title": _clip(r["title"], 200), "content_id": r["content_id"]} for r in rows]

    async def _source_decisions(self, start: datetime, end: datetime) -> list[dict]:
        rows = await self.db_pool.fetch(
            # `status = 'resolved'` matters: archiving a card that timed out
            # UNANSWERED also stamps resolved_at, so without the filter the day
            # log reports "Decided:" for a decision nobody ever made.
            "SELECT kind, origin, prompt, status, response FROM interactions "
            "WHERE resolved_at >= $1 AND resolved_at < $2 AND status = 'resolved' "
            "ORDER BY resolved_at LIMIT $3",
            start,
            end,
            _LIMIT,
        )
        out = []
        for r in rows:
            resp = r["response"]
            answer = ""
            if isinstance(resp, dict):
                answer = _clip(resp.get("value") or resp.get("choice") or resp.get("text"), 200)
            elif resp:
                answer = _clip(resp, 200)
            out.append(
                {
                    "kind": r["kind"],
                    "origin": r["origin"],
                    "prompt": _clip(r["prompt"], 300),
                    "status": r["status"],
                    "answer": answer,
                }
            )
        return out

    async def _source_captures(self, start: datetime, end: datetime) -> list[dict]:
        rows = await self.db_pool.fetch(
            "SELECT g.todoist_task_id, g.classification, g.applied, t.content "
            "FROM gtd_clarify_log g "
            "LEFT JOIN todoist_tasks t ON t.id = g.todoist_task_id "
            "WHERE g.created_at >= $1 AND g.created_at < $2 "
            "ORDER BY g.created_at LIMIT $3",
            start,
            end,
            _LIMIT,
        )
        return [
            {
                "task_id": r["todoist_task_id"],
                "classification": r["classification"],
                "applied": bool(r["applied"]),
                "content": _clip(r["content"], 200),
            }
            for r in rows
        ]

    async def _source_email(self, start: datetime, end: datetime) -> list[dict]:
        rows = await self.db_pool.fetch(
            "SELECT title FROM knowledge_content "
            "WHERE source_type = 'email' AND ingested_at >= $1 AND ingested_at < $2 "
            "ORDER BY ingested_at LIMIT $3",
            start,
            end,
            _LIMIT,
        )
        return [{"title": _clip(r["title"], 200)} for r in rows]

    async def _source_failures(self, start: datetime, end: datetime) -> list[dict]:
        """Failed runs — including runs that "completed" with an error result
        (same shape `gather_briefing_changes` has to cope with)."""
        rows = await self.db_pool.fetch(
            "SELECT workflow_type, error, result_summary FROM workflow_runs "
            "WHERE completed_at >= $1 AND completed_at < $2 AND ("
            "status = 'failed' OR error IS NOT NULL OR result_summary->>'status' = 'error'"
            ") ORDER BY completed_at LIMIT $3",
            start,
            end,
            _LIMIT,
        )
        out = []
        for r in rows:
            rs = r["result_summary"] if isinstance(r["result_summary"], dict) else {}
            out.append(
                {
                    "workflow_type": r["workflow_type"],
                    "error": _clip(r["error"] or rs.get("reason") or "error", 160),
                }
            )
        return out

    # ------------------------------------------------------------- distilling

    @activity.defn
    async def distil_daylog(self, events: dict, date: str, agent_id: str = "raphael") -> str:
        """One short narrative for the day. Never raises, never returns "".

        The deterministic rendering is built first and is the floor: an
        absent, failing or empty LLM leaves it in place rather than losing
        the day.
        """
        import json

        fallback = _format_daylog_fallback(events, date)
        if not self.llm_client:
            return fallback

        # db_pool + purpose ⇒ think() writes the llm_calls row itself, for
        # success and failure alike (LLMClient._record_call). Do not record here.
        payload = {k: v for k, v in events.items() if k != "counts"}
        try:
            result = await self.llm_client.think(
                prompt=json.dumps(payload, default=str)[:6000],
                model=self.model,
                system_prompt=_SYSTEM_PROMPT,
                max_tokens=1800,
                db_pool=self.db_pool,
                purpose="daylog_narrative",
                agent_id=agent_id,
            )
        except Exception as exc:  # noqa: BLE001 — degrade to the bullets, never fail the day
            activity.logger.warning("daylog_distil_llm_failed date=%s err=%s", date, str(exc)[:200])
            return fallback

        return (result.get("response") or "").strip() or fallback

    # ---------------------------------------------------------------- rollups

    @activity.defn
    async def gather_daylogs(self, start: str, end: str) -> list[dict]:
        """Filed day logs whose `metadata.date` falls in `[start, end]`, oldest first.

        DEVIATION from the A9 sketch, which routed this through
        `KnowledgeStore.list_content_items` / `search`. Neither can do it:

        * `list_content_items` selects the `summary` COLUMN, and A8 files the
          narrative as `raw_text` — `knowledge_content` has no raw-text column
          at all, the body exists only as `knowledge_chunks` rows. It also
          filters on nothing but `source_type`, returning an `ingested_at
          DESC` window rather than a date range.
        * `search` is a semantic top-k over a query string, so it can neither
          bound a window nor guarantee every date in it.

        Going through either yields a rollup of titles with no content — the
        precise silent failure A9's acceptance criteria forbid. Read the
        chunks directly, exactly as `_source_meetings` above already reads
        `source_type='calendar'`.
        """
        if self.db_pool is None:
            return []
        rows = await self.db_pool.fetch(
            "SELECT c.metadata->>'date' AS date, c.title, "
            "       array_agg(k.chunk_text ORDER BY k.chunk_index) "
            "         FILTER (WHERE k.chunk_text IS NOT NULL) AS chunks "
            "FROM knowledge_content c "
            "LEFT JOIN knowledge_chunks k ON k.content_id = c.content_id "
            "WHERE c.source_type = 'daylog' "
            "  AND c.metadata->>'date' >= $1 AND c.metadata->>'date' <= $2 "
            "GROUP BY c.content_id, c.title, c.metadata "
            "ORDER BY 1",
            start,
            end,
        )
        out = [
            {
                "date": r["date"],
                "title": _clip(r["title"], 200),
                "text": _stitch(list(r["chunks"] or []))[:_ROLLUP_ENTRY_CLIP],
            }
            for r in rows
        ]
        activity.logger.info("daylog_rollup_gathered start=%s end=%s n=%d", start, end, len(out))
        return out

    @activity.defn
    async def distil_rollup(
        self,
        entries: list[dict],
        period: str,
        label: str,
        agent_id: str = "raphael",
    ) -> str:
        """One narrative for the whole period. Never raises, never returns "".

        Same discipline as `distil_daylog`: the deterministic concatenation is
        built first and is the floor, so an absent or failing LLM costs prose
        quality, never the rollup.
        """
        import json

        fallback = _format_rollup_fallback(entries, period, label)
        if not self.llm_client:
            return fallback

        # think() records this call (db_pool + purpose) — see distil_daylog.
        try:
            result = await self.llm_client.think(
                prompt=json.dumps(entries, default=str)[:_ROLLUP_PROMPT_CLIP],
                model=self.model,
                system_prompt=_ROLLUP_SYSTEM_PROMPT,
                max_tokens=2400,
                db_pool=self.db_pool,
                purpose="daylog_rollup",
                agent_id=agent_id,
            )
        except Exception as exc:  # noqa: BLE001 — degrade to the concatenation
            activity.logger.warning(
                "daylog_rollup_llm_failed label=%s err=%s", label, str(exc)[:200]
            )
            return fallback

        return (result.get("response") or "").strip() or fallback

    # ------------------------------------------------------------------ state

    @activity.defn
    async def commit_daylog_state(self, state: dict) -> None:
        """Persist the day-log cursor. Called ONLY after a successful ingest."""
        if not self.db_pool:
            return
        await self.db_pool.execute(
            "INSERT INTO settings (key, value) VALUES ('daylog_state', $1) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            state,
        )
