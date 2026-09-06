"""The three books WRITES — journal first, index second — in one place.

`services/tools/ledger.py` validates a chat tool call and then hands the write
here; `MoneyActivities.books_write` runs the same call inside a Temporal
activity on the worker. Both go through :func:`perform_write`, so there is
exactly ONE implementation of each write and the two processes cannot drift.

Why the split exists (issue #388): a books write is flock → clone-or-pull →
mutate → `hledger check --strict` → commit → push, and `books.py`'s own budgets
add up to :data:`BOOKS_WRITE_TIMEOUT_S`. That is 90% of the 600s the whole chat
turn gets, and `asyncio.wait_for` cannot cancel the thread the write runs in —
so the chat loop could only ever misreport a slow write, never stop it. The
write now runs as its own durable workflow and the tool waits on it briefly.

Three rules hold here, and every caller depends on them:

* **The journal is the record; the index follows it.** Every function below
  writes `finance.journal_index` only AFTER `books.py` has committed, so an
  index row can never describe a posting that does not exist.
* **A refusal is a returned sentence, not a raise.** These run inside the chat
  tool loop (through the activity), where a raise is a failed turn and a
  sentence is something the model can read and correct. `perform_write` turns
  every `BooksError` into `error: …` for that reason.
* **Every write is retry-idempotent, and its workflow id says so.**
  :func:`write_workflow_id` derives the id from the same content the write's
  own idempotency keys on, so a re-issued chat turn re-attaches to the write
  already in flight instead of starting a second one.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date as date_type
from decimal import Decimal, InvalidOperation
from typing import Any

import structlog

from aegis.api.models.money import MoneyEvent, payee_key
from aegis.services import books
from aegis.services import journal_index as ji

logger = structlog.get_logger()

# How long a books WRITE may take, from `books.py`'s own budgets: clone (180s,
# first write only) + pull (120s) + `check --strict` (60s) + commit (60s) +
# push (120s). This is the ACTIVITY's budget now, not a chat tool's: it is what
# `BooksWriteFlow` gives `books_write`, where spending it costs a durable
# workflow its time and nothing else.
BOOKS_WRITE_TIMEOUT_S = books.CLONE_TIMEOUT_S + 120 + 60 + 60 + 120

# Placeholder msgid used while rendering the block a msgid is derived FROM.
# Constant, so it contributes nothing to the digest.
_MSGID_SEED = "manual/0"

# A ledger amount, bounded. `Decimal` happily builds NaN, Infinity and 1e400;
# all three survive a bare constructor and then raise `InvalidOperation` inside
# `quantize()` further down. Nothing real is a trillion of anything.
_MAX_AMOUNT = Decimal("1e12")

# Ceiling on one `ledger_add_rule` sweep. The rewrites are batched into a single
# commit, but `{"match": ".", ...}` would still rewrite the whole unknown
# backlog in one unreviewable change; past this the model is told to narrow the
# rule (or run it again) rather than being handed the entire ledger.
_MAX_APPLY = 200

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


def parse_amounts(postings: list[dict]) -> tuple[list[Decimal | None], str | None]:
    """The postings' amounts as `Decimal`s, or (partial list, problem).

    Shared by the tool's validation and by :func:`_write_post`, so the model is
    refused with the same sentence the writer would have refused on — and the
    writer never re-derives an amount by a second set of rules.
    """
    amounts: list[Decimal | None] = []
    for p in postings:
        raw = p.get("amount")
        if raw in (None, ""):
            amounts.append(None)
            continue
        try:
            value = Decimal(str(raw))
        except (InvalidOperation, ValueError):
            return amounts, f"{raw!r} is not an amount"
        # `Decimal` accepts NaN, Infinity and 1e400 — all of which pass a bare
        # constructor call and then raise `InvalidOperation` inside
        # `quantize()`, several frames down in the renderer, where the model
        # gets an exception repr instead of a sentence it can act on.
        if not value.is_finite() or abs(value) >= _MAX_AMOUNT:
            return amounts, f"{raw!r} is not a usable amount"
        amounts.append(value)
    if sum(1 for a in amounts if a is None) > 1:
        return amounts, "at most one posting may omit its amount"
    return amounts, None


def manual_msgid(
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


def post_msgid(payload: dict) -> str:
    """The msgid a `post` payload will be written under.

    Called twice per write — once by the tool, to name the workflow, and once
    by the writer itself — and it must give the same answer both times, so the
    payee is sanitized here rather than by either caller.
    """
    return manual_msgid(
        payload["entity"],
        date_type.fromisoformat(payload["date"]),
        books.sanitize_payee(payload["payee"]),
        payload["postings"],
        payload.get("note", "") or "",
    )


def write_workflow_id(op: str, payload: dict) -> str:
    """A deterministic workflow id for one write, derived from its own content.

    The point is re-attachment, not naming: a chat turn that is retried (or a
    model that re-issues a call it thinks timed out) must land on the workflow
    already doing that write, and `WorkflowAlreadyStartedError` is what tells
    the caller it did. So the digest is taken from exactly what the write's own
    idempotency keys on — the rendered block for a post, the target and its new
    account for a reclassify, the rule for a rule — and never from a uuid or a
    timestamp, which would make every retry a second write.
    """
    if op == "post":
        digest = post_msgid(payload).split("/", 1)[1]
    elif op == "reclassify":
        digest = _digest(
            f"{payload['message_id']}\n{payload['account']}\n{payload.get('payee') or ''}"
        )
    elif op == "add_rule":
        digest = _digest(
            json.dumps(
                {"rule": payload["rule"], "apply": bool(payload.get("apply", True))},
                sort_keys=True,
                ensure_ascii=False,
            )
        )
    else:
        raise ValueError(f"unknown books write {op!r}")
    return f"books-write-{op.replace('_', '-')}-{digest}"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


async def perform_write(op: str, payload: dict, pool: Any, cfg: books.BooksConfig) -> dict:
    """Run one books write. `{"ok": bool, "message": str}` — never raises for a
    books-level refusal.

    `ok` is False for anything the caller should relay as a failure, including
    a `message` the writer itself produced with an `error:` prefix; a caller
    reading only `message` still gets the whole answer.
    """
    write = _WRITES.get(op)
    if write is None:
        return {"ok": False, "message": f"error: unknown books write {op!r}"}
    try:
        message = await write(pool, cfg, payload)
    except books.BooksError as exc:
        return {"ok": False, "message": f"error: {exc}"}
    return {"ok": not message.startswith("error:"), "message": message}


async def _write_post(pool: Any, cfg: books.BooksConfig, payload: dict) -> str:
    """`ledger_post`'s write half: one rendered block, then its index row."""
    entity = payload["entity"]
    occurred_on = date_type.fromisoformat(payload["date"])
    postings = payload["postings"]
    note = payload.get("note", "") or ""
    # Sanitized once, here, so the index and the journal carry the same name —
    # `render_manual` would otherwise sanitize only its half of the pair.
    payee = books.sanitize_payee(payload["payee"])
    amounts, problem = parse_amounts(postings)
    if problem:  # pragma: no cover — the tool refuses these before dispatching
        return f"error: {problem}"
    msgid = post_msgid(payload)
    block = books.render_manual(occurred_on, payee, postings, msgid, note)
    rel, created = await books.post_block(block, entity, occurred_on, msgid, cfg)

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
        account=str(postings[lead].get("account") or "").strip(),
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


async def _write_reclassify(pool: Any, cfg: books.BooksConfig, payload: dict) -> str:
    """`ledger_reclassify`'s write half: move the block, then its index row."""
    message_id = payload["message_id"]
    account = payload["account"]
    payee = payload.get("payee")
    rel = await books.rewrite_event(message_id, cfg, account=account, payee=payee)
    # After the journal, never before: the journal is the record, and an index
    # row updated for a rewrite that then failed would describe a posting that
    # does not exist.
    await pool.execute(
        _REINDEX_SQL, message_id, account, books.sanitize_payee(payee) if payee else None
    )
    return f"reclassified {message_id} -> {account} in {rel}"


async def _write_add_rule(pool: Any, cfg: books.BooksConfig, payload: dict) -> str:
    """`ledger_add_rule`'s write half: persist the rule, then sweep the backlog.

    The rule dict arrives already built and already validated — it carries its
    own `account`, `entity` and `payee`, so the sweep below reads the rule
    rather than re-deriving what the tool decided.
    """
    rule: dict = payload["rule"]
    account = rule["account"]
    entity = rule.get("entity")
    payee = rule.get("payee")
    await books.append_rule(rule, cfg)
    if not payload.get("apply", True):
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
    try:
        # ONE write for the whole backlog: one flock, one strict check, one
        # commit, one push. Per-posting writes would hold the books against
        # every other writer for the length of the sweep.
        rewritten, failed = await books.rewrite_events(
            targets, cfg, account=account, payee=payee
        )
    except books.BooksError as exc:
        # Not re-raised: the rule IS persisted, and "rule added, but …" is a
        # different fact from "nothing happened". `perform_write` would report
        # a raise as the whole write failing.
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
    #
    # Counted over `rewritten`, NOT over `targets`: the sentence beside it says
    # how many postings MOVED, so a share taken from the wider set would read
    # as "reclassified 2 postings (1 failed); 3 matched the sender" — true,
    # since matching is not moving, and unreadable as anything but a mistake.
    moved_ids = set(rewritten)
    sender_hits = sum(1 for m in sender_only if m in moved_ids)
    if sender_hits:
        tail += (
            f"; {sender_hits} matched the sender rather than the payee, "
            "so this rule is wider than its name"
        )
    if capped:
        tail += f"; stopped at the {_MAX_APPLY}-posting limit, run again to continue"
    return f"rule added; reclassified {len(rewritten)} postings{tail}"


_WRITES = {
    "post": _write_post,
    "reclassify": _write_reclassify,
    "add_rule": _write_add_rule,
}

BOOKS_WRITE_OPS = tuple(_WRITES)
