"""Tender watch (#673): government IT bids Hikmah could answer, from GeM, as
items for Raphael's area digest.

Each run searches every keyword in the `tender-watch-daily` row's config,
newest bids first, and files each ongoing bid that still has at least
``min_days_left`` to its close as an item on ONE tracked topic (``topic``),
titled with what it buys, who is buying and when it closes. The bid's URL is
the item's key, so a bid is filed once, however many keywords find it. No
model: the area judge decides which reach the brief.

Only GeM is watched. CPPP's public listing shows the latest ten tenders and
ignores its page number, and its search sits behind a captcha; MahaTenders'
`robots.txt` disallows every path. Neither is read (spike, 2026-09-25).

Configuration is the activities row; nothing here names a keyword.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import structlog

from aegis.errors import error_text

logger = structlog.get_logger()

CONNECTOR = "gem"
BID_URL = "https://bidplus.gem.gov.in/showbidDocument/{b_id}"


@dataclass(frozen=True)
class TenderConfig:
    keywords: tuple[str, ...] = ()
    topic: str = "Government IT tenders"
    per_keyword: int = 10
    min_days_left: int = 3
    # A bid is kept only when its title names one of these, at the start of a
    # word ("digiti" catches digitisation). Empty = keep every bid.
    title_terms: tuple[str, ...] = ()

    @classmethod
    def from_config(cls, raw: Any) -> TenderConfig:
        c = raw if isinstance(raw, dict) else {}

        def num(key: str, default: int) -> int:
            try:
                return max(0, int(c.get(key, default)))
            except (TypeError, ValueError):
                return default

        kws = c.get("keywords")
        terms = c.get("title_terms")
        return cls(
            keywords=tuple(str(k).strip() for k in kws if str(k).strip()) if isinstance(kws, list) else (),
            topic=str(c.get("topic") or cls.topic).strip() or cls.topic,
            per_keyword=max(1, min(num("per_keyword", cls.per_keyword), 10)),
            min_days_left=num("min_days_left", cls.min_days_left),
            title_terms=tuple(str(t).strip() for t in terms if str(t).strip())
            if isinstance(terms, list)
            else (),
        )


def _one(v: Any) -> str:
    """GeM's search index returns most fields as one-element lists."""
    if isinstance(v, list):
        v = v[0] if v else ""
    return str(v or "").strip()


def _closes(doc: dict) -> date | None:
    raw = _one(doc.get("final_end_date_sort"))
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date() if raw else None
    except ValueError:
        return None


def names_a_term(title: str, terms: tuple[str, ...]) -> bool:
    """Whether the bid's title names one of ``terms`` at the start of a word, in
    any case. GeM's full-text search is loose — on 2026-09-25 "MIS" returned an
    EMISSIVITY coating, housekeeping and bus hire, and "GIS" mostly stationery —
    and it matches inside the bid documents, so the keyword alone says little.
    The title naming IT work is the test. No terms = every bid passes.

    A term written in capitals is an acronym — IT, GIS, API — and matches only
    as a whole word in capitals, so "IT" is not "Items" and "GIS" is not
    "logistics". Any other term matches at the start of a word, in any case."""
    if not terms:
        return True
    for t in terms:
        if t.isupper():
            if re.search(rf"(?<![\w]){re.escape(t)}(?![\w])", title):
                return True
        elif re.search(rf"(?<![\w]){re.escape(t)}", title, re.IGNORECASE):
            return True
    return False


def tender_item(
    doc: dict, keyword: str, today: date, min_days_left: int, title_terms: tuple[str, ...] = ()
) -> dict | None:
    """A bid as an area item, or None: no id, no close date, closing too soon,
    or a title that names none of ``title_terms``."""
    b_id = _one(doc.get("b_id"))
    number = _one(doc.get("b_bid_number"))
    what = _one(doc.get("b_category_name")) or _one(doc.get("bd_category_name"))
    closes = _closes(doc)
    if not b_id or not what or closes is None or (closes - today).days < min_days_left:
        return None
    if not names_a_term(what, title_terms):
        return None
    buyer = _one(doc.get("ba_official_details_minName")) or _one(doc.get("ba_official_details_deptName"))
    title = f"GeM bid: {what[:180]}" + (f" — {buyer}" if buyer else "") + f" (closes {closes:%d %b})"
    qty = _one(doc.get("b_total_quantity"))
    return {
        "title": title,
        "url": BID_URL.format(b_id=b_id),
        "summary": " · ".join(x for x in (number, f"qty {qty}" if qty else "", f"matched '{keyword}'") if x),
    }


async def run(pool: Any, client: Any, cfg: TenderConfig, *, today: date, settings: Any = None) -> dict:
    """One watch. A keyword whose search fails is skipped and named; when every
    one fails the connector is reported unhealthy."""
    from aegis.services import research_topics
    from aegis.services.connector_health import record_connector_health

    if not cfg.keywords:
        return {"skipped": "no_keywords"}
    items: dict[str, dict] = {}
    failed: list[str] = []
    for kw in cfg.keywords:
        try:
            docs = await client.search(kw)
        except Exception as exc:  # noqa: BLE001 — one keyword must not stop the rest
            logger.warning("tender_watch_search_failed", keyword=kw, error=error_text(exc))
            failed.append(kw)
            continue
        for doc in docs[: cfg.per_keyword]:
            item = tender_item(doc, kw, today, cfg.min_days_left, cfg.title_terms)
            if item and item["url"] not in items:
                items[item["url"]] = item
    ok = len(failed) < len(cfg.keywords)
    await record_connector_health(
        pool, settings, CONNECTOR, ok=ok, error="" if ok else f"every search failed: {', '.join(failed)}"
    )
    attached = await research_topics.attach_to_topic(pool, cfg.topic, list(items.values()), origin=CONNECTOR)
    logger.info("tender_watch_done", items=len(items), failed=failed, **attached)
    return {"items": len(items), "failed_topics": failed, **attached}
