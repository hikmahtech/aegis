"""Raphael's notes: the Obsidian vault is the record (#514).

Spec: `docs/superpowers/specs/2026-09-12-raphael-notes-design.md`.

This is the ONLY module that writes the vault. Everything else — the daylog, the
research lane, the `note_write`/`note_link` chat tools through `notes_write.py`
— hands it an :class:`Append` and gets back what happened. Four rules hold here
and every caller relies on them:

* **Insert-only.** A write creates a note, or inserts ONE contiguous block
  into it: a journal entry at the end of the note's own section (`Journal`,
  `Review`, or an older note's `Month Review`, at whatever heading level the
  note uses), anything else at the end of the note. It
  never rewrites, reorders or deletes a line the user wrote, and `_apply`
  checks exactly that (`is_one_insertion`) before it writes. Each block carries
  a hidden Obsidian comment marker (`%% aegis:<key> %%`), and a write whose
  marker is already in the note is a no-op — so a re-run or a retry can
  neither duplicate a block nor change one.
* **Only where Raphael may write.** Anything under `raphael/`, and the journal
  notes the daylog owns, filed as the vault files them
  (`journal/<YYYY>/<NN. Mon>/`). A live periodic-notes note at the `journal/`
  root takes the write only when it already exists; it is never created.
  Every other path is refused before git is touched.
* **A write counts only once it is pushed.** The phone and laptop auto-commit
  through `obsidian-git`, so a push can be rejected and a rebase can conflict.
  The writer then drops its own unpushed commit, pulls fresh and re-applies the
  append once; if that fails too it drops the commit again and reports. It
  never force-pushes, and the checkout never carries a divergent commit.
* **Encrypted blocks never leave the file.** meld-encrypt keeps ciphertext
  between `%%🔐` and `🔐%%` inside ordinary notes. :func:`strip_encrypted` runs
  before anything is indexed, shown by a tool or sent to a model.

The git plumbing is the books layer (`books.py`), reused rather than copied:
the clone happens inside the flock, `.aegis.lock` lives in the checkout, and a
failed write reverts only the paths it touched.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog

from aegis.services import books

logger = structlog.get_logger()

DEPLOY_KEY_NAME = "notes_deploy_key"
RAPHAEL_DIR = "raphael"
# Folders that are configuration, templates or binaries, not notes.
SKIP_PREFIXES = (".obsidian/", "_templates/", "backups/", "_attachments/", ".trash/")
# One note is indexed whole up to this; a longer one is cut (a note that long
# is a paste, not a note).
INDEX_MAX_CHARS = 100_000
# What a chat tool may read back in one call.
READ_MAX_CHARS = 60_000

_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Raphael",
    "GIT_AUTHOR_EMAIL": "raphael@aegis.local",
    "GIT_COMMITTER_NAME": "Raphael",
    "GIT_COMMITTER_EMAIL": "raphael@aegis.local",
}
_LOCK_NAME = ".aegis.lock"

# meld-encrypt 1.6.2 (the vault's version) writes `%%🔐<ciphertext> 🔐%%`, and
# its later α/β formats keep those markers around a longer prefix
# (`%%🔐α … 🔐%%`, `%%🔐β … 🔐%%`). With "show marker in reading view" on, the
# later versions drop the `%%` and write `🔐α … 🔐` / `🔐β … 🔐` — the second
# form below. A lone 🔐 in ordinary text is not a start marker: only one
# followed by α or β is.
_ENC_START = "%%🔐"
_ENC_END = "🔐%%"
_ENC_BARE_START_RE = re.compile("🔐[αβ]")
_ENC_BARE_END = "🔐"
ENCRYPTED_PLACEHOLDER = "[encrypted block]"


class NotesError(Exception):
    """A vault operation failed; the checkout is left as it was upstream."""


class NotesDisabled(NotesError):  # noqa: N818 — a state, not an error suffix (as BooksDisabled)
    """The vault is not configured (no repo url, or no deploy key)."""


class NotesConflict(NotesError):  # noqa: N818 — a state, as NotesDisabled / BooksDisabled
    """The push was rejected or the rebase conflicted, twice. Nothing was kept."""


class NotesPathError(NotesError):
    """A path Raphael may not write."""


# ----------------------------------------------------------------- config


@dataclass(frozen=True)
class NotesConfig:
    """The vault checkout. Same attribute names as `books.BooksConfig`, which
    is what lets this module reuse the books git layer."""

    path: Path
    repo_url: str = ""
    deploy_key: Path | None = None

    @property
    def configured(self) -> bool:
        """Both halves of the integration are set. Until then nothing writes,
        indexes or reads the vault, and the daylog keeps its old behaviour."""
        return bool(self.repo_url) and self.deploy_key is not None


def config_from_settings(settings: Any) -> NotesConfig:
    key = Path(getattr(settings, "gmail_token_dir", "config/") or "config/") / DEPLOY_KEY_NAME
    return NotesConfig(
        path=Path(getattr(settings, "notes_path", "/app/config/notes") or "/app/config/notes"),
        repo_url=getattr(settings, "notes_repo_url", "") or "",
        deploy_key=key if key.exists() else None,
    )


def install_deploy_key(settings: Any) -> Path | None:
    """Write `settings.notes_deploy_key` to `<gmail_token_dir>/notes_deploy_key`
    with mode 0600 — the same writer the books key uses. Never logs the value."""
    path = Path(getattr(settings, "gmail_token_dir", "config/") or "config/") / DEPLOY_KEY_NAME
    return books.write_deploy_key(
        getattr(settings, "notes_deploy_key", "") or "", path, DEPLOY_KEY_NAME
    )


# ------------------------------------------------------------ encryption


def strip_encrypted(text: str) -> str:
    """`text` with every meld-encrypt block replaced by a placeholder: first the
    `%%🔐 … 🔐%%` form every version writes, then the bare `🔐α … 🔐` /
    `🔐β … 🔐` form later versions write when the marker is shown in reading
    view.

    An unterminated start marker drops everything after it: a block whose end
    was lost is still ciphertext, and leaking a tail of it is worse than losing
    the rest of one note from the index.
    """
    if "🔐" not in text:
        return text
    out: list[str] = []
    i = 0
    while True:
        start = text.find(_ENC_START, i)
        if start < 0:
            out.append(text[i:])
            break
        out.append(text[i:start])
        out.append(ENCRYPTED_PLACEHOLDER)
        end = text.find(_ENC_END, start + len(_ENC_START))
        if end < 0:
            break
        i = end + len(_ENC_END)
    text = "".join(out)
    out = []
    i = 0
    while True:
        bare = _ENC_BARE_START_RE.search(text, i)
        if bare is None:
            out.append(text[i:])
            break
        out.append(text[i : bare.start()])
        out.append(ENCRYPTED_PLACEHOLDER)
        end = text.find(_ENC_BARE_END, bare.end())
        if end < 0:
            break
        i = end + len(_ENC_BARE_END)
    return "".join(out)


def split_section(text: str, key: str) -> tuple[str, str]:
    """What a journal write put under `key`, and the note without it.
    `("", text)` when the marker is not in the note, or not on a bullet.

    The block (`journal_block`) is a `- #raphael … %% aegis:<key> %%` bullet
    whose indented child bullets are the outline. Each depth-1 child comes
    back as a paragraph, and a deeper one as a `- ` item indented two spaces
    a level under it, which is the text `body_outline` laid out in the first
    place."""
    mark = marker(key)
    at = text.find(mark)
    if at < 0:
        return "", text
    line_start = text.rfind("\n", 0, at) + 1
    line_end = text.find("\n", at)
    line_end = len(text) if line_end < 0 else line_end + 1
    line = text[line_start:line_end]
    if not line.lstrip().startswith("- "):
        return "", text
    indent = len(line) - len(line.lstrip())
    base = _indent_levels(line)
    end = line_end
    paras: list[str] = []
    while end < len(text):
        nxt = text.find("\n", end)
        nxt = len(text) if nxt < 0 else nxt + 1
        row = text[end:nxt].rstrip("\n")
        if not row.strip() or len(row) - len(row.lstrip()) <= indent:
            break
        item = _BULLET_RE.sub("", row.strip(), count=1)
        depth = max(1, _indent_levels(row) - base)
        if depth == 1 or not paras:
            paras.append(item)
        else:
            paras[-1] += "\n" + "  " * (depth - 1) + "- " + item
        end = nxt
    return "\n\n".join(p for p in paras if p), (text[:line_start] + text[end:]).strip()


# ------------------------------------------------------ dates and names

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_MONTHS_LONG = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_DAYS_LONG = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def week_start(d: date) -> date:
    """The Monday that starts `d`'s week. The vault's weekly notes have been
    dated from their Monday since 2023 (`W40 Oct 23` opens `# Oct 02, 2023`,
    `W05 Jan 23` is Mon 30 Jan): the calendar plugin follows the system locale
    (`weekStart: locale`), which is Monday-first. Its 2022 notes were
    Sunday-dated; the current convention is the one Raphael writes."""
    return d - timedelta(days=d.weekday())


def locale_week(d: date) -> int:
    """moment's `ww` in that Monday-first locale (`en-gb`: dow 1, doy 4), which
    is the ISO week number: `W40 Oct 23` is Mon 2 Oct 2023, and Mon 29 Dec
    2025 is week 1 of 2026."""
    return d.isocalendar().week


# Longest first, so `MMMM` is never read as `MM` + `MM`.
_TOKENS = (
    "YYYY", "MMMM", "dddd", "MMM", "ddd", "YY", "MM", "DD", "Do", "HH", "hh", "mm", "ss",
    "ww", "M", "D", "H", "h", "w", "A", "a",
)


def _ordinal(n: int) -> str:
    """moment's `Do`: 1st, 2nd, 3rd, 4th … 11th, 12th, 13th … 21st, 22nd."""
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def moment_format(fmt: str, when: date | datetime) -> str:
    """A small moment.js `format()`: the tokens Obsidian's date settings use,
    and `[...]` for literal text. Anything unrecognised is copied as is."""
    if not isinstance(when, datetime):
        when = datetime(when.year, when.month, when.day)
    values = {
        "YYYY": f"{when.year:04d}",
        "YY": f"{when.year % 100:02d}",
        "MMMM": _MONTHS_LONG[when.month - 1],
        "MMM": _MONTHS[when.month - 1],
        "MM": f"{when.month:02d}",
        "M": str(when.month),
        "DD": f"{when.day:02d}",
        "Do": _ordinal(when.day),
        "D": str(when.day),
        "dddd": _DAYS_LONG[when.weekday()],
        "ddd": _DAYS[when.weekday()],
        "HH": f"{when.hour:02d}",
        "H": str(when.hour),
        "hh": f"{(when.hour % 12) or 12:02d}",
        "h": str((when.hour % 12) or 12),
        "mm": f"{when.minute:02d}",
        "ss": f"{when.second:02d}",
        "ww": f"{locale_week(when.date()):02d}",
        "w": str(locale_week(when.date())),
        "A": "AM" if when.hour < 12 else "PM",
        "a": "am" if when.hour < 12 else "pm",
    }
    out: list[str] = []
    i = 0
    while i < len(fmt):
        if fmt[i] == "[":
            close = fmt.find("]", i + 1)
            if close > 0:
                out.append(fmt[i + 1 : close])
                i = close + 1
                continue
        for tok in _TOKENS:
            if fmt.startswith(tok, i):
                out.append(values[tok])
                i += len(tok)
                break
        else:
            out.append(fmt[i])
            i += 1
    return "".join(out)


# The vault's periodic-notes settings (`.obsidian/plugins/periodic-notes`).
DAILY_FORMAT = "DD MMM YY"
WEEKLY_FORMAT = "[W]ww MMM YY"
MONTHLY_FORMAT = "MM. MMM"

TEMPLATES = {
    "daily": "_templates/{{tp_title_today}}.md",
    "weekly": "_templates/weekly-{{tp_title_today}}.md",
    "monthly": "_templates/monthly.md",
}


def _month_folder(d: date) -> str:
    """`journal/<YYYY>/<NN. Mon>`: where the vault files a month's notes."""
    return f"journal/{d.year:04d}/{d.month:02d}. {_MONTHS[d.month - 1]}"


def daily_note_path(d: date) -> str:
    """The day's note, filed in its month folder as every filed daily note in
    the vault is: `journal/2023/10. Oct/24 Oct 23.md`."""
    return f"{_month_folder(d)}/{moment_format(DAILY_FORMAT, d)}.md"


def daily_root_path(d: date) -> str:
    """Where periodic-notes creates today's note before the user files it
    (`journal/25 Oct 23.md`). Raphael appends to it when it exists and never
    creates it."""
    return f"journal/{moment_format(DAILY_FORMAT, d)}.md"


def weekly_note_path(d: date) -> str:
    """The week holding `d`, named from its Monday and filed in that Monday's
    month folder: `journal/2023/10. Oct/W40 Oct 23.md`. The daylog's weekly
    rollup is an ISO week, Monday to Sunday — the same week."""
    ws = week_start(d)
    return f"{_month_folder(ws)}/{moment_format(WEEKLY_FORMAT, ws)}.md"


def weekly_root_path(d: date) -> str:
    """The week's live periodic-notes note at the journal root, if the user made one."""
    return f"journal/{moment_format(WEEKLY_FORMAT, week_start(d))}.md"


def monthly_note_path(d: date) -> str:
    """The month folder's own folder note, as the vault's months are
    (`journal/2023/08. Aug/08. Aug.md`). `MM. MMM` has no year, so a month
    note only ever lives inside its year."""
    return f"{_month_folder(d)}/{moment_format(MONTHLY_FORMAT, d)}.md"


_MON_RE = r"[A-Z][a-z]{2}"
_FOLDER_RE = rf"journal/\d{{4}}/\d{{2}}\. {_MON_RE}"
# The journal notes the daylog may create: filed daily, weekly and monthly notes.
_JOURNAL_RES = (
    re.compile(rf"^{_FOLDER_RE}/\d{{2}} {_MON_RE} \d{{2}}\.md$"),
    re.compile(rf"^{_FOLDER_RE}/W\d{{2}} {_MON_RE} \d{{2}}\.md$"),
    re.compile(rf"^journal/\d{{4}}/(\d{{2}}\. {_MON_RE})/\1\.md$"),
)
# A live periodic-notes note at the journal root: written only when it exists.
_JOURNAL_ROOT_RES = (
    re.compile(rf"^journal/\d{{2}} {_MON_RE} \d{{2}}\.md$"),
    re.compile(rf"^journal/W\d{{2}} {_MON_RE} \d{{2}}\.md$"),
)


def is_journal_path(rel: str) -> bool:
    """A journal note the daylog may create (filed in its month folder)."""
    return any(r.match(rel) for r in _JOURNAL_RES)


def is_journal_root_path(rel: str) -> bool:
    """A daily or weekly note at the journal root: appended to, never created."""
    return any(r.match(rel) for r in _JOURNAL_ROOT_RES)


# ------------------------------------------------------------ templates

_PLACEHOLDER_RE = re.compile(r"\{\{\s*(date|time|title)(?:\s*:\s*([^}]*?))?\s*\}\}")
_TEMPLATER_RE = re.compile(r"<%[\s\S]*?%>")


def render_template(text: str, *, title: str, when: datetime) -> str:
    """Obsidian's core-template placeholders, rendered. The vault's templates
    use only `{{date:FMT}}`, `{{date}}`, `{{time}}` and `{{title}}`; a
    Templater tag (`<% … %>`) is dropped — Raphael never executes one, and
    never writes one into a note either."""
    text = _TEMPLATER_RE.sub("", text)

    def sub(m: re.Match) -> str:
        kind, fmt = m.group(1), (m.group(2) or "").strip()
        if kind == "title":
            return title
        if kind == "time":
            return moment_format(fmt or "HH:mm", when)
        return moment_format(fmt or "YYYY-MM-DD", when)

    return _PLACEHOLDER_RE.sub(sub, text)


# ------------------------------------------------------------- sections

_KEY_RE = re.compile(r"^[A-Za-z0-9:_.\-]{1,160}$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# A body must not be able to forge another write's marker.
_FORGED_MARKER_RE = re.compile(r"%%\s*aegis:")


def marker(key: str) -> str:
    return f"%% aegis:{key} %%"


def clean_body(text: str) -> str:
    text = _CONTROL_RE.sub("", text or "")
    return _FORGED_MARKER_RE.sub("%% aegis-quoted:", text).strip()


@dataclass(frozen=True)
class Append:
    """One insert-only write.

    rel       path inside the vault
    key       the marker; a note that already carries it is left alone
    body      the text to write
    heading   `## <heading>` before the body; empty = one inline line whose
              marker sits at its end (a link). Not used with `section`.
    template  `daily` / `weekly` / `monthly`: render the vault's template when
              the note does not exist yet
    title     `# <title>` for a new note that has no template
    when      the date a template's placeholders are rendered for
    journal   the path is a journal note (only the daylog writes those)
    alt_rel   a live journal note at the root that takes the write instead of
              `rel` when it already exists; never created
    section   heading texts, in order of preference (`("Review", "Month
              Review")`): insert a `#raphael` bullet block at the end of the
              first such section of the note, at any heading level, instead
              of appending at the end
    label     the block's title after `#raphael` (`day log`)
    """

    rel: str
    key: str
    body: str
    heading: str = ""
    template: str = ""
    title: str = ""
    when: datetime | None = None
    journal: bool = False
    alt_rel: str = ""
    section: tuple[str, ...] = ()
    label: str = ""


def check_path(rel: str, *, journal: bool = False) -> str:
    """`rel` if Raphael may write it, else NotesPathError. Nothing is
    normalised away: a path that needs normalising is refused."""
    if not rel or "\x00" in rel or "\\" in rel or rel.startswith("/"):
        raise NotesPathError(f"not a vault path: {rel!r}")
    parts = rel.split("/")
    if any(p in ("", ".", "..") or p.startswith(".") for p in parts):
        raise NotesPathError(f"not a vault path: {rel!r}")
    if not rel.endswith(".md"):
        raise NotesPathError(f"only markdown notes can be written: {rel!r}")
    if parts[0] == RAPHAEL_DIR and len(parts) > 1:
        return rel
    if journal and is_journal_path(rel):
        return rel
    raise NotesPathError(
        f"Raphael may only write under {RAPHAEL_DIR}/ (and the daylog's journal notes): {rel!r}"
    )


def _section(ap: Append) -> str:
    body = clean_body(ap.body)
    if not ap.heading:
        return f"{body} {marker(ap.key)}\n"
    return f"\n## {ap.heading}\n{marker(ap.key)}\n\n{body}\n"


# A journal entry goes into the note's own section, as the user's bullets do.
# Matched by heading TEXT at any level: the current templates use `## Review`,
# older notes `### Review` and, for the month, `### Month Review`.
JOURNAL_SECTIONS: dict[str, tuple[str, ...]] = {
    "daily": ("Journal",),
    "weekly": ("Review",),
    "monthly": ("Review", "Month Review"),
}
JOURNAL_LABELS = {"daily": "day log", "weekly": "week in review", "monthly": "month in review"}
_HEADING_RE = re.compile(r"^#{1,6} ")


def _is_boundary(line: str) -> bool:
    """A line that ends a section: the next heading, a `---` rule or a code fence."""
    s = line.strip()
    return bool(_HEADING_RE.match(line)) or s == "---" or s.startswith("```")


def _heading_text(line: str) -> str | None:
    """`Review` for `### Review `, None for a line that is not a heading."""
    if not _HEADING_RE.match(line):
        return None
    return line.strip().lstrip("#").strip()


def _find_section(lines: list[str], names: tuple[str, ...]) -> int | None:
    """The line index of the heading that opens the first of `names` the note
    has (the first such heading), or None."""
    wanted = [n.strip().casefold() for n in names]
    found: dict[str, int] = {}
    for i, line in enumerate(lines):
        text = _heading_text(line)
        if text is not None and text.casefold() in wanted:
            found.setdefault(text.casefold(), i)
    for name in wanted:
        if name in found:
            return found[name]
    return None


_BULLET_RE = re.compile(r"^[-*+•]\s+")
_NUMBERED_RE = re.compile(r"^\d+[.)]\s+")
MAX_OUTLINE_DEPTH = 4


def _indent_levels(line: str) -> int:
    """Leading indentation in outline levels: a tab or two spaces each."""
    lead = line[: len(line) - len(line.lstrip(" \t"))]
    return lead.count("\t") + lead.count(" ") // 2


def body_outline(body: str) -> list[tuple[int, str]]:
    """`body` as outline nodes `(depth, text)`, depth 1 sitting directly under
    the `#raphael` bullet. Both kinds of daylog text go through here:

    * an LLM narrative — prose paragraphs split by blank lines — gives one
      depth-1 node per paragraph, its wrapped lines joined with a space;
    * the deterministic fallback (`daylog._format_daylog_fallback`) keeps its
      outline: a `Label:` line, then its items indented under it.

    Every non-blank line is a node, except that consecutive plain unindented
    lines join into one; a blank line, a label, a list item or an indented
    line ends that join. A plain unindented line ending in `:` is a label.
    Depth is 1 plus the line's indent (a tab or two spaces a level), at most
    one deeper than the node before and never past `MAX_OUTLINE_DEPTH`. A
    `-`/`*`/`+`/`•` marker (or a number) is dropped, a heading becomes bold,
    and the text is otherwise kept as it is."""
    nodes: list[tuple[int, str]] = []
    prose: list[str] = []

    def flush() -> None:
        if prose:
            nodes.append((1, " ".join(prose)))
            prose.clear()

    for raw in clean_body(body).splitlines():
        text = raw.strip()
        if not text:
            flush()
            continue
        levels = _indent_levels(raw)
        heading = _HEADING_RE.match(text)
        bullet = _BULLET_RE.match(text) or _NUMBERED_RE.match(text)
        if not levels and not heading and not bullet and not text.endswith(":"):
            prose.append(text)
            continue
        flush()
        if heading:
            text = f"**{_HEADING_RE.sub('', text).strip()}**"
        elif bullet:
            text = text[bullet.end() :]
        if text:
            prev = nodes[-1][0] if nodes else 0
            nodes.append((min(1 + levels, prev + 1, MAX_OUTLINE_DEPTH), text))
    flush()
    return nodes


def journal_block(key: str, label: str, body: str) -> str:
    """Raphael's entry in the user's own bullet style (they write `- ` bullets
    under `## Journal`, with obsidian-outliner)::

        - #raphael day log %% aegis:<key> %%
        <TAB>- <first paragraph, as one line>
        <TAB>- Completed:
        <TAB><TAB>- <an item under that label>

    The body is laid out by :func:`body_outline`. The marker is an Obsidian
    comment, so reading view shows the tag, the label and the outline."""
    title = " ".join(p for p in ("#raphael", clean_body(label).replace("\n", " ")) if p)
    rows = [f"- {title} {marker(key)}"] + ["\t" * d + f"- {t}" for d, t in body_outline(body)]
    return "\n".join(rows) + "\n"


def _section_end(lines: list[str], names: tuple[str, ...]) -> int | None:
    """The index just after the last non-blank line of the section (just after
    its heading when it is empty), or None when the note has no such heading.
    The section ends before the next heading, a `---` rule or a code fence —
    the monthly note's `ccard` fence included."""
    i = _find_section(lines, names)
    if i is None:
        return None
    last = i
    for j in range(i + 1, len(lines)):
        if _is_boundary(lines[j]):
            break
        if lines[j].strip():
            last = j
    return last + 1


def insert_block(text: str, names: tuple[str, ...], block: str) -> str:
    """`text` with `block` inserted at the end of the section — after its last
    non-blank line, before the next heading, `---` or code fence — or, when
    the note has none of `names`, a `## <first name>` heading and the block at
    the end. Nothing that is there moves or changes."""
    lines = text.splitlines(keepends=True)
    at = _section_end(lines, names)
    if at is None:
        base = text if not text or text.endswith("\n") else text + "\n"
        return f"{base}\n## {names[0]}\n{block}"
    before = "".join(lines[:at])
    if before and not before.endswith("\n"):
        before += "\n"
    return before + block + "".join(lines[at:])


def drop_placeholders(text: str, names: tuple[str, ...]) -> str:
    """A new note's target section without its empty `- ` placeholder bullets:
    Raphael fills that section, and a lone `- ` left above the entry would be
    an empty bullet in the note. Every other line of the template stays."""
    lines = text.splitlines(keepends=True)
    i = _find_section(lines, names)
    if i is None:
        return text
    j = i + 1
    while j < len(lines) and not _is_boundary(lines[j]):
        j += 1
    kept = [ln for ln in lines[i + 1 : j] if ln.strip() not in ("-", "*", "+")]
    return "".join(lines[: i + 1] + kept + lines[j:])


def is_one_insertion(old: str, new: str) -> bool:
    """True when `new` is `old` with one contiguous run of text inserted:
    every character of `old` still there, unchanged and in order."""
    if len(new) < len(old):
        return False
    p = 0
    while p < len(old) and old[p] == new[p]:
        p += 1
    s = 0
    while s < len(old) - p and old[-1 - s] == new[-1 - s]:
        s += 1
    return p + s == len(old)


def append_text(existing: str | None, ap: Append, new_note: str = "") -> str | None:
    """The note's new text, or None when its marker is already there. The
    result is always `existing` with ONE block inserted — at the end of
    the `ap.section` it finds for a journal entry, at the end of the note
    otherwise — and
    `_apply` checks that again before writing."""
    if not _KEY_RE.match(ap.key):
        raise NotesError(f"bad marker key {ap.key!r}")
    if existing is not None and marker(ap.key) in existing:
        return None
    base = existing if existing is not None else new_note
    if ap.section:
        return insert_block(base, ap.section, journal_block(ap.key, ap.label, ap.body))
    if base and not base.endswith("\n"):
        base += "\n"
    return base + _section(ap)


# An unticked task line: `- [ ] …` (or a `*`/`+` bullet), indented or not.
_OPEN_TASK_RE = re.compile(r"^[ \t]*[-*+] \[ \][^\n]*(?:\n|$)", re.M)


def drop_open_tasks(text: str) -> str:
    """`text` without its unticked task lines. The vault's templates carry
    prompts for the person filling the day in ("- [ ] #admin Plan the day").
    In a note Raphael creates they are to-dos nobody can do: the day has
    already passed, and obsidian-checklist-plugin lists every open box in the
    vault, so the 2026-09-12 backfill added 125 of them. Ticked boxes, headings
    and everything else in the template stay."""
    return _OPEN_TASK_RE.sub("", text)


def _new_note_text(cfg: NotesConfig, ap: Append) -> str:
    """What a note starts with when Raphael creates it: the vault's own
    template for a journal note (read at write time, so edits to the template
    apply) without its open tasks, else a `# title` line. A note that already
    exists is never touched here — only the text of a brand-new note."""
    stem = Path(ap.rel).stem
    if ap.template:
        tpl = cfg.path / TEMPLATES[ap.template]
        if tpl.is_file():
            when = ap.when or datetime.now()
            rendered = drop_open_tasks(
                render_template(tpl.read_text("utf-8"), title=stem, when=when)
            )
            return drop_placeholders(rendered, ap.section) if ap.section else rendered
        return f"# {stem}\n"
    title = clean_body(ap.title or stem).replace("\n", " ")
    return f"# {title}\n"


# ---------------------------------------------------------------- git


def _env(cfg: NotesConfig) -> dict[str, str]:
    env = {**os.environ, **_GIT_IDENTITY}
    if cfg.deploy_key:
        env["GIT_SSH_COMMAND"] = (
            f"ssh -i {cfg.deploy_key} -o StrictHostKeyChecking=accept-new -o IdentitiesOnly=yes"
        )
    return env


# Credentials in a URL, as git prints them (`https://user:token@host/...`).
_URL_USERINFO_RE = re.compile(r"(\w+://)[^/\s@]+@")
# git saying the remote moved on under the write: a push rejected as not a
# fast-forward, or a rebase that stopped on a conflict. Only these are worth
# dropping the write, pulling fresh and trying once more.
_CONFLICT_RE = re.compile(r"\[rejected\]|non-fast-forward|\(fetch first\)|CONFLICT|could not apply")
_KEY_REFUSED_RE = re.compile(
    r"Permission denied|publickey|Authentication failed|could not read Username", re.I
)
_NOT_FOUND_RE = re.compile(r"does not appear to be a git repository|repository not found", re.I)
_UNREACHABLE_RE = re.compile(
    r"Could not resolve host|unable to access|Connection (?:timed out|refused|reset)|"
    r"Network is unreachable|Could not read from remote repository",
    re.I,
)


def _scrub(text: str) -> str:
    """git's stderr without the credentials of any URL in it."""
    return _URL_USERINFO_RE.sub(r"\1", text or "")


def _git_failure(what: str, proc: subprocess.CompletedProcess) -> NotesError:
    """The error for a failed pull or push.

    A `NotesConflict` only when git says the remote moved on, which the retry
    can fix. Anything else — no network, a refused key, a missing repository —
    fails the same way twice, so it is a `NotesError` at once, with a reason
    that is ours: short, and free of the URL, paths and whatever else git
    printed. Every failure used to be a conflict, retried and then reported as
    "the vault changed under the write twice" with git's stderr in it."""
    err = proc.stderr or ""
    if _CONFLICT_RE.search(err):
        return NotesConflict(f"{what}: the vault changed on the remote")
    if _KEY_REFUSED_RE.search(err):
        reason = "the remote refused the deploy key"
    elif _NOT_FOUND_RE.search(err):
        reason = "the remote repository was not found"
    elif _UNREACHABLE_RE.search(err):
        reason = "the remote could not be reached"
    else:
        reason = f"git exited {proc.returncode}"
    return NotesError(f"{what} failed: {reason}")


def _run(
    args: list[str], cfg: NotesConfig, *, timeout: int = 60, check: bool = True
) -> subprocess.CompletedProcess:
    try:
        proc = books._spawn(args, cwd=str(cfg.path), timeout=timeout, env=_env(cfg))
    except books.BooksError as exc:
        raise NotesError(_scrub(str(exc))) from exc
    if check and proc.returncode != 0:
        raise NotesError(f"{' '.join(args[:2])} failed: {_scrub(proc.stderr.strip())[:500]}")
    return proc


def _has_remote(cfg: NotesConfig) -> bool:
    return bool(_run(["git", "remote"], cfg, check=False).stdout.strip())


def _has_upstream(cfg: NotesConfig) -> bool:
    return _run(["git", "rev-parse", "-q", "--verify", "@{u}"], cfg, check=False).returncode == 0


def _ensure_checkout(cfg: NotesConfig) -> None:
    """Clone on first use (inside the flock, via the books layer), and keep the
    lock file out of `git status` for good."""
    try:
        books.ensure_checkout_sync(cfg)  # type: ignore[arg-type] — same attribute names
    except books.BooksDisabled as exc:
        raise NotesDisabled("notes_repo_url is not configured and no checkout exists") from exc
    except books.BooksError as exc:
        raise NotesError(str(exc)) from exc
    exclude = cfg.path / ".git" / "info" / "exclude"
    try:
        current = exclude.read_text("utf-8") if exclude.exists() else ""
        if _LOCK_NAME not in current.split():
            exclude.parent.mkdir(parents=True, exist_ok=True)
            exclude.write_text(current + ("" if current.endswith("\n") or not current else "\n")
                               + _LOCK_NAME + "\n", "utf-8")
    except OSError as exc:  # pragma: no cover — cosmetic only
        logger.warning("notes_exclude_write_failed", error=str(exc)[:200])


def _drop_local(cfg: NotesConfig, paths: list[str]) -> None:
    """Back to exactly what is upstream: no rebase in progress, no unpushed
    commit, none of this write's files left behind. Only AEGIS writes in this
    checkout, so this discards AEGIS's own unfinished write and nothing else."""
    _run(["git", "rebase", "--abort"], cfg, check=False)
    if _has_upstream(cfg):
        _run(["git", "reset", "-q", "--hard", "@{u}"], cfg, check=False)
    try:
        books._revert_sync(cfg, paths)  # type: ignore[arg-type]
    except books.BooksError as exc:  # pragma: no cover — best effort
        logger.warning("notes_revert_failed", error=str(exc)[:200])


def _pull(cfg: NotesConfig) -> None:
    """Upstream, exactly. A leftover unpushed commit (a write killed between
    commit and push) is dropped first: a write only counts once pushed."""
    if not _has_remote(cfg):
        return
    if _has_upstream(cfg):
        ahead = _run(["git", "rev-list", "--count", "@{u}..HEAD"], cfg, check=False).stdout
        if ahead.strip().isdigit() and int(ahead.strip()) > 0:
            _run(["git", "reset", "-q", "--hard", "@{u}"], cfg, check=False)
    proc = _run(["git", "pull", "-q", "--rebase"], cfg, check=False, timeout=120)
    if proc.returncode != 0:
        _run(["git", "rebase", "--abort"], cfg, check=False)
        raise _git_failure("git pull", proc)


def _target_rel(cfg: NotesConfig, ap: Append) -> str:
    """The note an append goes to: the live root note when there is one
    (periodic-notes made the day's note and the user has not filed it yet),
    else the filed path."""
    if ap.alt_rel and (cfg.path / ap.alt_rel).is_file():
        return ap.alt_rel
    return ap.rel


def _apply(cfg: NotesConfig, ap: Append) -> tuple[str, bool]:
    """Apply one append to the working copy: `(the note it went to, whether it
    changed)`. The marker in EITHER place means the write is already done —
    the user may have filed the root note since Raphael wrote into it."""
    rel = _target_rel(cfg, ap)
    for other in (ap.rel, ap.alt_rel):
        if other and other != rel:
            path = cfg.path / other
            if path.is_file() and marker(ap.key) in path.read_text("utf-8"):
                return other, False
    target = cfg.path / rel
    existing = target.read_text("utf-8") if target.exists() else None
    new_note = _new_note_text(cfg, ap) if existing is None else ""
    text = append_text(existing, ap, new_note)
    if text is None:
        return rel, False
    if existing is not None and not is_one_insertion(existing, text):
        raise NotesError(f"refusing a write that would change existing text in {rel}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, "utf-8")
    return rel, True


def _commit(cfg: NotesConfig, summary: str, paths: list[str]) -> None:
    scoped = books._git_paths(cfg, paths)  # type: ignore[arg-type]
    if not scoped:
        return
    _run(["git", "add", "-A", "--", *scoped], cfg)
    if _run(["git", "diff", "--cached", "--quiet", "--", *scoped], cfg, check=False).returncode == 0:
        return
    _run(["git", "commit", "-q", "-m", summary, "--", *scoped], cfg)


def _push(cfg: NotesConfig) -> None:
    if not _has_remote(cfg):
        return
    proc = _run(["git", "push", "-q"], cfg, check=False, timeout=120)
    if proc.returncode != 0:
        raise _git_failure("git push", proc)


class _Lock:
    """The books flock, on `<vault>/.aegis.lock`: core and worker share the
    checkout, so they must take turns."""

    def __init__(self, cfg: NotesConfig) -> None:
        self._inner = books._FileLock(cfg)  # type: ignore[arg-type]

    def __enter__(self):
        self._inner.__enter__()
        return self

    def __exit__(self, *exc):
        self._inner.__exit__(*exc)


def write_sync(cfg: NotesConfig, appends: list[Append], summary: str) -> dict:
    """Apply `appends` in one commit and push it.

    `{"status": "written" | "exists", "paths", "changed", "outcomes",
    "attempts"}` — `outcomes` is one `{"path", "changed"}` per append, in
    order, `path` being the note it actually went to.
    Raises NotesDisabled, NotesPathError, NotesConflict or NotesError; after
    any raise the checkout is exactly upstream again.
    """
    if not cfg.configured:
        raise NotesDisabled("the vault is not configured (notes_repo_url and notes_deploy_key)")
    for ap in appends:
        check_path(ap.rel, journal=ap.journal)
        if ap.alt_rel and not (ap.journal and is_journal_root_path(ap.alt_rel)):
            raise NotesPathError(f"not a live journal note: {ap.alt_rel!r}")
        if ap.template and ap.template not in TEMPLATES:
            raise NotesError(f"unknown template {ap.template!r}")
    # Everything a write here could touch, so a failed one is undone whole.
    touchable = list(dict.fromkeys(p for ap in appends for p in (ap.rel, ap.alt_rel) if p))
    summary = clean_body(summary).replace("\n", " ")[:120] or "raphael: notes"
    with _Lock(cfg):
        _ensure_checkout(cfg)
        last = ""
        for attempt in (1, 2):
            try:
                _pull(cfg)
                outcomes = [_apply(cfg, ap) for ap in appends]
                result = {
                    "paths": list(dict.fromkeys(rel for rel, _ in outcomes)),
                    "outcomes": [{"path": rel, "changed": did} for rel, did in outcomes],
                    "attempts": attempt,
                }
                changed = list(dict.fromkeys(rel for rel, did in outcomes if did))
                if not changed:
                    return {"status": "exists", "changed": [], **result}
                _commit(cfg, summary, changed)
                _push(cfg)
                return {"status": "written", "changed": changed, **result}
            except NotesConflict as exc:
                last = str(exc)
                logger.warning("notes_write_conflict", attempt=attempt, error=last[:200])
                _drop_local(cfg, touchable)
            except Exception:
                _drop_local(cfg, touchable)
                raise
        raise NotesConflict(f"the vault changed under the write twice; nothing was kept ({last})")


_ASYNC_LOCK = asyncio.Lock()


async def write(cfg: NotesConfig, appends: list[Append], summary: str) -> dict:
    async with _ASYNC_LOCK:
        return await asyncio.to_thread(write_sync, cfg, appends, summary)


# --------------------------------------------------------------- reads


def _safe_read_path(rel: str) -> str:
    """Any note in the vault may be READ (the user's decision on #514), but
    only a plain relative `.md` path inside it."""
    if not rel or "\x00" in rel or "\\" in rel or rel.startswith("/"):
        raise NotesPathError(f"not a vault path: {rel!r}")
    if any(p in ("", ".", "..") for p in rel.split("/")) or not rel.endswith(".md"):
        raise NotesPathError(f"not a vault note: {rel!r}")
    if rel.startswith(".obsidian/"):
        raise NotesPathError(f"not a vault note: {rel!r}")
    return rel


def _pull_quietly(cfg: NotesConfig) -> None:
    """A read uses the freshest vault it can get, and the local copy when the
    pull fails — an unreachable GitHub must not stop Raphael reading."""
    try:
        _pull(cfg)
    except NotesError as exc:
        logger.warning("notes_pull_failed", error=str(exc)[:200])


def read_many_sync(cfg: NotesConfig, rels: list[str], *, pull: bool = False) -> dict[str, str]:
    """`{rel: text}` for the notes that exist, encrypted blocks stripped."""
    if not cfg.configured:
        raise NotesDisabled("the vault is not configured")
    out: dict[str, str] = {}
    with _Lock(cfg):
        _ensure_checkout(cfg)
        if pull:
            _pull_quietly(cfg)
        for rel in rels:
            path = cfg.path / rel
            if path.is_file():
                try:
                    out[rel] = strip_encrypted(path.read_text("utf-8", errors="replace"))
                except OSError as exc:  # pragma: no cover
                    logger.warning("notes_read_failed", path=rel, error=str(exc)[:200])
    return out


async def read_note(cfg: NotesConfig, rel: str, max_chars: int = READ_MAX_CHARS) -> dict:
    """One note for a chat tool: `{path, text, truncated}` or `{error}`."""
    try:
        rel = _safe_read_path(rel)
        found = await asyncio.to_thread(read_many_sync, cfg, [rel], pull=True)
    except NotesError as exc:
        return {"error": str(exc)}
    if rel not in found:
        return {"error": f"no such note: {rel}"}
    text = found[rel]
    limit = max(500, min(int(max_chars or READ_MAX_CHARS), READ_MAX_CHARS))
    return {"path": rel, "text": text[:limit], "truncated": len(text) > limit}


def read_journal_days_sync(cfg: NotesConfig, days: list[date]) -> dict[str, str]:
    """`{YYYY-MM-DD: note text}` for the days that have a journal note: the
    filed one, the live one at the journal root (where periodic-notes makes
    today's note before the user files it), or both, joined — the day is
    whatever either holds."""
    by_rel: dict[str, str] = {}
    for d in days:
        for rel in (daily_note_path(d), daily_root_path(d)):
            by_rel[rel] = d.isoformat()
    found = read_many_sync(cfg, list(by_rel), pull=True)
    out: dict[str, str] = {}
    for rel, text in found.items():
        day = by_rel[rel]
        out[day] = f"{out[day]}\n\n{text}" if day in out else text
    return out


# --------------------------------------------------------------- index


def is_indexable(rel: str) -> bool:
    if not rel.endswith(".md") or rel.startswith(SKIP_PREFIXES):
        return False
    return not any(part.startswith(".") for part in rel.split("/"))


def note_url(rel: str) -> str:
    return f"vault://{rel}"


def note_path_from_url(url: str) -> str:
    return url[len("vault://"):] if (url or "").startswith("vault://") else ""


def note_title(rel: str) -> str:
    return Path(rel).stem


def top_folder(rel: str) -> str:
    return rel.split("/", 1)[0] if "/" in rel else ""


@dataclass
class VaultChanges:
    head: str
    changed: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    full: bool = False


def _ls_files(cfg: NotesConfig) -> list[str]:
    out = _run(["git", "ls-files", "-z"], cfg).stdout
    return sorted(p for p in out.split("\0") if p and is_indexable(p))


def vault_changes_sync(cfg: NotesConfig, since: str | None) -> VaultChanges:
    """What changed since commit `since` — every note on a first run, or when
    `since` is no longer in the history (a force-push upstream). A rename is a
    delete plus an add, so the old url leaves the index."""
    if not cfg.configured:
        raise NotesDisabled("the vault is not configured")
    with _Lock(cfg):
        _ensure_checkout(cfg)
        _pull_quietly(cfg)
        head = _run(["git", "rev-parse", "HEAD"], cfg).stdout.strip()
        if since and since == head:
            return VaultChanges(head=head)
        known = bool(since) and _run(
            ["git", "cat-file", "-e", f"{since}^{{commit}}"], cfg, check=False
        ).returncode == 0
        if not known:
            return VaultChanges(head=head, changed=_ls_files(cfg), full=True)
        diff = _run(
            ["git", "diff", "--name-status", "--no-renames", "-z", since, head], cfg
        ).stdout.split("\0")
        changed: list[str] = []
        deleted: list[str] = []
        for status, rel in zip(diff[0::2], diff[1::2], strict=False):
            if not rel or not is_indexable(rel):
                continue
            (deleted if status.startswith("D") else changed).append(rel)
        return VaultChanges(head=head, changed=sorted(changed), deleted=sorted(deleted))


# ------------------------------------------------------ what writes what


def journal_key(kind: str, label: str) -> str:
    """The marker the daylog and the backfill share, so a backfilled day and a
    live one are the same write and never appear twice."""
    return f"daylog:{label}" if kind == "daily" else f"daylog:{kind}:{label}"


def journal_append(kind: str, day: date, label: str, body: str, now: datetime) -> Append:
    """The daylog's entry for one day, week or month.

    kind   `daily` / `weekly` / `monthly`
    day    the day; the ISO week's Monday; the month's first day
    label  the daylog's own label: `2026-09-12`, `2026-W37`, `2026-09`
    """
    if kind == "daily":
        rel, alt = daily_note_path(day), daily_root_path(day)
    elif kind == "weekly":
        rel, alt = weekly_note_path(day), weekly_root_path(day)
    elif kind == "monthly":
        rel, alt = monthly_note_path(day), ""
    else:
        raise NotesError(f"unknown journal kind {kind!r}")
    # A weekly note is named from its week's Monday, so its template's dates
    # are that Monday's too (the user's own: `W40 Oct 23` opens `# Oct 02, 2023`).
    when = datetime.combine(week_start(day) if kind == "weekly" else day, now.time())
    return Append(
        rel=rel,
        key=journal_key(kind, label),
        body=body,
        template=kind,
        when=when,
        journal=True,
        alt_rel=alt,
        section=JOURNAL_SECTIONS[kind],
        label=JOURNAL_LABELS[kind],
    )


def _slug(text: str, limit: int = 60) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (base or "question")[:limit].strip("-") or "question"


def question_append(question: str, report: str, asked: datetime) -> Append:
    """A research answer as `raphael/questions/<slug>-<hash>.md`. The marker is
    keyed on the question AND the answer: the same answer is never appended
    twice, and a different one later is a new dated section."""
    from aegis.services.research import normalise_question

    norm = normalise_question(question)
    qh = hashlib.sha256(norm.encode()).hexdigest()[:10]
    ah = hashlib.sha256((report or "").encode()).hexdigest()[:10]
    return Append(
        rel=f"{RAPHAEL_DIR}/questions/{_slug(norm)}-{qh}.md",
        key=f"question:{qh}:{ah}",
        body=report,
        heading=asked.strftime("%Y-%m-%d"),
        title=question.strip()[:200],
    )
