"""Chat tools over the `life` schema — the people registry and observations.

Both are read-only summaries. They go through the owning services
(`services/people.py`, `services/observations.py`) rather than querying the
tables, because those services hold the normalisation the lookups depend on.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import asyncpg

from aegis.services.tools.base import ToolContext
from aegis.services.tools.registry import aegis_tool


@aegis_tool
async def _exec_last_contact_with_person(
    pool: asyncpg.Pool, ctx: ToolContext, *, name: str
) -> str:
    """Look up someone in the people registry: when you were last in contact, how you know them, their key dates and any notes. Matches their name or any alias (nickname, maiden name, email).

    Args:
        name: Name, nickname or email address of the person, as the user said it. Case doesn't matter.

    Returns:
        Answers "when did I last talk to X?" from life.people (migration 016).
        Goes through services.people.find_people, which lowercases the needle
        before probing `lower(name)` / `aliases @> ARRAY[$1]` — aliases are
        stored lowercased (normalize_aliases), so any lookup that skips that
        normalisation silently misses every mixed-case alias.
    """
    from aegis.services.people import find_people

    name = (name or "").strip()
    if not name:
        return "Refused: empty name"
    if pool is None:
        return "People registry unavailable."
    matches = await find_people(pool, name)
    if not matches:
        return (
            f"No one called '{name}' is in the people registry — "
            "add them on the admin People page to track contact."
        )
    lines: list[str] = []
    for person in matches[:5]:
        header = person["name"]
        if person.get("relationship"):
            header += f" ({person['relationship']})"
        last = person.get("last_contact")
        if last:
            days = (datetime.now(UTC) - last).days
            ago = "today" if days <= 0 else ("yesterday" if days == 1 else f"{days} days ago")
            lines.append(f"{header} — last contact {last.date().isoformat()} ({ago})")
        else:
            lines.append(f"{header} — no contact recorded yet")
        key_dates = person.get("key_dates") or {}
        if isinstance(key_dates, str):
            try:
                key_dates = json.loads(key_dates)
            except (ValueError, TypeError):
                key_dates = {}
        if isinstance(key_dates, dict) and key_dates:
            rendered = ", ".join(f"{k}: {v}" for k, v in list(key_dates.items())[:5])
            lines.append(f"  key dates — {rendered}")
        if person.get("notes"):
            lines.append(f"  notes — {str(person['notes'])[:300]}")
    return "\n".join(lines)


@aegis_tool
async def _exec_query_observations(
    pool: asyncpg.Pool, ctx: ToolContext, *, metric: str, window_days: int = 30
) -> str:
    """Summarise a recorded life metric (weight, sleep hours, steps, a home-sensor reading) over a recent window: how many readings, latest value, min/max/average, and whether it is trending up or down against the window before it.

    Args:
        metric: Metric name as it was recorded, e.g. 'weight_kg', 'sleep_hours', 'steps'. Case doesn't matter.
        window_days: How many days back to look. Default 30.

    Returns:
        Two `summarize` calls against life.observations (migration 017): the
        requested window, and the window immediately before it, so the answer
        can say which way the metric is moving instead of just quoting an
        average. `services.observations` lowercases the metric on both write
        and read, so 'Weight' finds what the sensor wrote.
    """
    from aegis.services.observations import summarize

    metric = (metric or "").strip()
    if not metric:
        return "Refused: empty metric"
    if pool is None:
        return "Observation store unavailable."
    try:
        window = int(window_days or 30)
    except (TypeError, ValueError):
        window = 30
    window = max(1, min(window, 3650))

    now = datetime.now(UTC)
    current = await summarize(pool, metric, window_days=window, until=now)
    if not current["count"]:
        return (
            f"No '{metric}' observations in the last {window} days — "
            "nothing has recorded that metric yet."
        )

    lines = [f"{metric} — {current['count']} observation(s) in the last {window} days"]
    if current["avg"] is None:
        # Rows exist but every `value` is NULL: a metadata-only signal
        # (location ping, door-open event) rather than a number series.
        lines.append("no numeric values recorded — this metric carries metadata only")
        return "\n".join(lines)

    if current["latest"] is not None and current["latest_at"] is not None:
        lines.append(
            f"latest {current['latest']:.2f} at {current['latest_at'].date().isoformat()}"
        )
    lines.append(
        f"min {current['min']:.2f} / max {current['max']:.2f} / avg {current['avg']:.2f}"
    )

    previous = await summarize(pool, metric, window_days=window, until=current["since"])
    prev_avg = previous["avg"]
    if prev_avg is None:
        lines.append(f"trend: no data for the previous {window} days to compare against")
    else:
        delta = current["avg"] - prev_avg
        # 1% relative tolerance so sensor noise doesn't read as a trend.
        flat = abs(delta) <= abs(prev_avg) * 0.01 if prev_avg else abs(delta) < 1e-9
        if flat:
            lines.append(f"trend: flat vs the previous {window} days (avg {prev_avg:.2f})")
        else:
            direction = "up" if delta > 0 else "down"
            lines.append(
                f"trend: {direction} {abs(delta):.2f} vs the previous {window} days "
                f"(avg {prev_avg:.2f})"
            )
    return "\n".join(lines)
