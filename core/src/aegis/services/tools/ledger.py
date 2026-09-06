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
* **A write is validated here and PERFORMED somewhere else.** The three
  writers refuse what they can refuse — an undeclared account, the wrong set of
  books, a catastrophic regex — and then hand the write to `BooksWriteFlow`,
  which runs `services/ledger_write.py` inside a Temporal activity. The tool
  waits `LEDGER_WRITE_WAIT_S` for it and relays whatever it says. Issue #388:
  the write's own budget is 540s, the whole chat turn gets 600s, and
  `asyncio.wait_for` cannot cancel the thread `books._write` runs in — so
  keeping the write on this side could only ever misreport it, never stop it.
"""

from __future__ import annotations

import asyncio
import math
import multiprocessing
import os
import re
import signal
import threading
import time
import warnings
from datetime import date as date_type
from typing import Literal

import asyncpg
import structlog

from aegis.services import books
from aegis.services import ledger_write as lw
from aegis.services.tools.base import ToolContext
from aegis.services.tools.registry import aegis_tool

logger = structlog.get_logger()

_ENTITIES = ("personal", "hikmah")

# How long the tool waits on the write's workflow before it answers "still
# running". Sized from the measured fast path, not from a round number: two
# GitHub round trips at 2.4s each (`git ls-remote`, this host), `hledger check
# --strict` at 0.03-0.16s over 500-5,000 transactions, and a local commit —
# about 5s, plus the Temporal hop. Twenty seconds is ~4x that, and it sits ten
# seconds under the chat loop's 30s default so the model always gets a sentence
# instead of a timeout error. Slower than this means the flock is held by
# another write (the ingest lane posts receipts through the same checkout); the
# workflow keeps going and reports the outcome to the agent's channel itself.
LEDGER_WRITE_WAIT_S = 20

# The chat loop's per-tool cap for the three writers. It exists only so a
# lowered `tool_timeout_seconds` cannot cut the wait above short — the WRITE is
# no longer on this budget at all (issue #388), which is why this is 30s and
# not the 540s a books write may take.
LEDGER_TOOL_TIMEOUT_S = LEDGER_WRITE_WAIT_S + 10

# Core never imports worker code, so the flow is started by NAME with a plain
# dict on the queue the worker serves — the same seam as `dispatch_agent_run`.
_BOOKS_WRITE_FLOW = "BooksWriteFlow"
_TASK_QUEUE = "aegis-main"

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


async def _dispatch_books_write(ctx: ToolContext, op: str, payload: dict) -> str:
    """Hand a validated write to `BooksWriteFlow` and wait a short while for it.

    Returns the sentence the model relays, and never raises. Three outcomes:
    the write finished inside the wait and the writer's own sentence comes
    back; it did not, and the model is told it is still running under an id;
    or it could not be queued at all, in which case NOTHING was written.

    The workflow id is derived from the write's own content, so a retried chat
    turn — or a model re-issuing a call it believes timed out — attaches to the
    write already in flight instead of starting a second one.

    There is deliberately no in-process fallback for an unreachable Temporal.
    Doing the write here is exactly what issue #388 moved away: the chat loop
    cannot cancel it, so a slow write would still be reported as a failure
    while it went on to commit and push. A refusal writes nothing and says so,
    which is the only failure mode a ledger should have.
    """
    from temporalio.exceptions import WorkflowAlreadyStartedError

    client = ctx.temporal_client
    if client is None:
        return (
            "error: the books write could not be queued — Temporal is not reachable. "
            "Nothing was written; try again once it is back."
        )
    workflow_id = lw.write_workflow_id(op, payload)
    reattached = False
    try:
        handle = await client.start_workflow(
            _BOOKS_WRITE_FLOW,
            {
                "agent_id": ctx.agent_id or "maou",
                "op": op,
                "payload": payload,
                "reply_after_seconds": LEDGER_WRITE_WAIT_S,
            },
            id=workflow_id,
            task_queue=_TASK_QUEUE,
        )
    except WorkflowAlreadyStartedError:
        reattached = True
        handle = client.get_workflow_handle(workflow_id)
    except Exception as exc:  # noqa: BLE001 — a dispatch failure is an answer, not a crash
        logger.warning("books_write_dispatch_failed", op=op, error=str(exc)[:200])
        return (
            f"error: the books write could not be queued: {str(exc)[:200]}. "
            "Nothing was written."
        )
    try:
        result = await asyncio.wait_for(handle.result(), timeout=LEDGER_WRITE_WAIT_S)
    except TimeoutError:
        # Cancelling `handle.result()` stops the WAIT, not the workflow: the
        # write carries on and reports itself to the agent's channel.
        logger.info(
            "books_write_still_running", op=op, workflow_id=workflow_id, reattached=reattached
        )
        return (
            f"the books write is still running as {workflow_id} — longer than "
            f"{LEDGER_WRITE_WAIT_S}s, which usually means it is queued behind another "
            "write. It will finish on its own and report the outcome here. Do not "
            "run it again."
        )
    except Exception as exc:  # noqa: BLE001 — the workflow failed; say so, don't raise
        logger.warning(
            "books_write_failed", op=op, workflow_id=workflow_id, error=str(exc)[:200]
        )
        return f"error: the books write failed: {str(exc)[:200]}"
    message = result.get("message") if isinstance(result, dict) else None
    return str(message) if message else f"the books write {workflow_id} reported nothing"


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
    # The same parse the writer will do, so a refusal here says what the write
    # would have said rather than a second opinion about the same number.
    _, problem = lw.parse_amounts(postings)
    if problem:
        return f"error: {problem}"

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

    return await _dispatch_books_write(
        ctx,
        "post",
        {
            "entity": entity,
            "date": occurred_on.isoformat(),
            # RAW: `ledger_write` sanitizes, so the journal, the index and the
            # msgid all take one pass over the same string.
            "payee": payee,
            "postings": postings,
            "note": note,
        },
    )


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
    return await _dispatch_books_write(
        ctx, "reclassify", {"message_id": message_id, "account": account, "payee": payee}
    )


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
    return await _dispatch_books_write(ctx, "add_rule", {"rule": rule, "apply": apply})
