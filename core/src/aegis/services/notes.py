"""The agent's notes: the user's Obsidian vault is the record (#514).

Spec: `docs/superpowers/specs/2026-09-12-raphael-notes-design.md`.

This is the ONLY module that writes the vault. Everything else — the daylog, the
research lane, the `note_write`/`note_link` chat tools through `notes_write.py`
— hands it an :class:`Append` and gets back what happened. Four rules hold here
and every caller relies on them:

* **Insert-only.** A write creates a note, or inserts ONE contiguous block
  into it: a journal entry at the end of the note's own section (the layout's
  `sections` for the kind, at whatever heading level the note uses), anything
  else at the end of the note. It never rewrites, reorders or deletes a line
  the user wrote, and `_apply` checks exactly that (`is_one_insertion`) before
  it writes. Each block carries a hidden Obsidian comment marker
  (`%% aegis:<key> %%`), and a write whose marker is already in the note is a
  no-op — so a re-run or a retry can neither duplicate a block nor change one.
* **Only where the agent may write.** Anything under the layout's `agent_dir`,
  and the journal notes the daylog owns, filed as the layout files them. A live
  periodic-notes note in the layout's `live_folder` takes the write only when it
  already exists; it is never created. Every other path is refused before git
  is touched.
* **A write counts only once it is pushed.** The phone and laptop auto-commit
  through `obsidian-git`, so a push can be rejected and a rebase can conflict.
  The writer then drops its own unpushed commit, pulls fresh and re-applies the
  append once; if that fails too it drops the commit again and reports. It
  never force-pushes, and the checkout never carries a divergent commit.
* **Encrypted blocks never leave the file.** meld-encrypt keeps ciphertext
  between `%%🔐` and `🔐%%` inside ordinary notes. :func:`strip_encrypted` runs
  before anything is indexed, shown by a tool or sent to a model.

Where things go and how a block looks is the vault's **layout**
(`services/vault_layout.py`, the `vault_layout` settings row): folders, file
name formats, templates, section headings, the week rule, the outline's tag and
indent. Every function here takes a `layout`, defaulting to the shipped one, so
a deployment with no row behaves as it always did. The rules above are not
layout and are not configurable.

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
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any

import structlog

from aegis.errors import error_text
from aegis.services import books
from aegis.services import vault_layout as vl
from aegis.services.vault_layout import DEFAULT_LAYOUT, KINDS, Layout, moment_format

logger = structlog.get_logger()

__all__ = ["DEFAULT_LAYOUT", "Layout", "moment_format"]

DEPLOY_KEY_NAME = "notes_deploy_key"
# One note is indexed whole up to this; a longer one is cut (a note that long
# is a paste, not a note). The `notes-sync-hourly` row's `index_max_chars`
# overrides it.
INDEX_MAX_CHARS = 100_000
# What a chat tool may read back in one call.
READ_MAX_CHARS = 60_000
# The shipped layout's values, for callers and tests that want them by name.
SKIP_PREFIXES = DEFAULT_LAYOUT.index_skip_prefixes
TEMPLATES = {kind: DEFAULT_LAYOUT.kind(kind).template for kind in KINDS}
JOURNAL_SECTIONS = {kind: DEFAULT_LAYOUT.kind(kind).sections for kind in KINDS}
JOURNAL_LABELS = {kind: DEFAULT_LAYOUT.kind(kind).label for kind in KINDS}
DAILY_FORMAT = DEFAULT_LAYOUT.daily.format
WEEKLY_FORMAT = DEFAULT_LAYOUT.weekly.format
MONTHLY_FORMAT = DEFAULT_LAYOUT.monthly.format

# Whose journal it is: the holder of this capability keeps it, and a run or a
# write that names no agent is his. Never a literal agent id (#36).
JOURNAL_OWNER_TAG = "gtd"

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
    """A path the agent may not write."""


class JournalKindDisabled(NotesError):  # noqa: N818 — a state, as NotesDisabled
    """The layout has this journal kind switched off; the daylog files its
    knowledge row instead."""


# ----------------------------------------------------------------- config


@dataclass(frozen=True)
class Author:
    """Who a commit is from. Derived from the owning agent's `agents.name`
    (`author_for`); a write with no known agent is AEGIS's own."""

    name: str = "AEGIS"
    email: str = "aegis@aegis.local"
    # The commit message's prefix: `raphael: journal 2026-09-12`.
    prefix: str = "aegis"


DEFAULT_AUTHOR = Author()


@dataclass(frozen=True)
class NotesConfig:
    """The vault checkout, and who commits to it. Same attribute names as
    `books.BooksConfig`, which is what lets this module reuse the books git
    layer."""

    path: Path
    repo_url: str = ""
    deploy_key: Path | None = None
    author: Author = DEFAULT_AUTHOR

    @property
    def configured(self) -> bool:
        """Both halves of the integration are set. Until then nothing writes,
        indexes or reads the vault, and the daylog keeps its old behaviour."""
        return bool(self.repo_url) and self.deploy_key is not None


def author_for(agent_id: str | None, name: str | None = None) -> Author:
    """The `Author` for an agent: its display name, `<id>@aegis.local`, and the
    id as the commit prefix."""
    aid = vl.agent_slug(agent_id)
    if not aid:
        return DEFAULT_AUTHOR
    return Author(name=(name or "").strip() or aid, email=f"{aid}@aegis.local", prefix=aid)


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


def split_section(text: str, key: str, *, indent_width: int = 2) -> tuple[str, str]:
    """What a journal write put under `key`, and the note without it.
    `("", text)` when the marker is not in the note, or not on a bullet.

    The block (`journal_block`) is a `- <tag> … %% aegis:<key> %%` bullet
    whose indented child bullets are the outline. Each depth-1 child comes
    back as a paragraph, and a deeper one as a `- ` item indented two spaces
    a level under it, which is the text `body_outline` laid out in the first
    place. `indent_width` is the layout's spaces per level (a tab is always
    one level), so a block written with four-space indents reads back too."""
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
    base = _indent_levels(line, indent_width)
    end = line_end
    paras: list[str] = []
    while end < len(text):
        nxt = text.find("\n", end)
        nxt = len(text) if nxt < 0 else nxt + 1
        row = text[end:nxt].rstrip("\n")
        if not row.strip() or len(row) - len(row.lstrip()) <= indent:
            break
        item = _BULLET_RE.sub("", row.strip(), count=1)
        depth = max(1, _indent_levels(row, indent_width) - base)
        if depth == 1 or not paras:
            paras.append(item)
        else:
            paras[-1] += "\n" + "  " * (depth - 1) + "- " + item
        end = nxt
    return "\n\n".join(p for p in paras if p), (text[:line_start] + text[end:]).strip()


# ------------------------------------------------------ dates and names


def week_start(d: date, layout: Layout = DEFAULT_LAYOUT) -> date:
    """The first day of `d`'s week under the layout's rule. The shipped rule
    is Monday-first (the calendar plugin's `weekStart: locale` in a Monday-first
    locale); `week_start: sunday` is the other."""
    return vl.week_start_of(d, layout.week_start)


def locale_week(d: date, layout: Layout = DEFAULT_LAYOUT) -> int:
    """moment's `ww` under the layout's rule. The shipped rule (Monday-first,
    ISO numbering — `en-gb`) is the ISO week number: Mon 29 Dec 2025 is week
    1 of 2026."""
    return vl.week_of(d, layout.week_start, layout.week_numbering)[1]


def daily_note_path(d: date, layout: Layout = DEFAULT_LAYOUT) -> str:
    """The day's note, filed where the layout files it — as shipped,
    `journal/2023/10. Oct/24 Oct 23.md`."""
    return layout.note_path("daily", d)


def daily_root_path(d: date, layout: Layout = DEFAULT_LAYOUT) -> str:
    """Where periodic-notes creates today's note before the user files it
    (`journal/25 Oct 23.md` as shipped). Appended to when it exists, never
    created. Empty when the layout has no live folder for days."""
    return layout.root_path("daily", d)


def weekly_note_path(d: date, layout: Layout = DEFAULT_LAYOUT) -> str:
    """The week holding `d`, named from its first day and filed in that day's
    folder: `journal/2023/10. Oct/W40 Oct 23.md` as shipped. The daylog's
    weekly rollup uses the same week rule, so it is the same week."""
    return layout.note_path("weekly", d)


def weekly_root_path(d: date, layout: Layout = DEFAULT_LAYOUT) -> str:
    """The week's live periodic-notes note, if the user made one."""
    return layout.root_path("weekly", d)


def monthly_note_path(d: date, layout: Layout = DEFAULT_LAYOUT) -> str:
    """The month's note — as shipped, the month folder's own folder note
    (`journal/2023/08. Aug/08. Aug.md`)."""
    return layout.note_path("monthly", d)


def is_journal_path(rel: str, layout: Layout = DEFAULT_LAYOUT) -> bool:
    """A journal note the daylog may create (filed in its folder)."""
    return layout.is_journal_path(rel)


def is_journal_root_path(rel: str, layout: Layout = DEFAULT_LAYOUT) -> bool:
    """A live daily or weekly note: appended to, never created."""
    return layout.is_journal_root_path(rel)


# ------------------------------------------------------------ templates

_PLACEHOLDER_RE = re.compile(r"\{\{\s*(date|time|title)(?:\s*:\s*([^}]*?))?\s*\}\}")
_TEMPLATER_RE = re.compile(r"<%[\s\S]*?%>")


def render_template(
    text: str, *, title: str, when: datetime, layout: Layout = DEFAULT_LAYOUT
) -> str:
    """Obsidian's core-template placeholders, rendered: `{{date:FMT}}`,
    `{{date}}`, `{{time}}` and `{{title}}`. A Templater tag (`<% … %>`) is
    dropped — the agent never executes one, and never writes one into a note
    either."""
    text = _TEMPLATER_RE.sub("", text)

    def sub(m: re.Match) -> str:
        kind, fmt = m.group(1), (m.group(2) or "").strip()
        if kind == "title":
            return title
        if kind == "time":
            return layout.render(fmt or "HH:mm", when)
        return layout.render(fmt or "YYYY-MM-DD", when)

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
    template  `daily` / `weekly` / `monthly`: render the layout's template for
              the kind when the note does not exist yet
    title     `# <title>` for a new note that has no template
    when      the date a template's placeholders are rendered for
    journal   the path is a journal note (only the daylog writes those)
    alt_rel   a live journal note that takes the write instead of `rel` when
              it already exists; never created
    also_rels other notes where the marker counts as written — the paths a
              previous layout gave the same entry — never written to
    section   heading texts, in order of preference (`("Review", "Month
              Review")`): insert a tagged bullet block at the end of the first
              such section of the note, at any heading level, instead of
              appending at the end
    label     the block's title after the tag (`day log`)
    agent     the agent writing the block; its id fills the layout's `{agent}`
    layout    the vault layout the append was built for: the gate, the
              template, the block's tag and indent all follow it
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
    also_rels: tuple[str, ...] = ()
    section: tuple[str, ...] = ()
    label: str = ""
    agent: str = ""
    layout: Layout = DEFAULT_LAYOUT


def check_path(rel: str, *, journal: bool = False, layout: Layout = DEFAULT_LAYOUT) -> str:
    """`rel` if the agent may write it, else NotesPathError. Nothing is
    normalised away: a path that needs normalising is refused."""
    if not rel or "\x00" in rel or "\\" in rel or rel.startswith("/"):
        raise NotesPathError(f"not a vault path: {rel!r}")
    parts = rel.split("/")
    if any(p in ("", ".", "..") or p.startswith(".") for p in parts):
        raise NotesPathError(f"not a vault path: {rel!r}")
    if not rel.endswith(".md"):
        raise NotesPathError(f"only markdown notes can be written: {rel!r}")
    if parts[0] == layout.agent_dir and len(parts) > 1:
        return rel
    if journal and is_journal_path(rel, layout):
        return rel
    raise NotesPathError(
        f"notes may only be written under {layout.agent_dir}/ "
        f"(and the daylog's journal notes): {rel!r}"
    )


def _section(ap: Append) -> str:
    body = clean_body(ap.body)
    if not ap.heading:
        return f"{body} {marker(ap.key)}\n"
    return f"\n## {ap.heading}\n{marker(ap.key)}\n\n{body}\n"


_HEADING_RE = re.compile(r"^#{1,6} ")


def _is_boundary(line: str, rule_or_fence: bool = True) -> bool:
    """A line that ends a section: the next heading and, unless the layout
    says otherwise, a `---` rule or a code fence."""
    if _HEADING_RE.match(line):
        return True
    s = line.strip()
    return rule_or_fence and (s == "---" or s.startswith("```"))


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
MAX_OUTLINE_DEPTH = DEFAULT_LAYOUT.max_outline_depth


def _indent_levels(line: str, space_width: int = 2) -> int:
    """Leading indentation in outline levels: a tab, or `space_width` spaces,
    each."""
    lead = line[: len(line) - len(line.lstrip(" \t"))]
    return lead.count("\t") + lead.count(" ") // max(1, space_width)


def body_outline(body: str, max_depth: int = MAX_OUTLINE_DEPTH) -> list[tuple[int, str]]:
    """`body` as outline nodes `(depth, text)`, depth 1 sitting directly under
    the tagged bullet. Both kinds of daylog text go through here:

    * an LLM narrative — prose paragraphs split by blank lines — gives one
      depth-1 node per paragraph, its wrapped lines joined with a space;
    * the deterministic fallback (`daylog._format_daylog_fallback`) keeps its
      outline: a `Label:` line, then its items indented under it.

    Every non-blank line is a node, except that consecutive plain unindented
    lines join into one; a blank line, a label, a list item or an indented
    line ends that join. A plain unindented line ending in `:` is a label.
    Depth is 1 plus the line's indent (a tab or two spaces a level), at most
    one deeper than the node before and never past `max_depth`. A
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
            nodes.append((min(1 + levels, prev + 1, max_depth), text))
    flush()
    return nodes


def journal_block(
    key: str, label: str, body: str, layout: Layout = DEFAULT_LAYOUT, agent: str = ""
) -> str:
    """The agent's entry in the user's own bullet style (they write `- `
    bullets under the section, with obsidian-outliner)::

        - #aegis/sebas day log %% aegis:<key> %%
        <TAB>- <first paragraph, as one line>
        <TAB>- Completed:
        <TAB><TAB>- <an item under that label>

    The tag and the indent are the layout's (`entry.tag`, `entry.indent`); the
    body is laid out by :func:`body_outline`. The marker is an Obsidian
    comment, so reading view shows the tag, the label and the outline."""
    title = " ".join(
        p for p in (layout.tag_for(agent), clean_body(label).replace("\n", " ")) if p
    )
    head = f"- {title} {marker(key)}" if title else f"- {marker(key)}"
    rows = [head] + [
        layout.indent_text * d + f"- {t}"
        for d, t in body_outline(body, layout.max_outline_depth)
    ]
    return "\n".join(rows) + "\n"


def _section_end(lines: list[str], names: tuple[str, ...], rule_or_fence: bool = True) -> int | None:
    """The index just after the last non-blank line of the section (just after
    its heading when it is empty), or None when the note has no such heading.
    The section ends before the next heading and, by default, a `---` rule or
    a code fence — the monthly note's `ccard` fence included."""
    i = _find_section(lines, names)
    if i is None:
        return None
    last = i
    for j in range(i + 1, len(lines)):
        if _is_boundary(lines[j], rule_or_fence):
            break
        if lines[j].strip():
            last = j
    return last + 1


def insert_block(
    text: str, names: tuple[str, ...], block: str, rule_or_fence: bool = True
) -> str:
    """`text` with `block` inserted at the end of the section — after its last
    non-blank line, before the next heading (`---` or code fence) — or, when
    the note has none of `names`, a `## <first name>` heading and the block at
    the end. Nothing that is there moves or changes."""
    lines = text.splitlines(keepends=True)
    at = _section_end(lines, names, rule_or_fence)
    if at is None:
        base = text if not text or text.endswith("\n") else text + "\n"
        return f"{base}\n## {names[0]}\n{block}"
    before = "".join(lines[:at])
    if before and not before.endswith("\n"):
        before += "\n"
    return before + block + "".join(lines[at:])


def drop_placeholders(text: str, names: tuple[str, ...], rule_or_fence: bool = True) -> str:
    """A new note's target section without its empty `- ` placeholder bullets:
    the agent fills that section, and a lone `- ` left above the entry would
    be an empty bullet in the note. Every other line of the template stays."""
    lines = text.splitlines(keepends=True)
    i = _find_section(lines, names)
    if i is None:
        return text
    j = i + 1
    while j < len(lines) and not _is_boundary(lines[j], rule_or_fence):
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
    otherwise — and `_apply` checks that again before writing."""
    if not _KEY_RE.match(ap.key):
        raise NotesError(f"bad marker key {ap.key!r}")
    if existing is not None and marker(ap.key) in existing:
        return None
    base = existing if existing is not None else new_note
    if ap.section:
        return insert_block(
            base,
            ap.section,
            journal_block(ap.key, ap.label, ap.body, ap.layout, ap.agent),
            ap.layout.section_ends_at_rule_or_fence,
        )
    if base and not base.endswith("\n"):
        base += "\n"
    return base + _section(ap)


# An unticked task line: `- [ ] …` (or a `*`/`+` bullet), indented or not.
_OPEN_TASK_RE = re.compile(r"^[ \t]*[-*+] \[ \][^\n]*(?:\n|$)", re.M)


def drop_open_tasks(text: str) -> str:
    """`text` without its unticked task lines. A vault's templates carry
    prompts for the person filling the day in ("- [ ] #admin Plan the day").
    In a note the agent creates they are to-dos nobody can do: the day has
    already passed, and obsidian-checklist-plugin lists every open box in the
    vault (a backfill once added 125 of them). Ticked boxes, headings and
    everything else in the template stay."""
    return _OPEN_TASK_RE.sub("", text)


def _new_note_text(cfg: NotesConfig, ap: Append) -> str:
    """What a note starts with when the agent creates it: the vault's own
    template for a journal note (read at write time, so edits to the template
    apply) without its open tasks, else a `# title` line. A note that already
    exists is never touched here — only the text of a brand-new note."""
    stem = Path(ap.rel).stem
    layout = ap.layout
    if ap.template:
        template = layout.kind(ap.template).template
        tpl = cfg.path / template if template else None
        if tpl is not None and tpl.is_file():
            when = ap.when or datetime.now()
            rendered = render_template(tpl.read_text("utf-8"), title=stem, when=when, layout=layout)
            if layout.drop_open_tasks:
                rendered = drop_open_tasks(rendered)
            if ap.section and layout.drop_empty_bullets_in_section:
                rendered = drop_placeholders(
                    rendered, ap.section, layout.section_ends_at_rule_or_fence
                )
            return rendered
        return f"# {stem}\n"
    title = clean_body(ap.title or stem).replace("\n", " ")
    return f"# {title}\n"


# ---------------------------------------------------------------- git


def _env(cfg: NotesConfig) -> dict[str, str]:
    author = getattr(cfg, "author", None) or DEFAULT_AUTHOR
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": author.name,
        "GIT_AUTHOR_EMAIL": author.email,
        "GIT_COMMITTER_NAME": author.name,
        "GIT_COMMITTER_EMAIL": author.email,
    }
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
        logger.warning("notes_exclude_write_failed", error=error_text(exc))


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
        logger.warning("notes_revert_failed", error=error_text(exc))


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
    changed)`. The marker in ANY of its places means the write is already
    done — the user may have filed the root note since the agent wrote into
    it, or the entry may sit where a previous layout put it (`also_rels`)."""
    rel = _target_rel(cfg, ap)
    for other in (ap.rel, ap.alt_rel, *ap.also_rels):
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


def write_sync(
    cfg: NotesConfig, appends: list[Append], summary: str, *, author: Author | None = None
) -> dict:
    """Apply `appends` in one commit and push it, as `cfg.author` (or
    `author`).

    `{"status": "written" | "exists", "paths", "changed", "outcomes",
    "attempts"}` — `outcomes` is one `{"path", "changed"}` per append, in
    order, `path` being the note it actually went to.
    Raises NotesDisabled, NotesPathError, NotesConflict or NotesError; after
    any raise the checkout is exactly upstream again.
    """
    if not cfg.configured:
        raise NotesDisabled("the vault is not configured (notes_repo_url and notes_deploy_key)")
    if author is not None and author != cfg.author:
        cfg = replace(cfg, author=author)
    for ap in appends:
        check_path(ap.rel, journal=ap.journal, layout=ap.layout)
        if ap.alt_rel and not (ap.journal and is_journal_root_path(ap.alt_rel, ap.layout)):
            raise NotesPathError(f"not a live journal note: {ap.alt_rel!r}")
        if ap.template and ap.template not in KINDS:
            raise NotesError(f"unknown template {ap.template!r}")
    # Everything a write here could touch, so a failed one is undone whole.
    touchable = list(dict.fromkeys(p for ap in appends for p in (ap.rel, ap.alt_rel) if p))
    summary = clean_body(summary).replace("\n", " ")[:120] or f"{cfg.author.prefix}: notes"
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


async def write(
    cfg: NotesConfig, appends: list[Append], summary: str, *, author: Author | None = None
) -> dict:
    async with _ASYNC_LOCK:
        return await asyncio.to_thread(write_sync, cfg, appends, summary, author=author)


# --------------------------------------------------------------- reads


def _safe_read_path(rel: str) -> str:
    """Any note in the vault may be READ (the user's decision on #514), but
    only a plain relative `.md` path inside it."""
    try:
        return vl.safe_note_path(rel)
    except ValueError as exc:
        raise NotesPathError(str(exc)) from None


def _pull_quietly(cfg: NotesConfig) -> None:
    """A read uses the freshest vault it can get, and the local copy when the
    pull fails — an unreachable GitHub must not stop the agent reading."""
    try:
        _pull(cfg)
    except NotesError as exc:
        logger.warning("notes_pull_failed", error=error_text(exc))


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
                    logger.warning("notes_read_failed", path=rel, error=error_text(exc))
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


def journal_paths(kind: str, d: date, layout: Layout = DEFAULT_LAYOUT) -> list[str]:
    """Every note the entry for `kind` on `d` may sit in: the filed note and
    the live one, under this layout and the one before it. Deduplicated, in
    that order."""
    out: list[str] = []
    for lay in (layout, layout.previous):
        if lay is None or not lay.kind(kind).enabled:
            continue
        out.append(lay.note_path(kind, d))
        if lay.root_path(kind, d):
            out.append(lay.root_path(kind, d))
    return list(dict.fromkeys(out))


def read_journal_days_sync(
    cfg: NotesConfig, days: list[date], layout: Layout = DEFAULT_LAYOUT
) -> dict[str, str]:
    """`{YYYY-MM-DD: note text}` for the days that have a journal note: the
    filed one, the live one (where periodic-notes makes today's note before
    the user files it), or both, joined — the day is whatever either holds. A
    note a previous layout filed is read too."""
    by_rel: dict[str, str] = {}
    for d in days:
        for rel in journal_paths("daily", d, layout):
            by_rel[rel] = d.isoformat()
    found = read_many_sync(cfg, list(by_rel), pull=True)
    out: dict[str, str] = {}
    for rel, text in found.items():
        day = by_rel[rel]
        out[day] = f"{out[day]}\n\n{text}" if day in out else text
    return out


# --------------------------------------------------------------- index


def is_indexable(rel: str, layout: Layout = DEFAULT_LAYOUT) -> bool:
    return layout.is_indexable(rel)


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


def _ls_files(cfg: NotesConfig, layout: Layout) -> list[str]:
    out = _run(["git", "ls-files", "-z"], cfg).stdout
    return sorted(p for p in out.split("\0") if p and is_indexable(p, layout))


def vault_changes_sync(
    cfg: NotesConfig, since: str | None, layout: Layout = DEFAULT_LAYOUT
) -> VaultChanges:
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
            return VaultChanges(head=head, changed=_ls_files(cfg, layout), full=True)
        diff = _run(
            ["git", "diff", "--name-status", "--no-renames", "-z", since, head], cfg
        ).stdout.split("\0")
        changed: list[str] = []
        deleted: list[str] = []
        for status, rel in zip(diff[0::2], diff[1::2], strict=False):
            if not rel or not is_indexable(rel, layout):
                continue
            (deleted if status.startswith("D") else changed).append(rel)
        return VaultChanges(head=head, changed=sorted(changed), deleted=sorted(deleted))


# ------------------------------------------------------ what writes what


def journal_key(kind: str, label: str) -> str:
    """The marker the daylog and the backfill share, so a backfilled day and a
    live one are the same write and never appear twice."""
    return f"daylog:{label}" if kind == "daily" else f"daylog:{kind}:{label}"


def journal_append(
    kind: str,
    day: date,
    label: str,
    body: str,
    now: datetime,
    layout: Layout = DEFAULT_LAYOUT,
    agent: str = "",
) -> Append:
    """The daylog's entry for one day, week or month.

    kind   `daily` / `weekly` / `monthly`
    day    the day; the week's first day; the month's first day
    label  the daylog's own label: `2026-09-12`, `2026-W37`, `2026-09`
    agent  the agent the entry is written by; its id fills the tag's `{agent}`

    Raises `JournalKindDisabled` when the layout has the kind switched off.
    """
    if kind not in KINDS:
        raise NotesError(f"unknown journal kind {kind!r}")
    k = layout.kind(kind)
    if not k.enabled:
        raise JournalKindDisabled(f"{kind} journal notes are switched off in the vault layout")
    rel, alt = layout.note_path(kind, day), layout.root_path(kind, day)
    # A weekly note is named from its week's first day, so its template's
    # dates are that day's too (`W40 Oct 23` opens `# Oct 02, 2023`).
    when = datetime.combine(layout.anchor(kind, day), now.time())
    prev = layout.previous
    also = tuple(
        p for p in (journal_paths(kind, day, prev) if prev is not None else ())
        if p not in (rel, alt)
    )
    return Append(
        rel=rel,
        key=journal_key(kind, label),
        body=body,
        template=kind,
        when=when,
        journal=True,
        alt_rel=alt,
        also_rels=also,
        section=k.sections,
        label=k.label,
        agent=agent,
        layout=layout,
    )


def _slug(text: str, limit: int = 60) -> str:
    """A file-name slug that keeps letters in any script: `¿Qué es RAG?` →
    `qué-es-rag`, `什么是RAG` → `什么是rag`. Only punctuation, symbols and
    whitespace become hyphens."""
    norm = unicodedata.normalize("NFKC", text or "").casefold()
    base = re.sub(r"[^\w]+|_+", "-", norm).strip("-")
    return (base or "question")[:limit].strip("-") or "question"


def question_append(
    question: str, report: str, asked: datetime, layout: Layout = DEFAULT_LAYOUT
) -> Append:
    """A research answer as `<questions_dir>/<slug>-<hash>.md`. The marker is
    keyed on the question AND the answer: the same answer is never appended
    twice, and a different one later is a new dated section, headed by the
    layout's `date_heading_format`."""
    from aegis.services.research import normalise_question

    norm = normalise_question(question)
    qh = hashlib.sha256(norm.encode()).hexdigest()[:10]
    ah = hashlib.sha256((report or "").encode()).hexdigest()[:10]
    return Append(
        rel=f"{layout.questions_dir.strip('/')}/{_slug(norm)}-{qh}.md",
        key=f"question:{qh}:{ah}",
        body=report,
        heading=layout.render(layout.date_heading_format, asked),
        title=question.strip()[:200],
        layout=layout,
    )
