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


# The real export that lost its notes. The notes tab opens with the doc's own
# title, which is speaker-shaped and name-like ("Data Foundations"), and the same
# string comes back on an "Attachments" line. Both were candidate labels, so the
# split opened on line 2 and folded every note into one pseudo-utterance. Only the
# density rule — speaker lines must dominate what follows — keeps them in the notes.
TITLED_NOTES = """✍️ Quick notes
Data Foundations: Session 4 - Seams as Contracts
Team walked through the seam contract for the ingest boundary.

Seams as contracts
* Ada framed the seam as the only place the two teams agree.
* Sam raised the migration risk on the older collections.
* Grace asked for a version field on the contract.

Suggested next steps
* Ada to write the ADR by Thursday.
* Sam to check parity on the older collections.

Attachments Data Foundations: Session 4 - Seams as Contracts
You should review Gemini's notes to make sure they are accurate.
Get tips and learn how Gemini takes notes.
Please provide feedback about using Gemini.

Transcript

Ada Lovelace: Morning all, let's start with the seams.
Sam Doe: The contract needs a version field before we migrate.
Ada Lovelace: Agreed, I will write that up as an ADR.
Sam Doe: I can review it on Thursday after the parity run.
Ada Lovelace: Thanks, that closes it.
"""


def test_a_title_case_heading_over_bullets_does_not_open_a_transcript():
    notes, utt = split_notes_transcript(TITLED_NOTES)
    assert notes.startswith("✍️ Quick notes")
    assert "Data Foundations: Session 4 - Seams as Contracts" in notes
    assert "* Ada framed the seam" in notes
    assert "* Sam to check parity" in notes
    assert "Attachments Data Foundations" in notes
    assert "Morning all" not in notes
    assert utt[0] == ("Ada Lovelace", "Morning all, let's start with the seams.")
    assert len(utt) == 5
    assert {s for s, _ in utt} == {"Ada Lovelace", "Sam Doe"}


# --- The density rule must never cost us a transcript we already detected. ---
# Filing a real transcript as `notes` is the one direction this lane forbids:
# `analyse_meeting` then reads a transcript-less doc and hands other people's
# words to the LLM as the user's own meeting notes. So when density finds no
# opening, the split falls back — first to a recurring label, then to the first
# candidate — and only a document with NO candidate at all stays wholly notes.

SPARSE_WRAP = """Notes
Ada Lovelace: First utterance line.
continuation line 1
continuation line 2
continuation line 3
continuation line 4
Sam Doe: Next utterance.
"""

# Density fails at every speaker line — four wrapped continuations apiece — but
# "Ada Lovelace" speaks twice and the heading label does not.
HEADING_OVER_SPARSE_TRANSCRIPT = """Data Foundations: Session 4
* We agreed the seam is the contract.
* Sam raised the migration risk.
Ada Lovelace: Morning all, and this first line
runs on for a while
and keeps running on
and on again
and once more.
Sam Doe: The contract needs a version field
and the migration is slow
and the parity check is slower
and that is all from me
for now.
Ada Lovelace: Agreed, let's write it up
in an ADR before Thursday
so the other team can read it
ahead of the session
next week.
"""

# The same shape with nothing to break the tie: no label recurs.
NO_RECURRING_LABEL = """✍️ Quick notes
Data Foundations: Session 4
* We agreed the seam is the contract.
* Sam raised the migration risk.
Ada Lovelace: Morning all, and this first line
runs on for a while
and keeps running on
and on again
and once more.
Sam Doe: The contract needs a version field
and the migration is slow
and the parity check is slower
and that is all from me
for now.
"""


def test_a_transcript_too_sparse_for_the_density_window_still_opens():
    """Four wrapped continuation lines put density at 2 of 6, and the second
    speaker sits at the end of the doc where the window is too short to pass
    either. Without a fallback the whole transcript is filed as notes."""
    notes, utt = split_notes_transcript(SPARSE_WRAP)
    assert notes == "Notes"
    assert utt == [
        (
            "Ada Lovelace",
            "First utterance line. continuation line 1 continuation line 2"
            " continuation line 3 continuation line 4",
        ),
        ("Sam Doe", "Next utterance."),
    ]


def test_a_recurring_speaker_beats_a_one_off_heading_when_density_fails():
    notes, utt = split_notes_transcript(HEADING_OVER_SPARSE_TRANSCRIPT)
    assert notes == (
        "Data Foundations: Session 4\n"
        "* We agreed the seam is the contract.\n"
        "* Sam raised the migration risk."
    )
    assert [s for s, _ in utt] == ["Ada Lovelace", "Sam Doe", "Ada Lovelace"]
    assert utt[0][1].endswith("and once more.")
    for _, u in utt:
        assert u not in notes


def test_with_no_recurring_label_the_split_sacrifices_notes_never_the_transcript():
    """The documented worst case, pinned so the failure DIRECTION is visible.

    Density fails everywhere and no label recurs, so the split opens on the first
    candidate — here the notes heading — and the headline and bullets are lost
    into the transcript. That is the price: notes are sacrificed, a transcript is
    never left sitting in `notes` where analyse_meeting would treat other
    people's words as the user's own."""
    notes, utt = split_notes_transcript(NO_RECURRING_LABEL)
    assert notes == "✍️ Quick notes"
    assert utt[0][0] == "Data Foundations"
    assert "Ada Lovelace" in {s for s, _ in utt}
    assert "Sam Doe" in {s for s, _ in utt}
    for _, u in utt:
        assert u not in notes


# --- A heading the note-taker reprints INSIDE the transcript is not a speaker. ---
# Gemini reprints the doc's own "Title: Subtitle" line in the transcript tab. That
# label is a candidate — it recurs, and it is name-like — so it used to open a
# one-line utterance by a speaker who was never in the room: `speakers` grew by
# one and the title words counted toward `meeting_words_total`.

REPRINTED_TITLE = """✍️ Quick notes
Data Foundations: Session 4 - Seams as Contracts
Team walked through the seam contract for the ingest boundary.

Seams as contracts
* Ada framed the seam as the only place the two teams agree.
* Sam raised the migration risk on the older collections.

Attachments Data Foundations: Session 4 files
You should review Gemini's notes to make sure they are accurate.
Get tips and learn how Gemini takes notes.
Please provide feedback about using Gemini.

Transcript

Ada Lovelace: Morning all, let's start with the seams.
Sam Doe: The contract needs a version field before we migrate.
Data Foundations: Session 4 - Seams as Contracts
Ada Lovelace: Agreed, I will write that up as an ADR.
Sam Doe: I can review it on Thursday after the parity run.
Ada Lovelace: Thanks, that closes it.
"""

# One correction from a person who speaks once and appears nowhere in the notes.
# The rule cannot be "the label recurs", or he is dropped with the headings.
ONE_LINE_SPEAKER = """✍️ Quick notes
Data Foundations: Session 4 - Seams as Contracts
Team walked through the seam contract for the ingest boundary.

Seams as contracts
* Ada framed the seam as the only place the two teams agree.
* Sam raised the migration risk on the older collections.

Transcript

Ada Lovelace: Morning all, let's start with the seams.
Sam Doe: The contract needs a version field before we migrate.
Oliver Cooper: One correction, the older collections are already on v2.
Ada Lovelace: Good, that closes it.
"""


def test_a_notes_heading_repeated_inside_the_transcript_is_not_a_speaker():
    notes, utt = split_notes_transcript(REPRINTED_TITLE)
    # Notes are untouched — the start selection already got this one right.
    assert notes.startswith("✍️ Quick notes")
    assert "Data Foundations: Session 4 - Seams as Contracts" in notes
    assert "* Ada framed the seam" in notes
    assert "* Sam raised the migration risk" in notes
    assert "Attachments Data Foundations" in notes
    assert "Morning all" not in notes
    # The reprinted title is dropped whole. It is not a speaker, and it is not
    # folded into the utterance above it either: those words are not speech.
    assert {s for s, _ in utt} == {"Ada Lovelace", "Sam Doe"}
    assert len(utt) == 5
    for _, u in utt:
        assert "Seams as Contracts" not in u
    assert utt[1] == ("Sam Doe", "The contract needs a version field before we migrate.")


def test_a_speaker_with_one_line_who_is_not_in_the_notes_is_kept():
    notes, utt = split_notes_transcript(ONE_LINE_SPEAKER)
    assert "* Ada framed the seam" in notes
    assert "Morning all" not in notes
    assert ("Oliver Cooper", "One correction, the older collections are already on v2.") in utt
    assert [s for s, _ in utt] == ["Ada Lovelace", "Sam Doe", "Oliver Cooper", "Ada Lovelace"]


# --- The line at `start` gets the same speaker test as every other line. ---
# Dropping a line is deleting it: speech that is neither notes nor an utterance
# is gone with no trace. So the rule is bounded by EVIDENCE, not by position —
# a label absent from the notes is a speaker even on one turn, and a label that
# heads a notes line is a heading even when it opens the transcript.

# "Ada Lovelace" heads a line in the notes AND speaks only once, so the speaker
# test drops her opening turn — and its two wrapped lines after it, since there
# is no utterance yet for them to fold into.
SPEAKS_ONCE_AT_THE_START = """✍️ Quick notes
Ada Lovelace: raised the seam contract ahead of the session.
* Sam raised the migration risk on the older collections.
* Grace asked for a version field on the contract.
* Ada agreed to write the ADR by Thursday.

Suggested next steps
* Sam to check parity on the older collections.

Transcript

Ada Lovelace: This is my one and only turn
and it runs on to a second line
and then a third.
Sam Doe: The contract needs a version field.
Sam Doe: And the parity run lands on Thursday.
"""

# The same shape, minus the notes `Ada Lovelace:` line. Nothing in the notes
# claims that label, so her sole turn is speech and survives with its wrapping.
OPENS_AND_SPEAKS_ONCE_NOT_IN_NOTES = """✍️ Quick notes
* Ada raised the seam contract ahead of the session.
* Sam raised the migration risk on the older collections.
* Grace asked for a version field on the contract.
* Ada agreed to write the ADR by Thursday.

Suggested next steps
* Sam to check parity on the older collections.

Transcript

Ada Lovelace: This is my one and only turn
and it runs on to a second line
and then a third.
Sam Doe: The contract needs a version field.
Sam Doe: And the parity run lands on Thursday.
"""

# The live export. The notes tab carries the doc title twice and closes with a
# "Transcript" line; the transcript tab then opens by reprinting that title with
# " - Transcript" appended. Density fails at both notes copies (bullets and prose
# follow them) and passes on the reprint, so the reprint IS the line at `start`.
REPRINTED_TITLE_AT_THE_START = """✍️ Quick notes
Data Foundations: Session 4 - Seams as Contracts
Team walked through the seam contract for the ingest boundary.

Seams as contracts
* Ada framed the seam as the only place the two teams agree.
* Sam raised the migration risk on the older collections.

Suggested next steps
Data Foundations: Session 4 - Seams as Contracts
* Sam to check parity on the older collections.
* Ada to write the ADR by Thursday.
You should review Gemini's notes to make sure they are accurate.
Get tips and learn how Gemini takes notes.

📖 Transcript

Data Foundations: Session 4 - Seams as Contracts - Transcript
Ada Lovelace: Morning all, let's start with the seams.
Sam Doe: The contract needs a version field before we migrate.
Ada Lovelace: Agreed, I will write that up as an ADR.
Sam Doe: I can review it on Thursday after the parity run.
Ada Lovelace: Thanks, that closes it.
"""

# The reprinted title wraps onto a second line. Folding that line into the
# utterance above puts heading text in Sam Doe's mouth.
REPRINTED_TITLE_WRAPPED = """✍️ Quick notes
Data Foundations: Session 4 - Seams as Contracts
Team walked through the seam contract for the ingest boundary.

Seams as contracts
* Ada framed the seam as the only place the two teams agree.
* Sam raised the migration risk on the older collections.

Attachments Data Foundations: Session 4 files
You should review Gemini's notes to make sure they are accurate.
Get tips and learn how Gemini takes notes.
Please provide feedback about using Gemini.

Transcript

Ada Lovelace: Morning all, let's start with the seams.
Sam Doe: The contract needs a version field before we migrate.
Data Foundations: Session 4 - Seams as Contracts
a recording of this session is attached to the invite
Ada Lovelace: Agreed, I will write that up as an ADR.
Sam Doe: I can review it on Thursday after the parity run.
Ada Lovelace: Thanks, that closes it.
"""


def test_a_lone_opening_speaker_who_also_heads_a_notes_line_loses_that_turn():
    """The documented ceiling of the speaker rule, pinned at its worst position.

    Ada heads a `Name:` line in the notes and speaks exactly once, so nothing
    below `start` tells the split she is a person rather than a heading the
    note-taker reprinted. Her turn and its two wrapped lines are deleted: they
    reach neither the notes nor an utterance.

    This inverts what the old `i == start` exemption did, and the trade is
    deliberate. Position is not evidence. Exempting the opening line cost more
    than it saved, because a note-taker that reprints the doc title as the first
    line of the transcript tab lands exactly there — and every such export then
    gained a speaker who was never in the room, in `speakers`, in
    `speaker_count` and in `meeting_words_total`. A phantom participant on every
    meeting of that shape is worse than one lost turn from a participant who
    never spoke again. The evidence that saves a real opening turn is the one
    below: a person is not also a heading in the notes.
    """
    notes, utt = split_notes_transcript(SPEAKS_ONCE_AT_THE_START)
    assert "* Sam to check parity" in notes
    # Deleted, not relocated: the turn is in neither half of the split.
    assert "one and only turn" not in notes
    assert "runs on to a second line" not in notes
    for _, u in utt:
        assert "one and only turn" not in u
        assert "runs on to a second line" not in u
        assert "and then a third" not in u
    assert [s for s, _ in utt] == ["Sam Doe", "Sam Doe"]
    assert utt[0] == ("Sam Doe", "The contract needs a version field.")


def test_a_lone_opening_speaker_absent_from_the_notes_is_kept():
    """The other side of the same evidence: no notes heading, so she is a person."""
    notes, utt = split_notes_transcript(OPENS_AND_SPEAKS_ONCE_NOT_IN_NOTES)
    assert "* Sam to check parity" in notes
    assert "one and only turn" not in notes
    # Her one turn survives, and so do the two lines it wraps onto.
    assert utt[0] == (
        "Ada Lovelace",
        "This is my one and only turn and it runs on to a second line and then a third.",
    )
    assert [s for s, _ in utt] == ["Ada Lovelace", "Sam Doe", "Sam Doe"]


def test_a_reprinted_title_at_the_transcript_opening_is_not_a_speaker():
    """The production case: the transcript opens on the doc's own title.

    The note-taker ends its notes tab with a `Transcript` line and opens the
    transcript tab by repeating the doc title with " - Transcript" glued on.
    Density selects exactly that line as `start` — the real speakers follow it —
    so an exemption keyed on position turned the title into a speaker.
    """
    notes, utt = split_notes_transcript(REPRINTED_TITLE_AT_THE_START)
    # Both notes-region title lines stay in the notes.
    assert notes.startswith("✍️ Quick notes")
    assert notes.count("Data Foundations: Session 4 - Seams as Contracts") == 2
    assert "* Ada framed the seam" in notes
    assert "Get tips and learn how Gemini takes notes." in notes
    assert "Morning all" not in notes
    # The reprinted title is dropped whole: not a speaker, not folded anywhere.
    assert {s for s, _ in utt} == {"Ada Lovelace", "Sam Doe"}
    assert len(utt) == 5  # one per real speaker line
    for _, u in utt:
        assert "Seams as Contracts" not in u
        assert "Transcript" not in u
    assert utt[0] == ("Ada Lovelace", "Morning all, let's start with the seams.")


def test_a_dropped_headings_wrapped_line_is_dropped_too_not_folded_upward():
    notes, utt = split_notes_transcript(REPRINTED_TITLE_WRAPPED)
    assert "Attachments Data Foundations" in notes
    assert "Morning all" not in notes
    assert {s for s, _ in utt} == {"Ada Lovelace", "Sam Doe"}
    assert len(utt) == 5
    for _, u in utt:
        assert "Seams as Contracts" not in u
        assert "a recording of this session" not in u
    # The speaker before the heading keeps exactly what they said.
    assert utt[1] == ("Sam Doe", "The contract needs a version field before we migrate.")
