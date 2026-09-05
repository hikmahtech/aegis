"""The books: journal helpers, rules, git sync and the hledger runner.

Spec: docs/superpowers/specs/2026-09-05-maou-books-design.md §1, §4, §5.
This module is shared by core (tools) and worker (flows). Amounts are
`Decimal` major units; `render_amount` is the journal form (no grouping),
`fmt_money` the human form.
"""

from __future__ import annotations

import asyncio
import base64
import fcntl
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

import structlog
import yaml

from aegis.api.models.money import MoneyEvent
from aegis.services.money_format import fmt_money  # noqa: F401 — re-export (spec §5.1)

logger = structlog.get_logger()

_SYMBOL = {"INR": "₹", "USD": "$", "GBP": "£", "EUR": "€"}
_CENT = Decimal("0.01")

UNKNOWN = {
    "personal": {"out": "expenses:unknown", "in": "income:unknown"},
    "hikmah": {"out": "expenses:hikmah:unknown", "in": "income:hikmah:other"},
}

# spec §4 — category → account, per entity. Missing key ⇒ the unknown account.
_ACCOUNT_MAP: dict[str, dict[str, str]] = {
    "personal": {
        "saas": "expenses:saas",
        "media": "expenses:media",
        "infra": "expenses:saas",
        "internet": "expenses:utilities:internet",
        "electricity": "expenses:utilities:electricity",
        "mobile": "expenses:utilities:mobile",
        "groceries": "expenses:groceries",
        "food": "expenses:food",
        "transport": "expenses:transport",
        "shopping": "expenses:shopping",
        "health": "expenses:health",
        "insurance": "expenses:insurance",
        "fees": "expenses:fees:bank",
        "tax": "expenses:tax",
        "people": "expenses:people",
        "salary": "income:salary",
        "interest": "income:interest",
        "refund": "income:refunds",
    },
    "hikmah": {
        "saas": "expenses:hikmah:saas",
        "media": "expenses:hikmah:saas",
        "infra": "expenses:hikmah:infra",
        "internet": "expenses:hikmah:internet",
        "fees": "expenses:hikmah:fees:bank",
        "tax": "expenses:hikmah:tax",
        "professional": "expenses:hikmah:professional",
        "ads": "expenses:hikmah:ads",
    },
}

_INCOME_CATEGORIES = frozenset({"salary", "interest", "refund"})


def render_amount(amount: Decimal, currency: str, *, negative: bool = False) -> str:
    """Journal amount: symbol-prefixed, no digit grouping, ISO suffix otherwise.

    `amount` is a magnitude — the sign comes from `negative` alone, as in
    `fmt_money`, so a negative input never double-negates.
    """
    q = abs(Decimal(amount).quantize(_CENT))
    # Letters only, capped at 3. `currency` is model-supplied
    # (`_LLM_EVENT_FIELDS`) and lands on the posting line, so a newline in it
    # used to add an attacker-chosen comment line INSIDE a committed block.
    # `MoneyEvent` validates it too, but this module is public and a direct
    # caller never passes through the model — the writer is the last gate.
    code = re.sub(r"[^A-Za-z]", "", currency or "")[:3].upper()
    sign = "-" if negative else ""
    sym = _SYMBOL.get(code)
    return f"{sign}{sym}{q}" if sym else f"{sign}{q} {code}".strip()


def account_for(category: str | None, direction: str | None, entity: str) -> str:
    """Counter account for an event (spec §4). Unknown ⇒ the entity's unknown account."""
    ent = "hikmah" if entity == "hikmah" else "personal"
    side = "in" if direction == "in" or (category or "") in _INCOME_CATEGORIES else "out"
    if side == "in" and ent == "hikmah":
        return "income:hikmah:other"
    mapped = _ACCOUNT_MAP[ent].get((category or "").lower())
    if mapped and (mapped.startswith("income:") == (side == "in")):
        return mapped
    return UNKNOWN[ent][side]


def instrument_account(
    instrument: str | None, declared: set[str] | frozenset[str] = frozenset()
) -> str:
    """`hdfc-1225` → `assets:bank:hdfc:1225`, `axis-cc-1313` → `liabilities:card:axis:1313`,
    `card-1313` → the declared `liabilities:card:*:1313`, else `assets:unknown`."""
    if not instrument:
        return "assets:unknown"
    parts = instrument.lower().split("-")
    if len(parts) == 2 and parts[0] == "card":
        tail = parts[1]
        for acct in sorted(declared):
            if acct.startswith("liabilities:card:") and acct.endswith(f":{tail}"):
                return acct
        return "assets:unknown"
    if len(parts) == 3 and parts[1] == "cc":
        computed = f"liabilities:card:{parts[0]}:{parts[2]}"
    elif len(parts) == 2:
        computed = f"assets:bank:{parts[0]}:{parts[1]}"
    else:
        return "assets:unknown"
    # An empty `declared` means "no chart to check against" (unit tests);
    # with a chart, an undeclared instrument must not break `check --strict`.
    if declared and computed not in declared:
        return "assets:unknown"
    return computed


# ----------------------------------------------------------------- errors/config

class BooksError(Exception):
    """A books operation failed; the working copy is left clean."""


class BooksDisabled(BooksError):  # noqa: N818 — a state, not an error suffix (spec §5)
    """No `books_repo_url` and no checkout: the books are not configured."""


class BooksCheckError(BooksError):
    """`hledger check --strict` rejected a write; it was reverted."""


@dataclass(frozen=True)
class BooksConfig:
    path: Path
    repo_url: str = ""
    deploy_key: Path | None = None
    main: str = "main.journal"


def config_from_settings(settings) -> BooksConfig:
    key = Path(getattr(settings, "gmail_token_dir", "config/")) / "books_deploy_key"
    return BooksConfig(
        path=Path(getattr(settings, "books_path", "/app/config/books")),
        repo_url=getattr(settings, "books_repo_url", "") or "",
        deploy_key=key if key.exists() else None,
    )


def install_deploy_key(settings) -> Path | None:
    """Write `settings.books_deploy_key` (PEM, or base64 of PEM) to
    `<gmail_token_dir>/books_deploy_key` with mode 0600. Never logs the value."""
    raw = (getattr(settings, "books_deploy_key", "") or "").strip()
    if not raw:
        return None
    if "\n" not in raw:
        try:
            raw = base64.b64decode(raw, validate=True).decode("utf-8").strip()
        except Exception as exc:  # noqa: BLE001
            raise BooksError("books_deploy_key is neither PEM text nor base64 PEM") from exc
    path = Path(getattr(settings, "gmail_token_dir", "config/")) / "books_deploy_key"
    path.parent.mkdir(parents=True, exist_ok=True)
    # O_CREAT's mode applies only when the file is NEW, so this closes the window
    # where a fresh key file exists world-readable; the chmod then covers the
    # case where the path already existed with looser permissions.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(raw + "\n")
    path.chmod(0o600)
    return path


def parse_csv_set(raw: str) -> frozenset[str]:
    """`" a, b ,,c"` → `{"a", "b", "c"}`. Blank/None ⇒ empty."""
    return frozenset(s.strip() for s in (raw or "").split(",") if s.strip())


def parse_kv(raw: str) -> dict[str, str]:
    """`"personal=6h2f, hikmah = 6h2g"` → `{"personal": "6h2f", ...}`.

    Lenient by design: a malformed pair is dropped, never raised — these are
    admin-typed strings and a typo must not take a boot path down.
    """
    out: dict[str, str] = {}
    for part in (raw or "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip() and v.strip():
                out[k.strip()] = v.strip()
    return out


# ----------------------------------------------------------------- block grammar

_INDENT = "    "
_ACCOUNT_WIDTH = 40
_PAYEE_MAX = 80
_TAG_MAX = 80
_POSTING_RE = re.compile(r"^    (\S+)(?:\s{2,}(\S.*))?$")


# Every C0 control and DEL. Three of them each break a different thing: a
# newline starts a new line, a TAB is folded into an account name and makes the
# transaction unbalanced (measured on hledger 1.52.3 — it does NOT separate
# account from amount), and a NUL reaches subprocess as `ValueError: embedded
# null byte`, which is not an OSError and so escapes `_spawn`'s guard.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")


def sanitize_payee(payee: str) -> str:
    text = re.sub(r"[;|]+", " ", _CONTROL_RE.sub(" ", payee or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:_PAYEE_MAX] or "unknown"


def sanitize_tag(value: str | None) -> str:
    """One tag value, safe to interpolate into a `; k: v, k: v` line.

    EVERY value on a tag line goes through this, because some of them are
    model-supplied: `instrument` is in `_LLM_EVENT_FIELDS`, so a steered email
    can put anything in it, and an unsanitized newline forged a whole
    transaction — with its own `; msgid:` line, so `find_block` resolved it —
    that `hledger check --strict` accepted and nothing reverted.

    What it removes, measured against hledger 1.52.3: a newline (starts a new
    posting, comment or transaction), `;` (a new comment) and `,` (the tag
    separator, so a value carrying one declares a SECOND tag). A colon is left
    alone — hledger runs a value to the next comma or end of line, so `evil:`
    inside a value does not become a tag, and refs like `UTR:1234` stay whole.
    The cap bounds what a hostile value can push onto the line.
    """
    text = re.sub(r"[;,]+", " ", _CONTROL_RE.sub(" ", value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:_TAG_MAX].strip()


def _safe_account(account: str) -> str:
    """An account name that cannot break out of its posting line. hledger ends
    an account at two spaces, a `;` or the line, so any of those inside one
    would split the posting or open a new block; a control character breaks it
    differently (see `_CONTROL_RE`). Accounts are chart-checked before they get
    here; this is the writer refusing to be the only gate."""
    text = re.sub(r"[;]+", " ", _CONTROL_RE.sub(" ", account or ""))
    return re.sub(r"\s{2,}", " ", text).strip()


def _posting(account: str, amount: str = "") -> str:
    # hledger splits an account from its amount on TWO or more spaces, so an
    # account that padding would leave one space short of the column takes the
    # explicit two-space branch — at 39 chars `:<40` yields a single space and
    # hledger folds the amount into the account name.
    account = _safe_account(account)
    if not amount:
        return f"{_INDENT}{account}"
    if len(account) >= _ACCOUNT_WIDTH - 1:
        return f"{_INDENT}{account}  {amount}"
    return f"{_INDENT}{account:<{_ACCOUNT_WIDTH}}{amount}"


def render_transaction(
    event: MoneyEvent, counter_account: str, instrument_acct: str, msgid: str
) -> str:
    """One journal block (spec §1 grammar). Posting 1 = category with signed
    amount (+ out, − in), posting 2 = instrument, no amount."""
    if event.amount is None or not event.currency or event.occurred_on is None:
        raise BooksError("render_transaction needs amount, currency and occurred_on")
    # Sanitized, not trusted: `instrument` comes from the model, and `channel`
    # is only a validated Literal today — a tag line takes no raw value.
    tags = [f"channel: {sanitize_tag(event.channel)}"]
    for name, raw in (("ref", event.ref), ("instrument", event.instrument)):
        value = sanitize_tag(raw)
        if value:
            tags.append(f"{name}: {value}")
    amount = render_amount(event.amount, event.currency, negative=(event.direction == "in"))
    lines = [
        f"{event.occurred_on.isoformat()} * {sanitize_payee(event.payee)}",
        f"{_INDENT}; msgid: {msgid}",
        f"{_INDENT}; {', '.join(tags)}",
        _posting(counter_account, amount),
        _posting(instrument_acct),
    ]
    return "\n".join(lines) + "\n"


def render_manual(d: date, payee: str, postings: list[dict], msgid: str, note: str = "") -> str:
    """One hand-written block: the same header lines as `render_transaction`,
    then one posting line per entry (`{"account", "amount", "currency"}`).

    Two differences from `render_transaction`, both deliberate:

    A manual amount is SIGNED. `MoneyEvent` carries the sign in `direction`
    and `render_amount` takes a magnitude plus a flag, so handing it a
    negative straight through would render it positive — the block would still
    balance and still pass `check --strict`, with the money pointing the wrong
    way and no gate anywhere to catch it.

    `note` goes through `sanitize_tag`, not `sanitize_payee`: it lands on the
    TAG line, where a comma is the tag separator, so `note="x, evil: true"`
    would declare a second tag of the caller's choosing. `sanitize_payee`
    leaves commas alone (they are harmless in a payee).
    """
    tags = "channel: manual" + (f", note: {sanitize_tag(note)}" if note else "")
    lines = [
        f"{d.isoformat()} * {sanitize_payee(payee)}",
        f"{_INDENT}; msgid: {msgid}",
        f"{_INDENT}; {tags}",
    ]
    for p in postings:
        raw = p.get("amount")
        if raw in (None, ""):
            amount = ""
        else:
            value = Decimal(str(raw))
            amount = render_amount(value, p.get("currency") or "INR", negative=value < 0)
        lines.append(_posting(str(p.get("account") or ""), amount))
    return "\n".join(lines) + "\n"


def iter_blocks(text: str) -> list[tuple[int, int]]:
    """(start, end) offsets of every maximal run of non-blank lines."""
    blocks: list[tuple[int, int]] = []
    pos = 0
    start: int | None = None
    for line in text.splitlines(keepends=True):
        blank = not line.strip()
        if blank and start is not None:
            blocks.append((start, pos))
            start = None
        elif not blank and start is None:
            start = pos
        pos += len(line)
    if start is not None:
        blocks.append((start, pos))
    return blocks


def find_block(text: str, msgid: str) -> tuple[int, int] | None:
    needle = f"{_INDENT}; msgid: {msgid}"
    for start, end in iter_blocks(text):
        if any(line == needle for line in text[start:end].splitlines()):
            return start, end
    return None


def append_block(text: str, block: str) -> str:
    body = text.rstrip("\n")
    return (body + "\n\n" if body else "") + block.rstrip("\n") + "\n"


def rewrite_block(
    text: str,
    msgid: str,
    *,
    payee: str | None = None,
    account: str | None = None,
    instrument_account: str | None = None,
    add_tags: dict[str, str] | None = None,
) -> str:
    span = find_block(text, msgid)
    if span is None:
        raise BooksError(f"no journal block carries msgid {msgid}")
    start, end = span
    lines = text[start:end].rstrip("\n").split("\n")
    if payee:
        date_part = lines[0].split(" * ", 1)[0]
        lines[0] = f"{date_part} * {sanitize_payee(payee)}"
    if add_tags:
        for key, value in add_tags.items():
            key, value = sanitize_tag(key), sanitize_tag(value)
            if key:
                lines[2] = lines[2] + f", {key}: {value}"
    postings = [
        i
        for i, line in enumerate(lines)
        if line.startswith(_INDENT) and not line.startswith(f"{_INDENT};")
    ]
    if len(postings) < 2:
        raise BooksError(f"block {msgid} has fewer than two postings")
    if account:
        m = _POSTING_RE.match(lines[postings[0]])
        lines[postings[0]] = _posting(account, m.group(2) if m and m.group(2) else "")
    if instrument_account:
        lines[postings[1]] = _posting(instrument_account)
    return text[:start] + "\n".join(lines) + "\n" + text[end:]


def journal_rel(entity: str, d: date) -> str:
    return f"{entity}/{d.year}.journal"


def journal_files(cfg: BooksConfig) -> list[Path]:
    return sorted(p for p in cfg.path.glob("*/[0-9][0-9][0-9][0-9].journal"))


# ----------------------------------------------------------------- rules

def load_rules(path: Path) -> list[dict]:
    if not Path(path).exists():
        return []
    data = yaml.safe_load(Path(path).read_text()) or []
    return [r for r in data if isinstance(r, dict) and r.get("match")]


def apply_rules(rules: list[dict], sender: str, payee: str) -> dict | None:
    haystack = f"{sender or ''} | {payee or ''}"
    for rule in rules:
        try:
            if re.search(str(rule["match"]), haystack, re.I):
                return rule
        except re.error:
            continue
    return None


# ----------------------------------------------------------------- git + hledger

_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Maou",
    "GIT_AUTHOR_EMAIL": "maou@aegis.local",
    "GIT_COMMITTER_NAME": "Maou",
    "GIT_COMMITTER_EMAIL": "maou@aegis.local",
}
_LOCK_NAME = ".aegis.lock"
# How long a clone may take. Every activity that can trigger the first write
# must allow MORE than this, or it times out mid-clone and burns every retry
# attempt on the same clone (`_POST_TIMEOUT` in the money flows).
CLONE_TIMEOUT_S = 180


def _env(cfg: BooksConfig) -> dict[str, str]:
    env = {**os.environ, **_GIT_IDENTITY}
    if cfg.deploy_key:
        env["GIT_SSH_COMMAND"] = (
            f"ssh -i {cfg.deploy_key} -o StrictHostKeyChecking=accept-new -o IdentitiesOnly=yes"
        )
    return env


def _spawn(
    cmd: list[str], *, cwd: str, timeout: int, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    """`subprocess.run`, with the two non-`BooksError` escapes closed: a missing
    binary or working copy (`OSError`) and a hung pull/push (`TimeoutExpired`).
    Callers see one exception type, so a degraded host never escapes as a bare
    `FileNotFoundError` through an async activity."""
    try:
        return subprocess.run(
            cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise BooksError(f"{cmd[0]} timed out after {timeout}s") from exc
    except OSError as exc:
        raise BooksError(f"{cmd[0]} could not run: {exc}") from exc


def _run(
    args: list[str], cfg: BooksConfig, *, timeout: int = 60, check: bool = True
) -> subprocess.CompletedProcess:
    proc = _spawn(args, cwd=str(cfg.path), timeout=timeout, env=_env(cfg))
    if check and proc.returncode != 0:
        raise BooksError(f"{' '.join(args[:2])} failed: {proc.stderr.strip()[:500]}")
    return proc


def _has_remote(cfg: BooksConfig) -> bool:
    return bool(_run(["git", "remote"], cfg, check=False).stdout.strip())


def ensure_checkout_sync(cfg: BooksConfig) -> None:
    """Clone if the working copy is missing. Raises BooksDisabled with no
    repo url and no checkout.

    Called with the flock HELD (see `_write_sync`), so core and worker cannot
    both clone on the first-ever write. That is also why the clone stages in a
    sibling directory: the lock lives INSIDE `cfg.path` (the spec gitignores it
    there), and a clone refuses a destination that is not empty — measured,
    exit 128, "already exists and is not an empty directory". Nothing is moved
    into place until the clone has succeeded.
    """
    if (cfg.path / ".git").exists():
        return
    if not cfg.repo_url:
        raise BooksDisabled("books_repo_url is not configured and no checkout exists")
    cfg.path.mkdir(parents=True, exist_ok=True)
    staging = cfg.path.parent / f".{cfg.path.name}.cloning"
    shutil.rmtree(staging, ignore_errors=True)
    proc = _spawn(
        ["git", "clone", "-q", cfg.repo_url, str(staging)],
        cwd=str(cfg.path.parent), timeout=CLONE_TIMEOUT_S, env=_env(cfg),
    )
    if proc.returncode != 0:
        shutil.rmtree(staging, ignore_errors=True)
        raise BooksError(f"git clone failed: {proc.stderr.strip()[:500]}")
    for item in staging.iterdir():
        item.rename(cfg.path / item.name)
    staging.rmdir()


def _pull_sync(cfg: BooksConfig) -> None:
    if _has_remote(cfg):
        _run(["git", "pull", "-q", "--rebase", "--autostash"], cfg, timeout=120)


def _check_sync(cfg: BooksConfig) -> None:
    proc = _spawn(
        ["hledger", "-f", cfg.main, "check", "--strict"], cwd=str(cfg.path), timeout=60
    )
    if proc.returncode != 0:
        raise BooksCheckError(proc.stderr.strip()[:1000] or proc.stdout.strip()[:1000])


def _git_paths(cfg: BooksConfig, paths: list[str], *, on_disk_only: bool = False) -> list[str]:
    """The pathspec git can actually act on.

    The lock file is dropped by NAME rather than with a `:!` exclusion pathspec:
    combining an exclusion with a positive pathspec makes `git add` stage
    nothing at all for a new file, silently and with exit 0 (measured on git
    2.x), which is how a written report would never reach a commit. And a
    pathspec matching neither the working tree nor the index makes `git add`
    and `git commit` fail outright, so those are dropped too — a journal file
    the write never had to create cannot have changed.
    """
    wanted = [p for p in paths if p != _LOCK_NAME]
    if not wanted or on_disk_only:
        return wanted
    listed = _run(["git", "ls-files", "-z", "--", *wanted], cfg, check=False).stdout
    tracked = set(listed.split("\0"))
    return [p for p in wanted if (cfg.path / p).exists() or p in tracked]


def _revert_sync(cfg: BooksConfig, paths: list[str]) -> None:
    """Undo ONLY the paths this write touched. A repo-wide revert would destroy
    a human's unrelated uncommitted edits — including the hand edit that made
    the write fail in the first place."""
    targets = _git_paths(cfg, paths, on_disk_only=True)
    if not targets:
        return
    # Unstage first. A write can fail AFTER `git add` (the commit itself), and
    # `git checkout -- <path>` restores from the INDEX, so a staged bad version
    # would be "restored" straight back into the working copy. Reset also makes
    # a newly-added file untracked again, so the `clean` below can remove it.
    _run(["git", "reset", "-q", "HEAD", "--", *targets], cfg, check=False)
    # One checkout per path: git aborts the WHOLE command when any pathspec
    # names an untracked file, reverting nothing, so a new year's journal in
    # the list would silently protect every other path from being restored.
    for rel in targets:
        _run(["git", "checkout", "-q", "--", rel], cfg, check=False)
    # The lock file is outside `targets`, so `clean` cannot delete it out from
    # under a holder — which would hand the next writer a different inode and
    # therefore no mutual exclusion at all.
    _run(["git", "clean", "-qfd", "--", *targets], cfg, check=False)


def _commit_push_sync(cfg: BooksConfig, summary: str, paths: list[str]) -> bool:
    # The empty guard is load-bearing, not defensive: `git add -A` with no
    # positive pathspec means the whole tree, so an empty scope would sweep in
    # every unrelated edit in the checkout.
    scoped = _git_paths(cfg, paths)
    if not scoped:
        return False
    _run(["git", "add", "-A", "--", *scoped], cfg)
    if _run(["git", "diff", "--cached", "--quiet", "--", *scoped], cfg, check=False).returncode == 0:
        return False
    # `commit -- <paths>` rather than a bare commit: a bare one would sweep in
    # anything the human happened to have staged in the checkout already.
    _run(["git", "commit", "-q", "-m", summary, "--", *scoped], cfg)
    if _has_remote(cfg):
        push = _run(["git", "push", "-q"], cfg, check=False, timeout=120)
        if push.returncode != 0:
            logger.warning("books_push_failed", error=push.stderr.strip()[:300])
    return True


def unpushed_commits_sync(cfg: BooksConfig) -> int:
    proc = _run(["git", "rev-list", "--count", "@{u}..HEAD"], cfg, check=False)
    if proc.returncode != 0 or not proc.stdout.strip().isdigit():
        return 0
    return int(proc.stdout.strip())


_ASYNC_LOCK = asyncio.Lock()


class _FileLock:
    """flock on <books>/.aegis.lock — core and worker share the directory."""

    def __init__(self, cfg: BooksConfig) -> None:
        self._path = cfg.path / _LOCK_NAME

    def __enter__(self):
        # The directory may not exist yet: the clone happens INSIDE this lock,
        # so the lock file has to be creatable before there is a checkout.
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = open(self._path, "w")  # noqa: SIM115 — held for the with-block
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self._fd, fcntl.LOCK_UN)
        self._fd.close()


def _write_sync(
    cfg: BooksConfig, summary: str, mutate: Callable[[], None], paths: list[str]
) -> None:
    """`paths` is the write's blast radius, relative to the checkout. A caller
    that only learns which file it touched inside `mutate` passes the list the
    closure appends to — it is read after `mutate` returns, and the closure must
    record a path BEFORE writing it."""
    # The clone is inside the lock: two processes reaching their first write at
    # the same moment would otherwise both clone into the same directory.
    with _FileLock(cfg):
        ensure_checkout_sync(cfg)
        _pull_sync(cfg)
        try:
            mutate()
            _check_sync(cfg)
            # Inside the try: the commit can fail too, and leaving a staged
            # working copy behind breaks `BooksError`'s contract — the next
            # write's `git add` would sweep the failed one into its commit.
            _commit_push_sync(cfg, summary, paths)
        except ValueError as exc:
            # A NUL byte in a path or an argv entry raises ValueError, not
            # OSError, so it is not one of the escapes `_spawn` closes. It
            # escaped raw, from the COMMIT, with the change already staged.
            _revert_sync(cfg, paths)
            raise BooksError(f"books write failed: {exc}") from exc
        except Exception:
            _revert_sync(cfg, paths)
            raise


async def _write(
    cfg: BooksConfig, summary: str, mutate: Callable[[], None], paths: list[str]
) -> None:
    async with _ASYNC_LOCK:
        await asyncio.to_thread(_write_sync, cfg, summary, mutate, paths)


def _declared_accounts_sync(cfg: BooksConfig) -> set[str]:
    """The declared chart. An empty set means hledger genuinely declared nothing
    — a failure raises, because returning `set()` for it would silently disable
    the unknown-account fallback and let an undeclared account reach the file."""
    proc = _spawn(
        ["hledger", "-f", cfg.main, "accounts", "--declared"], cwd=str(cfg.path), timeout=30
    )
    if proc.returncode != 0:
        raise BooksError(
            f"hledger accounts --declared failed: {proc.stderr.strip()[:300]}"
        )
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


def _ensure_journal_file(cfg: BooksConfig, rel: str) -> Path:
    path = cfg.path / rel
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    entity, year = rel.split("/")[0], rel.split("/")[1][:4]
    path.write_text(f"; {entity.capitalize()} transactions, {year}. Written by AEGIS (Maou).\n")
    main = cfg.path / cfg.main
    text = main.read_text()
    line = f"include {rel}\n"
    if line not in text:
        marker = "include recurring.journal\n"
        if marker in text:
            text = text.replace(marker, line + marker)
        else:
            # Guard the newline: appending to a main.journal whose last line has
            # no trailing "\n" would glue the include onto it.
            text = (text.rstrip("\n") + "\n" if text else "") + line
        main.write_text(text)
    return path


async def post_event(event: MoneyEvent, msgid: str, cfg: BooksConfig) -> str:
    """Append one transaction block. Idempotent on msgid. Returns the
    journal file's relative path."""
    if event.kind != "transaction" or event.entity == "none":
        raise BooksError(
            f"post_event needs a transaction with an entity, got {event.kind}/{event.entity}"
        )
    if event.amount is None or not event.currency or event.occurred_on is None:
        raise BooksError("post_event needs amount, currency and occurred_on")
    rel = journal_rel(event.entity, event.occurred_on)
    posted: list[str] = []

    def mutate() -> None:
        # Idempotency is repo-WIDE, not per-file: a corrected date or entity
        # sends the same msgid to a different journal, and scoping the check to
        # `rel` would write the block a second time somewhere else.
        for existing in journal_files(cfg):
            if find_block(existing.read_text(), msgid):
                posted.append(str(existing.relative_to(cfg.path)))
                return
        declared = _declared_accounts_sync(cfg)
        counter = event.account or account_for(event.category, event.direction, event.entity)
        if declared and counter not in declared:
            counter = UNKNOWN["hikmah" if event.entity == "hikmah" else "personal"][
                "in" if event.direction == "in" else "out"
            ]
        instrument = instrument_account(event.instrument, declared)
        posted.append(rel)
        path = _ensure_journal_file(cfg, rel)
        text = path.read_text()
        path.write_text(append_block(text, render_transaction(event, counter, instrument, msgid)))

    summary = (
        f"post {event.entity} {event.occurred_on} {sanitize_payee(event.payee)} "
        f"{render_amount(event.amount, event.currency)}"
    )
    await _write(cfg, summary, mutate, [rel, cfg.main])
    return posted[0] if posted else rel


async def post_block(
    block: str, entity: str, d: date, msgid: str, cfg: BooksConfig
) -> tuple[str, bool]:
    """Append an already-rendered block (`render_manual`) under the same lock /
    `check --strict` / revert protocol as `post_event`. Returns the journal
    file's relative path and whether this call is what wrote it.

    Idempotency is repo-WIDE for the same reason as `post_event`: the msgid is
    the only handle on the block, so a re-post with a corrected date or entity
    must find the existing one wherever it landed.

    The `created` half of the return is what lets a caller tell "I wrote it"
    from "it was already there". A caller whose write can be retried needs
    that: the retry is the SAME logical post, and reporting it as a fresh one
    tells the user they now have two.
    """
    rel = journal_rel(entity, d)
    posted: list[str] = []
    created = [True]

    def mutate() -> None:
        for existing in journal_files(cfg):
            if find_block(existing.read_text(), msgid):
                posted.append(str(existing.relative_to(cfg.path)))
                created[0] = False
                return
        # Recorded before the write: `paths` is this write's git scope.
        posted.append(rel)
        path = _ensure_journal_file(cfg, rel)
        path.write_text(append_block(path.read_text(), block))

    await _write(cfg, f"post {entity} {d} manual", mutate, [rel, cfg.main])
    return (posted[0] if posted else rel), created[0]


async def locate_event(msgid: str, cfg: BooksConfig) -> str | None:
    """The journal file holding `msgid`, relative to the checkout, or None.

    Read-only and lock-free: a caller that must know which set of books a block
    lives in before it writes reads the journal, which is the record, rather
    than `finance.journal_index`, which is only its index and does not cover a
    hand-written block.
    """

    def _go() -> str | None:
        for path in journal_files(cfg):
            if find_block(path.read_text(), msgid):
                return str(path.relative_to(cfg.path))
        return None

    return await asyncio.to_thread(_go)


async def rewrite_event(
    msgid: str,
    cfg: BooksConfig,
    *,
    payee: str | None = None,
    account: str | None = None,
    instrument_account: str | None = None,
    add_tags: dict[str, str] | None = None,
) -> str:
    found: list[str] = []

    def mutate() -> None:
        for path in journal_files(cfg):
            text = path.read_text()
            if find_block(text, msgid) is None:
                continue
            # Record before writing: `found` is also this write's git scope, so
            # a path must be in it before the file can possibly change.
            found.append(str(path.relative_to(cfg.path)))
            path.write_text(
                rewrite_block(text, msgid, payee=payee, account=account,
                              instrument_account=instrument_account, add_tags=add_tags)
            )
            return
        raise BooksError(f"no journal block carries msgid {msgid}")

    await _write(cfg, f"reclassify {msgid}" + (f" -> {account}" if account else ""), mutate, found)
    return found[0]


async def rewrite_events(
    msgids: list[str], cfg: BooksConfig, *, payee: str | None = None, account: str | None = None
) -> tuple[list[str], list[str]]:
    """Reclassify many blocks in ONE write. Returns (rewritten, failed) msgids.

    A loop over `rewrite_event` is a loop over the whole protocol — flock, pull,
    strict check, commit, push — per posting, which serialises every other
    writer of the books behind it and leaves one commit per posting in the
    history. Applying a rule to its backlog is one intent, so it is one commit.

    A msgid whose block is missing or unrewritable is collected, not raised:
    one stale index row must not revert the rewrites that did land.
    """
    rewritten: list[str] = []
    failed: list[str] = []
    touched: list[str] = []

    def mutate() -> None:
        for msgid in msgids:
            target: tuple[Path, str] | None = None
            for path in journal_files(cfg):
                text = path.read_text()
                if find_block(text, msgid) is not None:
                    target = (path, text)
                    break
            if target is None:
                failed.append(msgid)
                continue
            path, text = target
            rel = str(path.relative_to(cfg.path))
            # Recorded before the write, like `rewrite_event`: `touched` is this
            # write's git scope AND its revert scope.
            if rel not in touched:
                touched.append(rel)
            try:
                # Rendered before it is written, so a rejected block leaves the
                # file exactly as it was and the rest of the batch continues.
                new_text = rewrite_block(text, msgid, payee=payee, account=account)
            except BooksError:
                failed.append(msgid)
                continue
            path.write_text(new_text)
            rewritten.append(msgid)

    if not msgids:
        return [], []
    await _write(cfg, f"reclassify {len(msgids)} postings -> {account}", mutate, touched)
    return rewritten, failed


async def remove_event(msgid: str, cfg: BooksConfig) -> None:
    found: list[str] = []

    def mutate() -> None:
        for path in journal_files(cfg):
            text = path.read_text()
            span = find_block(text, msgid)
            if span is None:
                continue
            found.append(str(path.relative_to(cfg.path)))
            start, end = span
            head, tail = text[:start].rstrip("\n"), text[end:].lstrip("\n")
            path.write_text((head + "\n\n" + tail).rstrip("\n") + "\n")
            return
        raise BooksError(f"no journal block carries msgid {msgid}")

    await _write(cfg, f"remove {msgid}", mutate, found)


async def append_prices(lines: list[str], cfg: BooksConfig) -> None:
    def mutate() -> None:
        path = cfg.path / "prices.journal"
        text = path.read_text() if path.exists() else ""
        # A push that times out leaves the commit made, so the caller retries a
        # write that already landed — appending unconditionally would duplicate.
        fresh = [ln for ln in lines if ln not in text.splitlines()]
        if not fresh:
            return
        body = "\n".join(fresh) + "\n"
        path.write_text((text.rstrip("\n") + "\n" + body) if text else body)

    await _write(cfg, f"prices {lines[0].split()[1] if lines else ''}", mutate, ["prices.journal"])


async def append_rule(rule: dict, cfg: BooksConfig) -> None:
    rel = "rules/accounts.yaml"

    def mutate() -> None:
        path = cfg.path / rel
        if rule in load_rules(path):  # same retry-after-timeout guard
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        text = path.read_text() if path.exists() else ""
        entry = yaml.safe_dump([rule], sort_keys=False, allow_unicode=True)
        path.write_text((text.rstrip("\n") + "\n" if text else "") + entry)

    summary = f"rule: {rule.get('match')} -> {rule.get('account', 'payee only')}"
    await _write(cfg, summary, mutate, [rel])


async def write_report(rel_path: str, text: str, cfg: BooksConfig) -> None:
    def mutate() -> None:
        path = cfg.path / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    await _write(cfg, f"report {rel_path}", mutate, [rel_path])


_READ_COMMANDS = frozenset({
    "bal", "balance", "reg", "register", "print", "is", "incomestatement", "bs",
    "balancesheet", "cf", "cashflow", "accounts", "payees", "tags", "stats",
    "activity", "aregister", "check",
})
# hledger accepts bundled short flags and unambiguous long-flag abbreviations,
# so this must be an exact-match allowlist, never a deny-prefix list. Measured
# against the real binary, a `startswith` denylist of -f/-o/--file/--output-file
# lets all three of these straight through:
#   -Ef<path>    = -E -f <path>  → reads any file, and its parse error echoes
#                                  the first line back through BooksError
#   -No<path>    = -N -o <path>  → arbitrary WRITE; it succeeded and created
#                                  the file with the report inside
#   --fil=<path> = --file=<path> → the same read, via abbreviation
# Anything not starting with "-" is a query term and needs no entry here.
_ALLOWED_OPTIONS = frozenset({
    "-X", "-b", "-e", "-p", "-M", "-W", "-D", "-Y",
    "--depth", "--forecast", "--pivot", "--flat", "--tree", "--sort-amount",
    "--average", "--transpose", "--no-total", "--empty", "--historical",
    "--declared", "--used", "--strict",
})
_OUTPUT_CAP = 12_000


async def run_hledger(args: list[str], cfg: BooksConfig, *, output_format: str = "text") -> str:
    """Read-only hledger over main.journal. Whitelisted subcommands; no file,
    rules or output-file overrides; output capped at 12,000 chars."""
    if not args or args[0] not in _READ_COMMANDS:
        raise BooksError(f"hledger command not allowed: {args[:1]}")
    for a in args[1:]:
        # `@FILE` is hledger's args-file syntax: it OPENS the file and splices
        # its lines in as arguments, re-admitting everything the allowlist keeps
        # out, so it is refused before anything else.
        if a.startswith("@"):
            raise BooksError(f"hledger argument not allowed: {a}")
        if not a.startswith("-"):
            continue  # a query term
        # The option token is everything before the first "=", so `--depth=2`
        # is checked as `--depth`. A value passed separately (`--depth 2`) is
        # then just a query term, which is why values are never consumed here.
        if a.split("=", 1)[0] not in _ALLOWED_OPTIONS:
            raise BooksError(f"hledger argument not allowed: {a}")
    if output_format not in ("text", "json", "csv"):
        raise BooksError(f"unknown output format {output_format}")
    cmd = ["hledger", "-f", cfg.main, *args]
    if output_format != "text":
        cmd += ["-O", output_format]

    def _go() -> str:
        proc = _spawn(cmd, cwd=str(cfg.path), timeout=30)
        if proc.returncode != 0:
            raise BooksError(proc.stderr.strip()[:800])
        out = proc.stdout
        return out if len(out) <= _OUTPUT_CAP else out[:_OUTPUT_CAP] + "\n… (truncated)"

    return await asyncio.to_thread(_go)


async def declared_accounts(cfg: BooksConfig) -> set[str]:
    """The declared chart, for a caller that must refuse an undeclared account.

    Deliberately NOT routed through `run_hledger`: that allowlist exists to
    police a CALLER-supplied argument list, and this argv is fixed and carries
    no caller text at all, while its 12,000-char output cap would silently drop
    the tail of a large chart — turning a declared account into a refusal.
    """
    return await asyncio.to_thread(_declared_accounts_sync, cfg)


async def unpushed_commits(cfg: BooksConfig) -> int:
    return await asyncio.to_thread(unpushed_commits_sync, cfg)
