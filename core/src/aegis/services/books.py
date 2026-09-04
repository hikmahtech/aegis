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
    code = (currency or "").upper()
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
    path.write_text(raw + "\n")
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
_POSTING_RE = re.compile(r"^    (\S+)(?:\s{2,}(\S.*))?$")


def sanitize_payee(payee: str) -> str:
    text = re.sub(r"[;|\r\n]+", " ", payee or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:_PAYEE_MAX] or "unknown"


def _posting(account: str, amount: str = "") -> str:
    if not amount:
        return f"{_INDENT}{account}"
    if len(account) >= _ACCOUNT_WIDTH:
        return f"{_INDENT}{account}  {amount}"
    return f"{_INDENT}{account:<{_ACCOUNT_WIDTH}}{amount}"


def render_transaction(
    event: MoneyEvent, counter_account: str, instrument_acct: str, msgid: str
) -> str:
    """One journal block (spec §1 grammar). Posting 1 = category with signed
    amount (+ out, − in), posting 2 = instrument, no amount."""
    if event.amount is None or not event.currency or event.occurred_on is None:
        raise BooksError("render_transaction needs amount, currency and occurred_on")
    tags = [f"channel: {event.channel}"]
    if event.ref:
        tags.append(f"ref: {event.ref}")
    if event.instrument:
        tags.append(f"instrument: {event.instrument}")
    amount = render_amount(event.amount, event.currency, negative=(event.direction == "in"))
    lines = [
        f"{event.occurred_on.isoformat()} * {sanitize_payee(event.payee)}",
        f"{_INDENT}; msgid: {msgid}",
        f"{_INDENT}; {', '.join(tags)}",
        _posting(counter_account, amount),
        _posting(instrument_acct),
    ]
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
        lines[2] = lines[2] + "".join(f", {k}: {v}" for k, v in add_tags.items())
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


def _env(cfg: BooksConfig) -> dict[str, str]:
    env = {**os.environ, **_GIT_IDENTITY}
    if cfg.deploy_key:
        env["GIT_SSH_COMMAND"] = (
            f"ssh -i {cfg.deploy_key} -o StrictHostKeyChecking=accept-new -o IdentitiesOnly=yes"
        )
    return env


def _run(
    args: list[str], cfg: BooksConfig, *, timeout: int = 60, check: bool = True
) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        args, cwd=str(cfg.path), env=_env(cfg), capture_output=True, text=True, timeout=timeout
    )
    if check and proc.returncode != 0:
        raise BooksError(f"{' '.join(args[:2])} failed: {proc.stderr.strip()[:500]}")
    return proc


def _has_remote(cfg: BooksConfig) -> bool:
    return bool(_run(["git", "remote"], cfg, check=False).stdout.strip())


def ensure_checkout_sync(cfg: BooksConfig) -> None:
    """Clone if the working copy is missing. Raises BooksDisabled with no
    repo url and no checkout."""
    if (cfg.path / ".git").exists():
        return
    if not cfg.repo_url:
        raise BooksDisabled("books_repo_url is not configured and no checkout exists")
    cfg.path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["git", "clone", "-q", cfg.repo_url, str(cfg.path)],
        env=_env(cfg), capture_output=True, text=True, timeout=180,
    )
    if proc.returncode != 0:
        raise BooksError(f"git clone failed: {proc.stderr.strip()[:500]}")


def _pull_sync(cfg: BooksConfig) -> None:
    if _has_remote(cfg):
        _run(["git", "pull", "-q", "--rebase", "--autostash"], cfg, timeout=120)


def _check_sync(cfg: BooksConfig) -> None:
    proc = subprocess.run(
        ["hledger", "-f", cfg.main, "check", "--strict"],
        cwd=str(cfg.path), capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise BooksCheckError(proc.stderr.strip()[:1000] or proc.stdout.strip()[:1000])


def _revert_sync(cfg: BooksConfig) -> None:
    _run(["git", "checkout", "-q", "--", "."], cfg, check=False)
    # `-e` keeps the flock file: deleting it while another process holds it
    # would hand the next writer a different inode and no mutual exclusion.
    _run(["git", "clean", "-qfd", "-e", _LOCK_NAME], cfg, check=False)


def _commit_push_sync(cfg: BooksConfig, summary: str) -> bool:
    # The lock file lives in the checkout; exclude it so it never enters history
    # (the real books repo also .gitignores it, test repos have no .gitignore).
    _run(["git", "add", "-A", "--", ".", f":!{_LOCK_NAME}"], cfg)
    if _run(["git", "diff", "--cached", "--quiet"], cfg, check=False).returncode == 0:
        return False
    _run(["git", "commit", "-q", "-m", summary], cfg)
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
        self._fd = open(self._path, "w")  # noqa: SIM115 — held for the with-block
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self._fd, fcntl.LOCK_UN)
        self._fd.close()


def _write_sync(cfg: BooksConfig, summary: str, mutate: Callable[[], None]) -> None:
    ensure_checkout_sync(cfg)
    with _FileLock(cfg):
        _pull_sync(cfg)
        try:
            mutate()
            _check_sync(cfg)
        except Exception:
            _revert_sync(cfg)
            raise
        _commit_push_sync(cfg, summary)


async def _write(cfg: BooksConfig, summary: str, mutate: Callable[[], None]) -> None:
    async with _ASYNC_LOCK:
        await asyncio.to_thread(_write_sync, cfg, summary, mutate)


def _declared_accounts_sync(cfg: BooksConfig) -> set[str]:
    proc = subprocess.run(
        ["hledger", "-f", cfg.main, "accounts", "--declared"],
        cwd=str(cfg.path), capture_output=True, text=True, timeout=30,
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
        text = text.replace(marker, line + marker) if marker in text else text + line
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

    def mutate() -> None:
        declared = _declared_accounts_sync(cfg)
        counter = event.account or account_for(event.category, event.direction, event.entity)
        if declared and counter not in declared:
            counter = UNKNOWN["hikmah" if event.entity == "hikmah" else "personal"][
                "in" if event.direction == "in" else "out"
            ]
        instrument = instrument_account(event.instrument, declared)
        path = _ensure_journal_file(cfg, rel)
        text = path.read_text()
        if find_block(text, msgid):
            return
        path.write_text(append_block(text, render_transaction(event, counter, instrument, msgid)))

    summary = (
        f"post {event.entity} {event.occurred_on} {sanitize_payee(event.payee)} "
        f"{render_amount(event.amount, event.currency)}"
    )
    await _write(cfg, summary, mutate)
    return rel


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
            path.write_text(
                rewrite_block(text, msgid, payee=payee, account=account,
                              instrument_account=instrument_account, add_tags=add_tags)
            )
            found.append(str(path.relative_to(cfg.path)))
            return
        raise BooksError(f"no journal block carries msgid {msgid}")

    await _write(cfg, f"reclassify {msgid}" + (f" -> {account}" if account else ""), mutate)
    return found[0]


async def remove_event(msgid: str, cfg: BooksConfig) -> None:
    def mutate() -> None:
        for path in journal_files(cfg):
            text = path.read_text()
            span = find_block(text, msgid)
            if span is None:
                continue
            start, end = span
            head, tail = text[:start].rstrip("\n"), text[end:].lstrip("\n")
            path.write_text((head + "\n\n" + tail).rstrip("\n") + "\n")
            return
        raise BooksError(f"no journal block carries msgid {msgid}")

    await _write(cfg, f"remove {msgid}", mutate)


async def append_prices(lines: list[str], cfg: BooksConfig) -> None:
    def mutate() -> None:
        path = cfg.path / "prices.journal"
        text = path.read_text() if path.exists() else ""
        body = "\n".join(lines) + "\n"
        path.write_text((text.rstrip("\n") + "\n" + body) if text else body)

    await _write(cfg, f"prices {lines[0].split()[1] if lines else ''}", mutate)


async def append_rule(rule: dict, cfg: BooksConfig) -> None:
    def mutate() -> None:
        path = cfg.path / "rules" / "accounts.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        text = path.read_text() if path.exists() else ""
        entry = yaml.safe_dump([rule], sort_keys=False, allow_unicode=True)
        path.write_text((text.rstrip("\n") + "\n" if text else "") + entry)

    await _write(cfg, f"rule: {rule.get('match')} -> {rule.get('account', 'payee only')}", mutate)


async def write_report(rel_path: str, text: str, cfg: BooksConfig) -> None:
    def mutate() -> None:
        path = cfg.path / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    await _write(cfg, f"report {rel_path}", mutate)


_READ_COMMANDS = frozenset({
    "bal", "balance", "reg", "register", "print", "is", "incomestatement", "bs",
    "balancesheet", "cf", "cashflow", "accounts", "payees", "tags", "stats",
    "activity", "aregister", "check",
})
_FORBIDDEN_ARG_PREFIXES = ("-f", "--file", "--rules", "-o", "--output-file", "--config")
_OUTPUT_CAP = 12_000


async def run_hledger(args: list[str], cfg: BooksConfig, *, output_format: str = "text") -> str:
    """Read-only hledger over main.journal. Whitelisted subcommands; no file,
    rules or output-file overrides; output capped at 12,000 chars."""
    if not args or args[0] not in _READ_COMMANDS:
        raise BooksError(f"hledger command not allowed: {args[:1]}")
    for a in args[1:]:
        if a.startswith(_FORBIDDEN_ARG_PREFIXES):
            raise BooksError(f"hledger argument not allowed: {a}")
    if output_format not in ("text", "json", "csv"):
        raise BooksError(f"unknown output format {output_format}")
    cmd = ["hledger", "-f", cfg.main, *args]
    if output_format != "text":
        cmd += ["-O", output_format]

    def _go() -> str:
        proc = subprocess.run(cmd, cwd=str(cfg.path), capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raise BooksError(proc.stderr.strip()[:800])
        out = proc.stdout
        return out if len(out) <= _OUTPUT_CAP else out[:_OUTPUT_CAP] + "\n… (truncated)"

    return await asyncio.to_thread(_go)


async def unpushed_commits(cfg: BooksConfig) -> int:
    return await asyncio.to_thread(unpushed_commits_sync, cfg)
