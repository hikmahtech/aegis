"""DayLogFlow — file one dated episodic entry per night.

The knowledge store has plenty of *topics* and no *timeline*: nothing answers
"what happened on 2026-07-14". This flow gathers the day out of the tables
that already record it, distils one narrative, and files it as a single
`source_type='daylog'` knowledge entry keyed `aegis://daylog/<date>` — the
natural key, so a re-run of the same date updates rather than duplicates
(`KnowledgeStore.ingest_content` upserts on `_content_id_for(url)`).

A quiet day is still filed (`metadata.quiet = true`): "nothing happened" is
data, and A9's rollups need every date present to reason about a week.

Scheduled nightly, any time after midnight in the user's timezone (the
`user_timezone` setting; the seed's cron is one such time). The run logs the
most recent COMPLETE local day — the local date of the run's clock, minus
one (`logged_day`) — and bounds its events on the same clock. The clock is
converted by an activity (`daylog_local_day`), because a workflow cannot read
the row; the old rule (the run's own UTC date) gave the day that had just
STARTED for anyone west of UTC.

A9 folds the weekly and monthly rollups into this same flow class via
`DayLogConfig.mode`: the period runs read the already-filed daily entries
back out and condense them into one `source_type='daylog_rollup'` document,
so a "last quarter" retrieval reads 3 documents instead of 90.

**The agent keeps the journal (#514).** When the Obsidian vault is configured,
the entry is appended to the vault's journal note for the day, week or month
instead (`notes_journal_write`, append-only) and no knowledge row is filed —
the vault is the record and `NotesSyncFlow` indexes the note. Unconfigured, or
when the vault write fails, the flow files the knowledge row exactly as before,
so no day is ever lost; a failure is reported as `vault_error`. The week a
rollup covers is the vault layout's week (`vault_week_rule`), so the rollup's
label and the weekly note's name are the same week.

The flow belongs to the agent the activities row names; started by hand with
no agent, it resolves the holder of the `gtd` capability, never a literal id.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis.errors import error_text
    from aegis.services.notes import JOURNAL_OWNER_TAG
    from aegis.services.notes_write import NOTES_WRITE_TIMEOUT_S
    from aegis.services.vault_layout import week_bounds

    from aegis_worker.activities.agent_registry import AgentRegistryActivities
    from aegis_worker.activities.content import ContentActivities
    from aegis_worker.activities.daylog import DayLogActivities
    from aegis_worker.shared.retry import (
        NO_RETRY,
        RETRY_ONCE,
        TIMEOUT_FAST,
        TIMEOUT_LLM,
        TIMEOUT_STANDARD,
    )

_JOURNAL_TIMEOUT = timedelta(seconds=NOTES_WRITE_TIMEOUT_S)
_JOURNALED = ("written", "exists")


@dataclass
class DayLogConfig:
    """Configuration for DayLogFlow.

    NOTE (deviation from the A8 sketch, which proposed `lookback_hours: int
    = 24`): the entry's identity is a calendar DATE, not a rolling window, so
    an hours knob can only ever be converted back into a date. `day_offset`
    says it without the off-by-one: 0 = the most recent complete day on the
    user's clock (yesterday, local time, at any time after local midnight),
    1 = the day before that.

    In a rollup mode `day_offset` shifts the same anchor, so the window is the
    period that date falls in: `day_offset=7` on a Sunday re-files the previous
    week. That is the only way to reproduce a past period — the window comes
    from the clock — and it is a rewrite, not a duplicate, because the url is
    keyed on the period label.
    """

    # The owning agent (the activities row's). Empty = the `research` holder.
    agent_id: str = ""
    day_offset: int = 0
    # "daily" | "weekly" | "monthly". One flow class with a mode switch rather
    # than three near-identical @workflow.defn classes — the schedule is what
    # differs, not the shape of the work.
    mode: str = "daily"


_ROLLUP_MODES = ("weekly", "monthly")

# Retired `workflow.patched` ids. The old branches are gone; the markers
# stay one release longer as `workflow.deprecate_patch`, because a run that
# RECORDED one is wedged by a worker whose code no longer mentions it at all
# ("[TMPRL1100] Non-deprecated patch marker encountered"). Drop the calls and
# these ids in the release after next — see #614.
_LOCAL_DATE_PATCH = "daylog-local-date"

def logged_day(local_today: date, day_offset: int = 0) -> date:
    """The day a run logs: the most recent complete day on the user's clock
    (`local_today` minus one), `day_offset` days further back. With the
    shipped cron (19:00 UTC = 00:30 IST) this is the date the old UTC rule
    gave, so a deployment east of UTC sees no change; west of UTC it is now
    the day that closed rather than the one that just began."""
    return local_today - timedelta(days=1 + day_offset)


def rollup_window(
    mode: str, now: datetime, week_start: str = "monday", week_numbering: str = "iso"
) -> tuple[str, str, str] | None:
    """`(start_date, end_date, label)` for the period `now` sits in.

    `None` means "this run is not a period end" — cron has no last-day-of-month
    operator, so the monthly schedule fires on days 28-31 and this guard drops
    every run but the real month end. February (28 or 29) and 30/31-day months
    all fall out of the same "is tomorrow a new month" test.

    Weekly anchors on the week that `now` belongs to under the vault layout's
    rule (`vault_layout.week_bounds`; the default is the ISO week, Mon-Sun),
    so the Sunday-evening scheduled run covers the week that just closed, and
    a manual mid-week run produces the SAME url — a later Sunday run then
    completes it in place rather than filing a second, partial rollup. The
    weekly journal note is named by the same rule, so the label — which is
    the entry's marker key — and the note agree.
    """
    if mode == "weekly":
        start, end, label = week_bounds(now.date(), week_start, week_numbering)
        return start.isoformat(), end.isoformat(), label
    if mode == "monthly":
        if (now + timedelta(days=1)).month == now.month:
            return None
        return now.strftime("%Y-%m-01"), now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")
    raise ValueError(f"unknown daylog rollup mode: {mode!r}")


@workflow.defn
class DayLogFlow:
    """Gather → distil → journal or ingest → commit cursor, for one day (or one period)."""

    @workflow.run
    async def run(self, config: DayLogConfig) -> dict:
        agent_id = config.agent_id or await self._owner()
        if config.mode != "daily":
            return await self._run_rollup(config, agent_id)

        target_date = (await self._anchor(config.day_offset)).isoformat()
        workflow.logger.info("daylog_starting date=%s", target_date)

        try:
            events = await workflow.execute_activity_method(
                DayLogActivities.gather_day_events,
                args=[target_date],
                start_to_close_timeout=TIMEOUT_STANDARD,
                retry_policy=RETRY_ONCE,
            )
        except Exception:
            workflow.logger.warning("daylog_gather_failed date=%s", target_date)
            return {"status": "skipped", "reason": "gather_failed", "date": target_date}

        # distil_daylog swallows LLM failure internally; this guard covers the
        # activity-level failures (timeout, worker loss) so an LLM problem can
        # only ever produce a SKIPPED run, never a failed one.
        try:
            narrative = await workflow.execute_activity_method(
                DayLogActivities.distil_daylog,
                args=[events, target_date, agent_id],
                start_to_close_timeout=TIMEOUT_LLM,
                retry_policy=NO_RETRY,
            )
        except Exception:
            workflow.logger.warning("daylog_distil_failed date=%s", target_date)
            return {"status": "skipped", "reason": "distil_failed", "date": target_date}

        if not (narrative or "").strip():
            return {"status": "skipped", "reason": "empty_narrative", "date": target_date}

        # The journal first (#514). Written or already there = the vault holds
        # the day, so no knowledge row is filed; anything else falls through to
        # the knowledge store exactly as before.
        vault_error = None
        vault = await self._journal("daily", target_date, target_date, narrative, agent_id)
        if vault.get("status") in _JOURNALED:
            path = str(vault.get("path") or "")
            await self._commit_state(target_date, f"vault://{path}")
            return {
                "status": "journaled",
                "date": target_date,
                "path": path,
                "vault": vault["status"],
                "quiet": bool(events.get("quiet")),
            }
        if vault.get("status") == "error":
            vault_error = str(vault.get("error") or "vault write failed")[:200]

        url = f"aegis://daylog/{target_date}"
        try:
            ingested = await workflow.execute_activity_method(
                ContentActivities.ingest_content,
                args=[
                    {
                        "url": url,
                        "title": f"Day Log {target_date}",
                        "source_type": "daylog",
                        "raw_text": narrative,
                        "tags": ["daylog"],
                        "metadata": {
                            "date": target_date,
                            "quiet": bool(events.get("quiet")),
                            "counts": events.get("counts") or {},
                        },
                    }
                ],
                start_to_close_timeout=TIMEOUT_STANDARD,
                retry_policy=RETRY_ONCE,
            )
        except Exception:
            workflow.logger.warning("daylog_ingest_failed date=%s", target_date)
            return {
                "status": "skipped",
                "reason": "ingest_failed",
                "date": target_date,
                **_error(vault_error),
            }

        # Allow-list, not a deny-list: ingest_content answers "disabled" with
        # no knowledge connector, "skipped" on a bad item and "empty" when the
        # body embedded to nothing. Only "ok" is a filed entry, so only "ok"
        # may move the cursor (same discipline as commit_briefing_state).
        status = (ingested or {}).get("status")
        if status != "ok":
            workflow.logger.warning("daylog_not_ingested date=%s status=%s", target_date, status)
            return {
                "status": "skipped",
                "reason": f"ingest_{status or 'no_result'}",
                "date": target_date,
                **_error(vault_error),
            }

        await self._commit_state(target_date, url)

        return {
            "status": "ingested",
            "date": target_date,
            "url": url,
            "quiet": bool(events.get("quiet")),
            "content_id": (ingested or {}).get("content_id"),
            **_error(vault_error),
        }

    async def _commit_state(self, target_date: str, url: str) -> None:
        try:
            await workflow.execute_activity_method(
                DayLogActivities.commit_daylog_state,
                args=[{"last_date": target_date, "url": url}],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=RETRY_ONCE,
            )
        except Exception:
            workflow.logger.warning("daylog_state_commit_failed date=%s", target_date)

    async def _owner(self) -> str:
        """The agent a run with no `agent_id` belongs to: the holder of the
        `gtd` capability, which is whose journal this is. Empty, with a
        warning, when nobody holds it — the day is still logged, under AEGIS's
        own name."""
        try:
            resolved = await workflow.execute_activity_method(
                AgentRegistryActivities.resolve_agents,
                args=[[JOURNAL_OWNER_TAG]],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=NO_RETRY,
            )
        except Exception as exc:  # noqa: BLE001 — the owner is a nicety
            workflow.logger.warning("daylog_owner_resolve_failed err=%s", error_text(exc))
            return ""
        owner = str((resolved or {}).get(JOURNAL_OWNER_TAG) or "")
        if not owner:
            workflow.logger.warning("daylog_owner_unresolved tag=%s", JOURNAL_OWNER_TAG)
        return owner

    async def _journal(
        self, kind: str, day: str, label: str, text: str, agent_id: str = ""
    ) -> dict:
        """Append the entry to the vault's journal note. Never raises: an
        activity failure is `status: error`, and the caller files the knowledge
        row instead."""
        try:
            return await workflow.execute_activity(
                "notes_journal_write",
                {"kind": kind, "day": day, "label": label, "text": text, "agent_id": agent_id},
                start_to_close_timeout=_JOURNAL_TIMEOUT,
                retry_policy=RETRY_ONCE,
            )
        except Exception as exc:
            workflow.logger.warning("daylog_journal_failed label=%s err=%s", label, error_text(exc))
            return {"status": "error", "error": error_text(exc)}

    async def _anchor(self, day_offset: int) -> date:
        """The day this run is about, on the user's clock: the most recent
        complete local day, `day_offset` days back (`logged_day`). A failed
        clock lookup falls back to the run's own UTC date, so a day is still
        logged."""
        now = workflow.now()
        # deprecate_patch: remove after the next release, see #614
        workflow.deprecate_patch(_LOCAL_DATE_PATCH)
        try:
            clock = await workflow.execute_activity_method(
                DayLogActivities.daylog_local_day,
                args=[now.isoformat()],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=RETRY_ONCE,
            )
            local_today = date.fromisoformat(str((clock or {}).get("date") or ""))
        except Exception as exc:  # noqa: BLE001 — a day is still logged, on UTC
            workflow.logger.warning("daylog_local_day_failed err=%s", error_text(exc))
            return (now - timedelta(days=day_offset)).date()
        return logged_day(local_today, day_offset)

    async def _week_rule(self) -> dict:
        """The vault layout's week rule. On a failure the shipped rule (ISO
        weeks) — a rollup must run even if the row cannot be read."""
        try:
            rule = await workflow.execute_activity_method(
                DayLogActivities.vault_week_rule,
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=RETRY_ONCE,
            )
        except Exception as exc:  # noqa: BLE001 — the default rule is the fallback
            workflow.logger.warning("daylog_week_rule_failed err=%s", error_text(exc))
            return {}
        return {k: str(v) for k, v in (rule or {}).items() if k in ("week_start", "week_numbering")}

    # ---------------------------------------------------------------- rollups

    async def _run_rollup(self, config: DayLogConfig, agent_id: str = "") -> dict:
        """Condense a window of already-filed day logs into one entry."""
        if config.mode not in _ROLLUP_MODES:
            # A typo'd activities.config must not crash a scheduled run.
            workflow.logger.warning("daylog_unknown_mode mode=%s", config.mode)
            return {"status": "skipped", "reason": "unknown_mode", "mode": config.mode}

        # `day_offset` shifts the anchor here exactly as it does for a daily
        # run, which is what makes a past period re-runnable at all: the window
        # is derived from the clock, so without it the only rollup you can ever
        # produce is the one for right now. With the vault OFF, `day_offset=7`
        # on a Sunday re-files last week's — the url is
        # `aegis://daylog/<kind>/<label>`, so a re-run OVERWRITES that period in
        # place (used 2026-08-23 to rewrite 2026-W33, which was filed
        # truncated). With the vault ON (#514) a re-run is a no-op for a period
        # already in the journal: the note carries the period's marker and the
        # journal is append-only (`vault: exists`). To redo one, delete
        # the agent's section from the note by hand, then re-run.
        # The anchor is the same "last complete local day" the daily run
        # uses, so the period's label — the entry's marker key — is the one
        # the daily entries in it carry.
        rule = await self._week_rule() if config.mode == "weekly" else {}
        anchor = await self._anchor(config.day_offset)
        window = rollup_window(config.mode, datetime.combine(anchor, datetime.min.time()), **rule)
        if window is None:
            workflow.logger.info("daylog_rollup_not_period_end mode=%s", config.mode)
            return {"status": "skipped", "reason": "not_period_end", "mode": config.mode}
        start, end, label = window
        kind = "week" if config.mode == "weekly" else "month"
        url = f"aegis://daylog/{kind}/{label}"

        try:
            entries = await workflow.execute_activity_method(
                DayLogActivities.gather_daylogs,
                args=[start, end],
                start_to_close_timeout=TIMEOUT_STANDARD,
                retry_policy=RETRY_ONCE,
            )
        except Exception:
            workflow.logger.warning("daylog_rollup_gather_failed label=%s", label)
            return {"status": "skipped", "reason": "gather_failed", "label": label}

        # One day does not make a week. Filing a "rollup" of a single entry
        # only duplicates that entry into the retrieval set.
        if len(entries) < 2:
            workflow.logger.info("daylog_rollup_insufficient label=%s n=%d", label, len(entries))
            return {"status": "insufficient", "label": label, "entries": len(entries)}

        try:
            narrative = await workflow.execute_activity_method(
                DayLogActivities.distil_rollup,
                args=[entries, config.mode, label, agent_id],
                start_to_close_timeout=TIMEOUT_LLM,
                retry_policy=NO_RETRY,
            )
        except Exception:
            workflow.logger.warning("daylog_rollup_distil_failed label=%s", label)
            return {"status": "skipped", "reason": "distil_failed", "label": label}

        if not (narrative or "").strip():
            return {"status": "skipped", "reason": "empty_narrative", "label": label}

        covers = [e.get("date") for e in entries]

        # The week's or month's journal note first (#514), as for a day.
        vault_error = None
        vault = await self._journal(config.mode, start, label, narrative, agent_id)
        if vault.get("status") in _JOURNALED:
            return {
                "status": "journaled",
                "mode": config.mode,
                "label": label,
                "path": str(vault.get("path") or ""),
                "vault": vault["status"],
                "covers": covers,
            }
        if vault.get("status") == "error":
            vault_error = str(vault.get("error") or "vault write failed")[:200]

        try:
            ingested = await workflow.execute_activity_method(
                ContentActivities.ingest_content,
                args=[
                    {
                        "url": url,
                        "title": f"{kind.capitalize()} Log {label}",
                        "source_type": "daylog_rollup",
                        "raw_text": narrative,
                        "tags": ["daylog", "daylog_rollup"],
                        "metadata": {
                            "period": config.mode,
                            "label": label,
                            "start": start,
                            "end": end,
                            "covers": covers,
                        },
                    }
                ],
                start_to_close_timeout=TIMEOUT_STANDARD,
                retry_policy=RETRY_ONCE,
            )
        except Exception:
            workflow.logger.warning("daylog_rollup_ingest_failed label=%s", label)
            return {
                "status": "skipped",
                "reason": "ingest_failed",
                "label": label,
                **_error(vault_error),
            }

        status = (ingested or {}).get("status")
        if status != "ok":
            workflow.logger.warning("daylog_rollup_not_ingested label=%s status=%s", label, status)
            return {
                "status": "skipped",
                "reason": f"ingest_{status or 'no_result'}",
                "label": label,
                **_error(vault_error),
            }

        return {
            "status": "ingested",
            "mode": config.mode,
            "label": label,
            "url": url,
            "covers": covers,
            "content_id": (ingested or {}).get("content_id"),
            **_error(vault_error),
        }


def _error(vault_error: str | None) -> dict:
    """`{"vault_error": ...}` when the journal write failed, else nothing — so
    an unconfigured vault leaves the run summary exactly as it was."""
    return {"vault_error": vault_error} if vault_error else {}
