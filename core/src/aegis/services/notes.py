"""Raphael's notes: the Obsidian vault is the record (#514).

Spec: `docs/superpowers/specs/2026-09-12-raphael-notes-design.md`.

This is the ONLY module that writes the vault. Everything else — the daylog, the
research lane, the `note_write`/`note_link` chat tools through `notes_write.py`
— hands it an :class:`Append` and gets back what happened. Four rules hold here
and every caller relies on them:

* **Append-only.** A write creates a note or appends a section at its end. It
  never rewrites, reorders or deletes a line the user wrote. Each section
  carries a hidden Obsidian comment marker (`%% aegis:<key> %%`), and a write
  whose marker is already in the note is a no-op — so a re-run or a retry can
  neither duplicate a section nor change one.
* **Only where Raphael may write.** Anything under `raphael/`, and the journal
  notes the daylog owns. Every other path is refused before git is touched.
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
    """The body of the section a write appended under `key`, and the note
    without that section. `("", text)` when the marker is not in the note.

    A section is `## heading`, the marker line, then its body, up to the next
    `## ` heading, the next aegis marker, or the end of the note — the shape
    `_section` writes."""
    mark = marker(key)
    at = text.find(mark)
    if at < 0:
        return "", text
    line_start = text.rfind("\n", 0, at) + 1
    start = line_start
    if line_start > 0:
        prev_start = text.rfind("\n", 0, line_start - 1) + 1
        if text[prev_start:line_start].startswith("## "):
            start = prev_start
    body_start = at + len(mark)
    ends = [i for i in (text.find("\n## ", body_start), text.find("%% aegis:", body_start)) if i >= 0]
    end = min(ends) if ends else len(text)
    return text[body_start:end].strip(), (text[:start] + text[end:]).strip()


# ------------------------------------------------------ dates and names

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_MONTHS_LONG = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_DAYS_LONG = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def week_start(d: date) -> date:
    """The Sunday that starts `d`'s week (moment's `en` locale)."""
    return d - timedelta(days=(d.weekday() + 1) % 7)


def locale_week(d: date) -> int:
    """moment's `ww`: weeks start on Sunday and week 1 holds 1 January. The
    week belongs to the year its Saturday falls in, so the last days of
    December can be week 1 of the next year."""
    ws = week_start(d)
    first = week_start(date((ws + timedelta(days=6)).year, 1, 1))
    return (ws - first).days // 7 + 1


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


def daily_note_path(d: date) -> str:
    return f"journal/{moment_format(DAILY_FORMAT, d)}.md"


def weekly_note_path(d: date) -> str:
    """The vault's weekly note for the week holding `d`, named from its Sunday
    as periodic-notes names it. The daylog passes its ISO week's Monday, so a
    Monday-to-Sunday rollup lands in the note holding six of its seven days."""
    return f"journal/{moment_format(WEEKLY_FORMAT, week_start(d))}.md"


def monthly_note_path(d: date) -> str:
    """`MM. MMM` has no year, so a note at the journal root would collide
    every year. The month goes in a year folder, as old notes are filed."""
    return f"journal/{d.year:04d}/{moment_format(MONTHLY_FORMAT, d)}.md"


_JOURNAL_RES = (
    re.compile(r"^journal/\d{2} [A-Z][a-z]{2} \d{2}\.md$"),
    re.compile(r"^journal/W\d{2} [A-Z][a-z]{2} \d{2}\.md$"),
    re.compile(r"^journal/\d{4}/\d{2}\. [A-Z][a-z]{2}\.md$"),
)


def is_journal_path(rel: str) -> bool:
    return any(r.match(rel) for r in _JOURNAL_RES)


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
    """One append-only write.

    rel       path inside the vault
    key       the marker; a note that already carries it is left alone
    body      the text to append
    heading   `## <heading>` before the body; empty = one inline line whose
              marker sits at its end (a link)
    template  `daily` / `weekly` / `monthly`: render the vault's template when
              the note does not exist yet
    title     `# <title>` for a new note that has no template
    when      the date a template's placeholders are rendered for
    journal   the path is a journal note (only the daylog writes those)
    """

    rel: str
    key: str
    body: str
    heading: str = ""
    template: str = ""
    title: str = ""
    when: datetime | None = None
    journal: bool = False


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


def append_text(existing: str | None, ap: Append, new_note: str = "") -> str | None:
    """The note's new text, or None when its marker is already there. The
    result always STARTS with `existing` — that is the append-only rule, and
    `_apply` checks it again before writing."""
    if not _KEY_RE.match(ap.key):
        raise NotesError(f"bad marker key {ap.key!r}")
    if existing is not None and marker(ap.key) in existing:
        return None
    base = existing if existing is not None else new_note
    if base and not base.endswith("\n"):
        base += "\n"
    return base + _section(ap)


def _new_note_text(cfg: NotesConfig, ap: Append) -> str:
    """What a note starts with when Raphael creates it: the vault's own
    template for a journal note (read at write time, so edits to the template
    apply), else a `# title` line."""
    stem = Path(ap.rel).stem
    if ap.template:
        tpl = cfg.path / TEMPLATES[ap.template]
        if tpl.is_file():
            when = ap.when or datetime.now()
            return render_template(tpl.read_text("utf-8"), title=stem, when=when)
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


def _run(
    args: list[str], cfg: NotesConfig, *, timeout: int = 60, check: bool = True
) -> subprocess.CompletedProcess:
    try:
        proc = books._spawn(args, cwd=str(cfg.path), timeout=timeout, env=_env(cfg))
    except books.BooksError as exc:
        raise NotesError(str(exc)) from exc
    if check and proc.returncode != 0:
        raise NotesError(f"{' '.join(args[:2])} failed: {proc.stderr.strip()[:500]}")
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
        raise NotesConflict(f"git pull failed: {proc.stderr.strip()[:300]}")


def _apply(cfg: NotesConfig, ap: Append) -> bool:
    """Apply one append to the working copy. True when the note changed."""
    target = cfg.path / ap.rel
    existing = target.read_text("utf-8") if target.exists() else None
    new_note = _new_note_text(cfg, ap) if existing is None else ""
    text = append_text(existing, ap, new_note)
    if text is None:
        return False
    if existing is not None and not text.startswith(existing):  # pragma: no cover — invariant
        raise NotesError(f"refusing a write that would change existing text in {ap.rel}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, "utf-8")
    return True


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
        raise NotesConflict(f"git push was rejected: {proc.stderr.strip()[:300]}")


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

    `{"status": "written" | "exists", "paths", "changed", "attempts"}`.
    Raises NotesDisabled, NotesPathError, NotesConflict or NotesError; after
    any raise the checkout is exactly upstream again.
    """
    if not cfg.configured:
        raise NotesDisabled("the vault is not configured (notes_repo_url and notes_deploy_key)")
    for ap in appends:
        check_path(ap.rel, journal=ap.journal)
        if ap.template and ap.template not in TEMPLATES:
            raise NotesError(f"unknown template {ap.template!r}")
    paths = list(dict.fromkeys(ap.rel for ap in appends))
    summary = clean_body(summary).replace("\n", " ")[:120] or "raphael: notes"
    with _Lock(cfg):
        _ensure_checkout(cfg)
        last = ""
        for attempt in (1, 2):
            try:
                _pull(cfg)
                changed = list(dict.fromkeys(ap.rel for ap in appends if _apply(cfg, ap)))
                if not changed:
                    return {"status": "exists", "paths": paths, "changed": [], "attempts": attempt}
                _commit(cfg, summary, changed)
                _push(cfg)
                return {"status": "written", "paths": paths, "changed": changed, "attempts": attempt}
            except NotesConflict as exc:
                last = str(exc)
                logger.warning("notes_write_conflict", attempt=attempt, error=last[:200])
                _drop_local(cfg, paths)
            except Exception:
                _drop_local(cfg, paths)
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
    """`{YYYY-MM-DD: note text}` for the days that have a journal note."""
    by_rel = {daily_note_path(d): d.isoformat() for d in days}
    found = read_many_sync(cfg, list(by_rel), pull=True)
    return {by_rel[rel]: text for rel, text in found.items()}


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
        rel = daily_note_path(day)
    elif kind == "weekly":
        rel = weekly_note_path(day)
    elif kind == "monthly":
        rel = monthly_note_path(day)
    else:
        raise NotesError(f"unknown journal kind {kind!r}")
    # A weekly note is named from its week's Sunday, so its template's dates
    # are that Sunday's too, not the ISO Monday the daylog passes in.
    when = datetime.combine(week_start(day) if kind == "weekly" else day, now.time())
    return Append(
        rel=rel,
        key=journal_key(kind, label),
        body=body,
        heading="Raphael",
        template=kind,
        when=when,
        journal=True,
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
