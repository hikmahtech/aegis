# Meeting Notes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn note-taker emails (Gemini, Read.ai, …) into full meeting notes in the knowledge store, a per-meeting self-review with speaking metrics, and a block in the Sunday weekly review.

**Architecture:** A `meeting` sender-override tag on the existing email triage spawns a `MeetingNotesFlow` child from `GmailIngestFlow`, mirroring the money fan-out. One activities class fetches the linked Google Doc with the mailbox's Drive token (falls back to the email body), splits notes from the speaker-labelled transcript, files the notes as `source_type=meeting`, computes the user's talk share in code, asks the LLM for contributions/problems/commitments/one brevity note using only the user's own lines, files that as `meeting_review`, and writes the numbers to `life.observations`. `WeeklyReviewFlow` gains a deterministic meetings block.

**Tech Stack:** Python 3.12, Temporal (`temporalio`), asyncpg, google-api-python-client (Gmail v1 + Drive v3), FastAPI, pytest + pytest-asyncio + `WorkflowEnvironment`.

**Spec:** `docs/superpowers/specs/2026-09-02-meeting-notes-design.md`

## Global Constraints

- Work in this worktree only: `/home/arshad/Workspace/hikmah/aegis/.claude/worktrees/meeting-notes` (branch `worktree-meeting-notes`). Never `cd` to the main checkout.
- Run tests ONE package at a time, exactly as CI does, and tee the output: `pytest tests/worker/<file> -n auto --dist loadfile --timeout=300 2>&1 | tee logs/test-<task>.log` (create `logs/` if missing; it is gitignored). Never bare `pytest`.
- Lint with `ruff check core/src/ tests/core/` and `ruff check worker/src/ tests/worker/`. Do NOT run `ruff format` on `core/src/aegis/services/chat.py` or `core/src/aegis/services/tools/infra.py`; nothing in this plan touches them.
- Commit messages: one line, semantic type (`feat(meeting): …`, `test(meeting): …`), no trailers, no co-author lines.
- No new migrations. No new env vars. No new pip dependencies.
- Every flow config dataclass has `agent_id: str` as its FIRST field.
- Never write the raw transcript into `knowledge_content`.
- `record_external_observation` returning `None` means "already ingested", not failure.
- Resolve the model through the tier-resolved `model_balanced` local in `worker/__main__.py`, never `settings.model_*`.
- Tests need Postgres on port 25432: `docker compose up -d postgres` from the worktree root if `tests/worker/conftest.py::db_pool` skips.
- Every test must fail when its logic is broken. After writing a test, break the implementation once (comment a line, flip a comparison), watch it fail, restore it.

---

## File map

| File | Responsibility |
|---|---|
| `core/src/aegis/services/meeting_rules.py` (new) | `settings.meeting_rules` read/validate; `is_self()` name matcher |
| `core/src/aegis/api/routes/email_admin.py` (modify) | `GET/PUT /api/admin/email/meeting-rules` (validating write path) |
| `core/src/aegis/services/source_types.py` (modify) | register `meeting`, `meeting_review` |
| `worker/src/aegis_worker/activities/meeting.py` (new) | pure helpers (doc-id, transcript split, stats, render) + `MeetingActivities` (`fetch_meeting_document`, `analyse_meeting`) |
| `worker/src/aegis_worker/flows/meeting_notes.py` (new) | `MeetingNotesFlow` child workflow |
| `worker/src/aegis_worker/registry.py` (modify) | `FlowSpec(MeetingNotesFlow)` |
| `worker/src/aegis_worker/__main__.py` (modify) | construct `MeetingActivities`, add to `collect_activities` |
| `worker/src/aegis_worker/flows/gmail_ingest.py` (modify) | `meeting` tag fan-out; skip `ingest_email_to_kg` |
| `worker/src/aegis_worker/activities/review.py` (modify) | `gather_meeting_week` activity, `format_meeting_week` formatter |
| `worker/src/aegis_worker/flows/review.py` (modify) | append the meetings block |
| `CLAUDE.md` (modify) | one bullet on the `meeting` tag + `meeting_rules` |
| tests | `tests/core/services/test_meeting_rules.py`, `tests/core/test_meeting_rules_route.py`, `tests/core/services/test_source_types.py`, `tests/worker/activities/test_meeting_helpers.py`, `tests/worker/activities/test_meeting_fetch.py`, `tests/worker/activities/test_meeting_analyse.py`, `tests/worker/flows/test_meeting_notes_flow.py`, `tests/worker/flows/test_gmail_meeting_fanout.py`, `tests/worker/test_review_flows.py`, `tests/worker/activities/test_review_meeting_week.py` |

---

### Task 1: `meeting_rules` settings module, admin route, source-type registry

**Files:**
- Create: `core/src/aegis/services/meeting_rules.py`
- Modify: `core/src/aegis/api/routes/email_admin.py` (append two routes)
- Modify: `core/src/aegis/services/source_types.py` (two dict entries, before the closing `}` of `SOURCE_TYPES`)
- Modify: `tests/core/services/test_source_types.py` (`VERIFIED_LITERALS`)
- Test: `tests/core/services/test_meeting_rules.py`, `tests/core/test_meeting_rules_route.py`

**Interfaces:**
- Produces: `meeting_rules.SETTINGS_KEY = "meeting_rules"`, `merge(value) -> {"self_names": list[str]}`, `validate(value) -> dict` (raises `ValueError`), `async get_meeting_rules(pool) -> dict`, `async save_meeting_rules(pool, rules) -> dict`, `is_self(speaker: str, self_names: list[str]) -> bool`.

- [ ] **Step 1: Write the failing unit tests**

`tests/core/services/test_meeting_rules.py`:

```python
"""settings.meeting_rules — who "you" are in a transcript."""

from __future__ import annotations

import pytest
from aegis.services.meeting_rules import is_self, merge, validate


def test_merge_defaults_are_empty_so_a_fork_ships_no_name():
    assert merge(None) == {"self_names": []}
    assert merge({}) == {"self_names": []}


def test_merge_is_lenient_and_strips():
    assert merge({"self_names": [" Sam Doe ", "", 3, "Sam"]}) == {"self_names": ["Sam Doe", "Sam"]}
    assert merge({"self_names": "Sam"}) == {"self_names": []}


def test_validate_rejects_non_list_and_blank_entries():
    with pytest.raises(ValueError):
        validate({"self_names": "Sam"})
    with pytest.raises(ValueError):
        validate({"self_names": ["Sam", ""]})
    with pytest.raises(ValueError):
        validate({"self_names": [1]})
    assert validate({"self_names": ["Sam Doe"]}) == {"self_names": ["Sam Doe"]}


def test_is_self_matches_case_insensitive_substring():
    assert is_self("Sam Doe", ["sam"])
    assert is_self("SAM DOE", ["Sam Doe"])
    assert not is_self("Samantha Roe", ["Sam Doe"])
    assert not is_self("Sam Doe", [])
    assert not is_self("", ["Sam"])
```

- [ ] **Step 2: Run them, expect ImportError**

Run: `pytest tests/core/services/test_meeting_rules.py -v 2>&1 | tee logs/test-t1a.log`
Expected: FAIL, `ModuleNotFoundError: aegis.services.meeting_rules`.

- [ ] **Step 3: Write the module**

`core/src/aegis/services/meeting_rules.py`:

```python
"""Meeting-notes rules — who "you" are in a transcript.

DB-owned so a fork ships nobody's name. Stored in the ``settings`` table under
``meeting_rules``:

    {"self_names": ["Sam Doe", "Sam"]}

``self_names`` is matched case-insensitively against transcript speaker labels,
substring allowed, so "Sam" matches "Sam Doe". Empty means MeetingNotesFlow files
the notes but skips the self-analysis and says so in its result.

``merge`` (read) is lenient and ``validate`` (write) is strict, for the same
reason as ``email_rules.py``: a bad row must never stop notes being filed, but a
typo saved through the admin API must not silently disable every analysis.
Edited at ``GET/PUT /api/admin/email/meeting-rules`` (``routes/email_admin.py``).
"""

from __future__ import annotations

from typing import Any

SETTINGS_KEY = "meeting_rules"

# Deliberately empty: the open-source default carries no name.
DEFAULT_SELF_NAMES: list[str] = []


def merge(value: dict | None) -> dict:
    """A stored (possibly partial) row merged over the defaults. Never raises."""
    v = value or {}
    raw = v.get("self_names")
    names = (
        [str(n).strip() for n in raw if isinstance(n, str) and n.strip()]
        if isinstance(raw, list)
        else []
    )
    return {"self_names": [*DEFAULT_SELF_NAMES, *names]}


def validate(value: dict | None) -> dict:
    """Strict counterpart to ``merge`` for the WRITE path. Raises ValueError."""
    v = value or {}
    raw = v.get("self_names")
    if not isinstance(raw, list):
        raise ValueError("self_names must be a list of strings")
    for n in raw:
        if not isinstance(n, str) or not n.strip():
            raise ValueError("self_names entries must be non-empty strings")
    return merge(v)


async def get_meeting_rules(pool: Any) -> dict:
    """Effective rules: DB row (settings.meeting_rules) over the empty defaults."""
    row = await pool.fetchrow("SELECT value FROM settings WHERE key = $1", SETTINGS_KEY)
    return merge(row["value"] if row and row["value"] else {})


async def save_meeting_rules(pool: Any, rules: dict) -> dict:
    """Validate then persist. Raises ValueError on bad input."""
    normalised = validate(rules)
    await pool.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ($1, $2, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()",
        SETTINGS_KEY,
        normalised,
    )
    return await get_meeting_rules(pool)


def is_self(speaker: str, self_names: list[str]) -> bool:
    """True when a transcript speaker label is the user."""
    s = (speaker or "").strip().lower()
    if not s:
        return False
    return any(n.strip().lower() in s for n in self_names if isinstance(n, str) and n.strip())
```

- [ ] **Step 4: Run the unit tests, expect PASS**

Run: `pytest tests/core/services/test_meeting_rules.py -v 2>&1 | tee logs/test-t1a.log`

- [ ] **Step 5: Write the failing route test**

`tests/core/test_meeting_rules_route.py`:

```python
"""GET/PUT /api/admin/email/meeting-rules — the validating write path for
settings.meeting_rules (the generic /api/settings editor validates nothing)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.config import Settings
from aegis.db import run_migrations
from httpx import ASGITransport, AsyncClient

_SETTINGS = {
    "database_url": "postgresql://test:test@localhost:5432/test",
    "litellm_url": "https://litellm.example.com/v1",
    "temporal_ui_url": "https://temporal.example.com",
    "admin_username": "admin",
    "admin_password": "admin",
}
AUTH = ("admin", "admin")
URL = "/api/admin/email/meeting-rules"


@pytest.fixture
def settings():
    return Settings(**_SETTINGS)


@pytest_asyncio.fixture(loop_scope="function")
async def rules_pool(db_pool):
    await run_migrations(db_pool)
    await db_pool.execute("DELETE FROM settings WHERE key='meeting_rules'")
    yield db_pool
    await db_pool.execute("DELETE FROM settings WHERE key='meeting_rules'")


@pytest_asyncio.fixture(loop_scope="function")
async def app_client(settings, rules_pool):
    app = create_app(run_lifespan=False)
    app.state.db_pool = rules_pool
    app.state.llm = AsyncMock()
    app.dependency_overrides[get_settings] = lambda: settings
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_requires_auth(app_client):
    assert (await app_client.get(URL)).status_code == 401


async def test_get_returns_empty_defaults(app_client):
    r = await app_client.get(URL, auth=AUTH)
    assert r.status_code == 200
    assert r.json() == {"self_names": []}


async def test_put_persists_and_get_reads_back(app_client):
    r = await app_client.put(URL, auth=AUTH, json={"self_names": ["Sam Doe", " Sam "]})
    assert r.status_code == 200
    assert r.json() == {"self_names": ["Sam Doe", "Sam"]}
    assert (await app_client.get(URL, auth=AUTH)).json() == {"self_names": ["Sam Doe", "Sam"]}


async def test_put_400s_on_bad_shape_instead_of_silently_dropping(app_client):
    r = await app_client.put(URL, auth=AUTH, json={"self_names": "Sam"})
    assert r.status_code == 400
    assert "self_names" in r.json()["detail"]
    # Nothing was written.
    assert (await app_client.get(URL, auth=AUTH)).json() == {"self_names": []}
```

- [ ] **Step 6: Run it, expect 404s**

Run: `pytest tests/core/test_meeting_rules_route.py -v 2>&1 | tee logs/test-t1b.log`
Expected: FAIL, status 404 (route missing). If it skips with "no Postgres reachable", start it: `docker compose up -d postgres`.

- [ ] **Step 7: Add the routes**

Append to `core/src/aegis/api/routes/email_admin.py` (after `put_triage_rules`); extend the import block at the top with `from aegis.services.meeting_rules import get_meeting_rules, save_meeting_rules`:

```python
@router.get("/meeting-rules")
async def get_meeting_rules_route(request: Request) -> dict[str, Any]:
    """`settings.meeting_rules` — who "you" are in a meeting transcript."""
    return await get_meeting_rules(request.app.state.db_pool)


@router.put("/meeting-rules")
async def put_meeting_rules_route(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """Replace the rules. 400 (not a silent drop) on a malformed self_names."""
    try:
        return await save_meeting_rules(request.app.state.db_pool, body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
```

- [ ] **Step 8: Run the route test, expect PASS**

Run: `pytest tests/core/test_meeting_rules_route.py -v 2>&1 | tee logs/test-t1b.log`

- [ ] **Step 9: Register the source types**

In `core/src/aegis/services/source_types.py`, add before the closing `}` of `SOURCE_TYPES` (after the `daylog_rollup` entry):

```python
    "meeting": SourceTypeInfo(
        "Meeting notes fetched from a note-taker's linked Google Doc, or the "
        "email body when no doc is reachable (worker activities/meeting.py, "
        "MeetingNotesFlow). The notes are the record, so they decay slowly.",
        decay_days=365,
    ),
    "meeting_review": SourceTypeInfo(
        "Per-meeting self-review — contributions, problems raised, commitments, "
        "one brevity note — written by MeetingNotesFlow from the user's own "
        "transcript lines (worker activities/meeting.py)"
    ),
```

In `tests/core/services/test_source_types.py`, add to `VERIFIED_LITERALS` after `"daylog_rollup",`:

```python
    # MeetingNotesFlow (worker activities/meeting.py, flows/meeting_notes.py)
    "meeting",
    "meeting_review",
```

- [ ] **Step 10: Run the registry test and lint**

Run: `pytest tests/core/services/test_source_types.py -v 2>&1 | tee logs/test-t1c.log && ruff check core/src/ tests/core/`
Expected: all PASS, ruff clean.

- [ ] **Step 11: Commit**

```bash
git add core/src/aegis/services/meeting_rules.py core/src/aegis/api/routes/email_admin.py core/src/aegis/services/source_types.py tests/core/services/test_meeting_rules.py tests/core/test_meeting_rules_route.py tests/core/services/test_source_types.py
git commit -m "feat(meeting): meeting_rules settings, admin route and source types"
```

---

### Task 2: Pure helpers — doc-id extraction, transcript split, speaker stats, review rendering

**Files:**
- Create: `worker/src/aegis_worker/activities/meeting.py` (helpers only in this task; the activities class comes in Task 3)
- Test: `tests/worker/activities/test_meeting_helpers.py`

**Interfaces:**
- Produces: `extract_doc_id(texts: Iterable[str]) -> str | None`; `split_notes_transcript(text: str) -> tuple[str, list[tuple[str, str]]]`; `speaker_stats(utterances: list[tuple[str, str]], self_names: list[str]) -> dict`; `self_lines(utterances, self_names, max_chars=6000) -> str`; `render_review(doc: dict, review: dict, stats: dict) -> str`.
- `speaker_stats` returns `{"speaker_count": int, "meeting_words_total": int, "speakers": {name: {"turns", "words"}}, "self": {"matched": bool, "turns": int, "words": int, "talk_share_pct": float, "words_per_turn": float, "longest_turn_words": int}}`.

- [ ] **Step 1: Write the failing tests**

`tests/worker/activities/test_meeting_helpers.py`:

```python
"""Pure helpers behind MeetingNotesFlow: link extraction, transcript split,
speaking stats, review rendering. Fixture names are invented."""

from __future__ import annotations

from aegis_worker.activities.meeting import (
    extract_doc_id,
    render_review,
    self_lines,
    speaker_stats,
    split_notes_transcript,
)

# Shaped like a Gemini "Notes by Gemini" text/plain export: notes tab first (with
# a stray "Tip:" bullet that must NOT read as a speaker), then the transcript.
GEMINI_LIKE = """﻿✍️ Quick notes
Widget Standup

Sep 1, 2026
Ada Lovelace Grace Hopper Sam Doe

Team reviewed the widget rollout.

Rollout status
* Grace reported the rollout is at 40%.
* Tip: keep the cache warm between runs.
* Sam is moving the config store to Postgres.

Suggested next steps
* Sam to move the remaining collections by Friday.

Transcript

Ada Lovelace: Morning all, let's start with the rollout.
Grace Hopper: We are at forty percent and the error rate is flat.
Sam Doe: I have the config store half migrated, the remaining collections go
this week if the parity check passes.
Ada Lovelace: Great. Anything blocking?
Sam Doe: Only the parity script, it is slow on the big collection.
Grace Hopper: I can look at that with you after this.
Ada Lovelace: Thanks both.
"""


def test_extract_doc_id_finds_the_first_docs_link_in_any_part():
    html = '<a href="https://docs.google.com/document/d/1AbC_d-9/edit?usp=x">Open</a>'
    assert extract_doc_id(["no link here", html]) == "1AbC_d-9"
    assert extract_doc_id(["nothing"]) is None
    assert extract_doc_id([]) is None


def test_split_puts_notes_before_the_speaker_run_and_keeps_all_speakers():
    notes, utt = split_notes_transcript(GEMINI_LIKE)
    assert notes.startswith("✍️ Quick notes")
    assert "Suggested next steps" in notes
    assert "Morning all" not in notes
    speakers = [s for s, _ in utt]
    assert speakers == [
        "Ada Lovelace", "Grace Hopper", "Sam Doe", "Ada Lovelace",
        "Sam Doe", "Grace Hopper", "Ada Lovelace",
    ]
    # Continuation line folded into the previous utterance.
    assert utt[2][1].endswith("if the parity check passes.")
    assert "Tip" not in speakers


def test_split_without_a_transcript_returns_everything_as_notes():
    text = "Summary\n* one thing\n* Tip: another thing\n"
    notes, utt = split_notes_transcript(text)
    assert notes == text.strip()
    assert utt == []


def test_speaker_stats_counts_words_turns_and_share():
    _, utt = split_notes_transcript(GEMINI_LIKE)
    stats = speaker_stats(utt, ["Sam Doe"])
    assert stats["speaker_count"] == 3
    total = sum(len(t.split()) for _, t in utt)
    assert stats["meeting_words_total"] == total
    me = stats["self"]
    assert me["matched"] is True
    assert me["turns"] == 2
    sam_words = sum(len(t.split()) for s, t in utt if s == "Sam Doe")
    assert me["words"] == sam_words
    assert me["talk_share_pct"] == round(100.0 * sam_words / total, 1)
    assert me["words_per_turn"] == round(sam_words / 2, 1)
    assert me["longest_turn_words"] == max(len(t.split()) for s, t in utt if s == "Sam Doe")


def test_speaker_stats_with_no_self_match_reports_unmatched_zeroes():
    _, utt = split_notes_transcript(GEMINI_LIKE)
    me = speaker_stats(utt, ["Nobody"])["self"]
    assert me == {
        "matched": False, "turns": 0, "words": 0,
        "talk_share_pct": 0.0, "words_per_turn": 0.0, "longest_turn_words": 0,
    }


def test_self_lines_keeps_only_own_lines_and_drops_oldest_over_budget():
    _, utt = split_notes_transcript(GEMINI_LIKE)
    mine = self_lines(utt, ["sam"])
    assert mine.count("\n") == 1 and "parity script" in mine and "Morning all" not in mine
    short = self_lines(utt, ["sam"], max_chars=60)
    assert short == "Only the parity script, it is slow on the big collection."


def test_render_review_lists_sections_and_stats_line():
    doc = {"title": "Widget Standup", "meeting_date": "2026-09-01T09:00:00+00:00"}
    review = {
        "contributions": ["Moved config store"], "problems_raised": [],
        "commitments": ["Finish by Friday"], "verbosity_note": "Lead with the decision.",
    }
    stats = {"speaker_count": 3, "self": {"matched": True, "talk_share_pct": 21.5,
             "turns": 2, "words_per_turn": 14.0, "longest_turn_words": 20}}
    out = render_review(doc, review, stats)
    assert out.startswith("# Meeting review: Widget Standup (2026-09-01)")
    assert "you: 21.5% of words, 2 turns, 14.0 words/turn, longest 20 words" in out
    assert "## Contributions\n- Moved config store" in out
    assert "## Problems raised\n- (none)" in out
    assert "## On brevity\nLead with the decision." in out
    # No stats line when the user was not matched.
    assert "of words" not in render_review(doc, review, {"self": {"matched": False}})
```

- [ ] **Step 2: Run, expect ImportError**

Run: `pytest tests/worker/activities/test_meeting_helpers.py -v 2>&1 | tee logs/test-t2.log`

- [ ] **Step 3: Write the helpers**

`worker/src/aegis_worker/activities/meeting.py`:

```python
"""Meeting notes — fetch the note-taker's document, measure the user's part in
it, and review it for their own eyes.

Design: docs/superpowers/specs/2026-09-02-meeting-notes-design.md. The pure
helpers here are what the tests pin; `MeetingActivities` (below) is thin glue
around Gmail, Drive, the LLM and `life.observations`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from aegis.services.meeting_rules import is_self

_DOC_ID_RE = re.compile(r"docs\.google\.com/document/d/([A-Za-z0-9_-]+)")
# `Speaker Name: words`. The label must start with a letter so a `10:30: …`
# timestamp never reads as a speaker, and may not contain a colon.
_SPEAKER_LINE_RE = re.compile(r"^([A-Za-z][^:\n]{1,59}): \S")
# The transcript starts at the first speaker line that opens a window of 5
# non-blank lines with at least 4 speaker lines in it. A lone "Tip: …" bullet
# in the notes never qualifies; a real transcript always does.
_TRANSCRIPT_WINDOW = 5
_TRANSCRIPT_MIN_HITS = 4
_SELF_LINES_CAP = 6_000


def extract_doc_id(texts: Iterable[str]) -> str | None:
    """First Google Docs id found in any of `texts` (plain or HTML bodies)."""
    for t in texts:
        m = _DOC_ID_RE.search(t or "")
        if m:
            return m.group(1)
    return None


def split_notes_transcript(text: str) -> tuple[str, list[tuple[str, str]]]:
    """(notes, [(speaker, utterance), …]).

    Notes are everything before the transcript. A non-speaker, non-blank line
    inside the transcript is a wrapped continuation of the previous utterance.
    Not keyed on a "Transcript" heading: the Gemini export mentions that word
    inside the notes tab too.
    # ponytail: longest-window heuristic; add a vendor-keyed splitter if a
    # second note-taker's layout breaks it.
    """
    lines = (text or "").splitlines()
    nonblank = [i for i, ln in enumerate(lines) if ln.strip()]
    start: int | None = None
    for k in range(len(nonblank)):
        window = nonblank[k : k + _TRANSCRIPT_WINDOW]
        if len(window) < _TRANSCRIPT_MIN_HITS:
            break
        hits = sum(1 for i in window if _SPEAKER_LINE_RE.match(lines[i]))
        if hits >= _TRANSCRIPT_MIN_HITS and _SPEAKER_LINE_RE.match(lines[window[0]]):
            start = window[0]
            break
    if start is None:
        return (text or "").strip(), []
    notes = "\n".join(lines[:start]).strip()
    utterances: list[tuple[str, str]] = []
    for ln in lines[start:]:
        if not ln.strip():
            continue
        if _SPEAKER_LINE_RE.match(ln):
            speaker, utterance = ln.split(": ", 1)
            utterances.append((speaker.strip(), utterance.strip()))
        elif utterances:
            speaker, utterance = utterances[-1]
            utterances[-1] = (speaker, f"{utterance} {ln.strip()}")
    return notes, utterances


def speaker_stats(utterances: list[tuple[str, str]], self_names: list[str]) -> dict:
    """Per-speaker turns/words plus the user's share. Deterministic, no LLM."""
    per: dict[str, dict] = {}
    for speaker, utterance in utterances:
        d = per.setdefault(speaker, {"turns": 0, "words": 0, "longest": 0})
        w = len(utterance.split())
        d["turns"] += 1
        d["words"] += w
        d["longest"] = max(d["longest"], w)
    total_words = sum(d["words"] for d in per.values())
    mine = [d for s, d in per.items() if is_self(s, self_names)]
    my_turns = sum(d["turns"] for d in mine)
    my_words = sum(d["words"] for d in mine)
    return {
        "speaker_count": len(per),
        "meeting_words_total": total_words,
        "speakers": {s: {"turns": d["turns"], "words": d["words"]} for s, d in per.items()},
        "self": {
            "matched": bool(mine),
            "turns": my_turns,
            "words": my_words,
            "talk_share_pct": round(100.0 * my_words / total_words, 1) if total_words else 0.0,
            "words_per_turn": round(my_words / my_turns, 1) if my_turns else 0.0,
            "longest_turn_words": max((d["longest"] for d in mine), default=0),
        },
    }


def self_lines(
    utterances: list[tuple[str, str]], self_names: list[str], max_chars: int = _SELF_LINES_CAP
) -> str:
    """The user's own utterances, newest kept when over budget."""
    mine = [u for s, u in utterances if is_self(s, self_names)]
    kept: list[str] = []
    total = 0
    for u in reversed(mine):
        if total + len(u) + 1 > max_chars:
            break
        kept.append(u)
        total += len(u) + 1
    return "\n".join(reversed(kept))


def render_review(doc: dict, review: dict, stats: dict) -> str:
    """Markdown filed as `source_type=meeting_review` — searchable by chat."""
    title = doc.get("title") or "Meeting"
    date = (doc.get("meeting_date") or "")[:10]
    lines = [f"# Meeting review: {title} ({date})" if date else f"# Meeting review: {title}"]
    me = (stats or {}).get("self") or {}
    if me.get("matched"):
        lines.append(
            f"Speakers: {stats.get('speaker_count', '?')} · you: {me['talk_share_pct']}% of words, "
            f"{me['turns']} turns, {me['words_per_turn']} words/turn, "
            f"longest {me['longest_turn_words']} words"
        )
    for heading, key in (
        ("Contributions", "contributions"),
        ("Problems raised", "problems_raised"),
        ("Commitments", "commitments"),
    ):
        lines.append(f"\n## {heading}")
        items = review.get(key) or []
        if items:
            lines.extend(f"- {it}" for it in items)
        else:
            lines.append("- (none)")
    if review.get("verbosity_note"):
        lines.append("\n## On brevity")
        lines.append(str(review["verbosity_note"]))
    return "\n".join(lines).strip()
```

- [ ] **Step 4: Run, expect PASS; lint**

Run: `pytest tests/worker/activities/test_meeting_helpers.py -v 2>&1 | tee logs/test-t2.log && ruff check worker/src/ tests/worker/`

- [ ] **Step 5: Commit**

```bash
git add worker/src/aegis_worker/activities/meeting.py tests/worker/activities/test_meeting_helpers.py
git commit -m "feat(meeting): transcript split, speaker stats and review rendering"
```

---

### Task 3: `MeetingActivities.fetch_meeting_document`

**Files:**
- Modify: `worker/src/aegis_worker/activities/meeting.py` (append)
- Test: `tests/worker/activities/test_meeting_fetch.py`

**Interfaces:**
- Consumes: `extract_doc_id`, `split_notes_transcript` (Task 2); `_build_gmail_service(creds_file, token_path)` and `_extract_text_from_part(part)` from `aegis_worker.activities.gmail`; `aegis.services.drive._build_drive_service(token_path)`.
- Produces: `MeetingActivities` dataclass (`gmail_credentials_file: str, gmail_token_dir: str, db_pool=None, llm_client=None, model_balanced="gemma4:e2b", agent_id="sebas"`) with activity `fetch_meeting_document(account_label: str, msg: dict) -> dict` returning `{title, meeting_date, doc_id, doc_url, doc_modified_time, notes, transcript: list[[speaker, text]], speakers: list[str], doc_status}` where `doc_status ∈ {ok, no_link, inaccessible, no_drive_scope, fetch_failed}`. Module-level seams for tests: `_export_doc(token_path, doc_id) -> (name, modified_time, text)` and `_token_has_drive_scope(token_path) -> bool`.

- [ ] **Step 1: Write the failing tests**

`tests/worker/activities/test_meeting_fetch.py`:

```python
"""fetch_meeting_document — Gmail body → Docs link → Drive export, with every
failure mapped to a doc_status and a body-only fallback."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest
from aegis_worker.activities import meeting as m
from aegis_worker.activities.meeting import MeetingActivities

pytestmark = pytest.mark.asyncio

DOC_ID = "1AbC_d-9xyz"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
DOC_TEXT = (
    "Notes\n* one\n* two\n\n"
    "A Person: hello there\nB Person: hi\nA Person: how are things\nB Person: fine\n"
)
MSG = {
    "id": "gm-1",
    "subject": "Notes: “Widget Standup” Sep 1, 2026",
    "snippet": "These notes have been sent",
    "internal_date_ms": 1_788_000_000_000,
}


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def _payload(plain: str, html: str | None) -> dict:
    parts = [{"mimeType": "text/plain", "body": {"data": _b64(plain)}}]
    if html is not None:
        parts.append({"mimeType": "text/html", "body": {"data": _b64(html)}})
    return {"mimeType": "multipart/alternative", "parts": parts}


class _FakeGmail:
    def __init__(self, payload: dict):
        self._payload = payload

    def users(self):
        return self

    def messages(self):
        return self

    def get(self, **_kw):
        return self

    def execute(self):
        return {"payload": self._payload}


class _Http(Exception):
    def __init__(self, status: int, content: bytes = b""):
        self.resp = SimpleNamespace(status=status)
        self.content = content


@pytest.fixture
def token(tmp_path):
    p = tmp_path / "acct.json"
    p.write_text(json.dumps({"scopes": [DRIVE_SCOPE]}))
    return p


def _act(tmp_path) -> MeetingActivities:
    return MeetingActivities(gmail_credentials_file="creds.json", gmail_token_dir=str(tmp_path))


def _wire(monkeypatch, payload, export):
    monkeypatch.setattr(m, "_build_gmail_service", lambda *_a: _FakeGmail(payload))
    monkeypatch.setattr(m, "_export_doc", export)


async def test_ok_path_exports_the_doc_and_splits_transcript(monkeypatch, tmp_path, token):
    html = f'<a href="https://docs.google.com/document/d/{DOC_ID}/edit">Open meeting notes</a>'
    seen = {}

    def export(token_path, doc_id):
        seen["args"] = (token_path, doc_id)
        return "Widget Standup – Notes by Gemini", "2026-09-01T09:06:33Z", DOC_TEXT

    _wire(monkeypatch, _payload("Open meeting notes", html), export)
    out = await _act(tmp_path).fetch_meeting_document("acct", MSG)
    assert seen["args"] == (token, DOC_ID)
    assert out["doc_status"] == "ok"
    assert out["doc_id"] == DOC_ID
    assert out["doc_url"].endswith(DOC_ID)
    assert out["title"] == "Widget Standup – Notes by Gemini"
    assert out["doc_modified_time"] == "2026-09-01T09:06:33Z"
    assert out["notes"] == "Notes\n* one\n* two"
    assert out["transcript"][0] == ["A Person", "hello there"] or out["transcript"][0] == ("A Person", "hello there")
    assert out["speakers"] == ["A Person", "B Person"]
    assert out["meeting_date"].startswith("2026-")


async def test_no_link_falls_back_to_body(monkeypatch, tmp_path, token):
    _wire(monkeypatch, _payload("Plain summary body only", None), lambda *_a: pytest.fail("must not export"))
    out = await _act(tmp_path).fetch_meeting_document("acct", MSG)
    assert out["doc_status"] == "no_link"
    assert out["doc_id"] == ""
    assert out["notes"] == "Plain summary body only"
    assert out["transcript"] == [] and out["speakers"] == []
    assert out["title"] == MSG["subject"]


async def test_token_without_drive_scope_is_reported_before_any_export(monkeypatch, tmp_path):
    (tmp_path / "acct.json").write_text(json.dumps({"scopes": ["https://www.googleapis.com/auth/gmail.modify"]}))
    html = f"https://docs.google.com/document/d/{DOC_ID}"
    _wire(monkeypatch, _payload("body", html), lambda *_a: pytest.fail("must not export"))
    out = await _act(tmp_path).fetch_meeting_document("acct", MSG)
    assert out["doc_status"] == "no_drive_scope"
    assert out["doc_id"] == DOC_ID
    assert out["notes"] == "body"


@pytest.mark.parametrize(
    "exc,status",
    [
        (_Http(404), "inaccessible"),
        (_Http(403, b'{"error": {"message": "The caller does not have permission"}}'), "inaccessible"),
        (_Http(403, b"Request had insufficient authentication scopes."), "no_drive_scope"),
        (_Http(500), "fetch_failed"),
        (RuntimeError("boom"), "fetch_failed"),
    ],
)
async def test_export_errors_map_to_doc_status_and_keep_the_body(monkeypatch, tmp_path, token, exc, status):
    def export(*_a):
        raise exc

    _wire(monkeypatch, _payload("body text", f"https://docs.google.com/document/d/{DOC_ID}"), export)
    out = await _act(tmp_path).fetch_meeting_document("acct", MSG)
    assert out["doc_status"] == status
    assert out["notes"] == "body text"


async def test_gmail_read_failure_is_fetch_failed_with_snippet(monkeypatch, tmp_path, token):
    def boom(*_a):
        raise RuntimeError("token_missing")

    monkeypatch.setattr(m, "_build_gmail_service", boom)
    out = await _act(tmp_path).fetch_meeting_document("acct", MSG)
    assert out["doc_status"] == "fetch_failed"
    assert out["notes"] == MSG["snippet"]
```

- [ ] **Step 2: Run, expect ImportError on `MeetingActivities`**

Run: `pytest tests/worker/activities/test_meeting_fetch.py -v 2>&1 | tee logs/test-t3.log`

- [ ] **Step 3: Append the activities class**

Add these imports at the top of `worker/src/aegis_worker/activities/meeting.py` (keep them sorted; ruff's isort will complain otherwise):

```python
import asyncio
import base64
import datetime as _dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from temporalio import activity

from aegis_worker.activities.gmail import _build_gmail_service, _extract_text_from_part
```

Append after `render_review`:

```python
_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"


def _token_has_drive_scope(token_path: Path) -> bool:
    """Cheap pre-check on the stored token so a missing scope is named, not
    discovered as an opaque 403 a call later."""
    try:
        scopes = json.loads(token_path.read_text()).get("scopes") or []
    except Exception:  # noqa: BLE001 — unreadable token reads as "no scope"
        return False
    return _DRIVE_SCOPE in scopes


def _export_doc(token_path: Path, doc_id: str) -> tuple[str, str, str]:
    """(name, modifiedTime, text/plain export). Blocking; run in a thread.
    Separated so tests can monkeypatch it."""
    from aegis.services.drive import _build_drive_service

    svc = _build_drive_service(token_path)
    meta = svc.files().get(fileId=doc_id, fields="name,modifiedTime").execute()
    data = svc.files().export(fileId=doc_id, mimeType="text/plain").execute()
    text = data.decode("utf-8", "ignore") if isinstance(data, bytes) else str(data)
    return meta.get("name") or "", meta.get("modifiedTime") or "", text.lstrip("﻿")


def _all_text_parts(payload: dict) -> list[str]:
    """Every decoded text/* body in the MIME tree — plain AND html, because
    Gemini puts the doc link only in the HTML part."""
    out: list[str] = []

    def walk(p: dict) -> None:
        data = (p.get("body") or {}).get("data")
        if data and str(p.get("mimeType", "")).startswith("text/"):
            try:
                out.append(base64.urlsafe_b64decode(data + "==").decode("utf-8", "replace"))
            except Exception:  # noqa: BLE001
                pass
        for sub in p.get("parts") or []:
            walk(sub)

    walk(payload or {})
    return out


def _iso_from_ms(ms: Any) -> str:
    try:
        return _dt.datetime.fromtimestamp(int(ms) / 1000, tz=_dt.UTC).isoformat()
    except (TypeError, ValueError, OSError):
        return _dt.datetime.now(tz=_dt.UTC).isoformat()


def _classify_export_error(exc: BaseException) -> str:
    status = getattr(getattr(exc, "resp", None), "status", 0)
    content = getattr(exc, "content", b"") or b""
    if isinstance(content, str):
        content = content.encode()
    if status == 403 and b"insufficient" in content.lower():
        return "no_drive_scope"
    if status in (403, 404):
        return "inaccessible"
    return "fetch_failed"


@dataclass
class MeetingActivities:
    gmail_credentials_file: str
    gmail_token_dir: str
    db_pool: Any = None
    llm_client: Any = None
    model_balanced: str = "gemma4:e2b"
    agent_id: str = "sebas"

    @activity.defn
    async def fetch_meeting_document(self, account_label: str, msg: dict) -> dict:
        """Read the email, follow its Google Docs link with the same account's
        Drive token, split notes from transcript. Never raises: every failure
        becomes a `doc_status` and the body (or snippet) still comes back."""
        token_path = Path(self.gmail_token_dir) / f"{account_label}.json"
        message_id = msg.get("id") or ""
        base = {
            "title": (msg.get("subject") or "").strip()[:200],
            "meeting_date": _iso_from_ms(msg.get("internal_date_ms")),
            "doc_id": "",
            "doc_url": "",
            "doc_modified_time": "",
            "notes": "",
            "transcript": [],
            "speakers": [],
            "doc_status": "no_link",
        }

        def _sync() -> dict:
            svc = _build_gmail_service(self.gmail_credentials_file, token_path)
            full = svc.users().messages().get(userId="me", id=message_id, format="full").execute()
            payload = full.get("payload") or {}
            parts = _all_text_parts(payload)
            body = (_extract_text_from_part(payload) or (parts[0] if parts else "")).strip()
            out = {**base, "notes": body or (msg.get("snippet") or "").strip()}
            doc_id = extract_doc_id(parts)
            if not doc_id:
                return out
            out["doc_id"] = doc_id
            out["doc_url"] = f"https://docs.google.com/document/d/{doc_id}"
            if not _token_has_drive_scope(token_path):
                out["doc_status"] = "no_drive_scope"
                return out
            try:
                name, mtime, text = _export_doc(token_path, doc_id)
            except Exception as exc:  # noqa: BLE001 — mapped, never raised
                out["doc_status"] = _classify_export_error(exc)
                return out
            notes, utterances = split_notes_transcript(text)
            out.update(
                {
                    "doc_status": "ok",
                    "title": (name or out["title"])[:200],
                    "doc_modified_time": mtime,
                    "notes": notes or text.strip(),
                    "transcript": utterances,
                    "speakers": sorted({s for s, _ in utterances}),
                }
            )
            return out

        try:
            result = await asyncio.to_thread(_sync)
        except Exception as exc:  # noqa: BLE001 — the Gmail read itself failed
            activity.logger.warning(
                "meeting_fetch_failed msg_id=%s err=%s", message_id, str(exc)[:200]
            )
            return {**base, "notes": (msg.get("snippet") or "").strip(), "doc_status": "fetch_failed"}
        if result["doc_status"] != "ok":
            activity.logger.warning(
                "meeting_doc_%s msg_id=%s account=%s",
                result["doc_status"],
                message_id,
                account_label,
            )
        return result
```

- [ ] **Step 4: Run, expect PASS; lint**

Run: `pytest tests/worker/activities/test_meeting_fetch.py -v 2>&1 | tee logs/test-t3.log && ruff check worker/src/ tests/worker/`

- [ ] **Step 5: Commit**

```bash
git add worker/src/aegis_worker/activities/meeting.py tests/worker/activities/test_meeting_fetch.py
git commit -m "feat(meeting): fetch the linked notes doc with the mailbox's Drive token"
```

---

### Task 4: `MeetingActivities.analyse_meeting`

**Files:**
- Modify: `worker/src/aegis_worker/activities/meeting.py` (append method + prompt)
- Test: `tests/worker/activities/test_meeting_analyse.py`

**Interfaces:**
- Consumes: `speaker_stats`, `self_lines`, `render_review` (Task 2); `get_meeting_rules`, `merge` (Task 1); `record_external_observation(pool, source, metric, external_id, value, observed_at, metadata)`; `LLMClient.think(prompt, model, system_prompt, max_tokens, db_pool, purpose, agent_id) -> {"response": str}`; `parse_llm_json`, `LLMTruncationError` from `aegis.llm`.
- Produces: activity `analyse_meeting(doc: dict) -> dict`. Success: `{"stats": dict, "observations": int, "self_matched": bool, "review": {contributions, problems_raised, commitments, verbosity_note}, "rendered": str}`. Skipped: `{"skipped": reason, "stats": dict, "observations": int}` with `reason ∈ {no_self_names, no_llm, too_thin, llm_failed}`. `doc` is the Task 3 result plus `message_id` and `account` (the flow adds them).

- [ ] **Step 1: Write the failing tests**

`tests/worker/activities/test_meeting_analyse.py`:

```python
"""analyse_meeting — code-computed stats + observations + one LLM review."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from aegis.llm import LLMTruncationError
from aegis_worker.activities.meeting import MeetingActivities

pytestmark = pytest.mark.asyncio

TRANSCRIPT = [
    ["Ada Lovelace", "Morning all, let's start with the rollout."],
    ["Sam Doe", "I have the config store half migrated, the rest goes this week."],
    ["Ada Lovelace", "Anything blocking?"],
    ["Sam Doe", "Only the parity script, it is slow on the big collection."],
]
DOC = {
    "title": "Widget Standup",
    "meeting_date": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
    "doc_id": "doc-analyse-1",
    "message_id": "gm-analyse-1",
    "account": "acct",
    "doc_status": "ok",
    "notes": "Rollout status\n* Grace reported 40%.\n* Sam is moving the config store." * 8,
    "transcript": TRANSCRIPT,
    "speakers": ["Ada Lovelace", "Sam Doe"],
}
REVIEW = {
    "contributions": ["Migrated half the config store"],
    "problems_raised": ["Parity script is slow"],
    "commitments": ["Finish the migration this week"],
    "verbosity_note": "Your second turn could drop the preamble.",
}


class _FakeLLM:
    def __init__(self, response=None, exc=None):
        self.response, self.exc, self.calls = response, exc, []

    async def think(self, **kw):
        self.calls.append(kw)
        if self.exc:
            raise self.exc
        return {"response": self.response}


class _RulesPool:
    """Only answers the settings read; any other query is a test failure."""

    def __init__(self, names):
        self._names = names

    async def fetchrow(self, sql, *args):
        assert "settings" in sql
        return {"value": {"self_names": self._names}}


def _act(pool, llm):
    return MeetingActivities(
        gmail_credentials_file="c", gmail_token_dir="t", db_pool=pool, llm_client=llm,
        model_balanced="balanced-model", agent_id="sebas",
    )


async def test_empty_self_names_skips_without_touching_llm():
    llm = _FakeLLM(json.dumps(REVIEW))
    out = await _act(_RulesPool([]), llm).analyse_meeting(DOC)
    assert out["skipped"] == "no_self_names"
    assert llm.calls == []


async def test_review_path_builds_prompt_from_own_lines_only():
    llm = _FakeLLM(json.dumps(REVIEW))
    out = await _act(_RulesPool(["Sam"]), llm).analyse_meeting(DOC)
    assert "skipped" not in out
    assert out["self_matched"] is True
    assert out["stats"]["self"]["turns"] == 2
    assert out["review"] == REVIEW
    assert out["rendered"].startswith("# Meeting review: Widget Standup")
    assert "Parity script is slow" in out["rendered"]
    call = llm.calls[0]
    assert call["purpose"] == "meeting_review" and call["model"] == "balanced-model"
    assert call["max_tokens"] >= 3000
    assert "parity script" in call["prompt"]
    assert "Anything blocking" not in call["prompt"]  # Ada's line never reaches the LLM
    assert "Rollout status" in call["prompt"]


async def test_llm_truncation_and_bad_json_are_skipped_not_raised():
    out = await _act(_RulesPool(["Sam"]), _FakeLLM(exc=LLMTruncationError("cut"))).analyse_meeting(DOC)
    assert out["skipped"] == "llm_failed" and out["stats"]["self"]["matched"] is True
    out = await _act(_RulesPool(["Sam"]), _FakeLLM("not json at all")).analyse_meeting(DOC)
    assert out["skipped"] == "llm_failed"


async def test_thin_body_without_own_lines_is_too_thin():
    thin = {**DOC, "notes": "short", "transcript": [], "doc_status": "no_link"}
    out = await _act(_RulesPool(["Sam"]), _FakeLLM(json.dumps(REVIEW))).analyse_meeting(thin)
    assert out["skipped"] == "too_thin"


async def test_lists_are_capped_and_coerced():
    resp = {"contributions": list(range(9)), "problems_raised": None, "commitments": "x", "verbosity_note": 5}
    out = await _act(_RulesPool(["Sam"]), _FakeLLM(json.dumps(resp))).analyse_meeting(DOC)
    assert out["review"]["contributions"] == ["0", "1", "2", "3", "4"]
    assert out["review"]["problems_raised"] == [] and out["review"]["commitments"] == []
    assert out["review"]["verbosity_note"] == "5"


@pytest_asyncio.fixture(loop_scope="function")
async def obs_pool(db_pool):
    await db_pool.execute("DELETE FROM life.observations WHERE source='meeting' AND external_id LIKE 'doc-analyse-%'")
    await db_pool.execute("DELETE FROM settings WHERE key='meeting_rules'")
    await db_pool.execute(
        "INSERT INTO settings (key, value) VALUES ('meeting_rules', $1)", {"self_names": ["Sam Doe"]}
    )
    yield db_pool
    await db_pool.execute("DELETE FROM life.observations WHERE source='meeting' AND external_id LIKE 'doc-analyse-%'")
    await db_pool.execute("DELETE FROM settings WHERE key='meeting_rules'")


async def test_observations_written_once_even_when_run_twice(obs_pool):
    act = _act(obs_pool, _FakeLLM(json.dumps(REVIEW)))
    first = await act.analyse_meeting(DOC)
    second = await act.analyse_meeting(DOC)
    assert first["observations"] == 3
    assert second["observations"] == 0  # None from record_external_observation = already there
    rows = await obs_pool.fetch(
        "SELECT metric, value::float AS value FROM life.observations "
        "WHERE source='meeting' AND external_id=$1 ORDER BY metric",
        DOC["doc_id"],
    )
    assert [r["metric"] for r in rows] == ["talk_share_pct", "turns", "words_per_turn"]
    by = {r["metric"]: r["value"] for r in rows}
    assert by["turns"] == 2.0
    assert by["talk_share_pct"] == first["stats"]["self"]["talk_share_pct"]
```

- [ ] **Step 2: Run, expect AttributeError (`analyse_meeting` missing)**

Run: `pytest tests/worker/activities/test_meeting_analyse.py -v 2>&1 | tee logs/test-t4.log`

- [ ] **Step 3: Append the method and prompt**

Add imports to `meeting.py` (sorted):

```python
from aegis.llm import LLMTruncationError, parse_llm_json
from aegis.services.meeting_rules import get_meeting_rules, is_self
from aegis.services.meeting_rules import merge as merge_meeting_rules
from aegis.services.observations import record_external_observation
```

Add module constants next to `_SELF_LINES_CAP`:

```python
_PROMPT_NOTES_CAP = 8_000
_MIN_NOTES_FOR_REVIEW = 400
_REVIEW_MAX_TOKENS = 3000  # _reasoning_floor lifts this to 4096 on kimi/qwen
_OBS_METRICS = ("talk_share_pct", "words_per_turn", "turns")
_REVIEW_SYSTEM = """\
You review ONE meeting on behalf of the person named below, for their own eyes only.
Return JSON only, no prose around it:
{"contributions": [...], "problems_raised": [...], "commitments": [...], "verbosity_note": "..."}

- contributions: what THEY added — proposals, decisions they drove, facts they supplied. Max 5, one line each.
- problems_raised: problems, risks or blockers THEY raised. Max 5.
- commitments: things THEY agreed to do, with any dates mentioned. Max 5.
- verbosity_note: one or two concrete sentences on how they could have said the same in fewer words, citing their own lines. Empty string when you were given no transcript lines.
Use only the material provided. Never invent. Empty list when nothing applies."""
```

Append inside `MeetingActivities` after `fetch_meeting_document`:

```python
    async def _load_rules(self) -> dict:
        if not self.db_pool:
            return merge_meeting_rules(None)
        try:
            return await get_meeting_rules(self.db_pool)
        except Exception as exc:  # noqa: BLE001 — a config read must not stop the flow
            activity.logger.warning("meeting_rules_read_failed err=%s", str(exc)[:200])
            return merge_meeting_rules(None)

    @activity.defn
    async def analyse_meeting(self, doc: dict) -> dict:
        """Stats in code, numbers to life.observations, one LLM review from the
        user's own lines. A skipped analysis is a normal outcome — the notes
        are already filed by the time this runs."""
        rules = await self._load_rules()
        self_names = rules["self_names"]
        if not self_names:
            return {"skipped": "no_self_names", "stats": {}, "observations": 0}

        utterances = [(str(u[0]), str(u[1])) for u in (doc.get("transcript") or []) if len(u) == 2]
        stats = speaker_stats(utterances, self_names) if utterances else {}
        matched = bool(stats and stats["self"]["matched"])
        observations = await self._record_observations(doc, stats) if matched else 0

        notes = (doc.get("notes") or "")[:_PROMPT_NOTES_CAP]
        mine = self_lines(utterances, self_names) if matched else ""
        if len(notes) < _MIN_NOTES_FOR_REVIEW and not mine:
            return {"skipped": "too_thin", "stats": stats, "observations": observations}
        if not self.llm_client:
            return {"skipped": "no_llm", "stats": stats, "observations": observations}

        me = stats.get("self") or {}
        prompt_parts = [
            f"Person: {', '.join(self_names)}",
            f"Meeting: {doc.get('title') or ''} ({(doc.get('meeting_date') or '')[:10]})",
        ]
        if matched:
            prompt_parts.append(
                f"Their speaking stats: {me['turns']} turns, {me['words']} words "
                f"({me['talk_share_pct']}% of all words), {me['words_per_turn']} words per turn, "
                f"longest turn {me['longest_turn_words']} words."
            )
            prompt_parts.append(f"Their own lines, in order:\n{mine}")
        prompt_parts.append(f"Meeting notes:\n{notes}")
        try:
            raw = await self.llm_client.think(
                prompt="\n\n".join(prompt_parts),
                model=self.model_balanced,
                system_prompt=_REVIEW_SYSTEM,
                max_tokens=_REVIEW_MAX_TOKENS,
                db_pool=self.db_pool,
                purpose="meeting_review",
                agent_id=self.agent_id,
            )
            parsed = parse_llm_json((raw.get("response") or "").strip())
            if not isinstance(parsed, dict):
                raise ValueError("unparseable meeting review")
        except LLMTruncationError as exc:
            activity.logger.warning("meeting_review_truncated: %s", str(exc)[:200])
            return {"skipped": "llm_failed", "stats": stats, "observations": observations}
        except Exception as exc:  # noqa: BLE001
            activity.logger.warning("meeting_review_llm_failed: %s", str(exc)[:200])
            return {"skipped": "llm_failed", "stats": stats, "observations": observations}

        review = {
            "contributions": _str_list(parsed.get("contributions")),
            "problems_raised": _str_list(parsed.get("problems_raised")),
            "commitments": _str_list(parsed.get("commitments")),
            "verbosity_note": str(parsed.get("verbosity_note") or "").strip()[:600],
        }
        return {
            "stats": stats,
            "observations": observations,
            "self_matched": matched,
            "review": review,
            "rendered": render_review(doc, review, stats),
        }

    async def _record_observations(self, doc: dict, stats: dict) -> int:
        """One row per metric, deduped on (source, metric, external_id).
        Returns how many rows were NEW; None from the writer means seen before."""
        ext_id = str(doc.get("doc_id") or doc.get("message_id") or "")
        if not self.db_pool or not ext_id:
            return 0
        observed_at = _parse_iso(doc.get("meeting_date"))
        written = 0
        for metric in _OBS_METRICS:
            try:
                row = await record_external_observation(
                    self.db_pool,
                    source="meeting",
                    metric=metric,
                    external_id=ext_id,
                    value=stats["self"][metric],
                    observed_at=observed_at,
                    metadata={"title": doc.get("title") or "", "speaker_count": stats["speaker_count"]},
                )
            except Exception as exc:  # noqa: BLE001
                activity.logger.warning(
                    "meeting_observation_failed metric=%s err=%s", metric, str(exc)[:200]
                )
                continue
            if row is not None:
                written += 1
        return written


def _str_list(value: Any, cap: int = 5) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()][:cap]


def _parse_iso(value: Any) -> _dt.datetime | None:
    try:
        return _dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
```

- [ ] **Step 4: Run, expect PASS; lint**

Run: `pytest tests/worker/activities/test_meeting_analyse.py -v 2>&1 | tee logs/test-t4.log && ruff check worker/src/ tests/worker/`

The DB test skips without Postgres; make sure it actually RAN (look for `PASSED`, not `SKIPPED`, on `test_observations_written_once_even_when_run_twice`).

- [ ] **Step 5: Commit**

```bash
git add worker/src/aegis_worker/activities/meeting.py tests/worker/activities/test_meeting_analyse.py
git commit -m "feat(meeting): self-review with code-computed speaking stats and observations"
```

---

### Task 5: `MeetingNotesFlow`, registry entry, worker DI

**Files:**
- Create: `worker/src/aegis_worker/flows/meeting_notes.py`
- Modify: `worker/src/aegis_worker/registry.py` (import + one `FlowSpec` at the end of `FLOWS`)
- Modify: `worker/src/aegis_worker/__main__.py` (construct `MeetingActivities` after `drive_act`; add `meeting_act` to `collect_activities(...)` after `drive_act`)
- Test: `tests/worker/flows/test_meeting_notes_flow.py`

**Interfaces:**
- Consumes: activities `fetch_meeting_document(account_label, msg)`, `analyse_meeting(doc)`, `ingest_content(item) -> {"status": "ok", "content_id": ...}` (`ContentActivities`).
- Produces: `MeetingNotesInput(agent_id: str, msg: dict, account_label: str)`; `MeetingNotesFlow` (`@workflow.defn(name="MeetingNotesFlow")`) returning `{"status": "stored" | "stored_no_analysis" | "skipped", "doc_status", "reason"?, "analysis"?, "content_id"?, "review_content_id"?, "url"}`.

- [ ] **Step 1: Write the failing flow tests**

`tests/worker/flows/test_meeting_notes_flow.py`:

```python
"""MeetingNotesFlow — fetch → file notes → analyse → file review."""

from __future__ import annotations

import pytest
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from aegis_worker.flows.meeting_notes import MeetingNotesFlow, MeetingNotesInput

_calls: dict[str, list] = {"fetch": [], "ingest": [], "analyse": []}


def _reset():
    for v in _calls.values():
        v.clear()


MSG = {"id": "gm-flow-1", "subject": "Notes: standup", "snippet": "x", "internal_date_ms": 1_788_000_000_000}
DOC_OK = {
    "title": "Standup – Notes by Gemini", "meeting_date": "2026-09-01T09:00:00+00:00",
    "doc_id": "doc-1", "doc_url": "https://docs.google.com/document/d/doc-1", "doc_modified_time": "m",
    "notes": "Notes " * 100, "transcript": [["Sam Doe", "hi"]], "speakers": ["Sam Doe"], "doc_status": "ok",
}


def _fetch(doc):
    @activity.defn(name="fetch_meeting_document")
    async def fetch(account_label: str, msg: dict) -> dict:
        _calls["fetch"].append((account_label, msg["id"]))
        return doc
    return fetch


@activity.defn(name="ingest_content")
async def ingest(item: dict) -> dict:
    _calls["ingest"].append(item)
    return {"status": "ok", "content_id": f"cid-{item['source_type']}"}


def _analyse(result):
    @activity.defn(name="analyse_meeting")
    async def analyse(doc: dict) -> dict:
        _calls["analyse"].append(doc)
        return result
    return analyse


async def _run(stubs, wf_id):
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(env.client, task_queue="tq", workflows=[MeetingNotesFlow], activities=stubs),
    ):
        return await env.client.execute_workflow(
            MeetingNotesFlow.run,
            MeetingNotesInput(agent_id="sebas", msg=MSG, account_label="acct"),
            id=wf_id,
            task_queue="tq",
        )


@pytest.mark.asyncio
async def test_ok_doc_files_notes_and_review():
    _reset()
    analysis = {"stats": {"self": {"matched": True}}, "observations": 3, "self_matched": True,
                "review": {"commitments": ["x"]}, "rendered": "# Meeting review: Standup"}
    res = await _run([_fetch(DOC_OK), ingest, _analyse(analysis)], "mn-ok")
    assert res["status"] == "stored" and res["doc_status"] == "ok"
    assert res["content_id"] == "cid-meeting" and res["review_content_id"] == "cid-meeting_review"
    assert _calls["fetch"] == [("acct", "gm-flow-1")]
    notes_item, review_item = _calls["ingest"]
    assert notes_item["url"] == "gdoc://doc-1" and notes_item["source_type"] == "meeting"
    assert notes_item["title"] == DOC_OK["title"] and notes_item["tags"] == ["meeting"]
    assert "transcript" not in notes_item["metadata"] and "hi" not in notes_item["raw_text"]
    assert notes_item["metadata"]["doc_status"] == "ok"
    assert notes_item["metadata"]["message_id"] == "gm-flow-1" and notes_item["metadata"]["account"] == "acct"
    assert notes_item["metadata"]["speakers"] == ["Sam Doe"]
    assert review_item["url"] == "aegis://meeting-review/doc-1"
    assert review_item["source_type"] == "meeting_review" and review_item["raw_text"] == "# Meeting review: Standup"
    assert review_item["metadata"]["meeting_url"] == "gdoc://doc-1"
    assert review_item["metadata"]["review"] == {"commitments": ["x"]}
    # The doc handed to analyse carries message_id + account for the observation key.
    assert _calls["analyse"][0]["message_id"] == "gm-flow-1" and _calls["analyse"][0]["account"] == "acct"


@pytest.mark.asyncio
async def test_no_link_files_body_under_the_gmail_permalink_and_skipped_analysis_files_no_review():
    _reset()
    doc = {**DOC_OK, "doc_id": "", "doc_url": "", "doc_status": "no_link", "transcript": [], "speakers": []}
    res = await _run([_fetch(doc), ingest, _analyse({"skipped": "no_self_names", "stats": {}, "observations": 0})], "mn-nolink")
    assert res["status"] == "stored_no_analysis" and res["analysis"] == "no_self_names"
    assert res["doc_status"] == "no_link"
    assert len(_calls["ingest"]) == 1
    item = _calls["ingest"][0]
    assert item["url"] == "https://mail.google.com/mail/u/0/#inbox/gm-flow-1"
    assert item["title"] == DOC_OK["title"]
    assert item["metadata"]["doc_status"] == "no_link"


@pytest.mark.asyncio
async def test_nothing_usable_is_skipped_without_filing():
    _reset()
    doc = {**DOC_OK, "doc_id": "", "doc_status": "fetch_failed", "notes": "tiny", "transcript": []}
    res = await _run([_fetch(doc), ingest, _analyse({})], "mn-thin")
    assert res == {"status": "skipped", "reason": "nothing_usable", "doc_status": "fetch_failed"}
    assert _calls["ingest"] == [] and _calls["analyse"] == []


@pytest.mark.asyncio
async def test_analysis_activity_failure_degrades_to_stored_no_analysis():
    _reset()

    @activity.defn(name="analyse_meeting")
    async def boom(doc: dict) -> dict:
        raise RuntimeError("llm down")

    res = await _run([_fetch(DOC_OK), ingest, boom], "mn-boom")
    assert res["status"] == "stored_no_analysis" and res["analysis"] == "analysis_failed"
    assert len(_calls["ingest"]) == 1


def test_registry_declares_the_flow_and_main_serves_the_activities():
    from aegis_worker.registry import FLOWS
    import aegis_worker.__main__ as main_mod  # noqa: F401 — import must not raise

    assert any(spec.flow is MeetingNotesFlow and spec.schedule_config is None for spec in FLOWS)
    from aegis_worker.activities.meeting import MeetingActivities

    assert hasattr(MeetingActivities, "fetch_meeting_document")
    assert hasattr(MeetingActivities, "analyse_meeting")
```

- [ ] **Step 2: Run, expect ImportError**

Run: `pytest tests/worker/flows/test_meeting_notes_flow.py -v 2>&1 | tee logs/test-t5.log`

- [ ] **Step 3: Write the flow**

`worker/src/aegis_worker/flows/meeting_notes.py`:

```python
"""MeetingNotesFlow — one note-taker email → notes in the knowledge store,
a self-review, and speaking metrics in life.observations.

Spawned by GmailIngestFlow (fire-and-forget, ParentClosePolicy.ABANDON) when a
triage classification carries the `meeting` tag — the tag comes from a
sender-override rule on the Email triage page, so any vendor works.

  fetch_meeting_document(account, msg)   # Gmail body → Docs link → Drive export
    → ingest_content  source_type=meeting          (notes only, never transcript)
    → analyse_meeting(doc)                          (stats in code, one LLM review)
    → ingest_content  source_type=meeting_review

Every downgrade is a normal result, never a failure: the notes are filed
before the analysis runs, and the analysis is best-effort.
"""

from __future__ import annotations

from dataclasses import dataclass

from temporalio import workflow
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.content import ContentActivities
    from aegis_worker.shared.retry import (
        NO_RETRY,
        RETRY_ONCE,
        TIMEOUT_LLM,
        TIMEOUT_LONG,
        TIMEOUT_STANDARD,
    )

# Below this many characters of notes with no fetched doc there is nothing
# worth filing (a Read.ai "sign in to view" nag is ~180 chars).
_MIN_BODY = 200
_NOTES_CAP = 16_000


@dataclass
class MeetingNotesInput:
    agent_id: str
    msg: dict
    account_label: str


@workflow.defn(name="MeetingNotesFlow")
class MeetingNotesFlow:
    @workflow.run
    async def run(self, input: MeetingNotesInput) -> dict:
        msg_id = str(input.msg.get("id") or "")
        step = "fetch_meeting_document"
        try:
            doc = await workflow.execute_activity(
                "fetch_meeting_document",
                args=[input.account_label, input.msg],
                start_to_close_timeout=TIMEOUT_LONG,
                retry_policy=RETRY_ONCE,
            )
            doc["message_id"] = msg_id
            doc["account"] = input.account_label
            doc_status = doc.get("doc_status") or "fetch_failed"
            notes = (doc.get("notes") or "")[:_NOTES_CAP]
            if doc_status != "ok" and len(notes) < _MIN_BODY:
                return {"status": "skipped", "reason": "nothing_usable", "doc_status": doc_status}

            step = "ingest_meeting"
            url = (
                f"gdoc://{doc['doc_id']}"
                if doc_status == "ok"
                else input.msg.get("permalink") or f"https://mail.google.com/mail/u/0/#inbox/{msg_id}"
            )
            ingested = await workflow.execute_activity_method(
                ContentActivities.ingest_content,
                args=[
                    {
                        "url": url,
                        "title": doc.get("title") or input.msg.get("subject") or "Meeting notes",
                        "source_type": "meeting",
                        "raw_text": notes,
                        "tags": ["meeting"],
                        "metadata": {
                            "doc_id": doc.get("doc_id") or "",
                            "doc_url": doc.get("doc_url") or "",
                            "doc_status": doc_status,
                            "message_id": msg_id,
                            "account": input.account_label,
                            "meeting_date": doc.get("meeting_date") or "",
                            "speakers": list(doc.get("speakers") or []),
                        },
                    }
                ],
                start_to_close_timeout=TIMEOUT_STANDARD,
                retry_policy=RETRY_ONCE,
            )
            status = (ingested or {}).get("status")
            if status != "ok":
                return {
                    "status": "skipped",
                    "reason": f"ingest_{status or 'no_result'}",
                    "doc_status": doc_status,
                }
            content_id = (ingested or {}).get("content_id")

            step = "analyse_meeting"
            try:
                analysis = await workflow.execute_activity(
                    "analyse_meeting",
                    args=[doc],
                    start_to_close_timeout=TIMEOUT_LLM,
                    retry_policy=NO_RETRY,
                )
            except Exception as exc:  # noqa: BLE001 — notes are filed; review is best-effort
                workflow.logger.warning("meeting_analyse_failed msg_id=%s err=%s", msg_id, str(exc)[:200])
                analysis = {"skipped": "analysis_failed"}
            if not analysis or analysis.get("skipped"):
                return {
                    "status": "stored_no_analysis",
                    "analysis": (analysis or {}).get("skipped") or "no_result",
                    "doc_status": doc_status,
                    "content_id": content_id,
                    "url": url,
                }

            step = "ingest_review"
            review_key = doc.get("doc_id") or msg_id
            review_ingested = await workflow.execute_activity_method(
                ContentActivities.ingest_content,
                args=[
                    {
                        "url": f"aegis://meeting-review/{review_key}",
                        "title": f"Meeting review: {doc.get('title') or msg_id}",
                        "source_type": "meeting_review",
                        "raw_text": analysis.get("rendered") or "",
                        "tags": ["meeting_review"],
                        "metadata": {
                            "review": analysis.get("review") or {},
                            "stats": analysis.get("stats") or {},
                            "self_matched": bool(analysis.get("self_matched")),
                            "meeting_url": url,
                            "meeting_date": doc.get("meeting_date") or "",
                            "title": doc.get("title") or "",
                            "message_id": msg_id,
                            "account": input.account_label,
                        },
                    }
                ],
                start_to_close_timeout=TIMEOUT_STANDARD,
                retry_policy=RETRY_ONCE,
            )
            return {
                "status": "stored",
                "doc_status": doc_status,
                "content_id": content_id,
                "review_content_id": (review_ingested or {}).get("content_id"),
                "observations": analysis.get("observations", 0),
                "url": url,
            }
        except ApplicationError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ApplicationError(
                f"meeting_notes_failed at step={step}: {exc!r}", non_retryable=True
            ) from exc
```

- [ ] **Step 4: Register the flow and wire the activities**

`worker/src/aegis_worker/registry.py`: add `from aegis_worker.flows.meeting_notes import MeetingNotesFlow` in the sorted import block, and append inside the `FLOWS` tuple, after `FlowSpec(MoneyProcessFlow, feature_flag="money_hygiene_enabled"),`:

```python
    # Child of GmailIngestFlow (the `meeting` tag fan-out); never scheduled.
    FlowSpec(MeetingNotesFlow),
```

`worker/src/aegis_worker/__main__.py`: import `from aegis_worker.activities.meeting import MeetingActivities` next to the `DriveActivities` import; after the `drive_act = DriveActivities(...)` block add:

```python
    meeting_act = MeetingActivities(
        gmail_credentials_file=getattr(
            settings, "gmail_credentials_file", "config/google_credentials.json"
        ),
        gmail_token_dir=getattr(settings, "gmail_token_dir", "config/"),
        db_pool=deps.pool,
        llm_client=deps.llm,
        # Tier-resolved, same reason as GmailActivities above.
        model_balanced=model_balanced,
    )
```

and add `meeting_act,` to the `collect_activities(` call directly after `drive_act,`.

- [ ] **Step 5: Run the flow tests plus the registration and registry tests; lint**

Run: `pytest tests/worker/flows/test_meeting_notes_flow.py tests/worker/test_activity_registration.py tests/worker/test_registry.py -n auto --dist loadfile --timeout=300 2>&1 | tee logs/test-t5.log && ruff check worker/src/ tests/worker/`

(If `tests/worker/test_registry.py` does not exist, run `ls tests/worker | grep -i registr` and include whatever registry test is there.)

- [ ] **Step 6: Commit**

```bash
git add worker/src/aegis_worker/flows/meeting_notes.py worker/src/aegis_worker/registry.py worker/src/aegis_worker/__main__.py tests/worker/flows/test_meeting_notes_flow.py
git commit -m "feat(meeting): MeetingNotesFlow child workflow, registered and wired"
```

---

### Task 6: Gmail fan-out on the `meeting` tag

**Files:**
- Modify: `worker/src/aegis_worker/flows/gmail_ingest.py` — import block; the tag fan-out block (search for `if tag_set & {"financial", "payments"} and finance_agent:`); the knowledge branch in `_route` (search for `if category in {"important_action", "important_read"} and classification:`).
- Test: `tests/worker/flows/test_gmail_meeting_fanout.py`

**Interfaces:**
- Consumes: `MeetingNotesFlow`, `MeetingNotesInput` (Task 5).

- [ ] **Step 1: Write the failing test pair**

`tests/worker/flows/test_gmail_meeting_fanout.py`. The stubs cover every activity `GmailIngestFlow` calls on the normal path; read `flows/gmail_ingest.py` once before running so the return shapes match (they are listed here from the flow as of this plan).

```python
"""GmailIngestFlow — a `meeting`-tagged classification starts MeetingNotesFlow
and skips the truncated email copy; an untagged one does neither."""

from __future__ import annotations

import pytest
from temporalio import activity, workflow
from temporalio.client import WorkflowHandle
from temporalio.service import RPCError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.gmail import FetchEmailsInput, FetchEmailsResult
    from aegis_worker.flows.gmail_ingest import GmailIngestFlow, GmailIngestInput
    from aegis_worker.flows.meeting_notes import MeetingNotesFlow

MSG = {
    "id": "gm-fan-1", "thread_id": "t1", "sender": "notes@example.com",
    "subject": "Notes: standup", "to": "", "date": "", "snippet": "notes",
    "internal_date_ms": 1_788_000_000_000, "labels": [], "lane": "own",
}
_calls: dict[str, list] = {"ingest_email_to_kg": [], "fetch_meeting_document": []}


def _reset():
    for v in _calls.values():
        v.clear()


def _stubs(tags: list[str]):
    @activity.defn(name="list_active_channels")
    async def list_active_channels(kind: str) -> list[dict]:
        return [{"identifier": "me@example.com", "config": {"label": "acct"}}]

    @activity.defn(name="resolve_agents")
    async def resolve_agents(tags_: list[str]) -> dict:
        return {"finance": None}

    @activity.defn(name="fetch_emails")
    async def fetch_emails(input: FetchEmailsInput) -> FetchEmailsResult:
        return FetchEmailsResult(messages=[MSG], latest_internal_date_ms=MSG["internal_date_ms"])

    @activity.defn(name="ingest_idempotency_claim")
    async def claim(source_type: str, external_id: str) -> bool:
        return True

    @activity.defn(name="fetch_thread")
    async def fetch_thread(account_label: str, thread_id: str, max_chars: int = 2000) -> str:
        return "Notes from the standup " * 20

    @activity.defn(name="classify_email")
    async def classify_email(msg: dict, thread_content: str = "") -> dict:
        return {"category": "important_read", "confidence": 1.0, "tags": tags,
                "reason": "", "summary": "", "lane": "own", "source": "override"}

    @activity.defn(name="record_triage_outcome")
    async def record_triage_outcome(email_id: str, predicted: str, labels: list, account_label: str = "") -> dict:
        return {"recorded": True}

    @activity.defn(name="enrich_people_from_email")
    async def enrich(msg: dict) -> dict:
        return {}

    @activity.defn(name="link_email_to_task")
    async def link(msg: dict, body: str = "") -> dict:
        return {"applied": False}

    @activity.defn(name="apply_label")
    async def apply_label(account_label: str, message_id: str, label: str) -> bool:
        return True

    @activity.defn(name="ingest_email_to_kg")
    async def ingest_email_to_kg(msg: dict, thread_content: str, classification: dict) -> dict:
        _calls["ingest_email_to_kg"].append(msg["id"])
        return {"ingested": True}

    @activity.defn(name="is_message_unread")
    async def unread(account_label: str, message_id: str) -> bool:
        return True

    @activity.defn(name="gather_email_context")
    async def ctx(subject: str, sender: str, exclude_url: str = "") -> str:
        return ""

    @activity.defn(name="update_channel_config_key")
    async def update_key(kind: str, identifier: str, key: str, value) -> None:
        return None

    @activity.defn(name="recheck_triage_outcomes")
    async def recheck(account_label: str, limit: int = 50) -> dict:
        return {}

    @activity.defn(name="send_system_event")
    async def send_event(message: str, chat_id: int = 0) -> dict:
        return {}

    @activity.defn(name="capture_to_inbox")
    async def capture(source_tag: str, external_id: str, title: str, description: str | None = None) -> str | None:
        return None

    # MeetingNotesFlow's own activities — it runs ABANDONED alongside the parent.
    @activity.defn(name="fetch_meeting_document")
    async def fetch_doc(account_label: str, msg: dict) -> dict:
        _calls["fetch_meeting_document"].append(msg["id"])
        return {"title": "x", "meeting_date": "", "doc_id": "", "doc_url": "", "doc_modified_time": "",
                "notes": "tiny", "transcript": [], "speakers": [], "doc_status": "no_link"}

    return [
        list_active_channels, resolve_agents, fetch_emails, claim, fetch_thread, classify_email,
        record_triage_outcome, enrich, link, apply_label, ingest_email_to_kg, unread, ctx,
        update_key, recheck, send_event, capture, fetch_doc,
    ]


async def _run(tags: list[str], wf_id: str):
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(env.client, task_queue="tq", workflows=[GmailIngestFlow, MeetingNotesFlow], activities=_stubs(tags)),
    ):
        result = await env.client.execute_workflow(
            GmailIngestFlow.run, GmailIngestInput(agent_id="sebas"), id=wf_id, task_queue="tq",
        )
        handle: WorkflowHandle = env.client.get_workflow_handle(f"meeting-notes-{MSG['id']}")
        try:
            desc = await handle.describe()
            child_started = desc is not None
        except RPCError:
            child_started = False
        return result, child_started


@pytest.mark.asyncio
async def test_meeting_tag_starts_child_and_skips_email_copy():
    _reset()
    result, child_started = await _run(["meeting"], "gi-meeting")
    assert result["processed"] == 1
    assert child_started is True
    assert _calls["ingest_email_to_kg"] == []


@pytest.mark.asyncio
async def test_without_meeting_tag_email_copy_is_filed_and_no_child_starts():
    _reset()
    result, child_started = await _run(["work"], "gi-plain")
    assert result["processed"] == 1
    assert child_started is False
    assert _calls["ingest_email_to_kg"] == ["gm-fan-1"]
```

- [ ] **Step 2: Run, expect the first test to FAIL (`child_started is False`, and the email copy filed)**

Run: `pytest tests/worker/flows/test_gmail_meeting_fanout.py -v 2>&1 | tee logs/test-t6.log`

If a stub's signature or return shape does not match what the flow passes (a `TypeError` from an activity), fix the STUB to match the flow, not the flow. If the flow calls an activity this list lacks, add a stub for it here. The second test must pass before the flow is touched; if it does not, the harness is wrong, not the flow.

- [ ] **Step 3: Edit the flow**

In the import block of `worker/src/aegis_worker/flows/gmail_ingest.py`, after the `money_process` import:

```python
    from aegis_worker.flows.meeting_notes import MeetingNotesFlow, MeetingNotesInput
```

In the fan-out block, directly after the `MoneyProcessFlow` `try/except` (still inside `if not input.link_only` / the per-message loop, at the same indentation as `if tag_set & {"financial", "payments"} and finance_agent:`):

```python
                # `meeting` — set by a sender-override rule on the Email triage
                # page (any note-taker vendor). The child fetches the linked doc,
                # files the full notes and reviews the user's part; it inherits
                # this run's agent. Same fire-and-forget contract as money.
                if "meeting" in tag_set:
                    try:
                        await workflow.start_child_workflow(
                            MeetingNotesFlow.run,
                            MeetingNotesInput(
                                agent_id=input.agent_id,
                                msg=msg,
                                account_label=label,
                            ),
                            id=f"meeting-notes-{msg['id']}",
                            parent_close_policy=ParentClosePolicy.ABANDON,
                        )
                    except Exception as exc:
                        workflow.logger.warning(
                            "meeting_fanout_start_failed msg=%s err=%s",
                            msg.get("id", ""),
                            str(exc)[:200],
                        )
```

In `_route`, change the knowledge-store guard:

```python
        # Important emails (action + read) land in the knowledge graph
        # so Raphael's search/ask tools can recall them later. Fire-and-
        # forget — ingest failures don't block the route's primary action.
        # A `meeting`-tagged email is filed in full by MeetingNotesFlow under
        # its own url; the 2000-char copy here would only be a truncated twin.
        is_meeting = "meeting" in {
            t for t in ((classification or {}).get("tags") or []) if isinstance(t, str)
        }
        if category in {"important_action", "important_read"} and classification and not is_meeting:
```

- [ ] **Step 4: Run, expect both PASS; lint**

Run: `pytest tests/worker/flows/test_gmail_meeting_fanout.py -v 2>&1 | tee logs/test-t6.log && ruff check worker/src/ tests/worker/`

- [ ] **Step 5: Commit**

```bash
git add worker/src/aegis_worker/flows/gmail_ingest.py tests/worker/flows/test_gmail_meeting_fanout.py
git commit -m "feat(gmail): fan out meeting-tagged mail to MeetingNotesFlow"
```

---

### Task 7: Weekly review block

**Files:**
- Modify: `worker/src/aegis_worker/activities/review.py` — new activity method on `ReviewActivities` (after `check_upcoming_key_dates`), new module function `format_meeting_week` (after `format_key_dates`)
- Modify: `worker/src/aegis_worker/flows/review.py` — import `format_meeting_week`; call the activity and append the block in `WeeklyReviewFlow.run` after the key-dates block
- Modify: `tests/worker/test_review_flows.py` — register a `gather_meeting_week` stub in the weekly-flow worker(s); add one assertion test
- Test: `tests/worker/activities/test_review_meeting_week.py`

**Interfaces:**
- Produces: `ReviewActivities.gather_meeting_week() -> {"meetings": [{"title", "talk_share_pct", "contributions", "problems_raised", "commitments", "verbosity_note"}], "talk_share_avg", "talk_share_prev", "words_per_turn_avg", "words_per_turn_prev", "missing_doc_by_account": {account: int}}`; `format_meeting_week(data: dict) -> str` (`""` when nothing to say).

- [ ] **Step 1: Write the failing formatter and activity tests**

Append to `tests/worker/test_review_flows.py` (it already imports `pytest`; add `from aegis_worker.activities.review import format_meeting_week` next to its other imports):

```python
def test_format_meeting_week_renders_block_and_is_empty_without_meetings():
    assert format_meeting_week({}) == ""
    assert format_meeting_week({"meetings": [], "missing_doc_by_account": {}}) == ""
    data = {
        "meetings": [
            {"title": "New Pipeline Standup", "talk_share_pct": 6.4,
             "contributions": ["proposed the pull-based batch pattern"],
             "problems_raised": ["parity script is slow"],
             "commitments": ["move reference collections to Postgres"],
             "verbosity_note": "Your longest turn ran 240 words; the decision landed in the first 40."},
            {"title": "1:1", "talk_share_pct": None, "contributions": [], "problems_raised": [],
             "commitments": [], "verbosity_note": ""},
        ],
        "talk_share_avg": 11.2, "talk_share_prev": 14.0,
        "words_per_turn_avg": 38.0, "words_per_turn_prev": None,
        "missing_doc_by_account": {"arshad-stpd": 2},
    }
    out = format_meeting_week(data)
    assert out.startswith("🎙 <b>Meetings this week</b> (2)")
    assert "• New Pipeline Standup — you spoke 6% · proposed the pull-based batch pattern" in out
    assert "• 1:1 — no transcript" in out
    assert "Commitments: move reference collections to Postgres" in out
    assert "Problems you raised: parity script is slow" in out
    assert "Talk share 11% (last week 14%) · 38 words per turn" in out
    assert "last week" in out.split("Talk share")[1].split("\n")[0]
    assert "On brevity: Your longest turn ran 240 words" in out
    assert "⚠ 2 meetings stored without their doc — re-authorise Drive for arshad-stpd" in out
    # Only the warning when there were no reviews at all this week.
    only_warn = format_meeting_week({"meetings": [], "missing_doc_by_account": {"a": 1}})
    assert only_warn.startswith("🎙 <b>Meetings this week</b> (0)") and "⚠ 1 meeting stored" in only_warn
```

`tests/worker/activities/test_review_meeting_week.py`:

```python
"""gather_meeting_week — SQL over meeting_review rows and meeting observations."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from aegis.services.observations import record_external_observation
from aegis_worker.activities.review import ReviewActivities

pytestmark = pytest.mark.asyncio
PREFIX = "mw-test-"


async def _content(pool, source_type, title, metadata, age_days=1):
    cid = PREFIX + uuid.uuid4().hex[:12]
    await pool.execute(
        "INSERT INTO knowledge_content (content_id, url, title, source_type, tags, metadata, ingested_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, now() - make_interval(days => $7))",
        cid, f"test://{cid}", title, source_type, [source_type], metadata, age_days,
    )


@pytest_asyncio.fixture(loop_scope="function")
async def pool(db_pool):
    async def _clean():
        await db_pool.execute("DELETE FROM knowledge_content WHERE content_id LIKE $1", PREFIX + "%")
        await db_pool.execute("DELETE FROM life.observations WHERE source='meeting' AND external_id LIKE $1", PREFIX + "%")
    await _clean()
    yield db_pool
    await _clean()


async def test_empty_week_returns_empty_shape(pool):
    out = await ReviewActivities(db_pool=pool).gather_meeting_week()
    assert out == {
        "meetings": [], "talk_share_avg": None, "talk_share_prev": None,
        "words_per_turn_avg": None, "words_per_turn_prev": None, "missing_doc_by_account": {},
    }


async def test_gathers_reviews_averages_and_missing_docs(pool):
    review = {"contributions": ["c1"], "problems_raised": ["p1"], "commitments": ["k1"], "verbosity_note": "v1"}
    await _content(pool, "meeting_review", "Standup", {"review": review, "stats": {"self": {"talk_share_pct": 12.0}}})
    await _content(pool, "meeting_review", "Old one", {"review": review, "stats": {}}, age_days=9)
    await _content(pool, "meeting", "Standup", {"doc_status": "ok", "account": "acct-a"})
    await _content(pool, "meeting", "Forwarded", {"doc_status": "no_drive_scope", "account": "acct-b"})
    await _content(pool, "meeting", "Forwarded 2", {"doc_status": "inaccessible", "account": "acct-b"})
    now = datetime.now(UTC)
    for days, share, wpt in ((1, 10.0, 30.0), (2, 14.0, 46.0), (9, 20.0, 60.0)):
        ext = f"{PREFIX}{days}"
        await record_external_observation(pool, "meeting", "talk_share_pct", ext, share, now - timedelta(days=days))
        await record_external_observation(pool, "meeting", "words_per_turn", ext, wpt, now - timedelta(days=days))

    out = await ReviewActivities(db_pool=pool).gather_meeting_week()
    assert [m["title"] for m in out["meetings"]] == ["Standup"]
    m = out["meetings"][0]
    assert m["talk_share_pct"] == 12.0 and m["contributions"] == ["c1"]
    assert m["problems_raised"] == ["p1"] and m["commitments"] == ["k1"] and m["verbosity_note"] == "v1"
    assert out["talk_share_avg"] == 12.0 and out["talk_share_prev"] == 20.0
    assert out["words_per_turn_avg"] == 38.0 and out["words_per_turn_prev"] == 60.0
    assert out["missing_doc_by_account"] == {"acct-b": 2}
```

- [ ] **Step 2: Run, expect ImportError / AttributeError**

Run: `pytest tests/worker/test_review_flows.py::test_format_meeting_week_renders_block_and_is_empty_without_meetings tests/worker/activities/test_review_meeting_week.py -v 2>&1 | tee logs/test-t7.log`

- [ ] **Step 3: Add the activity and the formatter**

In `worker/src/aegis_worker/activities/review.py`, after `check_upcoming_key_dates` inside `ReviewActivities`:

```python
    @activity.defn
    async def gather_meeting_week(self) -> dict:
        """This week's meeting self-reviews, talk-share/words-per-turn averages
        against the previous 7 days, and meetings filed without their doc.
        SQL only — the per-meeting LLM review already happened in
        MeetingNotesFlow; the weekly block is aggregation, not another call."""
        empty = {
            "meetings": [],
            "talk_share_avg": None,
            "talk_share_prev": None,
            "words_per_turn_avg": None,
            "words_per_turn_prev": None,
            "missing_doc_by_account": {},
        }
        if self.db_pool is None:
            return empty
        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT title, metadata FROM knowledge_content "
                "WHERE source_type='meeting_review' "
                "AND ingested_at >= now() - interval '7 days' "
                "ORDER BY ingested_at DESC LIMIT 20"
            )
            meetings = []
            for r in rows:
                md = _decode_counts(r["metadata"])
                review = md.get("review") if isinstance(md.get("review"), dict) else {}
                stats = md.get("stats") if isinstance(md.get("stats"), dict) else {}
                me = stats.get("self") if isinstance(stats.get("self"), dict) else {}
                meetings.append(
                    {
                        "title": r["title"] or "",
                        "talk_share_pct": me.get("talk_share_pct"),
                        "contributions": list(review.get("contributions") or []),
                        "problems_raised": list(review.get("problems_raised") or []),
                        "commitments": list(review.get("commitments") or []),
                        "verbosity_note": str(review.get("verbosity_note") or ""),
                    }
                )

            async def _avg(metric: str, from_days: int, to_days: int):
                return await conn.fetchval(
                    "SELECT avg(value)::float FROM life.observations "
                    "WHERE source='meeting' AND metric=$1 "
                    "AND observed_at >= now() - make_interval(days => $2) "
                    "AND observed_at < now() - make_interval(days => $3)",
                    metric,
                    from_days,
                    to_days,
                )

            ts_now = await _avg("talk_share_pct", 7, 0)
            ts_prev = await _avg("talk_share_pct", 14, 7)
            wpt_now = await _avg("words_per_turn", 7, 0)
            wpt_prev = await _avg("words_per_turn", 14, 7)
            missing = await conn.fetch(
                "SELECT COALESCE(metadata->>'account', '?') AS account, count(*) AS n "
                "FROM knowledge_content "
                "WHERE source_type='meeting' "
                "AND ingested_at >= now() - interval '7 days' "
                "AND COALESCE(metadata->>'doc_status', '') <> 'ok' "
                "GROUP BY 1"
            )

        def _r(v):
            return round(float(v), 1) if v is not None else None

        return {
            "meetings": meetings,
            "talk_share_avg": _r(ts_now),
            "talk_share_prev": _r(ts_prev),
            "words_per_turn_avg": _r(wpt_now),
            "words_per_turn_prev": _r(wpt_prev),
            "missing_doc_by_account": {r["account"]: int(r["n"]) for r in missing},
        }
```

After `format_key_dates` (module level):

```python
def format_meeting_week(data: dict) -> str:
    """Weekly-review meetings block, or "" when there is nothing to say.
    Deterministic — safe to call inside the workflow sandbox."""
    data = data or {}
    meetings = data.get("meetings") or []
    missing = data.get("missing_doc_by_account") or {}
    if not meetings and not missing:
        return ""
    lines = [f"🎙 <b>Meetings this week</b> ({len(meetings)})"]
    for m in meetings[:8]:
        share = m.get("talk_share_pct")
        spoke = f"you spoke {share:.0f}%" if share is not None else "no transcript"
        top = (m.get("contributions") or [""])[0]
        tail = f" · {_clip(top, 70)}" if top else ""
        lines.append(f"  • {_clip(m.get('title'), 48)} — {spoke}{tail}")
    commitments = [c for m in meetings for c in (m.get("commitments") or [])][:5]
    if commitments:
        lines.append("  Commitments: " + " · ".join(_clip(c, 60) for c in commitments))
    problems = [p for m in meetings for p in (m.get("problems_raised") or [])][:3]
    if problems:
        lines.append("  Problems you raised: " + " · ".join(_clip(p, 60) for p in problems))
    ts, ts_prev = data.get("talk_share_avg"), data.get("talk_share_prev")
    wpt, wpt_prev = data.get("words_per_turn_avg"), data.get("words_per_turn_prev")
    if ts is not None or wpt is not None:
        parts = []
        if ts is not None:
            parts.append(f"Talk share {ts:.0f}%" + (f" (last week {ts_prev:.0f}%)" if ts_prev is not None else ""))
        if wpt is not None:
            parts.append(f"{wpt:.0f} words per turn" + (f" (last week {wpt_prev:.0f})" if wpt_prev is not None else ""))
        lines.append("  " + " · ".join(parts))
    note = next((m["verbosity_note"] for m in meetings if m.get("verbosity_note")), "")
    if note:
        lines.append(f"  On brevity: {_clip(note, 240)}")
    for account, n in sorted(missing.items()):
        plural = "s" if n != 1 else ""
        lines.append(
            f"  ⚠ {n} meeting{plural} stored without their doc — re-authorise Drive for {account}"
        )
    return "\n".join(lines)
```

- [ ] **Step 4: Run those two tests, expect PASS**

Run: `pytest tests/worker/test_review_flows.py::test_format_meeting_week_renders_block_and_is_empty_without_meetings tests/worker/activities/test_review_meeting_week.py -v 2>&1 | tee logs/test-t7.log`

- [ ] **Step 5: Write the failing flow assertion**

In `tests/worker/test_review_flows.py`, find the weekly-flow test(s): they build a `Worker(... workflows=[WeeklyReviewFlow, InteractionFlow], activities=[...])` with stubs for `gather_weekly_state`, `frame_review`, `check_upcoming_key_dates`, `send_message`, `log_review_digest`. Add a module-level stub and register it in EVERY weekly worker's `activities=[...]` list:

```python
_meeting_week_payload: dict = {}


@activity.defn(name="gather_meeting_week")
async def stub_gather_meeting_week() -> dict:
    return dict(_meeting_week_payload)
```

Then add a test, modelled on the existing weekly test that captures `send_message` (copy its worker setup; the stub for `send_message` in that test records the text — reuse that recorder):

```python
@pytest.mark.asyncio
async def test_weekly_review_appends_meeting_block_when_present():
    _meeting_week_payload.clear()
    _meeting_week_payload.update(
        {"meetings": [{"title": "Standup", "talk_share_pct": 9.0, "contributions": ["c"],
                       "problems_raised": [], "commitments": [], "verbosity_note": ""}],
         "talk_share_avg": 9.0, "talk_share_prev": None,
         "words_per_turn_avg": None, "words_per_turn_prev": None, "missing_doc_by_account": {}}
    )
    try:
        # <same worker/stub setup as the existing weekly send test; run WeeklyReviewFlow>
        # then, on the recorded outbound text:
        assert "🎙 <b>Meetings this week</b> (1)" in sent_text
        assert "• Standup — you spoke 9% · c" in sent_text
    finally:
        _meeting_week_payload.clear()
```

Replace the comment lines with the concrete setup copied from the neighbouring weekly test (same stubs, same `execute_workflow` call, same way it reads back the sent message). The block must land in the SAME message as the narrative, after the key-dates block.

- [ ] **Step 6: Run the review flow tests, expect the new one to FAIL (block absent)**

Run: `pytest tests/worker/test_review_flows.py -n auto --dist loadfile --timeout=300 2>&1 | tee logs/test-t7b.log`

- [ ] **Step 7: Edit the flow**

In `worker/src/aegis_worker/flows/review.py`, add `format_meeting_week,` to the `aegis_worker.activities.review` import list. In `WeeklyReviewFlow.run`, directly after the key-dates block (`if key_dates_block: narrative = ...`) and before `step = "send_message"`:

```python
            # Meetings block (MeetingNotesFlow's weekly digest). Same
            # best-effort contract as key dates: a broken meeting query must
            # never cost the user their weekly review.
            step = "gather_meeting_week"
            try:
                meeting_week = await workflow.execute_activity_method(
                    ReviewActivities.gather_meeting_week,
                    start_to_close_timeout=TIMEOUT_FAST,
                    retry_policy=NO_RETRY,
                )
            except Exception as exc:  # noqa: BLE001
                workflow.logger.warning("weekly_meeting_week_failed err=%s", str(exc)[:200])
                meeting_week = {}
            meeting_block = format_meeting_week(meeting_week)
            if meeting_block:
                narrative = f"{narrative}\n\n{meeting_block}"
```

- [ ] **Step 8: Run the whole review test file, expect PASS; lint**

Run: `pytest tests/worker/test_review_flows.py tests/worker/activities/test_review_meeting_week.py -n auto --dist loadfile --timeout=300 2>&1 | tee logs/test-t7c.log && ruff check worker/src/ tests/worker/`

- [ ] **Step 9: Commit**

```bash
git add worker/src/aegis_worker/activities/review.py worker/src/aegis_worker/flows/review.py tests/worker/test_review_flows.py tests/worker/activities/test_review_meeting_week.py
git commit -m "feat(review): meetings block in the weekly review"
```

---

### Task 8: Docs, full-suite gate, PR

**Files:**
- Modify: `CLAUDE.md` (one bullet under "## Domain model", after the `interactions` bullet)
- Modify: `docs/superpowers/specs/2026-09-02-meeting-notes-design.md` (status line)

- [ ] **Step 1: Add the CLAUDE.md bullet**

```markdown
- **Meeting notes ride the email triage.** A sender-override rule on the Email triage page with tag `meeting` (e.g. `gemini-notes@google.com` → `important_read` + `["meeting"]`) makes `GmailIngestFlow` spawn `MeetingNotesFlow` per email, which follows the Google Docs link with that mailbox's Drive token (`drive.readonly` — re-authorise once via Admin → Gmail re-auth), files the notes as `source_type=meeting` (never the transcript), computes the user's talk share in code, files an LLM self-review as `meeting_review`, and writes `talk_share_pct`/`words_per_turn`/`turns` to `life.observations` (source `meeting`, external id = doc id). Who "you" are is `settings.meeting_rules.self_names` (`GET/PUT /api/admin/email/meeting-rules`); empty ⇒ notes only, review skipped with `analysis=no_self_names`. The Sunday `WeeklyReviewFlow` appends a meetings block from `ReviewActivities.gather_meeting_week`. Every doc failure is a `doc_status` (`no_link`/`inaccessible`/`no_drive_scope`/`fetch_failed`) on the stored row, and the weekly block names the mailbox to re-authorise.
```

Change the spec's `**Status:**` line to `implemented 2026-09-02 (PR pending)`.

- [ ] **Step 2: Full per-package gate, exactly as CI**

```bash
mkdir -p logs
pytest tests/core/ -n auto --dist loadfile --timeout=300 2>&1 | tee logs/test-core-full.log | tail -5
pytest tests/worker/ -n auto --dist loadfile --timeout=300 2>&1 | tee logs/test-worker-full.log | tail -5
ruff check core/src/ tests/core/ && ruff check worker/src/ tests/worker/ && ruff check comms/src/ tests/comms/
```

Expected: 0 failures in both (skips for optional services are fine), ruff clean. If a pre-existing test fails identically on `main` (check with `git stash` is forbidden — instead run the same file in the main checkout's venv from `/home/arshad/Workspace/hikmah/aegis` read-only), note it in the PR body; otherwise fix it.

- [ ] **Step 3: Commit and push, open the PR**

```bash
git add CLAUDE.md docs/superpowers/specs/2026-09-02-meeting-notes-design.md
git commit -m "docs(meeting): document the meeting-notes lane"
git push -u origin worktree-meeting-notes
gh pr create --title "feat(meeting): meeting notes ingest, self-review and weekly digest" --body-file - <<'EOF'
Implements docs/superpowers/specs/2026-09-02-meeting-notes-design.md.

- `meeting` sender-override tag → `MeetingNotesFlow` child from `GmailIngestFlow` (mirrors the money fan-out); the 2000-char `ingest_email_to_kg` copy is skipped for tagged mail.
- `fetch_meeting_document`: Gmail body → Google Docs link → Drive export with the mailbox's own token; `doc_status ∈ {ok, no_link, inaccessible, no_drive_scope, fetch_failed}`, body fallback on every failure.
- Notes filed as `source_type=meeting` (never the transcript); `analyse_meeting` computes talk share / words per turn / turns in code, writes them to `life.observations` (source `meeting`), and asks the LLM for contributions, problems raised, commitments and one brevity note using only the user's own lines → `source_type=meeting_review`.
- `settings.meeting_rules.self_names` with a validating `GET/PUT /api/admin/email/meeting-rules`.
- `WeeklyReviewFlow` appends a deterministic meetings block (per-meeting share + top contribution, commitments, week-over-week talk share and words per turn, one brevity note, missing-doc warning per mailbox).

Rollout (this deployment): add the `gemini-notes@google.com` → `important_read` + `["meeting"]` override; PUT `self_names`; release; trigger `gmail-ingest-hourly`; backfill past notes with `temporal workflow start MeetingNotesFlow` per historical message id (see spec §Rollout).

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01SvSrjASU7Fuicq1ejRVQE4
EOF
```

---

## Self-review against the spec

- Trigger via `meeting` tag + child spawn + skipped email copy → Task 6.
- `fetch_meeting_document` with all five `doc_status` values, HTML-part link, scope pre-check, body fallback → Task 3.
- Notes filed as `meeting`, transcript excluded, url `gdoc://` vs permalink, metadata fields → Task 5.
- Stats in code, observations deduped on doc id, LLM prompt from own lines + notes, caps, truncation handling, `meeting_review` filing → Tasks 2, 4, 5.
- `settings.meeting_rules` with strict write path → Task 1 (the spec's "generic settings editor" note is superseded by the dedicated route, which is what actually delivers the 400-on-typo promise).
- Weekly block after key dates, deterministic, missing-doc warning → Task 7.
- Source-type registry + literal test → Task 1.
- No migrations, no env vars, no seed rows → holds throughout.
- Deviation from the spec, deliberate: `stats` live on the `meeting_review` row rather than the `meeting` row, because the notes are filed before the analysis knows the user's name. The weekly gather reads them from `meeting_review.metadata.stats`, which Task 7 does.
