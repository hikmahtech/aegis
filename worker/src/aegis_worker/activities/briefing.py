"""Daily briefing activities — gather data for morning summary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape as _esc
from typing import Any

import httpx
from temporalio import activity


def _within_hours(ts_raw: Any, cutoff: datetime) -> bool:
    """Return True if an item with timestamp `ts_raw` is at/after `cutoff`.

    Missing or unparseable timestamps keep the item (defence — better to
    over-surface in the digest than drop a fresh item silently). KS returns
    ISO strings; tolerate a trailing 'Z' and naive timestamps.
    """
    if not ts_raw:
        return True
    try:
        ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return ts >= cutoff
    except Exception:
        return True


@dataclass
class BriefingActivities:
    """Activities for gathering briefing data."""

    db_pool: Any = None
    llm_client: Any = None
    knowledge_connector: Any = None
    core_api_url: str = ""
    api_key: str = ""
    frame_model: str = "gpt-oss:20b"

    async def gather_calendar_events(self) -> dict:
        """Read calendar events from settings KV (populated by n8n Calendar Fetcher)."""
        if not self.db_pool:
            return {"events": [], "count": 0}

        events = []
        try:
            rows = await self.db_pool.fetch(
                "SELECT key, value FROM settings WHERE key LIKE 'calendar_events_%'"
            )
            import json

            for row in rows:
                try:
                    parsed = (
                        json.loads(row["value"]) if isinstance(row["value"], str) else row["value"]
                    )
                    if isinstance(parsed, list):
                        events.extend(parsed)
                except (json.JSONDecodeError, TypeError):
                    pass
        except Exception as exc:
            activity.logger.warning("gather_calendar_failed error=%s", str(exc)[:200])

        activity.logger.info("calendar_events_gathered count=%d", len(events))
        return {"events": events, "count": len(events)}

    @activity.defn
    async def gather_market_data(self) -> dict:
        """Fetch market summary from Core API."""
        if not self.core_api_url:
            return {"available": False}
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                headers = {"X-API-Key": self.api_key} if self.api_key else {}
                resp = await client.get(f"{self.core_api_url}/api/market/summary", headers=headers)
                resp.raise_for_status()
                return resp.json()
        except Exception as exc:
            activity.logger.warning("gather_market_data_failed error=%s", str(exc)[:200])
            return {"available": False}

    @activity.defn
    async def format_market_section(self, market: dict) -> str:
        """Format index quotes (FinanceConnector overview) into briefing HTML."""
        if not market.get("available"):
            return ""

        indices = market.get("indices") or []
        lines = []
        for q in indices[:10]:
            symbol = q.get("symbol")
            price = q.get("price")
            if not symbol or not isinstance(price, (int, float)):
                continue
            pct = q.get("change_percent")
            arrow = "\U0001f4c9" if isinstance(pct, (int, float)) and pct < 0 else "\U0001f4c8"
            pct_str = f" ({pct:+.2f}%)" if isinstance(pct, (int, float)) else ""
            lines.append(f"  {arrow} {_esc(str(symbol))} {price:,.2f}{pct_str}")
        if not lines:
            return ""
        return "<b>Markets</b>\n" + "\n".join(lines)

    async def gather_intelligence_summary(self, hours: int = 24) -> list[dict]:
        """Gather recent intelligence items for the daily briefing.

        Keeps the `significance >= 3` threshold even though the
        intel-scan seed now ingests only items rated 5: items can land
        in KS from other paths (manual ingest, older history) and the
        briefing is the surface where the user sees *almost-worthy*
        items they didn't auto-route. The seed's 5-threshold filters
        the INGEST funnel; this 3-threshold filters the DIGEST surface.
        """
        if not self.knowledge_connector:
            return []

        try:
            results = await self.knowledge_connector.search(
                "recent intelligence news events",
                limit=20,
                source_type="intelligence",
            )
        except Exception as exc:
            activity.logger.warning("recent_intelligence_query_failed: %s", str(exc)[:200])
            return []

        from datetime import timedelta

        cutoff = datetime.now(UTC) - timedelta(hours=hours)
        filtered: list[dict] = []
        for r in results:
            meta = r.get("metadata") or {}
            if meta.get("significance", 0) < 3:
                continue
            ts_raw = (
                r.get("ingested_at") or meta.get("ingested_at") or r.get("created_at")
            )
            if not _within_hours(ts_raw, cutoff):
                continue
            filtered.append(r)
        return filtered

    async def gather_references_filed(self, hours: int = 24) -> list[dict]:
        """Return references filed into KS in the last `hours`.

        Used by raphael's daily briefing to surface a "References filed"
        section in place of the per-message chat noise that automated
        ingest flows (raindrop / RSS / intel-scan / email) would otherwise
        produce. The digest covers BOTH `source_type='reference'`
        (raindrop / chat / manual reference closure) AND
        `source_type='intelligence'` (intel-scan auto-ingest) — both
        flows produce knowledge-shaped items raphael owns and the
        briefing is the user's only signal that auto-ingest happened.

        KS's `/api/admin/stats/content-items` server-side default sort
        is `ingested_at DESC` (see
        `knowledge-service:src/knowledge_service/admin/stats.py`), so
        the first 200 rows reliably cover a 24h window over our
        steady-state (~10 references/day, ~20 intel-scan/day).
        """
        if not self.knowledge_connector:
            return []
        # KS endpoint takes a single `source_type` filter — call twice
        # and merge. The list endpoint orders DESC server-side; the
        # per-source 200 cap is comfortable headroom for 24h windows.
        merged: list[dict] = []
        for st in ("reference", "intelligence"):
            try:
                items = await self.knowledge_connector.list_content_items(
                    limit=200, source_type=st
                )
            except Exception as exc:
                activity.logger.warning(
                    "references_filed_query_failed source_type=%s err=%s",
                    st,
                    str(exc)[:200],
                )
                continue
            for it in items or []:
                merged.append(it)

        from datetime import timedelta

        cutoff = datetime.now(UTC) - timedelta(hours=hours)
        filtered: list[dict] = []
        for item in merged:
            if item.get("source_type") not in ("reference", "intelligence"):
                continue
            ts_raw = item.get("ingested_at") or item.get("created_at")
            if not _within_hours(ts_raw, cutoff):
                continue
            filtered.append(item)
        # Sort merged batches by ingested_at DESC so the digest order is
        # stable across source_types. Items missing ingested_at sink to
        # the end (we keep them — see test_briefing_references).
        filtered.sort(
            key=lambda it: it.get("ingested_at") or "",
            reverse=True,
        )
        return filtered[:50]

    @activity.defn
    async def gather_briefing_changes(self) -> dict:
        """Diff current state vs the prior run (briefing_state KV). Reuses the
        intel/calendar/knowledge gathers; adds a what-broke SQL pass. Each
        dimension is isolated so one failing source degrades to empty, not a
        dead briefing. Returns the diff bundle + the snapshot to commit."""
        import json
        from datetime import timedelta

        prior: dict = {}
        if self.db_pool:
            try:
                row = await self.db_pool.fetchrow(
                    "SELECT value FROM settings WHERE key='briefing_state'"
                )
                if row and row["value"]:
                    prior = (
                        json.loads(row["value"])
                        if isinstance(row["value"], str)
                        else row["value"]
                    )
            except Exception as exc:
                activity.logger.warning("briefing_state_read_failed err=%s", str(exc)[:200])

        now = datetime.now(UTC)
        last_raw = prior.get("last_briefing_at")
        try:
            cursor = (
                datetime.fromisoformat(str(last_raw).replace("Z", "+00:00"))
                if last_raw else now - timedelta(hours=24)
            )
            if cursor.tzinfo is None:
                cursor = cursor.replace(tzinfo=UTC)
        except Exception:
            cursor = now - timedelta(hours=24)
        prior_intel_ids = list(prior.get("seen_intel_ids") or [])
        seen_intel = set(prior_intel_ids)
        seen_cal = set(prior.get("seen_calendar_ids") or [])
        prior_ref_ids = list(prior.get("seen_reference_ids") or [])
        seen_ref = set(prior_ref_ids)
        elapsed_h = int((now - cursor).total_seconds() // 3600) + 1

        # intelligence: reuse the existing gather, then sig>=4 + dedup by id
        intel_out: list[dict] = []
        new_intel_ids: list[str] = []
        try:
            items = await self.gather_intelligence_summary(hours=max(24, min(elapsed_h, 72)))
            for r in items:
                meta = r.get("metadata") or {}
                if int(meta.get("significance", 0) or 0) < 4:
                    continue
                cid = str(r.get("content_id") or r.get("id") or r.get("title") or "")
                if not cid or cid in seen_intel:
                    continue
                seen_intel.add(cid)
                new_intel_ids.append(cid)
                intel_out.append({
                    "title": r.get("title") or (r.get("content") or "")[:80],
                    "significance": int(meta.get("significance", 0) or 0),
                    "topic": meta.get("topic", ""),
                    "url": r.get("url") or r.get("source_url") or "",
                })
        except Exception as exc:
            activity.logger.warning("briefing_intel_diff_failed err=%s", str(exc)[:200])

        # collected: references filed (raindrop / RSS / email / chat) since the
        # last briefing — the "what I learned from what I collected" digest. The
        # ingest flows otherwise fill KS silently and the user never sees it.
        # Intelligence is already covered by `intel` (sig>=4), so restrict to
        # source_type='reference' here to avoid double-listing.
        collected_out: list[dict] = []
        new_ref_ids: list[str] = []
        try:
            refs = await self.gather_references_filed(hours=max(24, min(elapsed_h, 72)))
            for r in refs:
                if r.get("source_type") != "reference":
                    continue
                cid = str(r.get("content_id") or r.get("id") or r.get("title") or "")
                if not cid or cid in seen_ref:
                    continue
                seen_ref.add(cid)
                new_ref_ids.append(cid)
                collected_out.append({
                    "title": r.get("title") or "",
                    "url": r.get("url") or r.get("source_url") or "",
                })
                if len(collected_out) >= 12:
                    break
        except Exception as exc:
            activity.logger.warning("briefing_collected_diff_failed err=%s", str(exc)[:200])

        # what broke: failed runs + new open drift since cursor
        failed_runs: list[dict] = []
        new_drift: list[dict] = []
        if self.db_pool:
            try:
                rows = await self.db_pool.fetch(
                    "SELECT workflow_type, error, completed_at FROM workflow_runs "
                    "WHERE completed_at > $1 AND (status='failed' OR error IS NOT NULL) "
                    "ORDER BY completed_at DESC LIMIT 10",
                    cursor,
                )
                failed_runs = [
                    {"workflow_type": r["workflow_type"],
                     "error": (r["error"] or "")[:160],
                     "completed_at": r["completed_at"].isoformat() if r["completed_at"] else None}
                    for r in rows
                ]
            except Exception as exc:
                activity.logger.warning("briefing_failed_runs_failed err=%s", str(exc)[:200])
            try:
                drows = await self.db_pool.fetch(
                    "SELECT service_name, severity FROM pandoras_actor.homelab_drift "
                    "WHERE detected_at > $1 AND resolved_at IS NULL "
                    "ORDER BY detected_at DESC LIMIT 10",
                    cursor,
                )
                new_drift = [{"service": r["service_name"], "severity": r["severity"]} for r in drows]
            except Exception as exc:
                activity.logger.warning("briefing_drift_failed err=%s", str(exc)[:200])

        # calendar: today's events, flag ids not seen before
        cal_today: list[dict] = []
        new_cal_ids: list[str] = []
        all_cal_ids: list[str] = []
        try:
            cal = await self.gather_calendar_events()
            for evt in cal.get("events", []):
                eid = str(evt.get("id") or evt.get("summary") or "")
                if not eid:
                    continue
                all_cal_ids.append(eid)
                cal_today.append({"summary": evt.get("summary", "(no title)"),
                                  "start": evt.get("start", "")})
                if eid not in seen_cal:
                    new_cal_ids.append(eid)
        except Exception as exc:
            activity.logger.warning("briefing_calendar_diff_failed err=%s", str(exc)[:200])

        quiet = not (intel_out or collected_out or failed_runs or new_drift or new_cal_ids)
        new_state = {
            "last_briefing_at": now.isoformat(),
            "seen_intel_ids": (prior_intel_ids + new_intel_ids)[-50:],
            "seen_reference_ids": (prior_ref_ids + new_ref_ids)[-100:],
            "seen_calendar_ids": all_cal_ids[-50:],
        }
        return {
            "quiet": quiet,
            "intel": intel_out,
            "collected": collected_out,
            "broke": {"failed_runs": failed_runs, "new_drift": new_drift},
            "calendar": {"today": cal_today, "new_ids": new_cal_ids},
            "_new_state": new_state,
        }

    @activity.defn
    async def frame_briefing(self, changes: dict) -> str:
        """One LLM call phrases the diff bundle into a tight narrative. Quiet
        bundle → one-liner. Any LLM failure → deterministic fallback, so the
        briefing always ships."""
        if changes.get("quiet"):
            return "\U0001f7e2 Quiet overnight — nothing needs you."
        fallback = self._format_changes_fallback(changes)
        if not self.llm_client:
            return fallback
        try:
            result = await self.llm_client.think(
                self._build_briefing_prompt(changes), model=self.frame_model
            )
            raw = result.get("response", "") if isinstance(result, dict) else (result or "")
            return (raw or "").strip() or fallback
        except Exception as exc:  # noqa: BLE001
            activity.logger.warning("frame_briefing_llm_failed err=%s", str(exc)[:200])
            return fallback

    def _format_changes_fallback(self, changes: dict) -> str:
        lines: list[str] = []
        for it in (changes.get("intel") or [])[:5]:
            if not lines:
                lines.append("<b>Worth your time</b>")
            tag = f" [{_esc(str(it.get('topic')))}]" if it.get("topic") else ""
            lines.append(f"  • {_esc(str(it.get('title', '')))}{tag} (sig {it.get('significance')})")
        collected = (changes.get("collected") or [])[:8]
        if collected:
            lines.append("<b>Came across your feeds</b>")
            for it in collected:
                lines.append(f"  • {_esc(str(it.get('title', '')))}")
        broke = changes.get("broke") or {}
        fr, dr = broke.get("failed_runs") or [], broke.get("new_drift") or []
        if fr or dr:
            lines.append("<b>Needs a look</b>")
            for r in fr[:5]:
                lines.append(f"  • {_esc(str(r.get('workflow_type')))} failed")
            for d in dr[:5]:
                lines.append(f"  • drift: {_esc(str(d.get('service')))} ({_esc(str(d.get('severity')))})")
        cal = changes.get("calendar") or {}
        if cal.get("new_ids"):
            lines.append("<b>Calendar</b>")
            for e in (cal.get("today") or [])[:5]:
                start = str(e.get("start", ""))
                hhmm = start.split("T")[1][:5] if "T" in start else start
                pre = f"{hhmm} — " if hhmm else ""
                lines.append(f"  • {pre}{_esc(str(e.get('summary')))}")
        return "\n".join(lines) if lines else "\U0001f7e2 Quiet overnight — nothing needs you."

    def _build_briefing_prompt(self, changes: dict) -> str:
        import json
        payload = {k: v for k, v in changes.items() if k != "_new_state"}
        return (
            "You are raphael writing a terse morning briefing. Given this JSON of "
            "what changed since the last briefing, write a 2-5 sentence plain-text "
            "summary (no markdown headers) leading with what most needs the user's "
            "attention. The `collected` list is what AEGIS read/saved from the "
            "user's feeds (raindrop/RSS/email) since yesterday — distil it into one "
            "sentence on the themes worth knowing, don't list every item. "
            "Do not invent items; only summarize what's present.\n\n"
            + json.dumps(payload)[:3000]
        )

    @activity.defn
    async def commit_briefing_state(self, state: dict) -> None:
        """Persist the new briefing snapshot (cursor + counts + seen ids)."""
        if not self.db_pool:
            return
        await self.db_pool.execute(
            "INSERT INTO settings (key, value) VALUES ('briefing_state', $1) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            state,
        )

    @activity.defn
    async def ingest_briefing(self, briefing_text: str, date: str) -> bool:
        """Ingest daily briefing into knowledge service."""
        if not self.knowledge_connector:
            return False
        try:
            await self.knowledge_connector.ingest_content(
                url=f"aegis://briefing/{date}",
                title=f"Daily Briefing {date}",
                source_type="briefing",
                raw_text=briefing_text,
                tags=["briefing", "daily"],
            )
            return True
        except Exception as exc:
            activity.logger.warning("briefing_ingest_failed: %s", str(exc))
            return False
