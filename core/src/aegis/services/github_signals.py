"""GitHub signals (#677): what builders are adopting, and where a tool hurts.

**Rising repos** (weekly, `GithubRisingFlow` on the `github-rising-weekly`
row): for each topic in the row's config, the repositories created in the last
``window_days`` with at least ``min_stars`` stars, most-starred first. Each is
an item on ONE tracked topic (``topic``), which the user puts in an area; the
repo's URL is the item's key, so a repo is filed once, ever. No model: the
area judge decides what is worth the brief.

**Issue search** (`github_issues` chat tool): a project's issues, ranked by
reactions, narrowed to bugs, performance or production trouble — "the docs say
what it can do, the issues say where it hurts".

Configuration is the activities row; nothing here names a topic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import structlog

from aegis.errors import error_text

logger = structlog.get_logger()

CONNECTOR = "github"

# What each focus adds to an issue search. The words are GitHub's own search
# syntax; a label that a project does not use simply matches nothing extra.
FOCUS: dict[str, str] = {
    "bug": 'label:bug',
    "performance": '(performance OR slow OR memory OR latency)',
    "production": '(production OR "in prod" OR outage OR "data loss" OR corruption)',
    "any": "",
}


@dataclass(frozen=True)
class RisingConfig:
    topics: tuple[str, ...] = ()
    topic: str = "GitHub rising"
    window_days: int = 30
    min_stars: int = 100
    per_topic: int = 3

    @classmethod
    def from_config(cls, raw: Any) -> RisingConfig:
        c = raw if isinstance(raw, dict) else {}

        def num(key: str, default: int) -> int:
            try:
                return max(1, int(c.get(key, default)))
            except (TypeError, ValueError):
                return default

        topics = c.get("topics")
        return cls(
            topics=tuple(str(t).strip().lower() for t in topics if str(t).strip())
            if isinstance(topics, list)
            else (),
            topic=str(c.get("topic") or cls.topic).strip() or cls.topic,
            window_days=num("window_days", cls.window_days),
            min_stars=num("min_stars", cls.min_stars),
            per_topic=num("per_topic", cls.per_topic),
        )


def rising_query(gh_topic: str, cfg: RisingConfig, today: date) -> str:
    since = (today - timedelta(days=cfg.window_days)).isoformat()
    return f"topic:{gh_topic} created:>={since} stars:>={cfg.min_stars} archived:false"


def repo_item(repo: dict, gh_topic: str) -> dict | None:
    """A search hit as an area item, or None when it is not a usable repo."""
    url = str(repo.get("html_url") or "")
    name = str(repo.get("full_name") or "")
    if not url or not name:
        return None
    desc = str(repo.get("description") or "").strip()
    stars = int(repo.get("stargazers_count") or 0)
    lang = repo.get("language") or ""
    title = f"{name}: {desc}" if desc else name
    return {
        "title": f"{title[:200]} (★{stars:,}, new)",
        "url": url,
        "summary": " · ".join(x for x in (f"#{gh_topic}", lang, f"created {str(repo.get('created_at'))[:10]}") if x),
    }


async def run_rising(pool: Any, client: Any, cfg: RisingConfig, *, today: date) -> dict:
    """One weekly run. A topic whose search fails is skipped and named; the
    rest still file."""
    from aegis.services import research_topics

    if not cfg.topics:
        return {"skipped": "no_topics"}
    items: dict[str, dict] = {}
    failed: list[str] = []
    for gh_topic in cfg.topics:
        try:
            hits = await client.search_repos(rising_query(gh_topic, cfg, today), per_page=cfg.per_topic)
        except Exception as exc:  # noqa: BLE001 — one topic must not stop the rest
            logger.warning("github_rising_search_failed", topic=gh_topic, error=error_text(exc))
            failed.append(gh_topic)
            continue
        for repo in hits[: cfg.per_topic]:
            item = repo_item(repo, gh_topic)
            if item and item["url"] not in items:
                items[item["url"]] = item
    attached = await research_topics.attach_to_topic(pool, cfg.topic, list(items.values()), origin=CONNECTOR)
    logger.info("github_rising_done", items=len(items), failed=failed, **attached)
    return {"items": len(items), "failed_topics": failed, **attached}


def issues_query(repo: str, focus: str, terms: str = "") -> str:
    parts = [f"repo:{repo}", "is:issue", FOCUS.get(focus, ""), terms.strip()]
    return " ".join(p for p in parts if p)


def issue_row(i: dict) -> dict:
    reactions = i.get("reactions") or {}
    return {
        "title": str(i.get("title") or "")[:300],
        "url": i.get("html_url") or "",
        "state": i.get("state") or "",
        "comments": int(i.get("comments") or 0),
        "reactions": int(reactions.get("total_count") or 0) if isinstance(reactions, dict) else 0,
        "labels": [lb.get("name") for lb in i.get("labels") or [] if isinstance(lb, dict)][:6],
        "created": str(i.get("created_at") or "")[:10],
        "excerpt": str(i.get("body") or "")[:300],
    }
