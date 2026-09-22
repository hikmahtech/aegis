"""The vault's layout — where the journal notes go and how an entry looks.

DB-owned, so a fork ships nobody's folder names. Stored in the ``settings``
table under ``vault_layout``; the code defaults are one particular vault's
conventions (the ones AEGIS was first written against) except the tag, which
was ``#raphael`` and is now ``#aegis/{agent}``, so a deployment with no row
behaves as before apart from the tag on new blocks — old blocks keep theirs
and still read, because every reader finds a block by its marker. Edited on the
admin **Vault** page through ``GET/PUT /api/admin/notes/layout``
(``routes/notes_admin.py``).

What is configurable here: the agent's own folder, the journal's folders, file
names (moment.js formats, as Obsidian's periodic-notes settings use), templates,
section headings and labels, the week rule, the outline's tag and indent, the
index's skip list and the wording of the deterministic day log. What stays in
code, deliberately: insert-only, the marker format, the path safety refusals,
pushed-only, the encrypted-block stripping — those are rules, not preferences.

``merge`` (read) is lenient and ``validate`` (write) is strict, as for
``email_rules.py``: a bad row must never stop the daylog writing, but a typo
saved through the admin API must not silently move the journal. A layout the
user changed keeps the one before it as ``previous``: the writer treats the old
paths as "already written" too, so a re-run or a backfill after a change never
writes a day twice, and nothing is ever moved.

The date machinery lives here too (``moment_format``, the locale table, the
week rule) because it IS layout: a file name is a format, and a week is a rule.
``notes.py`` re-exports what its callers use.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any

import structlog

from aegis.agent_tags import BEHAVIOR_TAGS
from aegis.errors import error_text
from aegis.services.settings_store import get_setting, put_setting

logger = structlog.get_logger()

SETTINGS_KEY = "vault_layout"
KINDS = ("daily", "weekly", "monthly")
WEEK_STARTS = ("monday", "sunday")
WEEK_NUMBERINGS = ("iso", "locale_us")
INDENTS = ("tab", "two_spaces", "four_spaces")
_INDENT_TEXT = {"tab": "\t", "two_spaces": "  ", "four_spaces": "    "}

# ------------------------------------------------------------------ locales


@dataclass(frozen=True)
class Locale:
    """Month and day names for `MMM`/`MMMM`/`ddd`/`dddd`. `en` is what ships;
    a second locale is one more entry in `LOCALES`."""

    months: tuple[str, ...]
    months_long: tuple[str, ...]
    days: tuple[str, ...]
    days_long: tuple[str, ...]


LOCALES: dict[str, Locale] = {
    "en": Locale(
        months=("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
        months_long=(
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ),
        days=("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
        days_long=("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
    ),
}

# ------------------------------------------------------------------ language

# The wording of what the daylog writes when there is no model, and the titles
# the rollups use. `name` is the language the two daylog prompts are asked to
# write in; English (the default) adds nothing to today's prompts.
DEFAULT_LANGUAGE: dict[str, str] = {
    "name": "English",
    "daylog_title": "Day log for {date}.",
    "quiet_day": "Quiet day — nothing was recorded.",
    "meetings": "Met / attended:",
    "tasks": "Completed:",
    "decisions": "Decided:",
    "captures": "Captured / clarified:",
    "email": "Email filed:",
    "failures": "Broke:",
    "rollup_header": "{period} log {label} — {n} day(s) recorded.",
    "journal_title": "Journal {day}",
    "also_in_note": "Also in the note:",
    # The label on a block that is not the day log; one `<slot>_label` per slot
    # (`Layout.label_for`), so a new slot is a word here, not a code change.
    "review_label": "weekly review",
    "selfreport_label": "in my words",
}

# ------------------------------------------------------------ the record

# The owner's record (vault record spec §4): one flat folder of notes, one per
# domain, which each agent's `user` document is compiled from while
# `enabled`. A draft the seed writes is `<name>.draft.md` in the same folder:
# never compiled, never indexed.
DRAFT_SUFFIX = ".draft.md"
RECORD_MAX_CHARS = (500, 50_000)
_ROOT_PROBES = (date(2001, 1, 1), date(2099, 12, 31))


@dataclass(frozen=True)
class RecordLayout:
    enabled: bool = False
    dir: str = "me"
    shared: tuple[str, ...] = ("about",)
    # Capability tag → note names. Sorted pairs, so the layout stays hashable.
    by_tag: tuple[tuple[str, tuple[str, ...]], ...] = ()
    max_chars: int = 6000

    def names_for(self, tag: str) -> tuple[str, ...]:
        return dict(self.by_tag).get(tag, ())

    def claimed(self) -> frozenset[str]:
        """Every note the map names: the shared ones and every tag's."""
        return frozenset(self.shared) | {n for _, names in self.by_tag for n in names}

    def note_path(self, name: str) -> str:
        return f"{self.dir}/{name}.md"

    def draft_path(self, name: str) -> str:
        return f"{self.dir}/{name}{DRAFT_SUFFIX}"

    def is_record_path(self, rel: str) -> bool:
        """`<dir>/<name>.md` and nothing deeper."""
        parts = rel.split("/")
        return len(parts) == 2 and parts[0] == self.dir and parts[1].endswith(".md") and len(parts[1]) > 3


# ------------------------------------------------------------------ the agent

# The one placeholder `entry.tag` takes: the id of the agent whose block it is.
# `#aegis/{agent}` is a nested Obsidian tag, so `#aegis` finds or hides
# everything AEGIS wrote, whoever keeps the journal next year.
AGENT_PLACEHOLDER = "{agent}"


def agent_slug(agent_id: str | None) -> str:
    """An agent id as a tag or an address may carry it: lower case, letters,
    digits, `.`, `_` and `-`. `notes.author_for` uses this same one, so the
    tag on a block and the author of its commit cannot disagree."""
    return re.sub(r"[^a-z0-9._-]+", "-", (agent_id or "").strip().lower()).strip("-")


# ------------------------------------------------------------------ defaults

DEFAULTS: dict[str, Any] = {
    "agent_dir": "raphael",
    "questions_dir": "raphael/questions",
    "locale": "en",
    "week_start": "monday",
    "week_numbering": "iso",
    "date_heading_format": "YYYY-MM-DD",
    "index_skip_prefixes": [".obsidian/", "_templates/", "backups/", "_attachments/", ".trash/"],
    "entry": {"tag": "#aegis/{agent}", "indent": "tab", "max_outline_depth": 4},
    "new_note": {"drop_open_tasks": True, "drop_empty_bullets_in_section": True},
    "section_ends_at_rule_or_fence": True,
    "language": dict(DEFAULT_LANGUAGE),
    "daily": {
        "enabled": True,
        "folder": "[journal/]YYYY/MM[. ]MMM",
        "format": "DD MMM YY",
        "live_folder": "journal",
        "template": "_templates/{{tp_title_today}}.md",
        "sections": ["Journal"],
        "label": "day log",
    },
    "weekly": {
        "enabled": True,
        "folder": "[journal/]YYYY/MM[. ]MMM",
        "format": "[W]ww MMM YY",
        "live_folder": "journal",
        "template": "_templates/weekly-{{tp_title_today}}.md",
        "sections": ["Review"],
        "label": "week in review",
    },
    "monthly": {
        "enabled": True,
        "folder": "[journal/]YYYY/MM[. ]MMM",
        "format": "MM[. ]MMM",
        "live_folder": "",
        "template": "_templates/monthly.md",
        "sections": ["Review", "Month Review"],
        "label": "month in review",
    },
    "record": {"enabled": False, "dir": "me", "shared": ["about"], "by_tag": {}, "max_chars": 6000},
}

# ------------------------------------------------------------ moment format

# Longest first, so `MMMM` is never read as `MM` + `MM`.
TOKENS = (
    "YYYY", "MMMM", "dddd", "MMM", "ddd", "YY", "MM", "DD", "Do", "HH", "hh", "mm", "ss",
    "ww", "M", "D", "H", "h", "w", "A", "a",
)
_DAY_TOKENS = {"D", "DD", "Do"}
_MONTH_OR_YEAR_TOKENS = {"M", "MM", "MMM", "MMMM", "YY", "YYYY"}
_WEEK_TOKENS = {"w", "ww"}


def tokenize(fmt: str) -> list[tuple[str, str]]:
    """`fmt` as `("token", name)` / `("literal", text)` pairs: `[...]` is
    literal text, a known token is a token, anything else is copied."""
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(fmt):
        if fmt[i] == "[":
            close = fmt.find("]", i + 1)
            if close > 0:
                out.append(("literal", fmt[i + 1 : close]))
                i = close + 1
                continue
        for tok in TOKENS:
            if fmt.startswith(tok, i):
                out.append(("token", tok))
                i += len(tok)
                break
        else:
            out.append(("literal", fmt[i]))
            i += 1
    return out


def format_tokens(fmt: str) -> set[str]:
    return {name for kind, name in tokenize(fmt) if kind == "token"}


def _ordinal(n: int) -> str:
    """moment's `Do`: 1st, 2nd, 3rd, 4th … 11th, 12th, 13th … 21st, 22nd."""
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


# ----------------------------------------------------------------- weeks


def _week_params(week_start: str, week_numbering: str) -> tuple[int, int]:
    """moment's `(dow, doy)` for the rule: `dow` is the first day of the week
    (0 Sunday, 1 Monday) and `doy` fixes week 1 — `4` is the ISO rule (the
    week holding January 4th), `6` the US one (the week holding January 1st).
    Monday + ISO is `en-gb`, Sunday + US is `en`."""
    dow = 1 if week_start == "monday" else 0
    doy = 4 if week_numbering == "iso" else 6
    return dow, doy


def week_start_of(d: date, week_start: str = "monday") -> date:
    """The first day of `d`'s week."""
    dow = 1 if week_start == "monday" else 0
    # Python: Monday 0 … Sunday 6; moment: Sunday 0 … Saturday 6.
    moment_day = (d.weekday() + 1) % 7
    return d - timedelta(days=(moment_day - dow) % 7)


def _first_week_offset(year: int, dow: int, doy: int) -> int:
    fwd = 7 + dow - doy
    fwdlw = (7 + (date(year, 1, fwd).weekday() + 1) % 7 - dow) % 7
    return -fwdlw + fwd - 1


def _weeks_in_year(year: int, dow: int, doy: int) -> int:
    days = 366 if _is_leap(year) else 365
    return (days - _first_week_offset(year, dow, doy) + _first_week_offset(year + 1, dow, doy)) // 7


def _is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def week_of(d: date, week_start: str = "monday", week_numbering: str = "iso") -> tuple[int, int]:
    """`(week_year, week)` of `d` under the rule — moment's `weekOfYear`. With
    the defaults it is `d.isocalendar()[:2]`: Mon 29 Dec 2025 is week 1 of
    2026, and Sun 3 Jan 2027 is week 53 of 2026."""
    dow, doy = _week_params(week_start, week_numbering)
    day_of_year = d.timetuple().tm_yday
    week = (day_of_year - _first_week_offset(d.year, dow, doy) - 1) // 7 + 1
    if week < 1:
        year = d.year - 1
        return year, week + _weeks_in_year(year, dow, doy)
    if week > _weeks_in_year(d.year, dow, doy):
        return d.year + 1, week - _weeks_in_year(d.year, dow, doy)
    return d.year, week


def week_bounds(
    d: date, week_start: str = "monday", week_numbering: str = "iso"
) -> tuple[date, date, str]:
    """`(first day, last day, label)` of the week holding `d`; the label is
    `2026-W37`, the daylog's own, which is also the entry's marker key."""
    start = week_start_of(d, week_start)
    year, week = week_of(d, week_start, week_numbering)
    return start, start + timedelta(days=6), f"{year}-W{week:02d}"


# ---------------------------------------------------------------- render


def moment_format(
    fmt: str,
    when: date | datetime,
    *,
    locale: str = "en",
    week_start: str = "monday",
    week_numbering: str = "iso",
) -> str:
    """A small moment.js `format()`: the tokens Obsidian's date settings use,
    and `[...]` for literal text. Anything unrecognised is copied as is."""
    if not isinstance(when, datetime):
        when = datetime(when.year, when.month, when.day)
    loc = LOCALES.get(locale) or LOCALES["en"]
    _, week = week_of(when.date(), week_start, week_numbering)
    values = {
        "YYYY": f"{when.year:04d}",
        "YY": f"{when.year % 100:02d}",
        "MMMM": loc.months_long[when.month - 1],
        "MMM": loc.months[when.month - 1],
        "MM": f"{when.month:02d}",
        "M": str(when.month),
        "DD": f"{when.day:02d}",
        "Do": _ordinal(when.day),
        "D": str(when.day),
        "dddd": loc.days_long[when.weekday()],
        "ddd": loc.days[when.weekday()],
        "HH": f"{when.hour:02d}",
        "H": str(when.hour),
        "hh": f"{(when.hour % 12) or 12:02d}",
        "h": str((when.hour % 12) or 12),
        "mm": f"{when.minute:02d}",
        "ss": f"{when.second:02d}",
        "ww": f"{week:02d}",
        "w": str(week),
        "A": "AM" if when.hour < 12 else "PM",
        "a": "am" if when.hour < 12 else "pm",
    }
    return "".join(values[name] if kind == "token" else name for kind, name in tokenize(fmt))


def _alternation(names: tuple[str, ...]) -> str:
    return "(?:" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)) + ")"


def format_regex(fmt: str, loc: Locale, seen: dict[str, int]) -> str:
    """The regex a rendered `fmt` matches. Literals are escaped; a token is a
    class; month and day names come from the locale. The first time a token
    appears it is a named group and every later time a back-reference, so a
    path that renders the same token twice (`MM` in the month folder and in
    the month note's own name) must agree with itself — which is what tells
    `journal/2026/09. Sep/08. Aug.md` from a real month note."""
    classes = {
        "YYYY": r"\d{4}", "YY": r"\d{2}",
        "MMMM": _alternation(loc.months_long), "MMM": _alternation(loc.months),
        "MM": r"\d{2}", "M": r"\d{1,2}",
        "DD": r"\d{2}", "Do": r"\d{1,2}(?:st|nd|rd|th)", "D": r"\d{1,2}",
        "dddd": _alternation(loc.days_long), "ddd": _alternation(loc.days),
        "HH": r"\d{2}", "H": r"\d{1,2}", "hh": r"\d{2}", "h": r"\d{1,2}",
        "mm": r"\d{2}", "ss": r"\d{2}", "ww": r"\d{2}", "w": r"\d{1,2}",
        "A": "(?:AM|PM)", "a": "(?:am|pm)",
    }
    out: list[str] = []
    for kind, name in tokenize(fmt):
        if kind == "literal":
            out.append(re.escape(name))
        elif name in seen:
            out.append(f"(?P=t{seen[name]})")
        else:
            seen[name] = len(seen)
            out.append(f"(?P<t{seen[name]}>{classes[name]})")
    return "".join(out)


# ---------------------------------------------------------------- layout


@dataclass(frozen=True)
class KindLayout:
    enabled: bool
    folder: str
    format: str
    live_folder: str
    template: str
    sections: tuple[str, ...]
    label: str


@dataclass(frozen=True)
class Layout:
    """The effective layout, as `notes.py` and the worker use it. Frozen and
    hashable, so the path patterns derived from it can be cached."""

    agent_dir: str = "raphael"
    questions_dir: str = "raphael/questions"
    locale: str = "en"
    week_start: str = "monday"
    week_numbering: str = "iso"
    date_heading_format: str = "YYYY-MM-DD"
    index_skip_prefixes: tuple[str, ...] = tuple(DEFAULTS["index_skip_prefixes"])
    entry_tag: str = "#aegis/{agent}"
    entry_indent: str = "tab"
    max_outline_depth: int = 4
    drop_open_tasks: bool = True
    drop_empty_bullets_in_section: bool = True
    section_ends_at_rule_or_fence: bool = True
    language: tuple[tuple[str, str], ...] = tuple(DEFAULT_LANGUAGE.items())
    daily: KindLayout = field(default_factory=lambda: _kind_from(DEFAULTS["daily"]))
    weekly: KindLayout = field(default_factory=lambda: _kind_from(DEFAULTS["weekly"]))
    monthly: KindLayout = field(default_factory=lambda: _kind_from(DEFAULTS["monthly"]))
    record: RecordLayout = field(default_factory=RecordLayout)
    # The layout before this one, when the user changed it: its paths still
    # count as written. One step back only.
    previous: Layout | None = None

    def kind(self, kind: str) -> KindLayout:
        if kind not in KINDS:
            raise ValueError(f"unknown journal kind {kind!r}")
        return getattr(self, kind)

    @property
    def indent_text(self) -> str:
        return _INDENT_TEXT.get(self.entry_indent, "\t")

    @property
    def indent_width(self) -> int:
        """Spaces per outline level when reading a block back; a tab is one level."""
        return 4 if self.entry_indent == "four_spaces" else 2

    def tag_for(self, agent: str = "") -> str:
        """`entry_tag` with `{agent}` filled in from the agent writing the
        block. With no agent the placeholder and the `/` before it drop, so
        `#aegis/{agent}` is `#aegis` — a real tag, not a literal brace in the
        user's note. A tag without the placeholder is returned as it is."""
        if AGENT_PLACEHOLDER not in self.entry_tag:
            return self.entry_tag
        aid = agent_slug(agent)
        if aid:
            return self.entry_tag.replace(AGENT_PLACEHOLDER, aid)
        bare = self.entry_tag.replace(AGENT_PLACEHOLDER, "").rstrip("/")
        return "" if bare == "#" else bare

    def label_for(self, slot: str, kind: str) -> str:
        """The block's label after the tag. The day log and the rollups use
        their kind's own label; anything else filed in the same note (the
        weekly review, a self-report, the week's dues) is named by its slot's
        wording, and by the slot itself when the wording has no word for it
        yet."""
        if not slot:
            return self.kind(kind).label
        return self.words.get(f"{slot}_label") or slot

    @property
    def words(self) -> dict[str, str]:
        return dict(self.language)

    def word(self, key: str) -> str:
        return self.words.get(key) or DEFAULT_LANGUAGE.get(key, "")

    @property
    def locale_table(self) -> Locale:
        return LOCALES.get(self.locale) or LOCALES["en"]

    def render(self, fmt: str, when: date | datetime) -> str:
        return moment_format(
            fmt, when, locale=self.locale, week_start=self.week_start,
            week_numbering=self.week_numbering,
        )

    def anchor(self, kind: str, d: date) -> date:
        """The day a kind's folder and name are rendered for: the day, the
        week's first day, the month's first day."""
        if kind == "weekly":
            return week_start_of(d, self.week_start)
        if kind == "monthly":
            return d.replace(day=1)
        return d

    def note_path(self, kind: str, d: date) -> str:
        """The filed note for `kind` on `d`: `<folder>/<name>.md`."""
        k = self.kind(kind)
        when = self.anchor(kind, d)
        folder = self.render(k.folder, when).strip("/")
        stem = self.render(k.format, when)
        return f"{folder}/{stem}.md" if folder else f"{stem}.md"

    def root_path(self, kind: str, d: date) -> str:
        """The live periodic-notes note, if the kind has a live folder, else ``."""
        k = self.kind(kind)
        if not k.live_folder:
            return ""
        when = self.anchor(kind, d)
        return f"{k.live_folder.strip('/')}/{self.render(k.format, when)}.md"

    def _pattern(self, folder: str, fmt: str) -> re.Pattern:
        seen: dict[str, int] = {}
        loc = self.locale_table
        head = format_regex(folder.strip("/"), loc, seen) if folder.strip("/") else ""
        stem = format_regex(fmt, loc, seen)
        return re.compile(f"^{head}/{stem}\\.md$" if head else f"^{stem}\\.md$")

    def journal_patterns(self) -> tuple[tuple[re.Pattern, ...], tuple[re.Pattern, ...]]:
        """`(filed, live)` regexes, one per enabled kind, generated from the
        layout — never a second hand-typed copy of the paths."""
        return _patterns(self)

    def is_journal_path(self, rel: str) -> bool:
        """A journal note the daylog may create (filed in its folder)."""
        return any(r.match(rel) for r in self.journal_patterns()[0])

    def is_journal_root_path(self, rel: str) -> bool:
        """A live note the daylog appends to when it exists, never creates."""
        return any(r.match(rel) for r in self.journal_patterns()[1])

    def journal_roots(self) -> tuple[str, ...]:
        """The top folders the journal lives under, when the layout fixes one
        (`journal` by default): a kind's folder whose first segment renders the
        same for any year, and every live folder. The layout before this one
        counts too. What the interests seed must never read (spec §11)."""
        roots: set[str] = set()
        for lay in (self, self.previous):
            if lay is None:
                continue
            for kind in KINDS:
                k = lay.kind(kind)
                tops = {lay.render(k.folder, d).strip("/").split("/")[0] for d in _ROOT_PROBES}
                if len(tops) == 1 and "" not in tops:
                    roots |= tops
                if k.live_folder.strip("/"):
                    roots.add(k.live_folder.strip("/").split("/")[0])
        return tuple(sorted(roots))

    def is_journal_area(self, rel: str) -> bool:
        return (
            rel.split("/", 1)[0] in self.journal_roots()
            or self.is_journal_path(rel)
            or self.is_journal_root_path(rel)
        )

    def is_indexable(self, rel: str) -> bool:
        if not rel.endswith(".md") or rel.startswith(self.index_skip_prefixes):
            return False
        # The record folder is compiled into the prompts; indexing it too would
        # put the same text in a prompt twice (spec §5). Drafts live there too.
        if rel.startswith(f"{self.record.dir}/"):
            return False
        return not any(part.startswith(".") for part in rel.split("/"))


_PATTERN_CACHE: dict[Layout, tuple[tuple[re.Pattern, ...], tuple[re.Pattern, ...]]] = {}


def _patterns(layout: Layout) -> tuple[tuple[re.Pattern, ...], tuple[re.Pattern, ...]]:
    key = _without_previous(layout)
    hit = _PATTERN_CACHE.get(key)
    if hit is None:
        filed: list[re.Pattern] = []
        live: list[re.Pattern] = []
        for kind in KINDS:
            k = layout.kind(kind)
            if not k.enabled:
                continue
            filed.append(layout._pattern(k.folder, k.format))
            if k.live_folder:
                live.append(layout._pattern(f"[{k.live_folder.strip('/')}]", k.format))
        hit = (tuple(filed), tuple(live))
        if len(_PATTERN_CACHE) > 64:  # pragma: no cover — a bounded memo, not a store
            _PATTERN_CACHE.clear()
        _PATTERN_CACHE[key] = hit
    return hit


def _without_previous(layout: Layout) -> Layout:
    return layout if layout.previous is None else replace(layout, previous=None)


def _kind_from(v: dict) -> KindLayout:
    return KindLayout(
        enabled=bool(v["enabled"]),
        folder=str(v["folder"]),
        format=str(v["format"]),
        live_folder=str(v["live_folder"]),
        template=str(v["template"]),
        sections=tuple(str(s) for s in v["sections"]),
        label=str(v["label"]),
    )


def _record_from(r: dict) -> RecordLayout:
    return RecordLayout(
        enabled=bool(r["enabled"]),
        dir=str(r["dir"]),
        shared=tuple(r["shared"]),
        by_tag=tuple((t, tuple(names)) for t, names in sorted(r["by_tag"].items())),
        max_chars=int(r["max_chars"]),
    )


def layout_from(value: Any) -> Layout:
    """A `Layout` from a stored row (merged over the defaults, leniently)."""
    v = merge(value)
    prev = v.get("previous")
    return Layout(
        agent_dir=v["agent_dir"],
        questions_dir=v["questions_dir"],
        locale=v["locale"],
        week_start=v["week_start"],
        week_numbering=v["week_numbering"],
        date_heading_format=v["date_heading_format"],
        index_skip_prefixes=tuple(v["index_skip_prefixes"]),
        entry_tag=v["entry"]["tag"],
        entry_indent=v["entry"]["indent"],
        max_outline_depth=int(v["entry"]["max_outline_depth"]),
        drop_open_tasks=bool(v["new_note"]["drop_open_tasks"]),
        drop_empty_bullets_in_section=bool(v["new_note"]["drop_empty_bullets_in_section"]),
        section_ends_at_rule_or_fence=bool(v["section_ends_at_rule_or_fence"]),
        language=tuple(v["language"].items()),
        daily=_kind_from(v["daily"]),
        weekly=_kind_from(v["weekly"]),
        monthly=_kind_from(v["monthly"]),
        record=_record_from(v["record"]),
        previous=layout_from({**prev, "previous": None}) if isinstance(prev, dict) else None,
    )


def layout_to_dict(layout: Layout) -> dict:
    """The stored shape of a `Layout` (without `previous`)."""
    return {
        "agent_dir": layout.agent_dir,
        "questions_dir": layout.questions_dir,
        "locale": layout.locale,
        "week_start": layout.week_start,
        "week_numbering": layout.week_numbering,
        "date_heading_format": layout.date_heading_format,
        "index_skip_prefixes": list(layout.index_skip_prefixes),
        "entry": {
            "tag": layout.entry_tag,
            "indent": layout.entry_indent,
            "max_outline_depth": layout.max_outline_depth,
        },
        "new_note": {
            "drop_open_tasks": layout.drop_open_tasks,
            "drop_empty_bullets_in_section": layout.drop_empty_bullets_in_section,
        },
        "section_ends_at_rule_or_fence": layout.section_ends_at_rule_or_fence,
        "language": dict(layout.language),
        **{
            kind: {
                "enabled": k.enabled,
                "folder": k.folder,
                "format": k.format,
                "live_folder": k.live_folder,
                "template": k.template,
                "sections": list(k.sections),
                "label": k.label,
            }
            for kind in KINDS
            for k in (layout.kind(kind),)
        },
        "record": {
            "enabled": layout.record.enabled,
            "dir": layout.record.dir,
            "shared": list(layout.record.shared),
            "by_tag": {t: list(names) for t, names in layout.record.by_tag},
            "max_chars": layout.record.max_chars,
        },
    }


# ----------------------------------------------------------------- merge


def _warn(key: str, why: str) -> None:
    logger.warning("vault_layout_key_ignored", key=key, reason=why)


def _str_or(v: dict, key: str, default: str, path: str) -> str:
    x = v.get(key, default)
    if isinstance(x, str):
        return x
    _warn(f"{path}{key}", "not a string")
    return default


def _bool_or(v: dict, key: str, default: bool, path: str) -> bool:
    x = v.get(key, default)
    if isinstance(x, bool):
        return x
    _warn(f"{path}{key}", "not true/false")
    return default


def _str_list_or(v: dict, key: str, default: list[str], path: str) -> list[str]:
    x = v.get(key, default)
    if isinstance(x, list) and all(isinstance(s, str) for s in x):
        return list(x)
    _warn(f"{path}{key}", "not a list of strings")
    return list(default)


def merge(value: Any) -> dict:
    """A stored (possibly partial) row merged over the defaults. Never raises:
    a key of the wrong shape is logged and read as its default, because the
    daylog must keep writing whatever a hand edit of the row did to it."""
    v = value if isinstance(value, dict) else {}
    if value is not None and not isinstance(value, dict):
        _warn(SETTINGS_KEY, "not an object")
    out: dict[str, Any] = {
        "agent_dir": _str_or(v, "agent_dir", DEFAULTS["agent_dir"], ""),
        "questions_dir": _str_or(v, "questions_dir", DEFAULTS["questions_dir"], ""),
        "date_heading_format": _str_or(
            v, "date_heading_format", DEFAULTS["date_heading_format"], ""
        ),
        "index_skip_prefixes": _str_list_or(
            v, "index_skip_prefixes", DEFAULTS["index_skip_prefixes"], ""
        ),
        "section_ends_at_rule_or_fence": _bool_or(
            v, "section_ends_at_rule_or_fence", DEFAULTS["section_ends_at_rule_or_fence"], ""
        ),
    }
    for key, vocab in (
        ("locale", tuple(LOCALES)),
        ("week_start", WEEK_STARTS),
        ("week_numbering", WEEK_NUMBERINGS),
    ):
        x = _str_or(v, key, DEFAULTS[key], "")
        if x not in vocab:
            _warn(key, f"not one of {', '.join(vocab)}")
            x = DEFAULTS[key]
        out[key] = x
    entry = v.get("entry") if isinstance(v.get("entry"), dict) else {}
    indent = _str_or(entry, "indent", DEFAULTS["entry"]["indent"], "entry.")
    if indent not in INDENTS:
        _warn("entry.indent", f"not one of {', '.join(INDENTS)}")
        indent = DEFAULTS["entry"]["indent"]
    depth = entry.get("max_outline_depth", DEFAULTS["entry"]["max_outline_depth"])
    if not isinstance(depth, int) or isinstance(depth, bool) or not 1 <= depth <= 10:
        _warn("entry.max_outline_depth", "not a whole number from 1 to 10")
        depth = DEFAULTS["entry"]["max_outline_depth"]
    out["entry"] = {
        "tag": _str_or(entry, "tag", DEFAULTS["entry"]["tag"], "entry."),
        "indent": indent,
        "max_outline_depth": depth,
    }
    new_note = v.get("new_note") if isinstance(v.get("new_note"), dict) else {}
    out["new_note"] = {
        k: _bool_or(new_note, k, DEFAULTS["new_note"][k], "new_note.")
        for k in DEFAULTS["new_note"]
    }
    lang = v.get("language") if isinstance(v.get("language"), dict) else {}
    out["language"] = {k: _str_or(lang, k, DEFAULT_LANGUAGE[k], "language.") for k in DEFAULT_LANGUAGE}
    for kind in KINDS:
        k = v.get(kind) if isinstance(v.get(kind), dict) else {}
        d = DEFAULTS[kind]
        out[kind] = {
            "enabled": _bool_or(k, "enabled", d["enabled"], f"{kind}."),
            "folder": _str_or(k, "folder", d["folder"], f"{kind}."),
            "format": _str_or(k, "format", d["format"], f"{kind}."),
            "live_folder": _str_or(k, "live_folder", d["live_folder"], f"{kind}."),
            "template": _str_or(k, "template", d["template"], f"{kind}."),
            "sections": _str_list_or(k, "sections", d["sections"], f"{kind}.") or list(d["sections"]),
            "label": _str_or(k, "label", d["label"], f"{kind}."),
        }
    out["record"] = _merge_record(v.get("record"))
    prev = v.get("previous")
    if isinstance(prev, dict):
        out["previous"] = {k: x for k, x in merge({**prev, "previous": None}).items() if k != "previous"}
    return out


def _merge_record(raw: Any) -> dict:
    d = DEFAULTS["record"]
    if raw is not None and not isinstance(raw, dict):
        _warn("record", "not an object")
    rec = raw if isinstance(raw, dict) else {}
    by_tag: dict[str, list[str]] = {}
    raw_map = rec.get("by_tag", {})
    if isinstance(raw_map, dict):
        for tag, names in raw_map.items():
            if isinstance(tag, str) and isinstance(names, list) and all(isinstance(n, str) for n in names):
                by_tag[tag] = list(names)
            else:
                _warn(f"record.by_tag.{tag}", "not a list of note names")
    else:
        _warn("record.by_tag", "not an object")
    cap = rec.get("max_chars", d["max_chars"])
    if not isinstance(cap, int) or isinstance(cap, bool) or not RECORD_MAX_CHARS[0] <= cap <= RECORD_MAX_CHARS[1]:
        _warn("record.max_chars", "not a whole number from 500 to 50000")
        cap = d["max_chars"]
    return {
        "enabled": _bool_or(rec, "enabled", d["enabled"], "record."),
        "dir": _str_or(rec, "dir", d["dir"], "record.") or d["dir"],
        "shared": _str_list_or(rec, "shared", d["shared"], "record."),
        "by_tag": by_tag,
        "max_chars": cap,
    }


DEFAULT_LAYOUT = layout_from({})


# -------------------------------------------------------------- validate

_SAMPLE_DATES = (date(2026, 1, 1), date(2026, 2, 28), date(2026, 9, 12), date(2026, 12, 31))


def _check_segment(seg: str, what: str) -> None:
    if not seg or seg in (".", "..") or seg.startswith("."):
        raise ValueError(f"{what}: {seg!r} is not a plain folder name")
    if "/" in seg or "\\" in seg or "\x00" in seg or seg != seg.strip():
        raise ValueError(f"{what}: {seg!r} is not a plain folder name")


def _check_relative(path: str, what: str, *, allow_empty: bool) -> None:
    if not path:
        if allow_empty:
            return
        raise ValueError(f"{what}: must not be empty")
    if path.startswith("/") or "\\" in path or "\x00" in path:
        raise ValueError(f"{what}: {path!r} must be a relative path inside the vault")
    for seg in path.strip("/").split("/"):
        _check_segment(seg, what)


def safe_note_path(rel: str, what: str = "path") -> str:
    """A plain relative `.md` path inside the vault, or ValueError — the rule
    `notes._safe_read_path` enforces for reads and a template must pass."""
    if not rel or "\x00" in rel or "\\" in rel or rel.startswith("/"):
        raise ValueError(f"{what}: {rel!r} is not a vault path")
    if any(p in ("", ".", "..") for p in rel.split("/")) or not rel.endswith(".md"):
        raise ValueError(f"{what}: {rel!r} is not a vault note")
    if rel.startswith(".obsidian/"):
        raise ValueError(f"{what}: {rel!r} is not a vault note")
    return rel


def _check_format(fmt: str, what: str, layout: Layout) -> None:
    if not fmt.strip():
        raise ValueError(f"{what}: must not be empty")
    for d in _SAMPLE_DATES:
        stem = layout.render(fmt, d)
        if not stem.strip() or stem != stem.strip():
            raise ValueError(f"{what}: {fmt!r} renders to an empty name")
        if "/" in stem or "\\" in stem or "\x00" in stem:
            raise ValueError(f"{what}: {fmt!r} renders a name with a slash in it")
        if stem in (".", "..") or stem.startswith("."):
            raise ValueError(f"{what}: {fmt!r} renders a hidden name")


def _check_folder(fmt: str, what: str, layout: Layout) -> None:
    if "\\" in fmt or "\x00" in fmt or fmt.startswith("/"):
        raise ValueError(f"{what}: {fmt!r} must be a relative path inside the vault")
    for d in _SAMPLE_DATES:
        _check_relative(layout.render(fmt, d).strip("/"), what, allow_empty=True)


def validate(value: Any) -> dict:
    """Strict counterpart to ``merge`` for the WRITE path: the merged row with
    every rule checked. Raises ValueError naming the first bad key."""
    if value is not None and not isinstance(value, dict):
        raise ValueError("vault_layout must be an object")
    v = dict(value or {})
    v.pop("previous", None)
    _check_shape(v)
    out = merge(v)
    layout = layout_from(out)

    _check_segment(out["agent_dir"], "agent_dir")
    _check_relative(out["questions_dir"], "questions_dir", allow_empty=False)
    if not (
        out["questions_dir"] == out["agent_dir"]
        or out["questions_dir"].startswith(out["agent_dir"] + "/")
    ):
        raise ValueError(f"questions_dir: must be inside {out['agent_dir']}/ (the agent's folder)")
    rec = out["record"]
    _check_segment(rec["dir"], "record.dir")
    if rec["dir"] == out["agent_dir"]:
        raise ValueError("record.dir: must not be the agent's own folder")
    if rec["dir"] in layout.journal_roots():
        raise ValueError("record.dir: must not be a journal folder")
    for name in [*rec["shared"], *(n for names in rec["by_tag"].values() for n in names)]:
        _check_note_name(name)
    if not out["date_heading_format"].strip() or not any(
        layout.render(out["date_heading_format"], d).strip() for d in _SAMPLE_DATES
    ):
        raise ValueError("date_heading_format: must render to a heading")
    for p in out["index_skip_prefixes"]:
        if not p.strip() or p.startswith("/") or "\\" in p:
            raise ValueError(f"index_skip_prefixes: {p!r} is not a relative prefix")
    tag = out["entry"]["tag"]
    if tag and (not tag.startswith("#") or len(tag) < 2 or any(c.isspace() for c in tag)):
        raise ValueError("entry.tag: must be empty or `#` followed by a word with no spaces")
    stems: dict[str, str] = {}
    for kind in KINDS:
        k = out[kind]
        _check_folder(k["folder"], f"{kind}.folder", layout)
        _check_format(k["format"], f"{kind}.format", layout)
        if k["live_folder"]:
            _check_relative(k["live_folder"], f"{kind}.live_folder", allow_empty=False)
        if k["template"]:
            try:
                safe_note_path(k["template"], f"{kind}.template")
            except ValueError as exc:
                raise ValueError(str(exc)) from None
        if not k["sections"] or any(not s.strip() for s in k["sections"]):
            raise ValueError(f"{kind}.sections: needs at least one heading, none empty")
        stems[kind] = layout.render(k["format"], date(2026, 9, 1))
    daily_tokens = format_tokens(out["daily"]["format"])
    if not daily_tokens & _DAY_TOKENS or not daily_tokens & _MONTH_OR_YEAR_TOKENS:
        raise ValueError("daily.format: needs a day token (D, DD, Do) and a month or year token")
    if not format_tokens(out["weekly"]["format"]) & (_WEEK_TOKENS | _DAY_TOKENS):
        raise ValueError("weekly.format: needs a week token (w, ww) or a day token")
    if stems["monthly"] == stems["daily"]:
        raise ValueError("monthly.format: renders the same name as daily.format for the same day")
    return out


def _check_note_name(name: str) -> None:
    _check_segment(name, "record note name")
    if name.endswith(".md") or name.endswith(".draft"):
        raise ValueError(f"record note name: {name!r} — give the name without .md, and not a draft")


_SHAPE: dict[str, type | tuple[type, ...]] = {
    "agent_dir": str, "questions_dir": str, "locale": str, "week_start": str,
    "week_numbering": str, "date_heading_format": str, "index_skip_prefixes": list,
    "entry": dict, "new_note": dict, "section_ends_at_rule_or_fence": bool,
    "language": dict, "daily": dict, "weekly": dict, "monthly": dict, "record": dict,
}
_KIND_SHAPE: dict[str, type | tuple[type, ...]] = {
    "enabled": bool, "folder": str, "format": str, "live_folder": str,
    "template": str, "sections": list, "label": str,
}
_RECORD_SHAPE: dict[str, type] = {
    "enabled": bool, "dir": str, "shared": list, "by_tag": dict, "max_chars": int,
}


def _check_shape(v: dict) -> None:
    """What `merge` would silently default, `validate` refuses: an unknown key,
    a wrong type, a value outside its vocabulary."""
    for key in v:
        if key not in _SHAPE:
            raise ValueError(f"{key}: not a vault_layout key")
    for key, typ in _SHAPE.items():
        if key in v and (not isinstance(v[key], typ) or (typ is not bool and isinstance(v[key], bool))):
            raise ValueError(f"{key}: wrong type")
    if v.get("locale") is not None and v["locale"] not in LOCALES:
        raise ValueError(f"locale: must be one of {', '.join(LOCALES)}")
    if v.get("week_start") is not None and v["week_start"] not in WEEK_STARTS:
        raise ValueError(f"week_start: must be one of {', '.join(WEEK_STARTS)}")
    if v.get("week_numbering") is not None and v["week_numbering"] not in WEEK_NUMBERINGS:
        raise ValueError(f"week_numbering: must be one of {', '.join(WEEK_NUMBERINGS)}")
    if "index_skip_prefixes" in v and not all(isinstance(s, str) for s in v["index_skip_prefixes"]):
        raise ValueError("index_skip_prefixes: must be a list of strings")
    entry = v.get("entry") or {}
    for key in entry:
        if key not in DEFAULTS["entry"]:
            raise ValueError(f"entry.{key}: not a vault_layout key")
    if "tag" in entry and not isinstance(entry["tag"], str):
        raise ValueError("entry.tag: must be a string")
    if "indent" in entry and entry["indent"] not in INDENTS:
        raise ValueError(f"entry.indent: must be one of {', '.join(INDENTS)}")
    depth = entry.get("max_outline_depth")
    if depth is not None and (not isinstance(depth, int) or isinstance(depth, bool) or not 1 <= depth <= 10):
        raise ValueError("entry.max_outline_depth: must be a whole number from 1 to 10")
    for key, x in (v.get("new_note") or {}).items():
        if key not in DEFAULTS["new_note"]:
            raise ValueError(f"new_note.{key}: not a vault_layout key")
        if not isinstance(x, bool):
            raise ValueError(f"new_note.{key}: must be true or false")
    for key, x in (v.get("language") or {}).items():
        if key not in DEFAULT_LANGUAGE:
            raise ValueError(f"language.{key}: not a vault_layout key")
        if not isinstance(x, str) or not x.strip():
            raise ValueError(f"language.{key}: must be a non-empty string")
    for kind in KINDS:
        k = v.get(kind) or {}
        for key, x in k.items():
            if key not in _KIND_SHAPE:
                raise ValueError(f"{kind}.{key}: not a vault_layout key")
            typ = _KIND_SHAPE[key]
            if not isinstance(x, typ) or (typ is not bool and isinstance(x, bool)):
                raise ValueError(f"{kind}.{key}: wrong type")
        if "sections" in k and (
            not k["sections"] or not all(isinstance(s, str) for s in k["sections"])
        ):
            raise ValueError(f"{kind}.sections: needs at least one heading, none empty")
    rec = v.get("record") or {}
    for key, x in rec.items():
        if key not in _RECORD_SHAPE:
            raise ValueError(f"record.{key}: not a vault_layout key")
        typ = _RECORD_SHAPE[key]
        if not isinstance(x, typ) or (typ is not bool and isinstance(x, bool)):
            raise ValueError(f"record.{key}: wrong type")
    if "shared" in rec and not all(isinstance(n, str) for n in rec["shared"]):
        raise ValueError("record.shared: must be a list of note names")
    for tag, names in (rec.get("by_tag") or {}).items():
        if tag not in BEHAVIOR_TAGS:
            raise ValueError(f"record.by_tag.{tag}: not a capability tag ({', '.join(BEHAVIOR_TAGS)})")
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise ValueError(f"record.by_tag.{tag}: must be a list of note names")
    cap = rec.get("max_chars")
    if cap is not None and not RECORD_MAX_CHARS[0] <= cap <= RECORD_MAX_CHARS[1]:
        raise ValueError("record.max_chars: must be a whole number from 500 to 50000")


# --------------------------------------------------------------- storage

CACHE_SECONDS = 30.0
_cache: dict[str, Any] = {"layout": None, "ts": 0.0}


def invalidate_cache() -> None:
    _cache.update(layout=None, ts=0.0)


async def get_layout_value(pool: Any) -> dict:
    """The merged row as the admin API returns it (with `previous` when set)."""
    return merge(await get_setting(pool, SETTINGS_KEY) or {})


async def get_layout(pool: Any) -> Layout:
    """The effective layout, DB-first, cached `CACHE_SECONDS`. Never raises:
    no pool, no row or a failed read all give the defaults, because a config
    read must never take the daylog down."""
    if pool is None:
        return DEFAULT_LAYOUT
    now = time.monotonic()
    if _cache["layout"] is not None and now - _cache["ts"] < CACHE_SECONDS:
        return _cache["layout"]
    try:
        layout = layout_from(await get_layout_value(pool))
    except Exception as exc:  # noqa: BLE001 — never break a caller on a config read
        logger.warning("vault_layout_read_failed", error=error_text(exc))
        return DEFAULT_LAYOUT
    _cache.update(layout=layout, ts=now)
    return layout


def _same(a: dict, b: dict) -> bool:
    """Same journal layout. `record` is left out: it moves no journal note,
    and rotating `previous` for it would forget the layout before a real
    change."""
    skip = ("previous", "record")
    return {k: v for k, v in a.items() if k not in skip} == {
        k: v for k, v in b.items() if k not in skip
    }


async def save_layout(pool: Any, value: Any) -> dict:
    """Validate then persist. The layout in force before a change is kept as
    `previous`, so the writer still recognises a day written under it. A save
    that changes nothing keeps whatever `previous` was there."""
    normalised = validate(value)
    current = await get_layout_value(pool)
    if _same(normalised, current):
        if "previous" in current:
            normalised["previous"] = current["previous"]
    else:
        normalised["previous"] = {k: v for k, v in current.items() if k != "previous"}
    await put_setting(pool, SETTINGS_KEY, normalised)
    invalidate_cache()
    return await get_layout_value(pool)


# --------------------------------------------------------------- preview


def preview(layout: Layout, d: date) -> dict:
    """What the layout renders for `d`: the filed and live note of each kind,
    the week's bounds and label. Computed here, by the real code, so the admin
    page shows what the writer will do and not a reimplementation."""
    start, end, label = week_bounds(d, layout.week_start, layout.week_numbering)
    return {
        "date": d.isoformat(),
        "week": {"start": start.isoformat(), "end": end.isoformat(), "label": label},
        "heading": layout.render(layout.date_heading_format, d),
        **{
            kind: {
                "enabled": layout.kind(kind).enabled,
                "path": layout.note_path(kind, d),
                "live_path": layout.root_path(kind, d),
                "template": layout.kind(kind).template,
                "sections": list(layout.kind(kind).sections),
            }
            for kind in KINDS
        },
    }
