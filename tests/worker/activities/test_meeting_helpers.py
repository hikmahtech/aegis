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


def test_a_lone_speaker_shaped_line_in_the_notes_does_not_open_a_transcript():
    """Pins the label rule, not just the regex.

    "Decision: ..." is speaker-shaped and, unlike the "* Tip:" bullet, is not
    saved by the leading-"*". Only the candidate-label rule keeps it in the
    notes: one word is not name-like and one occurrence does not recur. Without
    it a single such line truncates the notes and swallows the rest.
    """
    text = (
        "Quick notes\n"
        "Decision: ship the widget on Friday.\n"
        "* Owner is Sam.\n"
        "* Tip: keep the cache warm.\n"
        "More prose that follows the decision line.\n"
        "Another line of prose.\n"
    )
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


# --- Export shapes the old 5-line/4-hit window heuristic did not recognise. ---
# Each one filed its whole transcript as `notes` (C1). The label rule opens the
# transcript on the first speaker line whose label recurs or looks like a name.

WRAPPED = """Weekly sync
Team agreed to ship on Friday.

Ada Lovelace: Morning all, let's start
with the rollout status please.
Sam Doe: The config store is half migrated
and the rest lands this week.
Ada Lovelace: Great, anything blocking
the parity check right now?
"""

THREE_TURNS = """Quick sync
We covered the rollout.

A Person: Are we ready to ship?
B Person: Yes, the parity check passed.
A Person: Then let's ship it.
"""

TIMESTAMPED = """Standup notes
Rollout is at forty percent.

Ada Lovelace: Morning all, let's start.
00:01:23
Sam Doe: The config store is half migrated.
00:02:05
Ada Lovelace: Great, thanks everyone.
"""


def test_wrapped_utterances_still_open_a_transcript():
    notes, utt = split_notes_transcript(WRAPPED)
    assert notes == "Weekly sync\nTeam agreed to ship on Friday."
    assert utt == [
        ("Ada Lovelace", "Morning all, let's start with the rollout status please."),
        ("Sam Doe", "The config store is half migrated and the rest lands this week."),
        ("Ada Lovelace", "Great, anything blocking the parity check right now?"),
    ]
    for _, u in utt:
        assert u not in notes


def test_a_three_turn_meeting_is_short_but_still_a_transcript():
    notes, utt = split_notes_transcript(THREE_TURNS)
    assert notes == "Quick sync\nWe covered the rollout."
    assert utt == [
        ("A Person", "Are we ready to ship?"),
        ("B Person", "Yes, the parity check passed."),
        ("A Person", "Then let's ship it."),
    ]
    for _, u in utt:
        assert u not in notes


def test_timestamp_lines_between_utterances_are_dropped_not_folded():
    notes, utt = split_notes_transcript(TIMESTAMPED)
    assert notes == "Standup notes\nRollout is at forty percent."
    assert utt == [
        ("Ada Lovelace", "Morning all, let's start."),
        ("Sam Doe", "The config store is half migrated."),
        ("Ada Lovelace", "Great, thanks everyone."),
    ]
    assert "00:01:23" not in notes
    for _, u in utt:
        assert "00:0" not in u
        assert u not in notes


def test_a_recurring_single_word_label_does_open_a_transcript():
    """The documented cost of the label rule, pinned so the trade-off is visible.

    A single-word label is not name-like, so one `Decision:` line stays in the
    notes (the test above). Two of them recur, which is exactly the signal the
    rule uses, so they read as a transcript. The ceiling is a vendor-keyed
    splitter; until a real export trips this, the leak it prevents costs more.
    """
    text = (
        "Quick notes\n"
        "Decision: ship the widget on Friday.\n"
        "More prose in between.\n"
        "Decision: hold the cache change.\n"
        "Final line of prose.\n"
    )
    notes, utt = split_notes_transcript(text)
    assert notes == "Quick notes"
    assert utt == [
        ("Decision", "ship the widget on Friday. More prose in between."),
        ("Decision", "hold the cache change. Final line of prose."),
    ]
