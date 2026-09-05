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
import math
import multiprocessing
import os
import re
import signal
import threading
import time
import warnings
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
#
# The three static bounds — a length cap, no quantified group, a quantifier cap
# — live in `books.rule_match_problem`, because the same three are applied
# again when the file is LOADED (issue #390): a rule hand-edited into the yaml,
# or written before those bounds existed, never passed through here. One
# implementation, two call sites; this one turns its answer into a sentence the
# model can act on.
#
# The BEHAVIOURAL check below stays here, and here only, because it forks a
# killable subprocess: one fork per rule per incoming email is not a price the
# ingest lane can pay, so it is a write-time gate on top of the static bounds
# the loader can afford. It is also the only check that catches quantifier
# stacking with no group at all. The probes are worst-case shapes for a
# backtracking engine at a length a real payee can reach (80 chars), ordered
# SHORTEST FIRST and stopping at the first one over budget — measured,
# `((a+))+$` costs 0.9s at 24 characters where at 48 it would not finish this
# decade, so the ordering alone turns a hang into a refusal.
#
# It runs in a SUBPROCESS the parent kills, because `re` cannot be interrupted
# and the static bounds are an argument about constants: correct today, and
# erodible by anyone who edits them without reading the measurements. Every
# layer of this particular defence has already been bypassed once (the first
# guard caught `(a+)+` and missed `((a+))+`), and what it protects is the money
# ingest lane in TWO processes, permanently, against a pattern a language model
# wrote and we then persisted. A kill is self-enforcing: it cannot be weakened
# by accident, only by deletion. The static bounds stay in front of it — they
# refuse fast, with a message that says what to change; this is the backstop,
# not the first line.
_REGEX_BUDGET_S = 0.1
_REGEX_PROBES = ("a" * 24 + "!", "a" * 48 + "!", "ab" * 24 + "!", "0" * 48 + " x")
# Wall clock the probe child gets before it is killed. Far above the ~1.2s worst
# case the bounds above allow, because a false refusal here is a real rule the
# user cannot add; reaching it at all means those bounds have a hole.
_REGEX_KILL_S = 10.0

# The probe child's exit codes. `1` is reserved for "it crashed" and is NOT a
# probe index, because `_bootstrap`'s own generic handler also exits 1: sharing
# the code made a `MemoryError` (or a broken `spawn` bootstrap) report itself as
# a slow 9-character probe — a confident, wrong diagnostic that sent the reader
# after a performance problem that did not exist.
_CODE_CRASHED = 1
_CODE_SLOW = 2

# One byte is the whole channel: `os._exit(256)` truncates to 0, which would
# report a slow pattern as safe. Enforced at import rather than left as a note,
# because "keep this list short" is exactly the kind of rule that erodes.
if _CODE_SLOW + len(_REGEX_PROBES) > 255:
    raise RuntimeError("too many regex probes to encode in an exit code")

# One probe at a time per process. `multiprocessing` reaps children through
# shared state — every `Process.start()` calls `process._cleanup()`, which polls
# OTHER threads' process objects — so concurrent probes race each other's
# `waitpid`. Measured at concurrency 8: 10 `ValueError: Cannot close a process
# while it is still running` in 240 calls, one false "did not finish" on a child
# that had already exited, and 28 `os.kill` calls on a pid that was no longer
# ours. The first of those escapes as an exception from a function whose
# docstring promises never to raise, and the last can signal an unrelated
# process. Serialising removes the race itself rather than catching its
# symptoms, and caps concurrent fork cost as a side effect.
_PROBE_LOCK = threading.Lock()


def _close_quietly(proc) -> None:
    """`Process.close()`, which polls and so can still lose a `waitpid` race to
    something outside this module. Releasing the handle is a courtesy; failing
    to is not worth an exception from a refusal path."""
    try:
        proc.close()
    except ValueError:
        pass

# One index row per reclassified posting; the journal is the record, so this
# only keeps the index from disagreeing with it.
_REINDEX_SQL = (
    "UPDATE finance.journal_index SET account = $2, payee = COALESCE($3, payee), "
    "updated_at = now() WHERE message_id = $1"
)

# The unexplained backlog, WITH the sender the worker matches on.
#
# `apply_rules` runs against `"<From header> | <payee>"`, so a sweep with an
# empty sender previews a narrower rule than the one being persisted: the live
# file carries bare words (`apple`, `medium`, `docker`, `github`, `reddit`),
# and Google Pay mirrors MSEDCL, Airtel and the rest, so `match: "google"`
# re-files bills whose payee never says Google. The sender is not a column on
# `journal_index` — it lives on the receipt this posting came from, keyed on
# the gmail id, which is the half of `<mailbox>/<gmail id>` after the slash.
# A LEFT join, because a hand-written `manual/<hash>` block has no receipt and
# genuinely has no sender; `''` is what production passes for those too. The
# `<> ''` is the join's own floor: `split_part` returns the empty string for a
# msgid with no slash, and joining that to an empty receipt id would hand one
# posting another mail's sender.
_SWEEP_SQL = """
SELECT ji.message_id, ji.payee, ji.entity, ji.direction,
       COALESCE(re.sender, '') AS sender
  FROM finance.journal_index ji
  LEFT JOIN finance.receipt_email re
         ON re.message_id = split_part(ji.message_id, '/', 2)
        AND re.message_id <> ''
 WHERE ji.kind = 'transaction'
   AND ji.account LIKE '%:unknown'
   AND ji.journal_file IS NOT NULL
"""


def _undeclared(account: str) -> str:
    return (
        f"error: account {account} is not declared in the chart "
        "(accounts.journal). Declare it there first, or pick a declared one."
    )


def _regex_probe_child(pattern: str, probes: tuple[str, ...], alarm_s: int) -> None:
    """Run every probe and report through the exit code. Runs in a FORK of a
    live API process.

    **The rule for editing this function: it must not import, log, allocate a
    lock, or use anything it inherited from the parent.** A fork carries the
    parent's locks in whatever state the other threads left them, and the
    threads that would release them do not exist here — so a `logger.info` on a
    handler another thread held mid-emit deadlocks this process, and that is a
    deadlock inside the code whose only purpose is not hanging. `re`,
    `time.perf_counter`, `signal` and `os._exit` are the whole safe vocabulary.
    (One inherited handle is already touched before this runs, by
    `multiprocessing` itself: `_close_stdin` swaps the child's stdin for
    `/dev/null`. That is its business, not ours.)

    The exit code IS the channel: `_CODE_CRASHED` for a failure, `_CODE_SLOW +
    index` for a probe over budget, 0 when every probe was quick. A pipe would
    have to be drained by a parent that may be about to kill the writer, and a
    killed writer holding a full pipe is a deadlock; an integer needs no
    draining.

    `os._exit` is NOT what stops interpreter shutdown — `popen_fork` already
    wraps `_bootstrap` in `finally: os._exit(...)`, so the parent's `atexit`
    handlers never run here either way. What this line actually skips is the
    tail of `BaseProcess._bootstrap`, and specifically `util._flush_std_streams`:
    the child holds a COPY of the parent's stdout/stderr buffers as they stood
    at fork, and flushing them prints the parent's un-flushed output a second
    time.

    `signal.alarm` is the child's own deadline, and it exists because
    `daemon=True` does not bound anything: daemon children are killed by
    `util._exit_function`, an `atexit` handler, which a `SIGKILL`ed parent never
    runs — measured, an orphan spun at 100% for 115 seconds after its parent
    died. SIGALRM's default action terminates, so the bound lives in the process
    that has to honour it rather than in one that may be gone.
    """
    # The DISPOSITION is inherited state too, and a deadline that depends on the
    # parent's is not a deadline. Measured: under pytest-timeout (which installs
    # a SIGALRM handler) the alarm arrived as an ordinary Python exception, was
    # caught by the `except` below, and reported itself as a crash instead of
    # terminating. SIG_DFL is what makes the signal kill this process.
    try:
        signal.signal(signal.SIGALRM, signal.SIG_DFL)
    except (ValueError, OSError):
        # Only the main thread may set a disposition. `_bootstrap` has already
        # made this one main, so this is the belt to that braces: the inherited
        # handler still ends the process, just via the crash code.
        pass
    signal.alarm(alarm_s)
    code = 0
    try:
        compiled = re.compile(pattern, re.I)
        for index, probe in enumerate(probes):
            started = time.perf_counter()
            compiled.search(probe)
            if time.perf_counter() - started > _REGEX_BUDGET_S:
                code = _CODE_SLOW + index
                break
    except BaseException:  # noqa: BLE001 — MemoryError and friends included
        code = _CODE_CRASHED
    os._exit(code)


def _regex_too_slow(pattern: str, kill_after: float = _REGEX_KILL_S) -> str | None:
    """Why this pattern is too slow to persist, or None if it is quick enough.

    The measurement happens in a child process so that the answer does not
    depend on the pattern cooperating: `re` holds its thread until the match
    completes and ignores every timeout Python can express, so the only real
    bound is a process the parent can kill. The child times each probe itself,
    which keeps the 100ms judgment free of process-startup noise; the parent's
    `kill_after` is purely the safety net.

    Failure is a refusal, never an exception: a crashed or killed child means we
    could not establish that the pattern is safe, and an unestablished pattern
    does not get written to a file the worker will read forever. That promise is
    what `_PROBE_LOCK` is for — see the comment on it.
    """
    ctx = multiprocessing.get_context(
        "fork" if "fork" in multiprocessing.get_all_start_methods() else "spawn"
    )
    proc = ctx.Process(
        target=_regex_probe_child,
        args=(pattern, _REGEX_PROBES, math.ceil(kill_after) + 1),
        daemon=True,
    )
    with _PROBE_LOCK:
        with warnings.catch_warnings():
            # Python 3.12 warns that forking a multi-threaded process can
            # deadlock the child, and it is right in general. It does not apply
            # to this child, which is why the warning is silenced HERE and
            # nowhere else: `_regex_probe_child`'s docstring carries the rule
            # that keeps it true, and the reviewer ran 240 concurrent probes
            # against threads spinning on a logging handler without one
            # deadlocking. `spawn` would avoid the question, at about a second
            # of interpreter startup on a tool the user is waiting on.
            warnings.simplefilter("ignore", DeprecationWarning)
            proc.start()
        proc.join(kill_after)
        # `exitcode`, not `is_alive()`: both poll, but this reads the value the
        # decision is actually made on, so a poll that loses its race reports
        # "still running" once rather than "alive" and then a stale code.
        code = proc.exitcode
        if code is None:
            proc.kill()
            proc.join()
            code = proc.exitcode
            _close_quietly(proc)
            return f"did not finish within {kill_after:g}s and had to be stopped"
        _close_quietly(proc)
    if code == 0:
        return None
    if _CODE_SLOW <= code < _CODE_SLOW + len(_REGEX_PROBES):
        probe = _REGEX_PROBES[code - _CODE_SLOW]
        return (
            f"took longer than {int(_REGEX_BUDGET_S * 1000)}ms on a "
            f"{len(probe)}-character test string"
        )
    if code == -signal.SIGALRM:
        # The child's own deadline fired, which means the parent's did not —
        # an orphaned child bounding itself. Same answer, different enforcer.
        return f"did not finish within {kill_after:g}s and had to be stopped"
    return f"could not be measured safely (the check exited {code})"


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
        entity: personal or hikmah — which set of books. Every expense and income account already belongs to one of them (expenses:hikmah:* and income:hikmah:* are hikmah, any other is personal), so the entity has to agree with the accounts posted; asset, liability and equity accounts belong to both and fit either.
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
    # The THIRD door onto the entity split, and the one that was open. The
    # chart check above says the account exists; it says nothing about which
    # set of books owns it, so `entity="hikmah"` with `expenses:groceries`
    # balanced, passed `check --strict` and wrote a personal account into
    # `hikmah/2026.journal` — where `ledger_reclassify` then REFUSES to correct
    # it, because its own cross-entity guard blocks the move. The repair path
    # was narrower than the path that made the mess.
    #
    # `account_entity` returns None for the asset, liability and equity trees,
    # which both sets of books share by design (`post_event` writes
    # `assets:bank:*` into either through `instrument_account`), so those go on
    # working under any entity — the hazard lives entirely in the two trees
    # that carry an entity.
    misfiled = sorted({a for a in accounts if (books.account_entity(a) or entity) != entity})
    if misfiled:
        other = "hikmah" if entity == "personal" else "personal"
        return (
            f"error: {', '.join(misfiled)} {'belongs' if len(misfiled) == 1 else 'belong'} "
            f"to the {other} books, and this transaction is being filed under {entity}. "
            f"Post it under {other}, or pick a {entity} expense or income account — "
            "asset, liability and equity accounts belong to both."
        )

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
    """Move a posting to another account (and optionally rename its payee) by its books message id (`<mailbox>/<gmail id>` for a posting from mail, or `manual/<hash>` for one `ledger_post` wrote). The new account must be one the posting's own set of books can use: an expense or income account of that entity, or any asset, liability or equity account, which both sets share.

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
    belongs_to = books.account_entity(account)
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
    direction: str | None = None,
    payee: str | None = None,
    apply: bool = True,
) -> str:
    """Add a payee → account rule to the books and reclassify matching unexplained postings.

    Args:
        match: case-insensitive regex tested against "<sender> | <payee>".
        account: a declared account.
        entity: personal or hikmah. Optional — an expense or income account already says which set of books it belongs to, so leaving this out takes the account's own entity (expenses:hikmah:* and income:hikmah:* are hikmah, any other expense or income account is personal). Asset, liability and equity accounts belong to both, and a rule on one gets no entity.
        direction: in or out. Optional, and NOT inferred from the account — leaving it out means the rule files this payee whichever way the money moves, which is what every rule written before this field existed does. Give it when the same name moves money both ways and the two belong in different accounts (a person you both pay and are paid by), so a payment is not filed to the income account you picked for a credit.
        payee: canonical display name, optional.
        apply: also reclassify existing postings in an unknown account that match (default true). They are matched exactly as future mail will be, against "<sender> | <payee>", so the count is the rule's real reach over the backlog; the reply says how many matched only because of the sender.
    """
    cfg = books.config_from_settings(ctx.settings)
    # This pattern is persisted, and the worker then runs it against every
    # incoming money event in another process, forever. `re` has no timeout and
    # matching happens on the event loop, so a catastrophic pattern is a durable
    # cross-process hang that no caller-side timeout can interrupt. Every bound
    # is checked BEFORE the rule reaches the file — and the same three static
    # ones are checked AGAIN when the file is read, by the loader that hands
    # them to `re.search` (issue #390).
    problem = books.rule_match_problem(match)
    if problem:
        return f"error: match {problem}"
    too_slow = await asyncio.to_thread(_regex_too_slow, match)
    if too_slow is not None:
        return (
            f"error: match {too_slow}. A payee can be 80 characters, and this rule runs "
            "against every money event from now on, so it has to be quick."
        )
    if entity is not None and entity not in _ENTITIES:
        return f"error: entity must be one of {', '.join(_ENTITIES)}, got {entity!r}"
    # Refused here rather than written and then skipped by `load_rules`, which
    # is what an unusable direction earns on the way back in (issue #396).
    if direction is not None and direction not in books.RULE_DIRECTIONS:
        return (
            f"error: direction must be one of {', '.join(books.RULE_DIRECTIONS)} "
            f"(or left out for either), got {direction!r}"
        )
    try:
        declared = await books.declared_accounts(cfg)
    except books.BooksError as exc:
        return f"error: {exc}"
    if account not in declared:
        return _undeclared(account)
    # An omitted entity is not "both books" — it is an unstated one, and the
    # account itself states it. Without this default a caller can persist an
    # `expenses:hikmah:*` rule with no entity, and every future mail from that
    # payee then gets the hikmah account written into whichever journal the
    # MAILBOX chose: `post_event` files by `event.entity`, which the rule never
    # corrected. Same permanent drift `ledger_reclassify` refuses above, one
    # door along.
    #
    # An explicit entity that CONTRADICTS the account is refused rather than
    # honoured. It used to win, on the reasoning that the caller might be
    # filing a shared bank account's rule against one set of books — but that
    # is exactly the case where `account_entity` returns None and there is
    # nothing to contradict. Where it returns an entity, the account has
    # already stated the answer, and a rule saying otherwise stamps every
    # future mail from that payee with one entity while pointing at the other
    # one's account: `post_event` files by `event.entity`, so the block lands
    # in the wrong journal and the sweep below rewrites the backlog to match.
    # An explicit entity that AGREES is still accepted, and so is any entity on
    # an entity-neutral account.
    belongs_to = books.account_entity(account)
    if entity is None:
        entity = belongs_to
    elif belongs_to is not None and belongs_to != entity:
        return (
            f"error: {account} belongs to the {belongs_to} books, but this rule says "
            f"entity {entity}. Leave the entity out and the account's own is used, or "
            f"name a {entity} account — asset, liability and equity accounts belong "
            "to both."
        )

    # Sanitized once, as in `ledger_post`: this name is written to the journal
    # by the rewrite below AND stored in the rule for every future event, so the
    # rule, the journal and the index must all carry the same string.
    payee = books.sanitize_payee(payee) if payee else None
    rule: dict = {"match": match, "account": account}
    if entity:
        rule["entity"] = entity
    # Never derived from the account, unlike `entity` above. An entity is a
    # property of the ACCOUNT — `expenses:hikmah:*` IS hikmah — so deriving it
    # states a fact the account already carries. A direction is not: an account
    # says what a posting is FOR, never which way the money went.
    #
    # The chart says so itself. `equity:transfers` is declared "between own
    # accounts when the far side is unknown", and a transfer moves either way;
    # `post_event` writes `assets:*` and `liabilities:card:*` through
    # `instrument_account`, which has no notion of direction at all. Deriving
    # `out` from an expense account would also be a mass silent narrowing:
    # 26 of the 28 rules in the live file point at `expenses:*` and not one of
    # their authors asked for a direction. The two that would escape are the
    # case in miniature — one is `ignore: true` with no account, and the other
    # files `liabilities:emi:bajaj`, a liability money moves against both ways.
    if direction:
        rule["direction"] = direction
    if payee:
        rule["payee"] = payee
    try:
        await books.append_rule(rule, cfg)
    except books.BooksError as exc:
        return f"error: {exc}"
    if not apply:
        return "rule added; reclassified 0 postings"

    rows = await pool.fetch(_SWEEP_SQL)
    targets: list[str] = []
    sender_only: list[str] = []
    for row in rows:
        # A rule that names an entity must not move a posting in the OTHER set
        # of books: the account would change while the block stayed in the
        # wrong journal file, which is how `expenses:hikmah:*` lands in
        # `personal/2026.journal`.
        if entity and row["entity"] != entity:
            continue
        # The SAME haystack production will use — `"<sender> | <payee>"` — and
        # the posting's OWN direction, so a rule that names one sweeps only the
        # half of the backlog it will go on filing (issue #396).
        text = row["payee"] or ""
        moved = row["direction"]
        if not books.apply_rules([rule], row["sender"], text, direction=moved):
            continue
        targets.append(row["message_id"])
        if row["sender"] and not books.apply_rules([rule], "", text, direction=moved):
            sender_only.append(row["message_id"])
    capped = len(targets) > _MAX_APPLY
    targets = targets[:_MAX_APPLY]
    kept = set(targets)
    sender_hits = sum(1 for m in sender_only if m in kept)
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
    # The count alone cannot warn: a rule matching the SENDER reaches payees
    # whose names are nothing like it, and the caller who wrote `google` was
    # thinking about Google, not about every bill Google Pay mirrors. Naming
    # the sender-only share turns the number into something the caller can act
    # on while the rule is still one edit old.
    if sender_hits:
        tail += (
            f"; {sender_hits} matched the sender rather than the payee, "
            "so this rule is wider than its name"
        )
    if capped:
        tail += f"; stopped at the {_MAX_APPLY}-posting limit, run again to continue"
    return f"rule added; reclassified {len(rewritten)} postings{tail}"
