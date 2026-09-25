"""World watch (#676): what changed in the countries and central banks the user
watches, from Quantamentry, as items for Raphael's area digest.

No model. Each run reads Quantamentry's scores and central-bank calendar and
turns a CHANGE into an item:

- a country on ``countries`` whose score tripped a regime shift recently;
- a country on ``countries`` whose score moved ``move_points`` or more in 7 days;
- a bank on ``banks`` (by country ISO) that meets within ``meeting_days``;
- a bank on ``banks`` whose stance moved ``stance_delta`` or more at a meeting
  held within ``recent_days``.

Items attach to one tracked topic (``topic``), which the user puts in an area,
so they reach the brief through the same judge as any article. Each item's URL
is keyed on the event (the regime's date, the move's week, the meeting's date),
so a run that sees the same event again attaches nothing.

Configuration is the ``world-watch-daily`` activities row; nothing here names a
country. With no ``countries`` and no ``banks`` it does nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import structlog

from aegis.errors import error_text

logger = structlog.get_logger()

CONNECTOR = "quantamentry"


@dataclass(frozen=True)
class WatchConfig:
    countries: tuple[str, ...] = ()
    banks: tuple[str, ...] = ()
    topic: str = "Country watch"
    move_points: float = 3.0
    meeting_days: int = 7
    recent_days: int = 7
    stance_delta: float = 0.2
    # Scores older than this are stale: nothing is filed and the connector is
    # reported unhealthy.
    stale_days: int = 3
    # "https://example.com/countries/{iso}"; empty = the item has no web link.
    link: str = ""

    @classmethod
    def from_config(cls, raw: Any) -> WatchConfig:
        c = raw if isinstance(raw, dict) else {}

        def isos(key: str) -> tuple[str, ...]:
            v = c.get(key)
            return tuple(str(x).strip().upper() for x in v if str(x).strip()) if isinstance(v, list) else ()

        def num(key: str, default: float) -> float:
            try:
                return float(c.get(key, default))
            except (TypeError, ValueError):
                return default

        return cls(
            countries=isos("countries"),
            banks=isos("banks"),
            topic=str(c.get("topic") or cls.topic).strip() or cls.topic,
            move_points=num("move_points", cls.move_points),
            meeting_days=int(num("meeting_days", cls.meeting_days)),
            recent_days=int(num("recent_days", cls.recent_days)),
            stance_delta=num("stance_delta", cls.stance_delta),
            stale_days=int(num("stale_days", cls.stale_days)),
            link=str(c.get("link") or ""),
        )


def _day(v: Any) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def _url(cfg: WatchConfig, iso: str, anchor: str) -> str:
    # The anchor is what makes the event unique: the URL is the item's key.
    base = cfg.link.format(iso=iso) if cfg.link else f"quantamentry://{iso}"
    return f"{base}#{anchor}"


def watch_items(
    scores: list[dict], calendar: list[dict], cfg: WatchConfig, today: date
) -> list[dict]:
    """The items worth filing today: ``{title, url, summary}``. Pure."""
    items: list[dict] = []
    watched = set(cfg.countries)
    for r in scores:
        iso = str(r.get("country_iso") or "").upper()
        if iso not in watched or r.get("is_current") is False:
            continue
        name = r.get("country_name") or iso
        score = r.get("composite_score")
        summary = str(r.get("summary") or "")
        regime = r.get("regime_shift") or {}
        tripped = _day(regime.get("tripped_date"))
        if regime.get("direction") in ("up", "down") and tripped and (today - tripped).days <= cfg.recent_days:
            way = "improving" if regime["direction"] == "up" else "deteriorating"
            items.append({
                "title": f"{name}: policy credibility has turned {way} (regime shift on {tripped:%d %b})",
                "url": _url(cfg, iso, f"regime-{tripped.isoformat()}"),
                "summary": f"Score {score}. {summary}",
            })
        d7 = r.get("delta_7d")
        if isinstance(d7, int | float) and abs(d7) >= cfg.move_points:
            year, week, _ = today.isocalendar()
            way = "rose" if d7 > 0 else "fell"
            items.append({
                "title": f"{name}: policy credibility score {way} {abs(d7):.1f} points in a week, to {score}",
                "url": _url(cfg, iso, f"move-{'up' if d7 > 0 else 'down'}-{year}-W{week:02d}"),
                "summary": summary,
            })
    banks = set(cfg.banks)
    for r in calendar:
        iso = str(r.get("country_iso") or "").upper()
        if iso not in banks:
            continue
        bank = r.get("bank") or iso
        nxt = _day(r.get("next_meeting"))
        if nxt and 0 <= (nxt - today).days <= cfg.meeting_days:
            items.append({
                "title": f"{bank} sets rates on {nxt:%A %d %b}",
                "url": _url(cfg, iso, f"meeting-{nxt.isoformat()}"),
                "summary": f"Next policy meeting of {bank} ({r.get('currency') or iso}).",
            })
        stance = r.get("stance") or {}
        last = _day(stance.get("latest_meeting"))
        delta = stance.get("delta")
        if (
            stance.get("status") == "available"
            and last
            and (today - last).days <= cfg.recent_days
            and isinstance(delta, int | float)
            and abs(delta) >= cfg.stance_delta
        ):
            way = "more hawkish" if delta > 0 else "more dovish"
            items.append({
                "title": f"{bank} turned {way} at its {last:%d %b} meeting",
                "url": _url(cfg, iso, f"stance-{last.isoformat()}"),
                "summary": f"Statement stance moved {delta:+.2f} from the meeting before.",
            })
    return items


async def run(pool: Any, client: Any, cfg: WatchConfig, *, today: date, settings: Any = None) -> dict:
    """One watch: check freshness, build the items, attach them to the topic.
    Reports connector health either way. Never files items from stale scores."""
    from aegis.services import research_topics
    from aegis.services.connector_health import record_connector_health

    if not cfg.countries and not cfg.banks:
        return {"skipped": "nothing_watched"}
    try:
        status = await client.status()
        latest = _day(status.get("latest_score_date"))
        if latest is None or (today - latest) > timedelta(days=cfg.stale_days):
            raise RuntimeError(f"scores are stale (latest {status.get('latest_score_date')})")
        scores = await client.scores() if cfg.countries else []
        calendar = await client.cb_calendar() if cfg.banks else []
    except Exception as exc:  # noqa: BLE001 — reported, never raised into the flow
        error = error_text(exc, 300)
        await record_connector_health(pool, settings, CONNECTOR, ok=False, error=error)
        logger.warning("world_watch_failed", error=error)
        return {"error": error}
    await record_connector_health(pool, settings, CONNECTOR, ok=True)
    items = watch_items(scores, calendar, cfg, today)
    attached = await research_topics.attach_to_topic(pool, cfg.topic, items, origin=CONNECTOR)
    logger.info("world_watch_done", items=len(items), **attached)
    return {"items": len(items), **attached}
