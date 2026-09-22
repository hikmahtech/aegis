"""The journal gap prompt (vault record spec §3): whether a day's journal note
holds the user's own words, and what happens to the answer when it does not.

A day counts as written when its notes hold at least `min_words` words the
user wrote. What is not the user's: every block AEGIS filed (each
`%% aegis:<key> %%` bullet and its outline), the frontmatter, headings, empty
bullets, `---` rules, checkbox lines, and the literal text of the vault's own
day template. A template placeholder (`{{date}}`, a Templater tag) matches
whatever was rendered or typed in its place, and what it holds counts: a
rendered date has no letters, so it counts nothing, while what the user typed
after a prompt does.

The answer is the user's own words. It is filed as written (one bullet per
line typed), the stored copy is blanked once the note holds it (nothing
prunes `interactions`), and nothing logs it — a log line carries the word
count at most.
"""

from __future__ import annotations

import re
from typing import Any

from aegis.services import notes

ORIGIN = "journal_prompt"
SLOT = "selfreport"
DEFAULT_MIN_WORDS = 5
# Gone before the next day's card at the same hour.
DEFAULT_TIMEOUT_S = 22 * 3600
# `{day}` is the day's name, rendered with DAY_NAME_FORMAT on the vault
# layout's calendar.
DEFAULT_PROMPT = (
    "Nothing in your journal for {day}. What happened that day? A few lines are "
    "enough: they go into that day's note as you write them. Ignore this if you "
    "already wrote the day on your phone."
)
DEFAULT_LABEL = "Your day"
DAY_NAME_FORMAT = "dddd D MMMM"

_MARKER_RE = re.compile(r"%% aegis:([A-Za-z0-9:_.\-]{1,160}) %%")
_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\n.*?\n---[ \t]*(?:\n|\Z)", re.S)
_HEADING_RE = re.compile(r"^#{1,6} ")
_CHECKBOX_RE = re.compile(r"^[-*+] \[.\]")
_PLACEHOLDER_RE = re.compile(r"\{\{.*?\}\}")
_EMPTY_LINES = ("-", "*", "+", "---")


def _words(text: str) -> int:
    """Whitespace-separated tokens with a letter in them: a bare date, a time
    or a `-` is not a word."""
    return sum(1 for w in text.split() if any(c.isalpha() for c in w))


def _template_patterns(template: str) -> list[re.Pattern]:
    """One pattern per line of the day template: its literal text, with a
    group wherever a placeholder is. A Templater tag, even one spanning
    lines, is a placeholder too. A line that is only placeholders is left
    out: it would match anything and take nothing away."""
    body = notes._TEMPLATER_RE.sub("{{}}", template or "")
    body = _FRONTMATTER_RE.sub("", body, count=1)
    patterns = []
    for line in body.splitlines():
        parts = [re.escape(p.strip()) for p in _PLACEHOLDER_RE.split(line.strip())]
        if any(parts):
            patterns.append(re.compile("(.*)".join(parts)))
    return patterns


def own_words(text: str, template: str = "", indent_width: int = 2) -> int:
    """How many words of one journal note the user wrote (see the module
    doc). `indent_width` is the layout's, so a block written with four-space
    indents is found whole."""
    text = _FRONTMATTER_RE.sub("", text or "", count=1)
    while (m := _MARKER_RE.search(text)) is not None:
        _block, rest = notes.split_section(text, m.group(1), indent_width=indent_width)
        if rest == text:  # a marker that is not on a bullet: drop its line
            start = text.rfind("\n", 0, m.start()) + 1
            end = text.find("\n", m.end())
            rest = text[:start] + ("" if end < 0 else text[end + 1 :])
        text = rest
    patterns = _template_patterns(template)
    words = 0
    for line in text.splitlines():
        s = line.strip()
        if not s or s in _EMPTY_LINES or _HEADING_RE.match(s) or _CHECKBOX_RE.match(s):
            continue
        held = [" ".join(m.groups()) for p in patterns if (m := p.fullmatch(s))]
        words += min((_words(h) for h in held), default=_words(s))
    return words


def gap_verdict(
    texts: list[str],
    template: str,
    day: str,
    min_words: int = DEFAULT_MIN_WORDS,
    indent_width: int = 2,
) -> tuple[str, int]:
    """`(verdict, words)` for one day's journal notes (the filed one and the
    live one, as `notes.journal_paths` lists them):

    answered   the day's self-report is already filed
    wrote      the user wrote at least `min_words` words
    encrypted  fewer words, but a note holds an encrypted block: the user
               wrote something and hid it
    gap        none of these; the day is worth asking about
    """
    if any(notes.marker(notes.journal_key("daily", day, SLOT)) in t for t in texts):
        return "answered", 0
    words = sum(own_words(t, template, indent_width) for t in texts)
    if words >= max(1, min_words):
        return "wrote", words
    if any(notes.ENCRYPTED_PLACEHOLDER in t for t in texts):
        return "encrypted", words
    return "gap", words


def answer_text(response: Any) -> str:
    """The user's words from an `input` card's response: `{"value": ...}`,
    the shape both the Slack text box and the admin textarea send. "" for
    anything else."""
    if not isinstance(response, dict):
        return ""
    return str(response.get("value") or "").strip()


def keep_lines(text: str) -> str:
    """`text` with every line break doubled. `notes.body_outline` joins
    consecutive plain lines into one bullet (right for a model's wrapped
    prose, wrong for a diary typed a line at a time); a blank line between
    them keeps each its own bullet. Indented and bulleted lines nest as
    before, and no character of any line changes."""
    return "\n\n".join(text.splitlines())


async def blank_answer(pool: Any, interaction_id: str, path: str) -> bool:
    """Take the user's words off the card once the vault holds them:
    `interactions.response` becomes `{"value": "", "filed": <note path>}`.
    Nothing prunes `interactions`, so without this the diary would stay in
    the database for good. Only a resolved `journal_prompt` card is touched.
    True when a row changed."""
    status = await pool.execute(
        "UPDATE interactions SET response = $2 "
        "WHERE id = $1::uuid AND origin = $3 AND status = 'resolved'",
        interaction_id,
        {"value": "", "filed": path},
        ORIGIN,
    )
    return status == "UPDATE 1"


_UNFILED_SQL = """
SELECT id::text AS id, response, metadata
  FROM interactions
 WHERE origin = $1 AND status = 'resolved'
   AND COALESCE(response->>'value', '') ~ '\\S'
   AND ($2::int <= 0 OR resolved_at > now() - make_interval(days => $2::int))
 ORDER BY resolved_at
"""


async def unfiled_answers(pool: Any, since_days: int = 0) -> list[dict]:
    """Resolved `journal_prompt` cards still holding an answer: their filing
    failed after the card resolved (`InteractionFlow` swallows a failed
    post-resolve hook). Only cards resolved in the last `since_days` days;
    0 takes them all. Oldest first."""
    rows = await pool.fetch(_UNFILED_SQL, ORIGIN, max(0, int(since_days or 0)))
    return [dict(r) for r in rows]
