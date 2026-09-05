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

import re
from datetime import date as date_type
from decimal import Decimal, InvalidOperation
from typing import Literal
from uuid import uuid4

import asyncpg
import structlog

from aegis.api.models.money import MoneyEvent, payee_key
from aegis.services import books
from aegis.services import journal_index as ji
from aegis.services.tools.base import ToolContext
from aegis.services.tools.registry import aegis_tool

logger = structlog.get_logger()

_ENTITIES = ("personal", "hikmah")

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
    """Record a transaction in the books by hand. Each posting is {"account": ..., "amount": ..., "currency": ...}; at most one posting may omit the amount.

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
            amounts.append(Decimal(str(raw)))
        except (InvalidOperation, ValueError):
            return f"error: {raw!r} is not an amount"
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
    msgid = f"manual/{uuid4()}"
    block = books.render_manual(occurred_on, payee, postings, msgid, note)
    try:
        rel = await books.post_block(block, entity, occurred_on, msgid, cfg)
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
    await ji.upsert(pool, msgid, "manual", event, journal_file=rel)
    return f"posted {msgid} to {rel}"


@aegis_tool
async def _exec_ledger_reclassify(
    pool: asyncpg.Pool, ctx: ToolContext, *, message_id: str, account: str, payee: str | None = None
) -> str:
    """Move a posting to another account (and optionally rename its payee) by its books message id (`<mailbox>/<gmail id>` or `manual/<uuid>`).

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
    if entity is not None and entity not in _ENTITIES:
        return f"error: entity must be one of {', '.join(_ENTITIES)}, got {entity!r}"
    try:
        declared = await books.declared_accounts(cfg)
    except books.BooksError as exc:
        return f"error: {exc}"
    if account not in declared:
        return _undeclared(account)

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
    reclassified = failed = 0
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
        try:
            await books.rewrite_event(row["message_id"], cfg, account=account, payee=payee)
        except books.BooksError as exc:
            failed += 1
            logger.warning(
                "ledger_add_rule_rewrite_failed",
                message_id=row["message_id"],
                error=str(exc)[:200],
            )
            continue
        await pool.execute(_REINDEX_SQL, row["message_id"], account, payee)
        reclassified += 1
    tail = f" ({failed} failed)" if failed else ""
    return f"rule added; reclassified {reclassified} postings{tail}"
