"""Flow-health watchdog activities — AEGIS watching AEGIS's own flows.

Issue #226: `TodoistSyncFlow` failed six consecutive times (2026-08-02
12:15–12:40 UTC) and nothing told anyone. Every failure was recorded — six
`workflow_runs` rows with `status='failed'` and a useful
`result_summary->>'reason'` — the data was there, nothing was watching it.

Three detectors — two over `workflow_runs`, one over `llm_calls` (no new
table, no migration):

1. **consecutive failures** — the most recent N runs of a `workflow_type`
   are ALL failed. A `workflow_runs` row is written only after the run's own
   Temporal retry budget is exhausted, so a single failure is already a
   post-retry failure and a second one in a row is a wedged flow, not a blip.
2. **stale schedule** — an active `activities` row whose *last successful*
   run is older than `multiplier x` its own cadence (derived from
   `schedule_cron`). This is the "silently stopped running" case, which
   detector 1 cannot see at all: a schedule that never fires writes no rows.
3. **dead LLM purpose** — the most recent N calls of one `llm_calls.purpose`
   ALL failed (#321). Neither detector above can see this: almost every LLM
   caller in AEGIS catches its own failure and degrades to non-LLM output, so
   the flow *completes* and the schedule stays fresh while the thinking part of
   it produces nothing. That is exactly how #255 ran for six days —
   `briefing_frame` returned empty content on 100% of calls and the briefing
   went out every morning looking fine. Purpose-level, because the cause is
   usually per-purpose (a budget, a prompt, a model in one tier), not
   whole-fleet.

Detectors 1 and 3 are both *last-N* tests, not rate-over-a-window tests, and
that is the load-bearing choice. Cadence in AEGIS spans three orders of
magnitude — `clarify_classification` runs 102 times a month, `review_frame`
twice — so no fixed time window is right for every subject. #321 shipped
detector 3 as "2 calls with 0 successes in 24h" and it could not fire for six
of fourteen live purposes, including the two the issue was opened about: they
never reach two calls in a single day, so the condition was unsatisfiable no
matter how broken they were. Time is now a *staleness bound* (don't dig into
ancient history) rather than the gate.

The one thing the staleness bound is for is the retired purpose: a purpose
deleted from the code whose final calls happened to fail keeps matching "its
last calls all failed" forever, because no newer call will ever arrive to
clear it. It stops matching once its last call is older than
`staleness_hours`. Until then it behaves like any other unfixed fault — one
card per `dedup_hours`, silenced with one `alert_mutes` row — which is the
same deal a permanently stale schedule already gets from detector 2, except
this one expires on its own. That is the accepted cost of not gating on
cadence: the alternative (deriving each purpose's expected interval from its
own history) needs a floor for burst-shaped purposes, and a mis-set floor
would silently stop the detector firing, which is precisely the failure this
rewrite exists to remove. A loud fault that expires beats a quiet one that
does not.

Alerting is deduped in `audit_log` (`flow_health_alert` /
`flow_health_recovered`), the same substrate `DeliveryWatchdogFlow` uses for
its comms-outage half. `alert_dedup_index` is deliberately NOT used: its
`task_id` is NOT NULL and joined against `todoist_tasks`, i.e. it dedups
@pandora *Todoist tasks* raised by the alert-investigation pipeline, and this
watchdog notifies with a plain chat card instead. Dedup is resolved-aware
(same shape as `AlertActivities.check_dedup`): a `flow_health_recovered` row
written after the last alert re-arms the subject, so a flow that breaks,
recovers and breaks again alerts twice — while a flow that stays wedged for a
day alerts once, not 288 times.
"""

from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass
from typing import Any

import structlog
from temporalio import activity

from aegis_worker.activities.delivery import safe_send_message

logger = structlog.get_logger()

ALERT_ACTION = "flow_health_alert"
RECOVERY_ACTION = "flow_health_recovered"
TARGET_TYPE = "flow"
#: `alert_mutes.mute_key` prefix — mute a known-broken flow with
#: `INSERT INTO alert_mutes (mute_key, muted_until) VALUES ('flow-health:<subject>', ...)`.
MUTE_PREFIX = "flow-health:"
ACTOR = "flow-health-watchdog"
#: Namespace for detector 3's subjects. Dedup, mutes and recovery all key on
#: the bare `subject` string, so an LLM purpose has to be unambiguous against a
#: `workflow_type` and an `activities.slug` sharing that keyspace.
LLM_SUBJECT_PREFIX = "llm-purpose:"

_MINUTES_PER_DAY = 1440
_MINUTES_PER_WEEK = 7 * _MINUTES_PER_DAY
# Deliberately the longest month: a day-of-month cron is treated as monthly and
# staleness must never false-alarm on the long February→March style gap.
_MINUTES_PER_MONTH = 31 * _MINUTES_PER_DAY

_FIELD = re.compile(r"^(?P<range>\*|\d+(?:-\d+)?)(?:/(?P<step>\d+))?$")


# ---------------------------------------------------------------------------
# cron → cadence (pure, no DB — the unit-testable half of the stale detector)
# ---------------------------------------------------------------------------


def _expand(field: str, lo: int, hi: int) -> set[int] | None:
    """Values one cron field matches, or None when it is not understood.

    Handles `*`, `n`, `a-b`, `*/n`, `a-b/n`, `n/step` and comma lists of those
    — the whole vocabulary in `config/seed/activities.yaml`. Anything else
    (`MON`, `L`, `#`, `?`) returns None so the caller SKIPS the row rather than
    guessing a cadence: a wrong guess either alerts forever or never alerts.
    """
    out: set[int] = set()
    for part in field.split(","):
        m = _FIELD.match(part.strip())
        if not m:
            return None
        rng, step_s = m.group("range"), m.group("step")
        step = int(step_s) if step_s else 1
        if step <= 0:
            return None
        if rng == "*":
            start, end = lo, hi
        elif "-" in rng:
            a, b = rng.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = int(rng)
            # `5/10` means "from 5, every 10" — a bare value with a step runs
            # to the end of the field's range.
            end = hi if step_s else start
        if start < lo or end > hi or start > end:
            return None
        out.update(range(start, end + 1, step))
    return out or None


def _max_gap(fires: list[int], cycle_minutes: int) -> int:
    """Largest gap between consecutive fire times on a wrapping cycle."""
    if len(fires) == 1:
        return cycle_minutes
    gaps = [b - a for a, b in zip(fires, fires[1:], strict=False)]
    gaps.append(fires[0] + cycle_minutes - fires[-1])
    return max(gaps)


def cron_interval_minutes(cron: str) -> int | None:
    """Longest gap, in minutes, between two fires of a 5-field cron.

    The *longest* gap (not the average) is what staleness must be measured
    against — `0 9 * * 1-5` fires five times a week but goes 72h quiet over a
    weekend, and a threshold built from the average would page every Monday.

    None means "not understood": the caller logs and skips the schedule.
    """
    parts = (cron or "").split()
    if len(parts) != 5:
        return None
    minute, hour, dom, month, dow = parts
    if month != "*":
        return None  # yearly-ish; nothing in this repo uses it, don't guess
    mins = _expand(minute, 0, 59)
    hrs = _expand(hour, 0, 23)
    if not mins or not hrs:
        return None
    if dom != "*":
        # Day-of-month restricted (`0 10 1 * *`, `20 21 28-31 * *`): fires a
        # handful of times a month. Treated as monthly — conservative on
        # purpose, staleness would rather be late than wrong.
        return _MINUTES_PER_MONTH
    fires = sorted(h * 60 + m for h in hrs for m in mins)
    if dow == "*":
        return _max_gap(fires, _MINUTES_PER_DAY)
    days = _expand(dow, 0, 7)
    if not days:
        return None
    days = {0 if d == 7 else d for d in days}  # cron: 0 and 7 are both Sunday
    week = sorted(d * _MINUTES_PER_DAY + f for d in sorted(days) for f in fires)
    return _max_gap(week, _MINUTES_PER_WEEK)


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

_FAILING_SQL = """
WITH recent AS (
    SELECT workflow_type, status, started_at, error, result_summary,
           row_number() OVER (
               PARTITION BY workflow_type ORDER BY started_at DESC
           ) AS rn
    FROM workflow_runs
    WHERE started_at > now() - make_interval(hours => $2)
)
SELECT workflow_type,
       max(started_at) AS last_run_at,
       (array_agg(
            coalesce(result_summary->>'reason', error, '') ORDER BY started_at DESC
        ))[1] AS reason
FROM recent
WHERE rn <= $1
GROUP BY workflow_type
HAVING count(*) = $1 AND count(*) FILTER (WHERE status = 'failed') = $1
ORDER BY workflow_type
"""

# The action id schedule_sync builds is `scheduled-<slug>--v<fingerprint>` and
# Temporal appends the scheduled time, so a scheduled run's workflow_id always
# starts with `scheduled-<slug>--` (verified against prod). starts_with(), not
# LIKE: a slug containing `_` would be a wildcard under LIKE.
_STALE_SQL = """
SELECT a.slug, a.workflow_type, a.schedule_cron, ls.last_success_at,
       EXTRACT(EPOCH FROM (now() - ls.last_success_at)) / 60.0 AS idle_minutes
FROM activities a
CROSS JOIN LATERAL (
    SELECT max(r.started_at) AS last_success_at
    FROM workflow_runs r
    WHERE r.workflow_type = a.workflow_type
      AND r.status = 'completed'
      AND starts_with(r.workflow_id, 'scheduled-' || a.slug || '--')
) ls
WHERE a.active = TRUE
  AND a.schedule_cron IS NOT NULL
ORDER BY a.slug
"""

# _FAILING_SQL's shape, applied to llm_calls: rank each purpose's own calls by
# recency and require that the newest `consecutive` of them ALL failed — a
# streak test, not "N calls in H hours", which no single H can express across a
# fleet whose cadences differ by 50x. `count(*) = $1` doubles as the "has it
# even run N times" floor, so a purpose called once and errored is not judged.
# $2 is a staleness bound, not the gate — it keeps the scan off ancient history
# and lets a purpose that broke, got retired and was never called again fall
# silent on its own.
#
# Measured against live production, which is why it is a streak and not "no
# successes among the last 3": `daylog_rollup` runs WEEKLY and its last two
# calls (error 2026-08-09, clipped 2026-08-16) are both dead, but a success
# from 2026-08-02 still sits in its last three. A last-3-with-no-successes
# test is suppressed by that fortnight-old success and misses a purpose that is
# dark right now — one of the two purposes #321 was opened about.
#
# `clipped` deliberately does NOT count as a success: it is a response cut
# mid-write, and a purpose whose every call clips is producing truncated JSON
# on every call — the same "loud about nothing" state, and worth the same card.
# `timeout` and `error` obviously don't count. The kill switch writes no row at
# all, so a spend freeze cannot masquerade as a dead purpose.
_DEAD_LLM_SQL = """
WITH recent AS (
    SELECT purpose, status, model, error, created_at,
           row_number() OVER (
               PARTITION BY purpose ORDER BY created_at DESC
           ) AS rn
    FROM llm_calls
    WHERE created_at > now() - make_interval(hours => $2)
      AND purpose IS NOT NULL
      AND purpose <> ''
)
SELECT purpose,
       count(*) AS calls,
       count(*) FILTER (WHERE status = 'error') AS errors,
       count(*) FILTER (WHERE status = 'timeout') AS timeouts,
       count(*) FILTER (WHERE status = 'clipped') AS clipped,
       min(created_at) AS first_call_at,
       max(created_at) AS last_call_at,
       (array_agg(model ORDER BY created_at DESC))[1] AS model,
       (array_agg(coalesce(error, '') ORDER BY created_at DESC))[1] AS reason
FROM recent
WHERE rn <= $1
GROUP BY purpose
HAVING count(*) = $1
   AND count(*) FILTER (WHERE status = 'success') = 0
ORDER BY purpose
"""

# Resolved-aware: an alert only suppresses a new one while no recovery row was
# written after it.
_SUPPRESSED_SQL = f"""
SELECT DISTINCT a.target_id
FROM audit_log a
WHERE a.action = '{ALERT_ACTION}'
  AND a.target_type = '{TARGET_TYPE}'
  AND a.target_id = ANY($1::text[])
  AND a.created_at > now() - make_interval(hours => $2)
  AND NOT EXISTS (
      SELECT 1 FROM audit_log r
      WHERE r.action = '{RECOVERY_ACTION}'
        AND r.target_type = '{TARGET_TYPE}'
        AND r.target_id = a.target_id
        AND r.created_at > a.created_at
  )
"""

_OPEN_SQL = f"""
SELECT DISTINCT a.target_id
FROM audit_log a
WHERE a.action = '{ALERT_ACTION}'
  AND a.target_type = '{TARGET_TYPE}'
  AND a.created_at > now() - make_interval(hours => $1)
  AND NOT EXISTS (
      SELECT 1 FROM audit_log r
      WHERE r.action = '{RECOVERY_ACTION}'
        AND r.target_type = '{TARGET_TYPE}'
        AND r.target_id = a.target_id
        AND r.created_at > a.created_at
  )
"""


def _card(title: str, body: str) -> str:
    return f"<b>{_html.escape(title)}</b>\n{_html.escape(body)}"


def _describe(finding: dict) -> str:
    subject = finding.get("subject", "?")
    if finding.get("kind") == "llm_dead":
        return (
            f"  {subject}: last {finding.get('calls', '?')} LLM calls, ZERO successful "
            f"(model {finding.get('model', '?')}; errors="
            f"{finding.get('errors', 0)} timeouts={finding.get('timeouts', 0)} "
            f"clipped={finding.get('clipped', 0)}; spread over "
            f"{finding.get('span_hours', '?')}h, last "
            f"{finding.get('last_call_at', '?')})\n"
            f"    last error: {str(finding.get('reason') or 'unknown')[:300]}"
        )
    if finding.get("kind") == "stale":
        return (
            f"  {subject} ({finding.get('workflow_type', '?')}, "
            f"cron {finding.get('schedule_cron', '?')}): no successful run in "
            f"{finding.get('idle_minutes', '?')} min "
            f"(threshold {finding.get('threshold_minutes', '?')} min)"
        )
    return (
        f"  {subject}: {finding.get('consecutive', '?')} consecutive failed runs, "
        f"last {finding.get('last_run_at', '?')}\n"
        f"    reason: {str(finding.get('reason') or 'unknown')[:300]}"
    )


@dataclass
class FlowHealthActivities:
    db_pool: Any
    delivery: Any = None  # DeliveryActivities

    # -- detection ---------------------------------------------------------

    @activity.defn
    async def find_failing_flows(
        self, consecutive: int = 2, lookback_hours: int = 24
    ) -> list[dict]:
        """Workflow types whose most recent `consecutive` runs ALL failed.

        The `lookback_hours` bound is load-bearing in two directions: it keeps
        the scan on the (workflow_type, started_at DESC) index, and it means a
        type that failed twice and then stopped running entirely does not
        re-alert forever — that case belongs to `find_stale_flows`.

        Returns [] (never raises) when `workflow_runs` is empty.
        """
        if not self.db_pool or consecutive < 1:
            return []
        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch(_FAILING_SQL, consecutive, lookback_hours)
        return [
            {
                "kind": "failing",
                "subject": r["workflow_type"],
                "workflow_type": r["workflow_type"],
                "consecutive": consecutive,
                "last_run_at": r["last_run_at"].isoformat() if r["last_run_at"] else None,
                "reason": (r["reason"] or "")[:500],
            }
            for r in rows
        ]

    @activity.defn
    async def find_stale_flows(
        self, multiplier: float = 3.0, min_stale_minutes: int = 60
    ) -> list[dict]:
        """Active schedules with no successful run in `multiplier x` cadence.

        Schedules that have NEVER succeeded are skipped: a never-run row is an
        unconfigured or feature-flag-gated flow, not a regression, and alerting
        on those would make a fresh install scream on first boot. This detector
        answers "it used to run and stopped".

        `min_stale_minutes` is a floor so a 2-minute schedule does not alert 6
        minutes into a worker redeploy.

        A `schedule_cron` this parser does not understand is logged and
        skipped, never guessed.
        """
        if not self.db_pool:
            return []
        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch(_STALE_SQL)
        out: list[dict] = []
        for r in rows:
            # The ONE never-succeeded guard. Kept in Python, not duplicated as
            # a SQL `IS NOT NULL`: two guards for one rule mask each other, and
            # neither can then be proven load-bearing by a test.
            if r["last_success_at"] is None:
                continue
            interval = cron_interval_minutes(r["schedule_cron"])
            if interval is None:
                logger.warning(
                    "flow_health_cron_unparseable",
                    slug=r["slug"],
                    schedule_cron=r["schedule_cron"],
                )
                continue
            threshold = max(int(interval * multiplier), int(min_stale_minutes))
            idle = int(r["idle_minutes"])
            if idle <= threshold:
                continue
            out.append(
                {
                    "kind": "stale",
                    "subject": r["slug"],
                    "workflow_type": r["workflow_type"],
                    "schedule_cron": r["schedule_cron"],
                    "idle_minutes": idle,
                    "threshold_minutes": threshold,
                    "last_success_at": (
                        r["last_success_at"].isoformat() if r["last_success_at"] else None
                    ),
                }
            )
        return out

    @activity.defn
    async def find_dead_llm_purposes(
        self, consecutive: int = 2, staleness_hours: int = 720
    ) -> list[dict]:
        """LLM purposes whose most recent `consecutive` calls ALL failed (#321).

        The de-silencing half of the reasoning-truncation fix. Retrying a
        truncated call heals *an instance*; this makes the failure CLASS loud,
        whatever its cause — a stale token floor, a decommissioned model behind
        a tier, a prompt that always overflows, a proxy rejecting one model.
        Every one of those looks identical from here: calls going out, nothing
        coming back, and a caller quietly degrading.

        The test is deliberately cadence-independent — the subject's own last
        `consecutive` calls, not a rate over a fixed window. Two knobs, because
        a third would only be another way to make this unfireable:

        * `consecutive=2` — the same number and the same reasoning as
          `find_failing_flows`: one failure is a blip, two in a row is a
          pattern. It is also the floor on "has this purpose even run": a
          purpose with fewer than `consecutive` calls in the whole staleness
          window is not judged at all, which keeps a one-shot chat tool that
          errored off the card.
        * `staleness_hours=720` (30 days) — the *only* time bound, and it has
          to reach the slowest live purpose: `profile_generalization` and
          `review_frame` each run twice a MONTH, so anything shorter is
          unsatisfiable for them however broken they are. It stays under the
          90-day `llm_calls` cleanup retention, so the bound is real data, not
          a window that quietly ends at whatever cleanup left behind.

        A retired purpose whose final calls failed alerts until its last call
        ages past `staleness_hours` — accepted deliberately, see the
        module-level note on bounding that.

        Returns [] (never raises) on an empty `llm_calls`.
        """
        if not self.db_pool or consecutive < 1:
            return []
        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch(_DEAD_LLM_SQL, consecutive, staleness_hours)
        out: list[dict] = []
        for r in rows:
            first, last = r["first_call_at"], r["last_call_at"]
            out.append(
                {
                    "kind": "llm_dead",
                    "subject": LLM_SUBJECT_PREFIX + r["purpose"],
                    "purpose": r["purpose"],
                    "model": r["model"],
                    "calls": r["calls"],
                    "errors": r["errors"],
                    "timeouts": r["timeouts"],
                    "clipped": r["clipped"],
                    "consecutive": consecutive,
                    "staleness_hours": staleness_hours,
                    # How far apart those calls were. On the card because a
                    # low-cadence purpose and a wedged busy one need different
                    # reactions, and only this number tells them apart.
                    "span_hours": (
                        round((last - first).total_seconds() / 3600.0, 1)
                        if first and last
                        else 0.0
                    ),
                    "first_call_at": first.isoformat() if first else None,
                    "last_call_at": last.isoformat() if last else None,
                    "reason": (r["reason"] or "")[:500],
                }
            )
        return out

    # -- alerting ----------------------------------------------------------

    @activity.defn
    async def report_flow_health(
        self,
        findings: list[dict],
        agent_id: str = "pandoras-actor",
        dedup_hours: int = 12,
        recovery_hours: int = 168,
    ) -> dict:
        """Notify about NEW unhealthy flows and about flows that recovered.

        One chat card per run, not one per finding, and a subject already
        alerted within `dedup_hours` (with no recovery since) is silent — so a
        flow wedged for a day produces one alert, not one per watchdog tick.

        Must be called even when `findings` is empty: that is precisely when
        recovery notices fire.
        """
        result = {"alerted": 0, "deduped": 0, "muted": 0, "recovered": 0}
        if not self.db_pool:
            return result

        subjects = [str(f.get("subject") or "") for f in findings]
        subjects = [s for s in subjects if s]

        async with self.db_pool.acquire() as conn:
            muted: set[str] = set()
            suppressed: set[str] = set()
            if subjects:
                muted = {
                    row["mute_key"][len(MUTE_PREFIX) :]
                    for row in await conn.fetch(
                        "SELECT mute_key FROM alert_mutes "
                        "WHERE mute_key = ANY($1::text[]) AND muted_until > now()",
                        [MUTE_PREFIX + s for s in subjects],
                    )
                }
                suppressed = {
                    row["target_id"]
                    for row in await conn.fetch(_SUPPRESSED_SQL, subjects, dedup_hours)
                }
            open_subjects = {
                row["target_id"] for row in await conn.fetch(_OPEN_SQL, recovery_hours)
            }

        fresh = [
            f
            for f in findings
            if f.get("subject")
            and f["subject"] not in muted
            and f["subject"] not in suppressed
        ]
        result["muted"] = sum(1 for f in findings if f.get("subject") in muted)
        result["deduped"] = sum(1 for f in findings if f.get("subject") in suppressed)

        if fresh:
            body = (
                "AEGIS scheduled-flow watchdog. These flows are not healthy:\n"
                + "\n".join(_describe(f) for f in fresh)
                + "\n\nInspect: SELECT status, started_at, result_summary->>'reason' "
                "FROM workflow_runs WHERE workflow_type = '<type>' "
                "ORDER BY started_at DESC LIMIT 10;"
            )
            if any(f.get("kind") == "llm_dead" for f in fresh):
                # A dead purpose is diagnosed in a different table, so the
                # operator gets the query that actually applies to it.
                body += (
                    "\nInspect an llm: purpose: SELECT status, model, error, created_at "
                    "FROM llm_calls WHERE purpose = '<purpose>' "
                    "ORDER BY created_at DESC LIMIT 10;"
                )
            body += (
                f"\nSilence one: INSERT INTO alert_mutes (mute_key, muted_until) "
                f"VALUES ('{MUTE_PREFIX}<subject>', now() + interval '2 days');"
            )
            await safe_send_message(
                self.delivery,
                agent_id=agent_id,
                message=_card(f"[FLOW] {len(fresh)} unhealthy flow(s)", body),
                log_event="flow_health_notify_failed",
            )
            await self._audit(ALERT_ACTION, fresh)
            result["alerted"] = len(fresh)

        # Recovery: previously alerted and no longer detected as unhealthy.
        # Writing the recovery row is what re-arms dedup, so this is both the
        # operator-visible signal AND the reason the next break alerts again.
        recovered = sorted(open_subjects - set(subjects))
        if recovered:
            await safe_send_message(
                self.delivery,
                agent_id=agent_id,
                message=_card(
                    f"[FLOW OK] {len(recovered)} flow(s) recovered",
                    "No longer failing or stale:\n"
                    + "\n".join(f"  {s}" for s in recovered),
                ),
                log_event="flow_health_recovery_notify_failed",
            )
            await self._audit(
                RECOVERY_ACTION, [{"subject": s, "kind": "recovered"} for s in recovered]
            )
            result["recovered"] = len(recovered)

        return result

    async def _audit(self, action: str, findings: list[dict]) -> None:
        from aegis.observability import log_audit

        for f in findings:
            await log_audit(
                self.db_pool,
                actor=ACTOR,
                action=action,
                target_type=TARGET_TYPE,
                target_id=str(f["subject"]),
                details={k: v for k, v in f.items() if k != "subject"},
            )
