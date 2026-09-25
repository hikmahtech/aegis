"""Research areas (#674): what changed for the user, not what was published.

An **area** groups tracked topics (``research_topics``) and says how the user
wants to hear about them. Areas live in the same `intelligence_topics` row as
the topics, under ``areas``, so the registry stays one row:

    {"topics": [...],
     "areas": [{"name": "India", "why": "I live and invest here",
                "cadence": "daily", "cap": 2, "topics": ["India politics"]}]}

- ``cadence`` — ``daily`` (the morning brief), ``weekly`` (the brief on the
  weekly day) or ``vault`` (a weekly note in the vault; never interrupts).
- ``cap`` — at most this many stories from the area per digest.
- ``why`` — the user's own sentence; the judge weighs every story against it.

A topic in an area never raises a Todoist task from item count: the digest is
how its news reaches the user (`research_topics.attach_items`).

A digest takes the area's topic items since a cursor, folds the ones about
the same event into one **story** (title-word overlap, no model), drops the
stories already shown, and asks one `think()` call which of the rest are real
developments for this user. A model failure falls back to the stories most
sources carried, so the brief always ships.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog

from aegis.errors import error_text
from aegis.services.hub import TOPIC_CLASS, slug

logger = structlog.get_logger()

CADENCES = ("daily", "weekly", "vault")
DEFAULT_CAP = 3
MAX_CAP = 20
# Stories shown to the judge per area; the rest are the least-carried.
JUDGE_STORIES = 40
# Items read per area per digest. ponytail: a flat cap, page it if an area
# ever outgrows it within one window.
MAX_ITEMS = 400
# Two titles are one story at this word overlap (Jaccard).
SAME_STORY = 0.5

_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    ["the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "at", "by", "from", "as", "is", "are", "was", "were", "be", "been", "its", "it", "this", "that", "after", "over", "into", "amid", "says", "said", "new", "how", "why", "what", "who", "will", "has", "have", "had", "not", "but", "than", "more", "about", "up", "out"]
)


@dataclass(frozen=True)
class Area:
    name: str
    why: str = ""
    cadence: str = "daily"
    cap: int = DEFAULT_CAP
    topics: tuple[str, ...] = ()

    @property
    def slug(self) -> str:
        return slug(self.name)


def _cap(raw: Any) -> int | None:
    if isinstance(raw, bool) or not isinstance(raw, int | float) or int(raw) != raw:
        return None
    return int(raw) if 1 <= int(raw) <= MAX_CAP else None


def parse_areas(value: Any, topic_names: list[str] | None = None) -> list[Area]:
    """The areas in an `intelligence_topics` value. Lenient: a bad entry is
    skipped, a topic nobody tracks is dropped, a topic claimed twice stays
    with the first area. Never raises."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    if not isinstance(value, dict) or not isinstance(value.get("areas"), list):
        return []
    known = {slug(n): n for n in topic_names} if topic_names is not None else None
    out: list[Area] = []
    seen: set[str] = set()
    claimed: set[str] = set()
    for raw in value["areas"]:
        if not isinstance(raw, dict):
            continue
        name = raw.get("name")
        if not isinstance(name, str) or not slug(name) or slug(name) in seen:
            continue
        topics: list[str] = []
        for t in raw.get("topics") or []:
            if not isinstance(t, str) or slug(t) in claimed:
                continue
            if known is not None and slug(t) not in known:
                continue
            claimed.add(slug(t))
            topics.append(known[slug(t)] if known is not None else t.strip())
        why = raw.get("why")
        out.append(
            Area(
                name=name.strip(),
                why=why.strip() if isinstance(why, str) else "",
                cadence=raw.get("cadence") if raw.get("cadence") in CADENCES else "daily",
                cap=_cap(raw.get("cap")) or DEFAULT_CAP,
                topics=tuple(topics),
            )
        )
        seen.add(slug(name))
    return out


def validate_areas(value: Any, topic_names: list[str]) -> list[dict[str, Any]]:
    """Strict counterpart to `parse_areas`, for the admin write path. Raises
    ValueError with a message the page can show."""
    if not isinstance(value, list):
        raise ValueError("areas must be a list")
    known = {slug(n): n for n in topic_names}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    claimed: dict[str, str] = {}
    for i, raw in enumerate(value, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"area {i} must be an object")
        name = raw.get("name")
        if not isinstance(name, str) or not slug(name):
            raise ValueError(f"area {i} needs a name")
        name = name.strip()
        if slug(name) in seen:
            raise ValueError(f"area {name!r} is listed twice")
        seen.add(slug(name))
        why = raw.get("why", "")
        if not isinstance(why, str) or len(why) > 500:
            raise ValueError(f"area {name!r}: why must be text of at most 500 characters")
        cadence = raw.get("cadence", "daily")
        if cadence not in CADENCES:
            raise ValueError(f"area {name!r}: cadence must be one of {', '.join(CADENCES)}")
        cap = raw.get("cap", DEFAULT_CAP)
        if _cap(cap) is None:
            raise ValueError(f"area {name!r}: cap must be a whole number from 1 to {MAX_CAP}")
        topics = raw.get("topics", [])
        if not isinstance(topics, list) or not all(isinstance(t, str) for t in topics):
            raise ValueError(f"area {name!r}: topics must be a list of topic names")
        names: list[str] = []
        for t in (t.strip() for t in topics if t.strip()):
            if slug(t) not in known:
                raise ValueError(f"area {name!r}: {t!r} is not a tracked topic")
            if slug(t) in claimed:
                raise ValueError(f"topic {t!r} is in both {claimed[slug(t)]!r} and {name!r}")
            claimed[slug(t)] = name
            names.append(known[slug(t)])
        out.append({"name": name, "why": why.strip(), "cadence": cadence, "cap": int(cap), "topics": names})
    return out


def area_topic_slugs(areas: list[Area]) -> set[str]:
    return {slug(t) for a in areas for t in a.topics}


# ------------------------------------------------------------------ stories


def _words(title: str) -> frozenset[str]:
    return frozenset(w for w in _WORD.findall(title.lower()) if len(w) > 2 and w not in _STOP)


def story_key(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:16]


def cluster(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold items about the same event into stories. ``items`` carry
    ``title``, ``url`` and ``occurred_at``; a story is keyed on its EARLIEST
    item's URL, so the key holds while later coverage joins it.

    ponytail: greedy title-word Jaccard, O(n²) over ≤ MAX_ITEMS. Two headlines
    worded differently stay two stories; move to the stored embeddings if the
    judge keeps seeing the same event twice.
    """
    stories: list[dict[str, Any]] = []
    for it in sorted(items, key=lambda i: (str(i.get("occurred_at") or ""), str(i.get("url")))):
        words = _words(str(it.get("title") or ""))
        for s in stories:
            union = words | s["_words"]
            if union and len(words & s["_words"]) / len(union) >= SAME_STORY:
                s["sources"] += 1
                s["_urls"].add(it["url"])
                break
        else:
            stories.append({
                "key": story_key(it["url"]),
                "title": str(it.get("title") or "")[:300],
                "url": it["url"],
                "summary": str(it.get("summary") or "")[:300],
                "topic": it.get("topic") or "",
                "sources": 1,
                "_words": words,
                "_urls": {it["url"]},
            })
    for s in stories:
        s.pop("_words")
        s["sources"] = len(s.pop("_urls"))
    return stories


async def area_items(pool: Any, area: Area, since: datetime) -> list[dict[str, Any]]:
    """The area's topic items attached after ``since``, one per URL."""
    if not area.topics:
        return []
    rows = await pool.fetch(
        "SELECT e.payload, e.occurred_at FROM problem_events e "
        "JOIN problems p ON p.id = e.problem_id "
        "WHERE p.class = $1 AND e.kind = 'occurrence' AND e.payload->>'item' = 'true' "
        "  AND e.occurred_at > $2 AND e.payload->>'topic' = ANY($3::text[]) "
        "ORDER BY e.occurred_at DESC LIMIT $4",
        TOPIC_CLASS,
        since,
        list(area.topics),
        MAX_ITEMS,
    )
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        p = r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"] or "{}")
        url = str(p.get("url") or "")
        if url and p.get("title"):
            out[url] = {**p, "url": url, "occurred_at": r["occurred_at"].isoformat()}
    return list(out.values())


# ------------------------------------------------------------------ judge


def judge_prompt(area: Area, stories: list[dict[str, Any]], shown: list[str]) -> str:
    listing = "\n".join(
        f"{n}. {s['title']} ({s['sources']} source{'s' if s['sources'] != 1 else ''})"
        + (f" — {s['summary'][:160]}" if s.get("summary") else "")
        for n, s in enumerate(stories, start=1)
    )
    recent = "\n".join(f"- {t}" for t in shown[-20:]) or "- (nothing yet)"
    return (
        f"You pick news for one person. Area: {area.name}.\n"
        f"Why they care, in their words: {area.why or '(not given)'}\n\n"
        "Most news is noise. Pick at most "
        f"{area.cap} of the stories below that are a real development this person "
        "would want to know about: something changed that affects their money, "
        "work, city or decisions, or a turning point in a story they follow. "
        "Skip opinion, listicles, minor updates, repeats of what they were "
        "already shown, and anything you would call routine. Picking none is fine.\n\n"
        f"Already shown recently:\n{recent}\n\n"
        f"Stories:\n{listing}\n\n"
        'Answer with JSON only: [{"n": <story number>, "why": "<one short line: '
        'what changed and why it matters to them>"}]'
    )


def parse_picks(raw: str, count: int, cap: int) -> list[dict[str, Any]] | None:
    """The judge's picks, in its order, deduped, within range and cap. `[]`
    is "nothing worth it"; None is an answer it cannot read (fall back)."""
    m = re.search(r"\[.*\]", raw or "", re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    for p in data:
        n = p.get("n") if isinstance(p, dict) else None
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= count or n in seen:
            continue
        seen.add(n)
        out.append({"n": n, "why": str(p.get("why") or "").strip()[:200]})
        if len(out) >= cap:
            break
    return out


def fallback_picks(stories: list[dict[str, Any]], cap: int) -> list[dict[str, Any]]:
    """The stories most sources carried: what a judge-less brief shows."""
    order = sorted(range(len(stories)), key=lambda i: -stories[i]["sources"])
    return [{"n": i + 1, "why": ""} for i in order[:cap]]


async def build_digest(
    pool: Any,
    area: Area,
    *,
    since: datetime,
    seen_keys: set[str],
    shown_titles: list[str],
    llm: Any = None,
    model: str | None = None,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """One area's digest: ``{"area", "stories": [{key, title, url, sources,
    why}], "candidates", "judged"}``. ``judged`` is False when the model was
    not asked or failed and the fallback picked."""
    stories = [s for s in cluster(await area_items(pool, area, since)) if s["key"] not in seen_keys]
    result: dict[str, Any] = {"area": area.name, "stories": [], "candidates": len(stories), "judged": False}
    if not stories:
        return result
    stories = sorted(stories, key=lambda s: -s["sources"])[:JUDGE_STORIES]
    picks: list[dict[str, Any]] = []
    if llm is not None:
        try:
            resp = await llm.think(
                judge_prompt(area, stories, shown_titles),
                model=model,
                db_pool=pool,
                purpose="area_digest",
                agent_id=agent_id,
            )
            raw = resp.get("response", "") if isinstance(resp, dict) else str(resp or "")
            parsed = parse_picks(raw, len(stories), area.cap)
            if parsed is None:
                logger.warning("area_digest_judge_unreadable", area=area.name)
            else:
                picks, result["judged"] = parsed, True
        except Exception as exc:  # noqa: BLE001 — the brief ships on the fallback
            logger.warning("area_digest_judge_failed", area=area.name, error=error_text(exc))
    if not result["judged"]:
        picks = fallback_picks(stories, area.cap)
    for p in picks:
        s = stories[p["n"] - 1]
        result["stories"].append({
            "key": s["key"], "title": s["title"], "url": s["url"],
            "sources": s["sources"], "why": p["why"],
        })
    return result
