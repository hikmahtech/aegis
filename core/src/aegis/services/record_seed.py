"""The record's seed (vault record spec §12): first drafts of the owner's `me/`
notes, built from what AEGIS already holds, written once as
`<dir>/<name>.draft.md` through `services/notes.py`, the only vault writer.

A draft is never compiled and never indexed. He accepts one by renaming it to
`<name>.md` (editing first if he likes) and rejects it by deleting it; nothing
here detects either. A note that already has a draft gets no second one. Each
draft opens with an Obsidian comment naming what it was built from and when,
so the line never reaches a prompt after he accepts it (`record.clean`
removes `%% … %%`). An empty draft is not written.

Three drafters, each written by its capability's holder:

* `draft_general` (`gtd`): the `user` document and the curiosity answers,
  sorted by ONE model call into about/work/people/health. The model returns
  line numbers, never text, so no line can be reworded; a line lost or
  placed twice refuses the whole draft. Meeting speaker labels (people) and
  recurring meeting titles (work) are added with no model.
* `draft_money` (`finance`): assembled from columns with no model. No line
  may pass `has_money_shape`: amounts stay in the books.
* `draft_interests` (`research`): one model call over note paths and tags,
  topic names, feed labels and tag counts. No journal path goes in, by path in
  code, and a theme must cite two sources that exist.

`retire_seeded_memory` is the hand-run step after the record is on: a
curiosity row whose answer is found word for word in an ACCEPTED record note
is retired through `apply_consolidation`, the only sanctioned writer.
"""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from datetime import date
from typing import Any

import structlog

from aegis.errors import error_text
from aegis.llm import LLMTruncationError, parse_llm_json
from aegis.services import books, notes
from aegis.services import vault_layout as vl
from aegis.services.bank_parsers import has_money_shape
from aegis.services.books_chart import get_chart
from aegis.services.feeds import feed_label
from aegis.services.meeting_rules import get_meeting_rules, is_self
from aegis.services.memory import CURIOSITY_ANSWER_PREFIX
from aegis.services.personalities import get_personality
from aegis.services.research_topics import load_topics
from aegis.services.user_time import user_now

logger = structlog.get_logger()

GENERAL_NOTES: dict[str, str] = {
    "about": "who he is, where he lives, his family, how he likes to be spoken to",
    "work": "his work, projects and businesses, and how he works",
    "people": "the people in his life and who they are to him",
    "health": "health, sleep, exercise and food",
}
MONEY_NOTE = "money"
INTERESTS_NOTE = "interests"
SORT_PURPOSE = "record_seed_sort"
INTERESTS_PURPOSE = "record_seed_interests"
# Kimi's hidden reasoning bills against max_tokens before the visible JSON
# (CLAUDE.md, "Reasoning-model token budget"): 8000 is over 3x the visible
# answer plus the reasoning for a ~6,000-token input.
SEED_MAX_TOKENS = 8000
INTERESTS_MAX_CHARS = 30_000
MAX_THEMES = 12
# An answer this short ("yes") must be found WITH its question to count.
MIN_ANSWER_CHARS = 12

_SEP = "\n" + CURIOSITY_ANSWER_PREFIX
_HEADING_RE = re.compile(r"^#{1,6}\s")
_LIST_MARK_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_GEMINI_TAIL_RE = re.compile(r"(?i)\s*[-–—]\s*notes by gemini\s*$")
_DATED_TAIL_RE = re.compile(r"\s+[-–—]\s+(?=\d)")
_ACCOUNT_RE = re.compile(r"(?i)^(assets|liabilities)(:|$)")

_SORT_SYSTEM = (
    "You sort numbered lines about one person into notes. You never reword, merge, split "
    "or drop a line. You answer with JSON only."
)
_SORT_PROMPT = (
    "Put every numbered line into exactly one of these notes:\n{notes}\n\n"
    "Answer with JSON: {{\"about\": [numbers], \"work\": [numbers], \"people\": [numbers], "
    "\"health\": [numbers]}}. Every number from 1 to {n} appears exactly once.\n\n{lines}"
)
_INTERESTS_SYSTEM = (
    "You read the outline of one person's notes, reading and feeds, and name the themes he "
    "cares about. You cite sources only by the ids given. You answer with JSON only."
)
_INTERESTS_PROMPT = (
    "Below are ids with labels: his notes (path and tags), the topics he tracks, the feeds "
    "he reads, and tag counts from his books and bookmarks. Name at most {max_themes} themes "
    "he cares about. For each give `theme` (a few words), `note` (one sentence: how deep he "
    "already is, or what he seems tired of, only as far as the sources show) and `sources` "
    "(at least two ids from the list). Answer with JSON: {{\"themes\": [{{\"theme\": \"\", "
    "\"note\": \"\", \"sources\": [\"n1\", \"t1\"]}}]}}.\n\n{lines}"
)


# --------------------------------------------------------------- pure parts


def one_line(text: Any) -> str:
    return " ".join(str(text or "").split())


def split_answer(content: str) -> tuple[str, str]:
    """`(question, answer)` of a curiosity memory row; `("", content)` when the
    row predates the format."""
    head, sep, answer = (content or "").partition(_SEP)
    return (head.strip(), answer.strip()) if sep else ("", (content or "").strip())


def answer_line(head: str, answer: str) -> str:
    return f"{one_line(head)} — {one_line(answer)}" if head.strip() else one_line(answer)


def doc_lines(doc: str) -> list[str]:
    """A persona document's lines to sort: no frontmatter, no headings (they
    are structure, not facts), list markers removed, words untouched."""
    out: list[str] = []
    for line in notes.FRONTMATTER_RE.sub("", doc or "", count=1).splitlines():
        if not line.strip() or _HEADING_RE.match(line.strip()):
            continue
        out.append(one_line(_LIST_MARK_RE.sub("", line)))
    return [x for x in out if x]


def placement(parsed: Any, n: int) -> dict[str, list[int]] | None:
    """The sort's answer when every line 1..n is placed exactly once in a known
    note, else None."""
    if not isinstance(parsed, dict) or set(parsed) - set(GENERAL_NOTES):
        return None
    out: dict[str, list[int]] = {}
    seen: list[int] = []
    for name in GENERAL_NOTES:
        nums = parsed.get(name) or []
        if not isinstance(nums, list) or not all(isinstance(x, int) and not isinstance(x, bool) for x in nums):
            return None
        out[name] = nums
        seen += nums
    return out if sorted(seen) == list(range(1, n + 1)) else None


def meeting_series(title: str) -> str:
    """A meeting's series name: the Google Doc title without its date and time
    and without "Notes by Gemini"."""
    return _DATED_TAIL_RE.split(_GEMINI_TAIL_RE.sub("", title or ""), maxsplit=1)[0].strip()


def draft_text(
    name: str, title: str, sources: str, day: date, sections: list[tuple[str, list[str]]]
) -> str:
    """A whole draft, or "" when every section is empty."""
    body = [
        f"## {heading}\n" + "\n".join(f"- {one_line(x)}" for x in lines)
        for heading, lines in sections
        if lines
    ]
    if not body:
        return ""
    head = (
        f"%% Draft by AEGIS on {day.isoformat()}, built from {sources}. Rename this file to "
        f"{name}.md to accept it (edit it first if you like); delete it to reject it. %%"
    )
    return f"{head}\n# {title}\n\n" + "\n\n".join(body) + "\n"


def build_sources(
    catalogue: list[tuple[str, tuple[str, ...]]],
    topics: list[tuple[str, str]],
    feeds: list[str],
    book_tags: list[tuple[str, int]],
    bookmark_tags: list[tuple[str, int]],
    layout: vl.Layout,
    max_chars: int = INTERESTS_MAX_CHARS,
) -> tuple[dict[str, str], list[str], int]:
    """`(label by id, prompt lines, notes left out for size)`. The journal is
    left out here by path, again, whatever the catalogue held."""
    labels: dict[str, str] = {}
    lines: list[str] = []
    for i, (name, priority) in enumerate(topics, 1):
        labels[f"t{i}"] = f'topic "{name}"'
        lines.append(f't{i} topic "{name}" (priority {priority})')
    for i, label in enumerate(feeds, 1):
        labels[f"f{i}"] = f'feed "{label}"'
        lines.append(f'f{i} feed "{label}"')
    for prefix, what, counts in (("b", "books", book_tags), ("r", "bookmarks", bookmark_tags)):
        for i, (tag, n) in enumerate(counts, 1):
            labels[f"{prefix}{i}"] = f'{what} tagged "{tag}"'
            lines.append(f'{prefix}{i} {what} tagged "{tag}" ({n})')
    size = sum(len(x) + 1 for x in lines)
    notes_in = sorted((rel, tags) for rel, tags in catalogue if not layout.is_journal_area(rel))
    omitted = 0
    for i, (rel, tags) in enumerate(notes_in, 1):
        line = f"n{i} note {rel}" + (f" tags: {', '.join(tags)}" if tags else "")
        if size + len(line) + 1 > max_chars:
            omitted = len(notes_in) - i + 1
            break
        labels[f"n{i}"] = rel
        lines.append(line)
        size += len(line) + 1
    return labels, lines, omitted


def keep_themes(parsed: Any, labels: dict[str, str], layout: vl.Layout) -> tuple[list[str], int]:
    """The draft's lines: themes citing at least two distinct sources that
    exist and none in the journal. `(lines, themes dropped)`."""
    kept: list[str] = []
    dropped = 0
    themes = parsed.get("themes") if isinstance(parsed, dict) else None
    for t in (themes if isinstance(themes, list) else [])[:MAX_THEMES]:
        if not isinstance(t, dict):
            dropped += 1
            continue
        raw = [str(x) for x in (t.get("sources") or []) if isinstance(x, str | int)]
        cited = [s for s in dict.fromkeys(raw) if s in labels]
        theme, note = one_line(t.get("theme"))[:120], one_line(t.get("note"))[:300]
        journal = any(layout.is_journal_area(r) or layout.is_journal_area(labels.get(r, "")) for r in raw)
        if not theme or len(cited) < 2 or journal:
            dropped += 1
            continue
        where = "; ".join(labels[s] for s in cited[:3])
        kept.append(f"{theme}: {note} (from {where})" if note else f"{theme} (from {where})")
    return kept, dropped


def seed_message(results: dict[str, dict]) -> str:
    """The one message to the owner: what was drafted, how to accept a draft,
    and what was not drafted and why."""
    written = [p for r in results.values() for p in (r.get("written") or [])]
    lines = ["I drafted your record in the vault. Nothing counts until you accept a draft:"]
    lines += [f"• {p}" for p in written]
    lines.append(
        "Accept one by renaming <name>.draft.md to <name>.md (edit it first if you like). "
        "Reject one by deleting it. Then turn the record on in Admin → Vault."
    )
    skipped = [
        f"{name}: {r.get('reason') or r.get('status')}"
        for name, r in results.items()
        if not r.get("written") and r.get("status") not in ("written",)
    ]
    if skipped:
        lines.append("Not drafted this time — " + "; ".join(skipped) + ".")
    return "\n".join(lines)


# ------------------------------------------------------------ the drafters


async def _write(
    pool: Any, cfg: notes.NotesConfig, layout: vl.Layout, agent_id: str, drafts: dict[str, str]
) -> dict:
    name = await pool.fetchval("SELECT name FROM agents WHERE id = $1", agent_id) if agent_id else None
    author = notes.author_for(agent_id, name)
    appends = [
        notes.Append(
            rel=layout.record.draft_path(n), key=f"record-seed:{n}", body=text,
            record=True, create_only=True, layout=layout,
        )
        for n, text in drafts.items()
    ]
    res = await notes.write(cfg, appends, f"{author.prefix}: record drafts", author=author)
    outcomes = res.get("outcomes") or []
    return {
        "written": [o["path"] for o in outcomes if o["changed"]],
        "existing": [o["path"] for o in outcomes if not o["changed"]],
    }


async def _files(cfg: notes.NotesConfig, layout: vl.Layout) -> notes.RecordFiles:
    return await asyncio.to_thread(notes.read_record_sync, cfg, layout)


async def _answers(pool: Any, agent_id: str) -> list[tuple[int, str, str]]:
    rows = await pool.fetch(
        "SELECT id, content FROM agent_memory WHERE agent_id = $1 AND source = 'curiosity' "
        "AND superseded_at IS NULL ORDER BY created_at, id",
        agent_id,
    )
    return [(int(r["id"]), *split_answer(r["content"])) for r in rows]


async def _think(
    llm: Any, model: str, pool: Any, agent_id: str, system: str, prompt: str, purpose: str
) -> Any:
    """The parsed JSON, or a reason string."""
    if llm is None:
        return "no_model"
    try:
        result = await llm.think(
            prompt=prompt, model=model, system_prompt=system, max_tokens=SEED_MAX_TOKENS,
            db_pool=pool, purpose=purpose, agent_id=agent_id or None,
        )
    except LLMTruncationError:
        return "truncated"
    except Exception as exc:  # noqa: BLE001 — a failed draft is reported, never raised
        logger.warning("record_seed_llm_failed", purpose=purpose, error=error_text(exc, 200))
        return "llm_failed"
    parsed = parse_llm_json((result or {}).get("response") or "")
    return parsed if parsed is not None else "unparseable"


async def _speaker_lines(pool: Any) -> list[str]:
    selves = (await get_meeting_rules(pool)).get("self_names") or []
    rows = await pool.fetch(
        "SELECT s AS speaker, count(DISTINCT c.content_id) AS n, max(c.metadata->>'meeting_date') AS last "
        "FROM knowledge_content c CROSS JOIN LATERAL jsonb_array_elements_text("
        "  CASE WHEN jsonb_typeof(c.metadata->'speakers') = 'array' THEN c.metadata->'speakers' ELSE '[]'::jsonb END"
        ") s WHERE c.source_type = 'meeting' GROUP BY s ORDER BY n DESC, s LIMIT 60"
    )
    return [
        f"{r['speaker']} — {r['n']} meeting{'s' if r['n'] != 1 else ''}"
        + (f", last {str(r['last'])[:7]}" if r["last"] else "")
        for r in rows
        if not is_self(r["speaker"], selves)
    ]


async def _series_lines(pool: Any) -> list[str]:
    rows = await pool.fetch("SELECT title FROM knowledge_content WHERE source_type = 'meeting'")
    counts = Counter(s for s in (meeting_series(r["title"]) for r in rows) if s)
    return [f"{s} — {n} meetings" for s, n in sorted(counts.items(), key=lambda x: (-x[1], x[0])) if n >= 2][:30]


async def draft_general(
    pool: Any, cfg: notes.NotesConfig, layout: vl.Layout, llm: Any, model: str, agent_id: str
) -> dict:
    """The about/work/people/health drafts, as the `gtd` holder."""
    if not cfg.configured:
        return {"status": "not_configured", "written": []}
    if layout.record.enabled:
        # The user row is then a cache of the vault; drafting from it would
        # only copy the record back into a draft.
        return {"status": "record_on", "reason": "the record is on", "written": []}
    try:
        files = await _files(cfg, layout)
        targets = [n for n in GENERAL_NOTES if n not in files.drafts]
        if not targets:
            return {"status": "exists", "written": [], "existing": [layout.record.draft_path(n) for n in GENERAL_NOTES]}
        doc = (await get_personality(pool, agent_id, use_cache=False)).get("user", "") or ""
        items = [*doc_lines(doc), *(answer_line(h, a) for _, h, a in await _answers(pool, agent_id))]
        placed: dict[str, list[str]] = {n: [] for n in GENERAL_NOTES}
        if items:
            prompt = _SORT_PROMPT.format(
                notes="\n".join(f"- {n}: {what}" for n, what in GENERAL_NOTES.items()),
                n=len(items),
                lines="\n".join(f"{i}. {t}" for i, t in enumerate(items, 1)),
            )
            parsed = await _think(llm, model, pool, agent_id, _SORT_SYSTEM, prompt, SORT_PURPOSE)
            where = placement(parsed, len(items)) if not isinstance(parsed, str) else None
            if where is None:
                reason = parsed if isinstance(parsed, str) else "lines_lost_or_repeated"
                return {"status": "refused", "reason": reason, "lines_in": len(items), "lines_out": 0, "written": []}
            placed = {n: [items[i - 1] for i in sorted(where[n])] for n in GENERAL_NOTES}
        extra = {"people": ("People you meet", await _speaker_lines(pool)),
                 "work": ("Recurring meetings", await _series_lines(pool))}
        day = (await user_now(pool)).date()
        drafts: dict[str, str] = {}
        for n in targets:
            sections = [("Notes", placed[n])]
            if n in extra:
                sections.append(extra[n])
            source = "your user document and curiosity answers" + (
                ", and meeting speakers" if n == "people" else ", and meeting titles" if n == "work" else ""
            )
            text = draft_text(n, n.title(), source, day, sections)
            if text:
                drafts[n] = text
        if not drafts:
            return {"status": "empty", "reason": "nothing to draft", "written": []}
        out = await _write(pool, cfg, layout, agent_id, drafts)
    except notes.NotesError as exc:
        return {"status": "error", "reason": error_text(exc, 200), "written": []}
    return {
        "status": "written" if out["written"] else "exists", **out,
        "lines_in": len(items), "lines_out": sum(len(v) for v in placed.values()),
    }


async def _account_lines(books_cfg: books.BooksConfig) -> list[str]:
    try:
        text = await books.run_hledger(["accounts", "--used"], books_cfg)
    except Exception as exc:  # noqa: BLE001 — no books is an empty section, not a failed draft
        logger.info("record_seed_accounts_unavailable", error=error_text(exc, 200))
        return []
    return [f"{a} — what is it for?" for a in (x.strip() for x in text.splitlines()) if _ACCOUNT_RE.match(a)]


async def _biller_lines(pool: Any) -> list[str]:
    rows = await pool.fetch(
        "WITH billers AS ("
        " SELECT payee_key, min(payee) AS payee,"
        "        mode() WITHIN GROUP (ORDER BY extract(day FROM due_on)::int) AS due_day"
        "   FROM finance.journal_index"
        "  WHERE kind IN ('due', 'failed') AND due_on IS NOT NULL AND payee_key IS NOT NULL"
        "    AND due_on >= current_date - 180"
        "  GROUP BY payee_key)"
        " SELECT b.payee, b.due_day, last.channel FROM billers b"
        " LEFT JOIN LATERAL (SELECT channel FROM finance.journal_index j"
        "   WHERE j.payee_key = b.payee_key AND j.channel IS NOT NULL"
        "   ORDER BY j.occurred_on DESC NULLS LAST LIMIT 1) last ON true"
        " ORDER BY b.payee"
    )
    out = []
    for r in rows:
        facts = [
            f"due around day {r['due_day']}" if r["due_day"] else "",
            f"last paid by {r['channel']}" if r["channel"] else "",
        ]
        said = ", ".join(f for f in facts if f)
        out.append(f"{r['payee']}: {said + '. ' if said else ''}Paid automatically or by hand, and from which account?")
    return out


async def _subscription_lines(pool: Any) -> list[str]:
    rows = await pool.fetch(
        "SELECT DISTINCT ON (lower(vendor_name)) vendor_name, cadence, last_seen_at "
        "FROM finance.recurring_charge ORDER BY lower(vendor_name), last_seen_at DESC"
    )
    items = await pool.fetch("SELECT title, kind FROM life.expiring_items ORDER BY expires_on, title")
    return [f"{r['vendor_name']}: {r['cadence']}, last seen {r['last_seen_at']:%Y-%m}" for r in rows] + [
        f"{r['title']} ({r['kind']}): renews" for r in items
    ]


async def draft_money(
    pool: Any, cfg: notes.NotesConfig, books_cfg: books.BooksConfig, layout: vl.Layout, agent_id: str
) -> dict:
    """The money draft, as the `finance` holder: columns, no model, no amounts."""
    if not cfg.configured:
        return {"status": "not_configured", "written": []}
    try:
        files = await _files(cfg, layout)
        if MONEY_NOTE in files.drafts:
            return {"status": "exists", "written": [], "existing": [layout.record.draft_path(MONEY_NOTE)]}
        chart = await get_chart(pool)
        sections = [
            ("Accounts and cards", await _account_lines(books_cfg)),
            ("Entities and filing", [f"{chart.label(e) or e} — what does it file, and how often?" for e in chart.ids]),
            ("Billers and how each is paid", await _biller_lines(pool)),
            ("Subscriptions", await _subscription_lines(pool)),
            ("What you told me", [answer_line(h, a) for _, h, a in await _answers(pool, agent_id)]),
        ]
        dropped = sum(1 for _, lines in sections for x in lines if has_money_shape(x))
        sections = [(h, [x for x in lines if not has_money_shape(x)]) for h, lines in sections]
        text = draft_text(MONEY_NOTE, "Money", "the books' account names, the chart's entities, the "
                          "billers and subscriptions AEGIS has seen, and your curiosity answers; "
                          "amounts stay in the books", (await user_now(pool)).date(), sections)
        if not text:
            return {"status": "empty", "reason": "nothing to draft", "written": [], "dropped": dropped}
        if any(has_money_shape(line) for line in text.splitlines()):
            return {"status": "refused", "reason": "money_shape", "written": [], "dropped": dropped}
        out = await _write(pool, cfg, layout, agent_id, {MONEY_NOTE: text})
    except notes.NotesError as exc:
        return {"status": "error", "reason": error_text(exc, 200), "written": []}
    return {"status": "written" if out["written"] else "exists", **out, "dropped": dropped}


async def _tag_counts(pool: Any, where: str) -> list[tuple[str, int]]:
    rows = await pool.fetch(
        f"SELECT t, count(*) AS n FROM knowledge_content, unnest(tags) t WHERE {where} "
        "GROUP BY t ORDER BY n DESC, t LIMIT 60"
    )
    return [(r["t"], int(r["n"])) for r in rows]


async def draft_interests(
    pool: Any, cfg: notes.NotesConfig, layout: vl.Layout, llm: Any, model: str, agent_id: str
) -> dict:
    """The interests draft, as the `research` holder: one model call over
    titles and tags, never a note body and never a journal path."""
    if not cfg.configured:
        return {"status": "not_configured", "written": []}
    try:
        files = await _files(cfg, layout)
        if INTERESTS_NOTE in files.drafts:
            return {"status": "exists", "written": [], "existing": [layout.record.draft_path(INTERESTS_NOTE)]}
        catalogue = await asyncio.to_thread(notes.catalogue_sync, cfg, layout)
        topics = [(t.name, t.priority) for t in await load_topics(pool)]
        feeds = [feed_label(r["identifier"], {"label": r["label"]}) for r in await pool.fetch(
            "SELECT identifier, config->>'label' AS label FROM channels "
            "WHERE kind = 'rss' AND active ORDER BY identifier")]
        labels, lines, omitted = build_sources(
            catalogue, topics, feeds,
            await _tag_counts(pool, "source_type = 'book' AND t <> 'book'"),
            await _tag_counts(pool, "'raindrop' = ANY(tags) AND t <> 'raindrop'"),
            layout,
        )
        if len(labels) < 2:
            return {"status": "empty", "reason": "no_sources", "written": []}
        prompt = _INTERESTS_PROMPT.format(max_themes=MAX_THEMES, lines="\n".join(lines))
        parsed = await _think(llm, model, pool, agent_id, _INTERESTS_SYSTEM, prompt, INTERESTS_PURPOSE)
        if isinstance(parsed, str):
            return {"status": "refused", "reason": parsed, "written": []}
        themes, dropped = keep_themes(parsed, labels, layout)
        text = draft_text(INTERESTS_NOTE, "Interests", "the titles and tags of your notes outside "
                          "the journal, your tracked topics, feeds, and book and bookmark tags",
                          (await user_now(pool)).date(), [("Themes", themes)])
        if not text:
            return {"status": "empty", "reason": "no_theme_cited_two_sources", "written": [], "dropped": dropped}
        out = await _write(pool, cfg, layout, agent_id, {INTERESTS_NOTE: text})
    except notes.NotesError as exc:
        return {"status": "error", "reason": error_text(exc, 200), "written": []}
    return {"status": "written" if out["written"] else "exists", **out, "dropped": dropped, "notes_omitted": omitted}
