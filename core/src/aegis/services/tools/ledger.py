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

import hashlib
import json
import re
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
# A quantifier applied to a group that already contains one (`(a+)+`, `(?:a*)*`,
# `(a{1,3})+`) or to an alternation (`(a|a)+`) is the shape whose backtracking
# is exponential. `re` has no timeout and `books.apply_rules` runs `re.search`
# synchronously on the event loop, so such a pattern hangs the whole process and
# `asyncio.wait_for` cannot interrupt it.
_NESTED_QUANTIFIER_RE = re.compile(r"\([^()]*[*+}|][^()]*\)[*+{]")

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


def _account_entity(account: str) -> str:
    """Which set of books an account belongs to. The chart puts the business
    accounts under a `hikmah` segment (`books._ACCOUNT_MAP`); everything else
    is personal."""
    return "hikmah" if ":hikmah:" in f"{account}:" else "personal"


def _manual_msgid(
    entity: str, d: date_type, payee: str, postings: list[dict], note: str
) -> str:
    """A msgid derived from the transaction itself, so a re-post is a RETRY.

    A `uuid4()` here would be fresh on every call, which means `post_block`'s
    idempotency scan can never match and the model's natural response to a
    timed-out write — call it again — puts a second copy of the same
    transaction in the ledger. Deriving the id from the content makes the
    second call find the first one's block and write nothing.

    The trade is that two genuinely identical transactions on the same day
    collapse into one; the tool says so in its description, and a distinguishing
    `note` (which is part of this digest) records the second.
    """
    material = json.dumps(
        [
            entity,
            d.isoformat(),
            payee,
            note,
            [
                [
                    str(p.get("account") or ""),
                    str(p.get("amount") if p.get("amount") not in (None, "") else ""),
                    str(p.get("currency") or ""),
                ]
                for p in postings
            ],
        ],
        sort_keys=True,
    )
    return f"manual/{hashlib.sha256(material.encode()).hexdigest()[:16]}"


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
    # Indexed even when the block was already there: the first attempt may have
    # committed the write and then been abandoned before it reached the index,
    # which is exactly the state a retry has to repair. `ji.upsert` is
    # idempotent on the msgid.
    await ji.upsert(pool, msgid, "manual", event, journal_file=rel)
    if not created:
        return f"already posted as {msgid} in {rel}; nothing was written twice"
    return f"posted {msgid} to {rel}"


@aegis_tool
async def _exec_ledger_reclassify(
    pool: asyncpg.Pool, ctx: ToolContext, *, message_id: str, account: str, payee: str | None = None
) -> str:
    """Move a posting to another account (and optionally rename its payee) by its books message id (`<mailbox>/<gmail id>` or `manual/<uuid>`). The new account must belong to the same set of books the posting is filed in.

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
    if _account_entity(account) != filed_in:
        return (
            f"error: {account} belongs to the {_account_entity(account)} books but "
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
    if _NESTED_QUANTIFIER_RE.search(match):
        return (
            "error: match repeats a group that already repeats (e.g. `(a+)+`, `(a|a)+`) — "
            "that pattern can take exponential time, and this rule runs against every "
            "money event from now on. Write it without the outer repetition."
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
