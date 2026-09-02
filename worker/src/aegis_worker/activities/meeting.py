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
    inside the notes tab too. A leading BOM is stripped first: Drive's plain-text
    export starts with one and ``str.strip`` does not remove it, so without this
    the notes begin with an invisible U+FEFF.
    # ponytail: longest-window heuristic; add a vendor-keyed splitter if a
    # second note-taker's layout breaks it.
    """
    lines = (text or "").lstrip("\ufeff").splitlines()
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
