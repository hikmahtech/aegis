"""Daily briefing activities — gather data for morning summary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape as _esc
from typing import Any

import httpx
from aegis.errors import error_text, logged_failure
from aegis.services.health import HEALTH_SOURCE
from aegis.services.settings_store import get_setting, put_setting
from temporalio import activity

# How old `settings.current_place` (written by the location webhook, B5) may
# be and still be reported in the briefing. Past this the phone has been off,
# offline, or the push has broken — and announcing a place the owner left two
# days ago is worse than announcing none.
_PLACE_STALE_HOURS = 12

# How old a health reading (B6) may be and still lead the morning briefing.
# Health Auto Export runs once a day, so 36h tolerates a late or skipped run
# while still refusing to report last week's resting heart rate as today's.
_HEALTH_STALE_HOURS = 36


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
    # Owning agent — the `gtd` holder, resolved at boot in `__main__` (#579).
    # Threaded into the `llm_calls` row for `frame_briefing` so the briefing's
    # LLM spend is attributable; "" (nobody holds the tag) records no agent.
    agent_id: str = ""
    # DeliveryActivities, wired in `__main__.py` after it is constructed (the
    # same pattern HomelabActivities/MoneyActivities use). Needed because the
    # health block is rendered and sent inside ONE activity — see
    # `deliver_briefing`.
    delivery: Any = None

    async def gather_calendar_events(self) -> dict:
        """Read calendar events from settings KV (populated by n8n Calendar Fetcher)."""
        if not self.db_pool:
            return {"events": [], "count": 0}

        events = []
        with logged_failure("gather_calendar_failed", logger=activity.logger, field="error"):
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
            activity.logger.warning("gather_market_data_failed error=%s", error_text(exc))
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
            activity.logger.warning("recent_intelligence_query_failed: %s", error_text(exc))
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
                    error_text(exc),
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

    async def gather_email_digest(self, hours: int = 24) -> list[dict]:
        """Return the mail AEGIS judged worth reading in the last `hours`.

        `important_read` is 60% of all triaged mail and it is the tier the
        owner never sees: `GmailIngestFlow._route` applies Gmail's IMPORTANT
        label and MARKS IT READ in the same step, so the only surface is a
        label on a message that no longer shows as unread. It IS already
        embedded in the knowledge store (~42 items/day), so no new ingest is
        needed — this just reads it back out.

        Deliberately NOT `informational`: that tier is marked read and stored
        nowhere, and it is the LOW-value half (LinkedIn, facebookmail, job
        boards). Surfacing it would need a new ingest AND would dilute the
        digest. `important_action` is included because it is the tier that
        becomes a Todoist task — naming it closes the loop on why a task
        appeared in the review card.

        Uses `list_content_items` (a plain ingested_at-ordered listing), NOT
        `search`: "what landed since yesterday" is an ORDER BY, and the vector
        path answers a similarity question instead — which is exactly how the
        intelligence section came to be empty every day.
        """
        if not self.knowledge_connector:
            return []
        try:
            items = await self.knowledge_connector.list_content_items(
                limit=200, source_type="email"
            )
        except Exception as exc:
            activity.logger.warning("email_digest_query_failed err=%s", error_text(exc))
            return []

        from datetime import timedelta

        cutoff = datetime.now(UTC) - timedelta(hours=hours)
        out: list[dict] = []
        for item in items or []:
            meta = item.get("metadata") or {}
            if meta.get("category") not in ("important_read", "important_action"):
                continue
            if not _within_hours(item.get("ingested_at") or item.get("created_at"), cutoff):
                continue
            out.append(
                {
                    "title": item.get("title") or "",
                    "sender": meta.get("sender") or "",
                    "category": meta.get("category") or "",
                    # Work vs personal reads differently in prose; the lane is
                    # what lets the model say so instead of flattening both.
                    "lane": meta.get("lane") or "own",
                    "content_id": item.get("content_id") or item.get("id") or "",
                }
            )
        return out

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
            with logged_failure("briefing_state_read_failed", logger=activity.logger):
                value = await get_setting(self.db_pool, "briefing_state")
                if value:
                    prior = json.loads(value) if isinstance(value, str) else value

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

        # areas (#674): with any configured, the news reaches the user as each
        # area's judged stories, and the old per-item `intel` and per-topic
        # `topics` sections step aside — they are the same items, counted.
        areas_out: list[dict] = []
        vault_digest: dict | None = None
        # Carried over as-is when the areas cannot be read this run, so a
        # failed morning does not forget what was already shown.
        area_state: dict = {
            k: prior[k] for k in ("seen_story_keys", "area_shown") if k in prior
        }
        areas: list = []
        if self.db_pool:
            with logged_failure("briefing_areas_failed", logger=activity.logger):
                from aegis.services.research_topics import load_areas

                areas = await load_areas(self.db_pool)
                if areas:
                    areas_out, vault_digest, area_state = await self._gather_areas(
                        areas, prior, now, cursor
                    )

        # intelligence: reuse the existing gather, then sig>=4 + dedup by id
        intel_out: list[dict] = []
        new_intel_ids: list[str] = []
        with logged_failure("briefing_intel_diff_failed", logger=activity.logger):
            items = [] if areas else await self.gather_intelligence_summary(
                hours=max(24, min(elapsed_h, 72))
            )
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

        # collected: references filed (raindrop / RSS / email / chat) since the
        # last briefing — the "what I learned from what I collected" digest. The
        # ingest flows otherwise fill KS silently and the user never sees it.
        #
        # This used to `continue` on anything that wasn't source_type
        # 'reference', on the stated assumption that "intelligence is already
        # covered by `intel` (sig>=4)". It was not: `intel` comes from
        # `gather_intelligence_summary`, a VECTOR search, and pgvector's HNSW
        # scan returns at most `hnsw.ef_search` candidates before the
        # source_type filter is applied — with intelligence at 0.05% of the
        # corpus that yielded ~nothing. Measured in prod: 162 intelligence
        # items ingested over 26 days, exactly 1 reached a briefing.
        # `gather_references_filed` already fetches BOTH source types through
        # the healthy `list_content_items` path, so dropping the filter is all
        # it takes to get them back.
        #
        # The old comment's double-listing worry was real, though, because
        # `seen_intel` and `seen_ref` are separate sets: an item the vector
        # search DID return would otherwise appear in both `intel` and
        # `collected`. The intel loop runs first and has already added its ids
        # to `seen_intel`, so skipping those here is what keeps each item in
        # exactly one section.
        collected_out: list[dict] = []
        new_ref_ids: list[str] = []
        with logged_failure("briefing_collected_diff_failed", logger=activity.logger):
            refs = await self.gather_references_filed(hours=max(24, min(elapsed_h, 72)))
            for r in refs:
                cid = str(r.get("content_id") or r.get("id") or r.get("title") or "")
                if not cid or cid in seen_ref or cid in seen_intel:
                    continue
                # The scans' finds reach an area digest through their topics;
                # listing them again here is the firehose the areas replace.
                if areas and r.get("source_type") == "intelligence":
                    continue
                seen_ref.add(cid)
                new_ref_ids.append(cid)
                collected_out.append({
                    "title": r.get("title") or "",
                    "url": r.get("url") or r.get("source_url") or "",
                })
                if len(collected_out) >= 12:
                    break

        # topics: tracked topics (#513) whose round gained items since the last
        # briefing. The hub holds these now, where the intel scans used to file
        # an Inbox task per item; a topic raises its own task only once its
        # round earns one, so this line is how the rest reach the user.
        topics_out: list[dict] = []
        if self.db_pool and not areas:
            with logged_failure("briefing_topics_failed", logger=activity.logger):
                trows = await self.db_pool.fetch(
                    "SELECT COALESCE(p.metadata->>'topic', p.subject) AS topic, "
                    "       p.todoist_task_id IS NOT NULL AS tasked, "
                    "       count(*) FILTER (WHERE e.occurred_at > $1) AS new_items, "
                    "       count(*) AS round_items "
                    "FROM problems p JOIN problem_events e ON e.problem_id = p.id "
                    "  AND e.kind = 'occurrence' AND e.payload->>'item' = 'true' "
                    "WHERE p.class = $2 AND p.closed_at IS NULL "
                    "GROUP BY p.id "
                    "HAVING count(*) FILTER (WHERE e.occurred_at > $1) > 0 "
                    "ORDER BY 3 DESC LIMIT 8",
                    cursor,
                    "topic",
                )
                topics_out = [
                    {
                        "topic": r["topic"],
                        "new_items": int(r["new_items"]),
                        "round_items": int(r["round_items"]),
                        "task": bool(r["tasked"]),
                    }
                    for r in trows
                ]

        # inbox: the `important_read` mail AEGIS filed and marked read without
        # ever showing the owner. Same diff-and-dedup shape as `collected`.
        prior_email_ids = list(prior.get("seen_email_ids") or [])
        seen_email = set(prior_email_ids)
        emails_out: list[dict] = []
        new_email_ids: list[str] = []
        with logged_failure("briefing_email_diff_failed", logger=activity.logger):
            mail = await self.gather_email_digest(hours=max(24, min(elapsed_h, 72)))
            # CI notifications repeat the same subject for the same commit
            # several times a day and are ~40% of this tier by volume. Collapse
            # on title so one noisy repo can't crowd out the rest of the digest.
            seen_titles: set[str] = set()
            for m in mail:
                cid = str(m.get("content_id") or m.get("title") or "")
                title_key = str(m.get("title") or "").strip().lower()
                if not cid or cid in seen_email or title_key in seen_titles:
                    continue
                seen_email.add(cid)
                seen_titles.add(title_key)
                new_email_ids.append(cid)
                emails_out.append(m)
                if len(emails_out) >= 12:
                    break

        # what broke: failed runs + new open drift since cursor
        failed_runs: list[dict] = []
        new_drift: list[dict] = []
        if self.db_pool:
            with logged_failure("briefing_failed_runs_failed", logger=activity.logger):
                # Also catch runs that ran to completion but whose own return
                # value encodes a failure (e.g. `{"status": "error", "reason":
                # "Connection error."}` — AgentChatReplyFlow's synth-failure
                # path) — the interceptor records those as status='completed'
                # with error IS NULL, so they'd otherwise never show up here.
                rows = await self.db_pool.fetch(
                    "SELECT workflow_type, error, completed_at, result_summary FROM workflow_runs "
                    "WHERE completed_at > $1 AND ("
                    "status='failed' OR error IS NOT NULL OR result_summary->>'status' = 'error'"
                    ") ORDER BY completed_at DESC LIMIT 10",
                    cursor,
                )
                failed_runs = []
                for r in rows:
                    rs = r["result_summary"]
                    if isinstance(rs, str):
                        try:
                            rs = json.loads(rs)
                        except (json.JSONDecodeError, TypeError):
                            rs = None
                    err = r["error"] or (rs or {}).get("reason") or "error"
                    failed_runs.append({
                        "workflow_type": r["workflow_type"],
                        "error": str(err)[:160],
                        "completed_at": r["completed_at"].isoformat() if r["completed_at"] else None,
                    })
            with logged_failure("briefing_drift_failed", logger=activity.logger):
                drows = await self.db_pool.fetch(
                    "SELECT service_name, severity FROM pandoras_actor.homelab_drift "
                    "WHERE detected_at > $1 AND resolved_at IS NULL "
                    "ORDER BY detected_at DESC LIMIT 10",
                    cursor,
                )
                new_drift = [{"service": r["service_name"], "severity": r["severity"]} for r in drows]

        # calendar: today's events, flag ids not seen before
        cal_today: list[dict] = []
        new_cal_ids: list[str] = []
        all_cal_ids: list[str] = []
        with logged_failure("briefing_calendar_diff_failed", logger=activity.logger):
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

        # location: where the owner currently is, as a LABEL (B5). The KV holds
        # {"place": "home", "at": iso} — never a coordinate — and a pointer
        # older than `_PLACE_STALE_HOURS` is dropped rather than reported.
        # Isolated like every other dimension: a missing, stale or garbage KV
        # degrades to no place line, never a dead briefing.
        place: dict = {}
        if self.db_pool:
            with logged_failure("briefing_place_failed", logger=activity.logger):
                raw = await get_setting(self.db_pool, "current_place")
                current = json.loads(raw) if isinstance(raw, str) else raw
                if not isinstance(current, dict):
                    current = {}
                name = str(current.get("place") or "")
                at = current.get("at")
                # No location set (no row, or no place/at) is normal, not a failure.
                if name and at:
                    seen_at = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
                    if seen_at.tzinfo is None:
                        seen_at = seen_at.replace(tzinfo=UTC)
                    if now - seen_at <= timedelta(hours=_PLACE_STALE_HOURS):
                        place = {"place": name, "at": seen_at.isoformat()}

        # Health (B6) is deliberately NOT gathered here — see `_recent_health`.
        # It is read at render time, inside `frame_briefing`, so that no body
        # data ever enters this bundle.
        nothing_else = not (
            intel_out
            or collected_out
            or topics_out
            or emails_out
            or failed_runs
            or new_drift
            or new_cal_ids
        )
        quiet = nothing_else and not areas_out
        new_state = {
            "last_briefing_at": now.isoformat(),
            "seen_intel_ids": (prior_intel_ids + new_intel_ids)[-50:],
            "seen_reference_ids": (prior_ref_ids + new_ref_ids)[-100:],
            "seen_calendar_ids": all_cal_ids[-50:],
            # Wider than the others: this tier runs ~42 items/day, so a 100-id
            # window would roll over inside three days and re-report old mail.
            "seen_email_ids": (prior_email_ids + new_email_ids)[-300:],
            **area_state,
        }
        return {
            "quiet": quiet,
            # True when only area stories are new: the brief is their block alone.
            "nothing_else": nothing_else,
            "areas": areas_out,
            "vault_digest": vault_digest,
            "intel": intel_out,
            "collected": collected_out,
            "topics": topics_out,
            "emails": emails_out,
            "broke": {"failed_runs": failed_runs, "new_drift": new_drift},
            "calendar": {"today": cal_today, "new_ids": new_cal_ids},
            "place": place,
            "_new_state": new_state,
        }

    async def _gather_areas(
        self, areas: list, prior: dict, now: datetime, cursor: datetime
    ) -> tuple[list[dict], dict | None, dict]:
        """Each due area's judged stories (#674): ``(brief, vault, state)``.

        Daily areas run every morning over the items since the last brief;
        weekly and vault areas run on the `weekly_day` over the past week.
        The brief holds at most `brief_items` stories, spent in the areas'
        order; vault areas go to the week's journal note instead, never the
        brief. ``state`` is the story keys and titles already shown, so a
        story is never shown twice and the judge knows what the user saw.

        ponytail: a weekly digest whose brief fails to send on the weekly day
        waits a week; carry a `weekly_done` marker if that ever matters.
        """
        from datetime import timedelta

        from aegis.services import research_areas, topics_config
        from aegis.services.user_time import user_now
        from aegis.services.vault_layout import get_layout, week_bounds

        cfg = await topics_config.get_topics_config(self.db_pool)
        local = await user_now(self.db_pool)
        weekly = local.weekday() == int(cfg["weekly_day"])
        seen = list(prior.get("seen_story_keys") or [])
        shown = dict(prior.get("area_shown") or {})
        daily_since = max(cursor, now - timedelta(hours=72))
        budget = int(cfg["brief_items"])
        brief: list[dict] = []
        vault_lines: list[str] = []
        for area in areas:
            if area.cadence != "daily" and not weekly:
                continue
            if area.cadence != "vault" and budget <= 0:
                continue
            digest = await research_areas.build_digest(
                self.db_pool,
                area,
                since=daily_since if area.cadence == "daily" else now - timedelta(days=7),
                seen_keys=set(seen),
                shown_titles=list(shown.get(area.slug) or []),
                llm=self.llm_client,
                model=self.frame_model,
                agent_id=self.agent_id or None,
            )
            stories = digest["stories"]
            if area.cadence != "vault":
                stories = stories[:budget]
                budget -= len(stories)
            if not stories:
                continue
            seen += [s["key"] for s in stories]
            shown[area.slug] = (list(shown.get(area.slug) or []) + [s["title"] for s in stories])[-20:]
            if area.cadence == "vault":
                vault_lines.append(f"{area.name}:")
                vault_lines += [
                    f"  - [{s['title']}]({s['url']})" + (f" — {s['why']}" if s["why"] else "")
                    for s in stories
                ]
            else:
                brief.append({"area": area.name, "stories": stories})
        vault = None
        if vault_lines:
            layout = await get_layout(self.db_pool)
            start, _end, label = week_bounds(local.date(), layout.week_start, layout.week_numbering)
            vault = {
                "kind": "weekly", "day": start.isoformat(), "label": label,
                "text": "\n".join(vault_lines), "slot": "reading",
            }
        return brief, vault, {"seen_story_keys": seen[-500:], "area_shown": shown}

    async def _recent_health(self) -> dict:
        """Newest reading of each metric the health push writes (B6).

        Read inside `deliver_briefing`, at send time, rather than in
        `gather_briefing_changes`, because that activity's return value is
        `DailyBriefingFlow`'s `changes` bundle — which Temporal persists
        verbatim as an activity RESULT and again as `frame_briefing`'s
        ARGUMENT. That is the second store with its own retention and its own
        web UI that `aegis.services.health` refused to create when it made
        health ingest inline instead of a flow. The readings are needed only to
        render one block, so they are fetched in the activity that also sends
        it, and neither the readings nor the rendered block ever cross an
        activity boundary (#214, #215).

        Deliberately outside the `quiet` test in `gather_briefing_changes`: a
        daily export always lands, so counting it as news would mean no
        briefing is ever quiet again.
        """
        if not self.db_pool:
            return {}
        from datetime import timedelta

        try:
            rows = await self.db_pool.fetch(
                "SELECT DISTINCT ON (metric) metric, value::float8 AS value "
                "FROM life.observations "
                "WHERE source = $1 AND observed_at >= $2 "
                "ORDER BY metric, observed_at DESC",
                HEALTH_SOURCE,
                datetime.now(UTC) - timedelta(hours=_HEALTH_STALE_HOURS),
            )
            return {r["metric"]: r["value"] for r in rows if r["value"] is not None}
        except Exception as exc:
            activity.logger.warning("briefing_health_failed err=%s", error_text(exc))
            return {}

    @activity.defn
    async def frame_briefing(self, changes: dict) -> str:
        """One LLM call phrases the diff bundle into a tight narrative. Quiet
        bundle → one-liner. Any LLM failure → deterministic fallback, so the
        briefing always ships.

        The failure block is appended AFTER the narrative either way — the LLM
        is asked for 2-5 sentences over the whole diff bundle, so a real failure
        can lose out to intel headlines and get silently dropped.
        `_format_failure_block` bypasses the LLM entirely so that can't happen.

        Health readings are NOT rendered here (issue #215). This return value is
        an activity result, then `send_voice`'s argument, then the text
        `ingest_briefing` embeds into the knowledge store — three copies with
        three different retentions, one of which `search_knowledge` can pull
        back into a prompt. They are rendered in `deliver_briefing` instead.
        """
        if changes.get("quiet"):
            return "\U0001f7e2 Quiet overnight — nothing needs you."
        # The area stories (#674) are a block of their own, like the failures:
        # each was already judged worth a line, and a 2-5 sentence summary
        # would drop some of them.
        areas_block = self._format_areas_block(changes)
        if areas_block and changes.get("nothing_else"):
            return areas_block
        fallback = self._format_changes_fallback(changes)
        narrative = fallback
        if self.llm_client:
            try:
                result = await self.llm_client.think(
                    self._build_briefing_prompt(changes),
                    model=self.frame_model,
                    db_pool=self.db_pool,
                    purpose="briefing_frame",
                    agent_id=self.agent_id or None,
                )
                raw = result.get("response", "") if isinstance(result, dict) else (result or "")
                narrative = (raw or "").strip() or fallback
            except Exception as exc:  # noqa: BLE001
                activity.logger.warning("frame_briefing_llm_failed err=%s", error_text(exc))
                # Keep shipping the briefing — "you always get one" is the point
                # of the fallback — but SAY that it is the degraded one. This
                # ran silently for six days: the flow reported `delivered`, the
                # only trace was an `llm_calls` row nothing alerts on, and the
                # reader had no way to tell a mechanical bullet list from a
                # quiet morning. The daily reader is the cheapest monitor there
                # is; this line is what lets them do the job.
                narrative = f"{fallback}\n\n_(fallback summary — the briefing model failed)_"
        blocks = [b for b in (narrative, areas_block, self._format_failure_block(changes)) if b]
        return "\n\n".join(blocks)

    def _format_areas_block(self, changes: dict) -> str:
        """The judged area stories, one line each with a link and why it
        matters. Empty when no area had anything."""
        lines: list[str] = []
        for area in changes.get("areas") or []:
            stories = area.get("stories") or []
            if not stories:
                continue
            lines.append(f"<b>{_esc(str(area.get('area', '')))}</b>")
            for s in stories:
                title = _esc(str(s.get("title", "")))
                url = str(s.get("url") or "")
                head = f'<a href="{_esc(url)}">{title}</a>' if url.startswith(("http://", "https://")) else title
                why = f" — {_esc(str(s['why']))}" if s.get("why") else ""
                lines.append(f"  • {head}{why}")
        return "\n".join(lines)

    @activity.defn
    async def feed_review_line(self) -> str:
        """The monthly "drop it?" line for the research agent (#511).

        Names the active feeds with at least `unused_after_days` (the
        `feeds_config` row; 90 by default) of history that no prompt used in
        that time, and says how to drop one. "" when every feed earns its
        keep, or none is old enough to judge.
        """
        if not self.db_pool:
            return ""
        from aegis.services import feeds, feeds_config

        rows = await feeds.unused_feeds(self.db_pool)
        if not rows:
            return ""
        days = int((await feeds_config.get_feeds_config(self.db_pool))["unused_after_days"])
        names = ", ".join(r["label"] for r in rows[:8])
        more = f" and {len(rows) - 8} more" if len(rows) > 8 else ""
        return (
            f"Feeds no prompt used in {days} days: {names}{more}. "
            'Drop any? Tell me "unsubscribe <name>".'
        )

    @activity.defn
    async def deliver_briefing(self, agent_id: str, message: str) -> dict:
        """Render the health block and send the briefing, in ONE activity.

        #214 kept health readings out of the `changes` bundle; this keeps the
        RENDERED block out of everything downstream of composition. Reading,
        formatting and sending all happen inside this activity, so the only copy
        that crosses a boundary is the one the owner asked for, in their own
        channel.

        Without this the same string is (a) `frame_briefing`'s activity result
        and `send_message`'s argument, both persisted verbatim in Temporal
        history; (b) `send_voice`'s argument, and the text a TTS provider is
        handed when `AEGIS_TTS_ENABLED`; and (c) the document `ingest_briefing`
        embeds into the pgvector knowledge store — indefinitely, where
        `search_knowledge`/`ask_knowledge` can retrieve it into a chat prompt
        bound for whatever `model_balanced`/`model_smart` resolve to. (c) is the
        one that matters: it re-opens exactly the hole `_format_health_block`
        was written to close.
        """
        if self.delivery is None:
            # Loud, not degraded — a briefing that silently stops arriving is
            # this flow's worst failure mode.
            raise RuntimeError("deliver_briefing: delivery not wired")
        block = self._format_health_block(await self._recent_health())
        body = f"{message}\n\n{block}" if block else message
        return await self.delivery.send_message(agent_id, body, 0)

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
        topics = (changes.get("topics") or [])[:8]
        if topics:
            lines.append("<b>Your topics</b>")
            for t in topics:
                task = " — task raised" if t.get("task") else ""
                lines.append(
                    f"  • {_esc(str(t.get('topic', '')))}: {t.get('new_items')} new "
                    f"({t.get('round_items')} this round){task}"
                )
        emails = (changes.get("emails") or [])[:8]
        if emails:
            lines.append("<b>Mail worth reading</b>")
            for it in emails:
                mark = "❗ " if it.get("category") == "important_action" else ""
                sender = str(it.get("sender") or "")
                frm = f" — {_esc(sender)}" if sender else ""
                lines.append(f"  • {mark}{_esc(str(it.get('title', '')))}{frm}")
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
        place = (changes.get("place") or {}).get("place")
        if place:
            lines.append("<b>Location</b>")
            lines.append(f"  • last seen at {_esc(str(place))}")
        # Health is deliberately NOT here — `_format_health_block` appends it
        # after the narrative on every path, so it cannot be dropped.
        return "\n".join(lines) if lines else "\U0001f7e2 Quiet overnight — nothing needs you."

    def _format_failure_block(self, changes: dict) -> str:
        """Plain counts + failing workflow types — no LLM involved. Covers
        both genuinely-failed runs and runs that completed but returned a
        `{"status": "error", ...}`-shaped result (see `gather_briefing_changes`).
        Empty string when there's nothing to report."""
        fr = (changes.get("broke") or {}).get("failed_runs") or []
        if not fr:
            return ""
        types = sorted({str(r.get("workflow_type")) for r in fr if r.get("workflow_type")})
        return f"<b>⚠️ {len(fr)} workflow failure(s)</b>: {_esc(', '.join(types))}"

    def _format_health_block(self, health: dict | None) -> str:
        """Health readings (B6), rendered deterministically. Empty when absent.

        Takes the readings directly (from `_recent_health`) rather than the
        `changes` bundle, because the bundle must never carry them. Called only
        from `deliver_briefing`, which sends the result without returning it.

        A block rather than a line inside the narrative, for two reasons. The
        framing model is `model_balanced`, which may well be a hosted API —
        body data must not leave the box merely to be phrased, so `health` is
        excluded from `_build_briefing_prompt` and formatted here instead. And
        the LLM is asked for 2-5 sentences over the whole bundle, so a health
        line can lose out to intel headlines exactly as a failure can.
        """
        health = health or {}
        if not health:
            return ""
        parts = [f"{_esc(str(m))} {round(float(health[m]), 1)}" for m in sorted(health)]
        return "<b>Health</b>: " + ", ".join(parts)

    def _build_briefing_prompt(self, changes: dict) -> str:
        import json
        # `health` is withheld: see `_format_health_block`. Adding it back sends
        # the owner's body data to whatever `model_balanced` resolves to.
        # `gather_briefing_changes` no longer puts it in the bundle at all, so
        # this filter is now the second lock rather than the only one — kept
        # because a caller could still hand-build a bundle that carries it, and
        # tested independently of the gather-side change.
        # The area stories are rendered as their own block (`_format_areas_block`),
        # so the summary is not handed them to repeat.
        payload = {
            k: v
            for k, v in changes.items()
            if k not in ("_new_state", "health", "areas", "vault_digest", "nothing_else")
        }
        return (
            "You are raphael writing a terse morning briefing. Given this JSON of "
            "what changed since the last briefing, write a 2-5 sentence plain-text "
            "summary (no markdown headers) leading with what most needs the user's "
            "attention. The `collected` list is what AEGIS read/saved from the "
            "user's feeds (raindrop/RSS/email) since yesterday — distil it into one "
            "sentence on the themes worth knowing, don't list every item. "
            "The `topics` list is news on topics the user asked AEGIS to track: "
            "say which moved, and name any whose `task` is true, since that one "
            "became a task. "
            "The `emails` list is mail AEGIS judged worth reading and already "
            "marked read, so the user has NOT seen it — give it its own sentence "
            "on what arrived, and phrase `lane: own` (work) separately from the "
            "personal lanes rather than merging them. Name any entry whose "
            "`category` is important_action, since that one became a task. "
            "Do not invent items; only summarize what's present.\n\n"
            + json.dumps(payload)[:3000]
        )

    @activity.defn
    async def commit_briefing_state(self, state: dict) -> None:
        """Persist the new briefing snapshot (cursor + counts + seen ids)."""
        if not self.db_pool:
            return
        await put_setting(self.db_pool, "briefing_state", state)

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
            activity.logger.warning("briefing_ingest_failed: %s", error_text(exc, 500))
            return False
