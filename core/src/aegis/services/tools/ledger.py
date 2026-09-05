"""The books' chat tools (spec §8) — query, post, reclassify, add a rule.

These four let a language model act on a git-backed double-entry ledger, so
four rules hold the module together and every one of them is load-bearing:

* **`books.run_hledger` is the only path to hledger for caller-supplied
  arguments.** It carries an exact-match option allowlist because hledger
  cannot be sandboxed with a deny list — it expands `@argsfile`, accepts
  bundled short flags (`-Ef/path` READS a file, `-No/path` WRITES one) and
  abbreviates long flags (`--fil=`). Nothing here builds an argv of its own,
  and nothing widens that allowlist. (`books.declared_accounts` also reaches
  hledger, with a fixed argv that no caller text touches — see its docstring.)
* **A write validates its account against `hledger accounts --declared`
  first.** An account the chart does not declare is refused, never created:
  the chart is the user's, and `check --strict` would reject the block anyway
  — as a whole-write failure rather than a sentence the model can act on.
* **Every `BooksError` becomes an `error: …` string.** These run inside the
  chat loop; a raised exception is a failed turn, while a returned sentence
  is something the model can read and correct.
* **Nothing writes a journal file directly.** Every mutation goes through
  `books.py`, which holds the flock, runs `hledger check --strict` and reverts
  the write when it fails.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from datetime import date as date_type
from decimal import Decimal, InvalidOperation
from typing import Literal

import asyncpg
import structlog

from aegis.api.models.money import MoneyEvent, payee_key
from aegis.services import books
from aegis.services import journal_index as ji
from aegis.services.tools.base import ToolContext
from aegis.services.tools.registry import aegis_tool

logger = structlog.get_logger()

_ENTITIES = ("personal", "hikmah")

# A ledger amount, bounded. `Decimal` happily builds NaN, Infinity and 1e400;
# all three survive a bare constructor and then raise `InvalidOperation` inside
# `quantize()` further down. Nothing real is a trillion of anything.
_MAX_AMOUNT = Decimal("1e12")

# Placeholder msgid used while rendering the block a msgid is derived FROM.
# Constant, so it contributes nothing to the digest.
_MSGID_SEED = "manual/0"

# How long a books WRITE may take, from `books.py`'s own budgets: clone (180s,
# first write only) + pull (120s) + `check --strict` (60s) + commit (60s) +
# push (120s). The chat loop's default is `tool_timeout_seconds` = 30, and
# `asyncio.wait_for` cannot cancel the thread `books._write` runs in — so a 30s
# cap does not stop a slow write, it only tells the model the write failed
# while it goes on to commit and push. The stable msgid below covers what is
# left: a retry of an abandoned call finds its own block and writes nothing.
LEDGER_WRITE_TIMEOUT_S = books.CLONE_TIMEOUT_S + 120 + 60 + 60 + 120

# Ceiling on one `ledger_add_rule` sweep. The rewrites are batched into a single
# commit, but `{"match": ".", ...}` would still rewrite the whole unknown
# backlog in one unreviewable change; past this the model is told to narrow the
# rule (or run it again) rather than being handed the entire ledger.
_MAX_APPLY = 200

# `match` is a model-authored regex that is PERSISTED: the worker runs every
# rule in `rules/accounts.yaml` against every incoming money event, forever, in
# another process. So the budget is not "this call" — it is every future call
# in two services.
_MAX_MATCH_LEN = 200

# Three bounds, in the order they are applied. `re` has no timeout and
# `books.apply_rules` runs `re.search` synchronously on the event loop, so a
# slow pattern hangs the whole process and `asyncio.wait_for` cannot interrupt
# it — which is why the pattern is measured BEFORE it is persisted.
#
# 1. ANY quantified group. Exponential backtracking needs nested quantification,
#    and nesting needs a group — so this removes the whole exponential class in
#    one rule. A guard that only caught self-nesting (`(a+)+`) missed `((a+))+`,
#    which was measured still running after 20s on a 31-character input.
_QUANTIFIED_GROUP_RE = re.compile(r"\)\s*[*+{?]")
# 2. How many quantifiers may stack. Without a group the cost is polynomial in
#    the number of them, and the probe below has to terminate. Measured on
#    CPython 3.12, `("a" * 48 + "!")` against `"a*" * k + "$"`:
#      k=4 → 0.015s   k=5 → 0.14s   k=6 → 1.15s   k=8 → 55s
#    so 6 is what keeps the probe's own worst case near a second.
_MAX_QUANTIFIERS = 6
# The `?` of `(?:`/`(?i)` is not a quantifier, and `\*` is a literal.
_QUANTIFIER_RE = re.compile(r"(?<!\\)(?<!\()[*+?]|(?<!\\)\{\d")
# 3. The behavioural check, which is the only one that catches quantifier
#    stacking with no group at all. The probes are worst-case shapes for a
#    backtracking engine at a length a real payee can reach (80 chars).
#
#    SHORTEST FIRST, and the loop stops at the first probe over budget. That
#    ordering is what keeps this from becoming a hang if rule 1 above is ever
#    weakened: measured, `((a+))+$` costs 0.9s at 24 characters and is refused
#    there, where at 48 it would not have finished this decade. Rule 1 means the
#    first probe is always fast in practice; this is the belt to its braces.
_REGEX_BUDGET_S = 0.1
_REGEX_PROBES = ("a" * 24 + "!", "a" * 48 + "!", "ab" * 24 + "!", "0" * 48 + " x")

# One index row per reclassified posting; the journal is the record, so this
# only keeps the index from disagreeing with it.
_REINDEX_SQL = (
    "UPDATE finance.journal_index SET account = $2, payee = COALESCE($3, payee), "
    "updated_at = now() WHERE message_id = $1"
)


def _undeclared(account: str) -> str:
    return (
        f"error: account {account} is not declared in the chart "
        "(accounts.journal). Declare it there first, or pick a declared one."
    )


def _account_entity(account: str) -> str | None:
    """Which set of books an account belongs to, or None when it belongs to
    both.

    Only the expense and income trees carry an entity — the business side is
    the `:hikmah:` segment (`books._ACCOUNT_MAP`). Assets, liabilities and
    equity are entity-NEUTRAL by design: `post_event` writes
    `assets:bank:hdfc:1225` into both sets of books through
    `instrument_account`, which has no notion of entity at all, and the chart
    declares `equity:transfers` precisely for a move between one's own
    accounts. Treating those as personal would refuse a real correction — a
    hikmah posting moved onto the shared bank account — for no gain, since the
    hazard the caller guards against (an `expenses:hikmah:*` posting filed in
    `personal/2026.journal`) lives entirely in the two trees this does cover.
    """
    if not account.startswith(("expenses:", "income:")):
        return None
    return "hikmah" if ":hikmah:" in f"{account}:" else "personal"


def _slow_regex_probe(pattern: str) -> str | None:
    """The probe string this pattern is too slow on, or None.

    Bounded by construction rather than by interruption: `re` cannot be
    interrupted, so the caller's syntactic bounds (no quantified group, at most
    `_MAX_QUANTIFIERS`) are what keep this from becoming the hang it is meant to
    prevent. Within them the worst measured case is ~1.2s, once, on a rule that
    is then refused.
    """
    compiled = re.compile(pattern, re.I)
    for probe in _REGEX_PROBES:
        started = time.perf_counter()
        compiled.search(probe)
        if time.perf_counter() - started > _REGEX_BUDGET_S:
            return probe
    return None


def _manual_msgid(
    entity: str, d: date_type, payee: str, postings: list[dict], note: str
) -> str:
    """A msgid derived from the transaction itself, so a re-post is a RETRY.

    A `uuid4()` here would be fresh on every call, which means `post_block`'s
    idempotency scan can never match and the model's natural response to a
    timed-out write — call it again — puts a second copy of the same
    transaction in the ledger. Deriving the id from the content makes the
    second call find the first one's block and write nothing.

    What is digested is the RENDERED BLOCK, not the caller's arguments. The
    journal stores normalized values — a quantized amount, a defaulted
    currency, a sanitized payee and note — so digesting the raw text would give
    `"245.50"` and `"245.5"` different ids for a byte-identical block and let
    the duplicate through anyway. An LLM re-issuing a timed-out call is not
    byte-stable, so that is the realistic retry, not an exotic one. Hashing the
    block makes "same id" mean exactly "same journal entry", and keeps meaning
    that if `render_manual` changes.

    `entity` is digested alongside it because it picks the FILE, and the same
    block in the two sets of books is two different transactions.

    The trade is that two genuinely identical transactions on the same day
    collapse into one; the tool says so in its description, and a distinguishing
    `note` (which is part of the block) records the second.
    """
    body = books.render_manual(d, payee, postings, _MSGID_SEED, note)
    return f"manual/{hashlib.sha256(f'{entity}\n{body}'.encode()).hexdigest()[:16]}"


@aegis_tool
async def _exec_ledger_query(
    pool: asyncpg.Pool,
    ctx: ToolContext,
    *,
    command: str,
    args: list[str] | None = None,
    output: Literal["text", "json", "csv"] = "text",
) -> str:
    """Run a read-only hledger report over the books.

    Args:
        command: hledger subcommand: bal, reg, is, bs, cf, print, accounts, payees, tags, stats, activity, aregister.
        args: extra hledger arguments, e.g. ["-X", "₹", "-p", "thismonth", "expenses", "--depth", "2"].
        output: text (default), json or csv.
    """
    cfg = books.config_from_settings(ctx.settings)
    try:
        return await books.run_hledger([command, *(args or [])], cfg, output_format=output)
    except books.BooksError as exc:
        return f"error: {exc}"


@aegis_tool
async def _exec_ledger_post(
    pool: asyncpg.Pool,
    ctx: ToolContext,
    *,
    date: str,
    payee: str,
    postings: list[dict],
    entity: str = "personal",
    note: str = "",
) -> str:
    """Record a transaction in the books by hand. Each posting is {"account": ..., "amount": ..., "currency": ...}; at most one posting may omit the amount. Re-posting the same date, payee, postings and note is treated as a retry of the first call, not a second transaction — give the second one a note to record a genuine duplicate.

    Args:
        date: YYYY-MM-DD.
        payee: who was paid or who paid.
        postings: two or more postings; amounts in major units. A negative amount is money coming in.
        entity: personal or hikmah — which set of books.
        note: optional free text stored as a `note:` tag.
    """
    cfg = books.config_from_settings(ctx.settings)
    if entity not in _ENTITIES:
        return f"error: entity must be one of {', '.join(_ENTITIES)}, got {entity!r}"
    try:
        occurred_on = date_type.fromisoformat(date)
    except (TypeError, ValueError):
        return f"error: date must be YYYY-MM-DD, got {date!r}"
    if not isinstance(postings, list) or len(postings) < 2:
        return "error: a transaction needs at least two postings"
    if any(not isinstance(p, dict) for p in postings):
        return "error: every posting must be an object with account, amount and currency"
    accounts = [str(p.get("account") or "").strip() for p in postings]
    if not all(accounts):
        return "error: every posting needs an account"
    amounts: list[Decimal | None] = []
    for p in postings:
        raw = p.get("amount")
        if raw in (None, ""):
            amounts.append(None)
            continue
        try:
            value = Decimal(str(raw))
        except (InvalidOperation, ValueError):
            return f"error: {raw!r} is not an amount"
        # `Decimal` accepts NaN, Infinity and 1e400 — all of which pass a bare
        # constructor call and then raise `InvalidOperation` inside
        # `quantize()`, several frames down in the renderer, where the model
        # gets an exception repr instead of a sentence it can act on.
        if not value.is_finite() or abs(value) >= _MAX_AMOUNT:
            return f"error: {raw!r} is not a usable amount"
        amounts.append(value)
    if sum(1 for a in amounts if a is None) > 1:
        return "error: at most one posting may omit its amount"

    try:
        declared = await books.declared_accounts(cfg)
    except books.BooksError as exc:
        return f"error: {exc}"
    missing = sorted({a for a in accounts if a not in declared})
    if missing:
        return _undeclared(", ".join(missing))

    # Sanitized once, here, so the index and the journal carry the same name —
    # `render_manual` would otherwise sanitize only its half of the pair.
    payee = books.sanitize_payee(payee)
    msgid = _manual_msgid(entity, occurred_on, payee, postings, note)
    block = books.render_manual(occurred_on, payee, postings, msgid, note)
    try:
        rel, created = await books.post_block(block, entity, occurred_on, msgid, cfg)
    except books.BooksError as exc:
        return f"error: {exc}"

    # The index keys on the first amount-bearing posting: it is the one the
    # money moved to or from, and the blank posting has no amount to record.
    lead = next(i for i, a in enumerate(amounts) if a is not None)
    signed = amounts[lead]
    event = MoneyEvent(
        kind="transaction",
        direction="in" if signed < 0 else "out",
        amount=abs(signed),
        currency=str(postings[lead].get("currency") or "INR"),
        payee=payee,
        payee_key=payee_key(payee),
        channel="manual",
        occurred_on=occurred_on,
        entity=entity,
        account=accounts[lead],
        parser="manual",
        source_class="other",
    )
    if not created:
        # The block was already there. Index it only if the row is MISSING —
        # that is the state a retry has to repair (the first attempt committed
        # the journal and was abandoned before it reached the index). Upserting
        # unconditionally would instead drag the index backwards: a reclassify
        # between the two posts moved the row's account, and `ji.upsert` sets
        # `account = EXCLUDED.account`, so the re-post would reset it while the
        # journal correctly keeps the new one.
        if await ji.get(pool, msgid) is None:
            await ji.upsert(pool, msgid, "manual", event, journal_file=rel)
        return f"already posted as {msgid} in {rel}; nothing was written twice"
    await ji.upsert(pool, msgid, "manual", event, journal_file=rel)
    return f"posted {msgid} to {rel}"


@aegis_tool
async def _exec_ledger_reclassify(
    pool: asyncpg.Pool, ctx: ToolContext, *, message_id: str, account: str, payee: str | None = None
) -> str:
    """Move a posting to another account (and optionally rename its payee) by its books message id (`<mailbox>/<gmail id>` or `manual/<uuid>`). The new account must be one the posting's own set of books can use: an expense or income account of that entity, or any asset, liability or equity account, which both sets share.

    Args:
        message_id: the msgid tag of the transaction.
        account: a declared account, e.g. expenses:groceries.
        payee: new display name, optional.
    """
    cfg = books.config_from_settings(ctx.settings)
    try:
        declared = await books.declared_accounts(cfg)
    except books.BooksError as exc:
        return f"error: {exc}"
    if account not in declared:
        return _undeclared(account)
    # The same hazard the apply sweep guards, reachable here in ONE call: an
    # `expenses:hikmah:*` account is declared and the block still balances, so
    # neither the chart check nor `check --strict` objects — and the posting
    # ends up in the wrong entity's journal file, counted in the wrong books.
    # Read from the journal, not the index: the index does not cover a
    # hand-written block, and the journal is the record.
    try:
        located = await books.locate_event(message_id, cfg)
    except books.BooksError as exc:
        return f"error: {exc}"
    if located is None:
        return f"error: no journal block carries msgid {message_id}"
    filed_in = located.split("/")[0]
    belongs_to = _account_entity(account)
    if belongs_to is not None and belongs_to != filed_in:
        return (
            f"error: {account} belongs to the {belongs_to} books but "
            f"{message_id} is filed in {located}. Moving a posting between "
            "entities means moving the block, which this tool does not do."
        )
    try:
        rel = await books.rewrite_event(message_id, cfg, account=account, payee=payee)
    except books.BooksError as exc:
        return f"error: {exc}"
    # After the journal, never before: the journal is the record, and an index
    # row updated for a rewrite that then failed would describe a posting that
    # does not exist.
    await pool.execute(
        _REINDEX_SQL, message_id, account, books.sanitize_payee(payee) if payee else None
    )
    return f"reclassified {message_id} -> {account} in {rel}"


@aegis_tool
async def _exec_ledger_add_rule(
    pool: asyncpg.Pool,
    ctx: ToolContext,
    *,
    match: str,
    account: str,
    entity: str | None = None,
    payee: str | None = None,
    apply: bool = True,
) -> str:
    """Add a payee → account rule to the books and reclassify matching unexplained postings.

    Args:
        match: case-insensitive regex tested against "<sender> | <payee>".
        account: a declared account.
        entity: personal or hikmah, optional.
        payee: canonical display name, optional.
        apply: also reclassify existing postings in an unknown account that match (default true). Existing postings are matched on their payee alone — the sender is not in the index.
    """
    cfg = books.config_from_settings(ctx.settings)
    try:
        re.compile(match)
    except re.error as exc:
        return f"error: {match!r} is not a valid regex: {exc}"
    # This pattern is persisted, and the worker then runs it against every
    # incoming money event in another process, forever. `re` has no timeout and
    # matching happens on the event loop, so a catastrophic pattern is a durable
    # cross-process hang that no caller-side timeout can interrupt. Both bounds
    # are checked BEFORE the rule reaches the file.
    if len(match) > _MAX_MATCH_LEN:
        return f"error: match is longer than {_MAX_MATCH_LEN} characters"
    if _QUANTIFIED_GROUP_RE.search(match):
        return (
            "error: match repeats a group (e.g. `(a+)+`, `(ab)*`, `(a|a)+`) — repeating a "
            "group can take exponential time, and this rule runs against every money event "
            "from now on. Write it without repeating the group."
        )
    if len(_QUANTIFIER_RE.findall(match)) > _MAX_QUANTIFIERS:
        return (
            f"error: match stacks more than {_MAX_QUANTIFIERS} quantifiers (*, +, ?, {{n}}), "
            "which gets slow faster than the length of what it is matched against."
        )
    slow_on = await asyncio.to_thread(_slow_regex_probe, match)
    if slow_on is not None:
        return (
            f"error: match took longer than {int(_REGEX_BUDGET_S * 1000)}ms on a "
            f"{len(slow_on)}-character test string. A payee can be 80 characters, and this "
            "rule runs against every money event from now on, so it has to be quick."
        )
    if entity is not None and entity not in _ENTITIES:
        return f"error: entity must be one of {', '.join(_ENTITIES)}, got {entity!r}"
    try:
        declared = await books.declared_accounts(cfg)
    except books.BooksError as exc:
        return f"error: {exc}"
    if account not in declared:
        return _undeclared(account)

    # Sanitized once, as in `ledger_post`: this name is written to the journal
    # by the rewrite below AND stored in the rule for every future event, so the
    # rule, the journal and the index must all carry the same string.
    payee = books.sanitize_payee(payee) if payee else None
    rule: dict = {"match": match, "account": account}
    if entity:
        rule["entity"] = entity
    if payee:
        rule["payee"] = payee
    try:
        await books.append_rule(rule, cfg)
    except books.BooksError as exc:
        return f"error: {exc}"
    if not apply:
        return "rule added; reclassified 0 postings"

    rows = await pool.fetch(
        "SELECT message_id, payee, entity FROM finance.journal_index "
        "WHERE kind = 'transaction' AND account LIKE '%:unknown' AND journal_file IS NOT NULL"
    )
    targets: list[str] = []
    for row in rows:
        # A rule that names an entity must not move a posting in the OTHER set
        # of books: the account would change while the block stayed in the
        # wrong journal file, which is how `expenses:hikmah:*` lands in
        # `personal/2026.journal`.
        if entity and row["entity"] != entity:
            continue
        # `apply_rules` with an empty sender, so one matcher serves both this
        # sweep and the pipeline that applies the rule to new mail.
        if not books.apply_rules([rule], "", row["payee"] or ""):
            continue
        targets.append(row["message_id"])
    capped = len(targets) > _MAX_APPLY
    targets = targets[:_MAX_APPLY]
    try:
        # ONE write for the whole backlog: one flock, one strict check, one
        # commit, one push. Per-posting writes would hold the books against
        # every other writer for the length of the sweep.
        rewritten, failed = await books.rewrite_events(
            targets, cfg, account=account, payee=payee
        )
    except books.BooksError as exc:
        return f"error: rule added, but reclassifying failed: {exc}"
    for msgid in rewritten:
        await pool.execute(_REINDEX_SQL, msgid, account, payee)
    if failed:
        logger.warning("ledger_add_rule_rewrite_failed", msgids=failed[:20], count=len(failed))
    tail = f" ({len(failed)} failed)" if failed else ""
    if capped:
        tail += f"; stopped at the {_MAX_APPLY}-posting limit, run again to continue"
    return f"rule added; reclassified {len(rewritten)} postings{tail}"
