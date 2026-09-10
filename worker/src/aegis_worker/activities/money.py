"""Money activities — receipt parse, journal posting, brief and month close."""

from __future__ import annotations

import asyncio
import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from aegis.api.models.money import MoneyEvent, payee_key
from aegis.services import books, ledger_write, reconciled
from aegis.services import journal_index as ji
from aegis.services.bank_parsers import has_money_shape, is_autopay, parse_any
from aegis.services.books import UNKNOWN, account_for, instrument_account
from temporalio import activity

from aegis_worker.activities import money_render
from aegis_worker.activities.delivery import safe_send_message

# The cut for `large_unexplained`, owned by the renderer because the renderer
# is what tells the reader the number. Re-exported here so a caller reading the
# data layer sees the same name (issue #391).
LARGE_UNEXPLAINED_CURRENCY = money_render.LARGE_UNEXPLAINED_CURRENCY
LARGE_UNEXPLAINED_MIN = money_render.LARGE_UNEXPLAINED_MIN


def _format_agent_persona(persona: dict) -> str | None:
    """Render soul + user kinds from a get_personality() dict, or None if empty.

    Kept narrow: only voice + user context to steer extraction, not the
    'agents' operational boundaries (receipt parsing doesn't call tools).
    """
    parts: list[str] = []
    s = (persona.get("soul") or "").strip()
    if s:
        parts.append(f"## Identity\n\n{s}")
    u = (persona.get("user") or "").strip()
    if u:
        parts.append(f"## User Context\n\n{u}")
    return "\n\n".join(parts) if parts else None


_NUM_RE = re.compile(r"-?[\d,]+(?:\.\d+)?")
# The commodity token of one amount: everything that is not a digit, a
# separator or a sign. Matches "$", "₹" and an ISO suffix like "CHF" alike.
_COMMODITY_RE = re.compile(r"[^\s\d,.+-]+")
# hledger joins the commodities of an unconvertible balance with a comma AND a
# space. A digit-group comma never has a space after it, so this splits the
# parts without ever cutting "1,00,000.00" in half.
_PART_SPLIT_RE = re.compile(r",\s+")
# Everything in this lane is reported `-X ₹`, so the home commodity's symbol
# is the one amount in a cell that needs no conversion. Defined once and used
# by BOTH the hledger arguments and the parser: if those drift, every foreign
# amount silently becomes a rupee amount.
HOME_SYMBOL = "₹"


def parse_hledger_csv(text: str) -> list[list[str]]:
    """`-O csv` output as rows. Blank lines dropped; nothing else interpreted."""
    return [row for row in csv.reader(io.StringIO(text)) if row]


def split_amount_cell(cell: str) -> list[str]:
    """One CSV amount cell as its per-commodity parts.

    `-X ₹` converts nothing it has no price for, and hledger then writes
    the balance as EVERY commodity at once in a single cell —
    "$ 50.00, ₹ 300.00" — which is what makes a naive first-number read
    report $50 of spend as ₹50.

    Non-breaking spaces are stripped first: they are hledger's digit-group
    separator under some commodity formats, and left in place they end a
    number match after the first group ("1 234.56" → 1).
    """
    text = (cell or "").replace("\u00a0", "").replace("\u202f", "")
    return [part for part in _PART_SPLIT_RE.split(text.strip()) if part.strip()]


def commodity_of(part: str) -> str:
    """The commodity token of one amount part; "" for a bare number like "0"."""
    m = _COMMODITY_RE.search(part)
    return m.group(0) if m else ""


def unconverted_commodities(cell: str) -> list[str]:
    """Commodity tokens in a cell that are NOT the home commodity, in order.

    Non-empty means the cell carries money the report could not value in
    ₹ — there is no price for it in `prices.journal` — so every
    home-currency figure derived from that cell understates reality and has to
    say so.
    """
    out: list[str] = []
    for part in split_amount_cell(cell):
        token = commodity_of(part)
        if token and token != HOME_SYMBOL and token not in out:
            out.append(token)
    return out


def amount_from_cell(cell: str) -> Decimal:
    """The HOME-commodity amount in a CSV cell. Decimal("0") when there is none.

    "₹ 1,234.56" → 1234.56, "0" → 0, "" → 0, and the mixed
    "$ 50.00, ₹ 300.00" → 300.00, never 50.00. A part in another commodity
    is worth an unknown number of rupees, so it contributes nothing here and
    is reported separately by `unconverted_commodities`: an understated total
    the reader is warned about beats a confident wrong one.

    A single part carrying no commodity token at all is taken at face value —
    hledger's bare "0" is the only unlabelled shape it writes.
    """
    for part in split_amount_cell(cell):
        token = commodity_of(part)
        if token and token != HOME_SYMBOL:
            continue
        m = _NUM_RE.search(part)
        if m:
            return Decimal(m.group(0).replace(",", ""))
    return Decimal("0")


def _is_account_cell(cell: str) -> bool:
    """True when a CSV first column names an account, not a report label.

    hledger's csv carries the report's own rows in the same shape as the
    account rows — "Total:", "Net:", the `is` report's "Revenues"/"Expenses"
    headers and its title line. "Total:" contains a colon, so a bare
    `":" in cell` test lets the grand total through and doubles every entity
    subtotal; an account name never ends in one (that would be an empty final
    component), which is what separates the two.
    """
    return ":" in cell and not cell.endswith(":")


logger = structlog.get_logger()

# How far a real record may sit from the day a `~ periodic` rule predicts and
# still be the same obligation. The rule's anchor day is hand-written and the
# bill is paid a day or two either side of it, so exact-date matching would
# leave every near miss duplicated. Three days is what `journal_index`
# already calls "the same money recorded twice" (`_MATCH_DAYS`), and it is the
# cautious end of the range: widening it removes more duplicate lines but
# brings the PREVIOUS cycle's payment within reach of the NEXT one's warning,
# and a warning lost is a payment missed.
#
# It does assume nothing recurs faster than once a week. A rule shorter than
# twice this — `~ every 3 days`, say — puts last cycle's payment inside the
# tolerance of the next prediction and silences it. Every live rule is
# `~ monthly`, and a weekly one is still safe at 3; anything faster needs this
# number reconsidered rather than inherited.
_FORECAST_MATCH_DAYS = 3


def drop_forecast_duplicates(
    forecast: list[dict], obligations: list[tuple[str, Decimal, date]]
) -> list[dict]:
    """The forecast rows the books do not already carry (issue #393).

    A forecast is hledger's prediction from a `~ periodic` rule; it fires
    whether or not the real bill has arrived and whether or not the money has
    already gone. The first live brief therefore listed Apple iCloud+ and
    MSEDCL Suncity twice — once as a due extracted from an email, once as a
    prediction of the same payment — and warned about an Airtel charge printed
    three lines below under "Paid this week".

    `obligations` is `(payee_key, absolute amount, the day it is expected or
    moved)` for every due, settled due and posted payment the index knows
    about in this brief's window. A predicted charge is dropped when one of
    them has the same key, the same amount and a date within
    `_FORECAST_MATCH_DAYS`.

    A *charge*, not a row, and each obligation retires at most one of them.
    `reg` reports POSTINGS, so one predicted transaction that splits across two
    expense accounts arrives as two rows sharing a `txnidx`; compared one at a
    time neither equals the bill. And two rules for the same payee at the same
    size on the same day are two charges — one bill accounting for both would
    take the second out of the brief altogether, which is the only way this
    filter can hide money instead of merely repeating it.

    Every uncertainty keeps the row: the forecast exists to warn about money
    the books have not otherwise seen, so a duplicate line costs a moment and
    a dropped line costs a payment. That is why the amount is compared in the
    home commodity only (`amount_from_cell` reports nothing for a cell `-X`
    could not convert, and 336.40 dollars is not ₹336.40), why one unconvertible
    posting keeps its whole transaction — a partial sum is not a total — why a
    description that normalises to nothing matches nothing, and why a date
    hledger did not write is never assumed to be near anything.
    """
    # Postings of one predicted transaction, in the order hledger wrote them.
    # A row with no `txnidx` stands alone rather than joining anything by
    # date and description: two identical rules would otherwise merge into one
    # charge, which is the mistake this grouping exists to prevent.
    groups: dict[object, list[int]] = {}
    for i, row in enumerate(forecast):
        txn = str(row.get("txnidx") or "")
        groups.setdefault(txn or ("unkeyed", i), []).append(i)

    unused = list(obligations)
    dropped: set[int] = set()
    for members in groups.values():
        rows = [forecast[i] for i in members]
        key = payee_key(str(rows[0].get("description") or ""))
        try:
            when = date.fromisoformat(str(rows[0].get("date") or ""))
        except ValueError:
            continue
        parts = [amount_from_cell(str(r.get("amount") or "")) for r in rows]
        if not key or not all(parts):
            continue
        amount = abs(sum(parts, Decimal(0)))
        match = next(
            (
                j
                for j, (ob_key, ob_amount, ob_when) in enumerate(unused)
                if key == ob_key
                and amount == ob_amount
                and abs((when - ob_when).days) <= _FORECAST_MATCH_DAYS
            ),
            None,
        )
        if match is not None:
            unused.pop(match)
            dropped.update(members)
    return [row for i, row in enumerate(forecast) if i not in dropped]


def match_to_event(row: dict) -> dict:
    """journal_index row → MoneyEvent kwargs (for re-upserting an enriched row)."""
    keys = ("kind", "direction", "amount", "currency", "payee", "payee_key", "channel",
            "instrument", "ref", "occurred_on", "due_on", "entity", "account", "parser",
            "confidence", "source_class")
    return {k: row[k] for k in keys if k in row and row[k] is not None}


@dataclass
class MoneyActivities:
    db_pool: Any
    llm: Any  # LLMClient (for Haiku batch extraction)
    delivery: Any  # DeliveryActivities
    agent_id: str = "maou"
    home_currency: str = "INR"
    # Receipt extraction needs reliable structured JSON. The local fast model
    # (gemma4:e2b) parse-failed ~81% of receipt-shaped mail in prod — wire the
    # smart tier here (worker __main__) so money data stops silently dropping.
    extract_model: str = "gemma4:e2b"
    # The books (spec §5/§10). `books_cfg` is a BooksConfig; None = disabled.
    books_cfg: Any = None
    ignored_mailboxes: frozenset[str] = frozenset()
    mailbox_entities: dict[str, str] = field(default_factory=dict)
    capture: Any = None  # CaptureActivities, for dues (set after construction in __main__)
    home_tz: str = "Asia/Kolkata"
    # FinanceConnector — keyless FX quotes for the books' price file. None =
    # no provider wired, and `refresh_fx_prices` reports itself disabled.
    finance: Any = None

    @activity.defn
    async def store_receipt_email(self, msg: dict, account: str) -> str:
        """Insert raw email into finance.receipt_email; return UUID id.

        Idempotent: ON CONFLICT (message_id) DO NOTHING. On conflict the row's
        `parsed.version` decides (v2): a row that never made it through the
        books pipeline is handed back by id so the caller re-processes it,
        and only a `version >= 2` row is a real duplicate (empty string).
        That is what lets the v1 backlog drain on the next ingest pass.

        `msg` is the Gmail dict from GmailActivities.fetch_emails:
        {id, thread_id, sender, subject, to, date, snippet, internal_date_ms}
        """
        if not self.db_pool:
            return ""
        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO finance.receipt_email
                  (message_id, account, sender, subject, received_at, parsed)
                VALUES (
                    $1, $2, $3, $4,
                    to_timestamp($5::bigint / 1000.0),
                    $6
                )
                ON CONFLICT (message_id) DO NOTHING
                RETURNING id
                """,
                msg.get("id", ""),
                account,
                msg.get("sender", ""),
                msg.get("subject", ""),
                int(msg.get("internal_date_ms") or 0),
                {
                    "snippet": msg.get("snippet", ""),
                    "thread_id": msg.get("thread_id", ""),
                    "to": msg.get("to", ""),
                    "date_header": msg.get("date", ""),
                },
            )
        if row is None:
            async with self.db_pool.acquire() as conn:
                existing = await conn.fetchval(
                    "SELECT id FROM finance.receipt_email WHERE message_id = $1 "
                    "  AND COALESCE((parsed->>'version')::int, 0) < 2",
                    msg.get("id", ""),
                )
            return str(existing) if existing else ""
        return str(row["id"])

    @activity.defn
    async def load_receipts(self, receipt_ids: list[str]) -> list[dict]:
        """Read raw receipt rows for parsing. Returns plain dicts (not records).

        v3 schema has no body_plain column — snippet is stored in parsed jsonb
        and aliased as body_plain, which is the key the extractor reads.

        Prefers the full message text `store_receipt_body` fetched over the
        200-char Gmail snippet, which routinely cuts off before the amount.
        Falls back to the snippet when the body fetch failed or never ran.
        """
        if not receipt_ids:
            return []
        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, account, message_id, sender, subject, "
                "COALESCE(NULLIF(parsed->>'body_text', ''), parsed->>'snippet') AS body_plain, "
                "received_at "
                "FROM finance.receipt_email WHERE id = ANY($1::uuid[])",
                receipt_ids,
            )
        return [
            {
                "id": str(r["id"]),
                "account": r["account"],
                "message_id": r["message_id"],
                "sender": r["sender"],
                "subject": r["subject"],
                "body_plain": r["body_plain"] or "",
                "received_at": r["received_at"].isoformat(),
            }
            for r in rows
        ]

    @activity.defn
    async def store_receipt_body(self, receipt_id: str, body_text: str) -> None:
        """Merge the fetched full text into `parsed.body_text` (spec §2 step 2)."""
        if not self.db_pool or not receipt_id:
            return
        async with self.db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE finance.receipt_email "
                "SET parsed = COALESCE(parsed, '{}'::jsonb) || $2 "
                "WHERE id = $1::uuid",
                receipt_id,
                {"body_text": body_text},
            )

    @activity.defn
    async def find_stuck_receipts(
        self, limit: int = 20, older_than_days: int = 1
    ) -> list[str]:
        """Return up to `limit` receipt_email ids not yet through the books
        pipeline — `parsed.version` below 2 (NULL `parsed` included).

        v2 widened the predicate. Fix #113 asked "did the old extractor run?"
        (`parsed ? 'is_receipt'`); the question now is "did this email reach
        the journal?", which only `store_money_result`'s `version: 2` stamp
        answers. So every v1-classified row is stuck by this definition, which
        is the point: the sweep is how the pre-books backlog gets re-parsed
        into MoneyEvents and posted. Oldest-first so it drains in order;
        `older_than_days` avoids racing a receipt still mid-flight in its
        original MoneyProcessFlow run.
        """
        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id FROM finance.receipt_email "
                "WHERE COALESCE((parsed->>'version')::int, 0) < 2 "
                "  AND received_at < NOW() - ($2 * INTERVAL '1 day') "
                "ORDER BY received_at ASC LIMIT $1",
                limit,
                older_than_days,
            )
        return [str(r["id"]) for r in rows]

    def _rules(self) -> list[dict]:
        if self.books_cfg is None:
            return []
        return books.load_rules(self.books_cfg.path / "rules" / "accounts.yaml")

    @activity.defn
    async def parse_money_email(self, receipt: dict) -> dict:
        """One MoneyEvent for one stored email (spec §2 step 3): deterministic
        parsers, else the LLM on the full body; then mailbox entity, rules,
        account fallback, date fallback."""
        mailbox = receipt.get("account", "")
        if mailbox in self.ignored_mailboxes:
            return MoneyEvent(kind="ignore", entity="none", parser="mailbox").model_dump(
                mode="json"
            )
        sender, subject = receipt.get("sender", ""), receipt.get("subject", "")
        body = receipt.get("body_plain") or ""
        ev = parse_any(sender, subject, body)
        if ev is None and not has_money_shape(f"{subject}\n{body}"):
            # No currency token anywhere, so there is no amount to extract and
            # the extraction is pure cost. This is the budget guard: 324 money
            # extractions burned 522,846 tokens on 2026-09-05 and tripped the
            # governor's kill switch, which blocks EVERY LLM call in AEGIS —
            # email triage included. Most of that was spent on mail triage
            # correctly tags `financial` but which holds no transaction: NSE
            # and BSE alerts, GST portal notices, Groww digests, KDP royalty
            # reports, newsletters. Indexed as `info` so the row is still a
            # record and still stamps version 2 rather than re-sweeping.
            activity.logger.info(
                "parse_money_skipped_no_amount sender=%s subject=%s", sender, subject[:80]
            )
            return MoneyEvent(kind="info", parser="no_amount", confidence=1.0).model_dump(
                mode="json"
            )
        if ev is None:
            system_prompt = None
            if self.agent_id:
                from aegis.services.personalities import get_personality

                system_prompt = _format_agent_persona(
                    await get_personality(self.db_pool, self.agent_id)
                )
            out = await self.llm.extract_money_batch(
                [receipt],
                model=self.extract_model,
                system_prompt=system_prompt,
                db_pool=self.db_pool,
                agent_id=self.agent_id or None,
            )
            item = out[0] if out else {"_parse_failed": True}
            if item.get("_parse_failed"):
                return {"kind": "ignore", "parser": "llm", "_parse_failed": True}
            ev = MoneyEvent(**{k: v for k, v in item.items() if not k.startswith("_")})

        # The mailbox decides the entity, except where a parser recognised a
        # business instrument — that is stronger evidence than which inbox the
        # mail happened to land in.
        #
        # SECURITY: only a deterministic parser may reach the `hikmah` branch.
        # That holds because `_LLM_EVENT_FIELDS` in `aegis/llm/__init__.py`
        # does NOT include `entity`, so an extraction can never carry one. If
        # that allowlist ever gains `entity`, this guard stops being a guard
        # and mail whose body says "this is a Hikmah invoice" routes itself
        # into the business books.
        if ev.entity != "hikmah":
            ev.entity = self.mailbox_entities.get(mailbox, "personal")  # type: ignore[assignment]
        # `ev.direction`, because a rule may name one (issue #396): a rule from
        # an inbound answer must not file this payee's next PAYMENT into an
        # income account. `ev.direction` is nullable and that is passed through
        # honestly — an unknown direction satisfies no directional rule.
        rule = books.apply_rules(self._rules(), sender, ev.payee, direction=ev.direction)
        if rule:
            if rule.get("ignore"):
                ev.kind, ev.entity, ev.parser = "ignore", "none", f"{ev.parser}+rule"
                ev.payee_key = payee_key(ev.payee)
                return ev.model_dump(mode="json")
            if rule.get("entity") in ("personal", "hikmah"):
                ev.entity = rule["entity"]
            if rule.get("payee"):
                ev.payee = str(rule["payee"])
            if rule.get("account"):
                ev.account = str(rule["account"])
        # A transaction with no amount is not a transaction (issue #394). The
        # same shape as the `has_money_shape` gate above, one step later: that
        # one reads the mail, this one reads what came back. 14 live rows are
        # notification mail the extractor labelled `transaction` with nothing
        # to post — Anthropic "Your Max subscription is confirmed", Route 53
        # "Automatic renewal succeeded", Amazon Pay "Update on refund
        # processed". Every amount-bearing transaction in the live index IS
        # posted and every amountless one is not, so this is not lost money and
        # not an extraction failure to retry: the number is genuinely absent
        # from the mail. Left as `transaction` they inflate the transaction
        # count and sit in the index permanently unpostable — and worse, they
        # never settle. `books.post_event` refuses an amountless event, which
        # `post_money_event` reports as `post_failed`; that is in
        # `UNSTAMPED_STATUSES`, so `parsed.version` never reaches 2, and
        # `find_stuck_receipts` has no lower date bound — it re-drives and
        # re-extracts the same mail week after week, paying the extractor each
        # time. As `info` the row indexes, stamps, and is done with.
        #
        # `due` and `failed` are deliberately NOT demoted: an obligation whose
        # size the mail did not state is still an obligation worth surfacing,
        # and `capture_due` already refuses to raise a task for a zero.
        if ev.kind == "transaction" and ev.amount is None:
            ev.kind, ev.parser = "info", f"{ev.parser}+no_amount"
        if not ev.account and ev.kind == "transaction":
            side = "in" if ev.direction == "in" else "out"
            # A guessed category on a low-confidence extraction is worse than
            # no category: it buries a wrong classification in a real account
            # where nobody reviews it. The unknown account is the review queue.
            low = ev.parser == "llm" and ev.confidence < 0.8
            ev.account = (
                UNKNOWN["hikmah" if ev.entity == "hikmah" else "personal"][side]
                if low
                else account_for(ev.category, ev.direction, ev.entity)
            )
        if ev.occurred_on is None and ev.kind == "transaction" and receipt.get("received_at"):
            received = datetime.fromisoformat(receipt["received_at"])
            ev.occurred_on = received.astimezone(ZoneInfo(self.home_tz)).date()
        ev.payee_key = payee_key(ev.payee)
        # Does this mail say the money moves on its own? Read from the text
        # here, where the body still exists — `capture_due` sees only the
        # event. Deliberately NOT `ev.channel == "autopay"`: `channel` is in
        # `_LLM_EVENT_FIELDS`, so a crafted body could claim it and silence a
        # real bill's task. The deterministic autopay parsers need no such
        # help — their own anchor phrases ("Upcoming AutoPay") match here.
        ev.autopay = is_autopay(f"{subject}\n{body}")
        return ev.model_dump(mode="json")

    @staticmethod
    def _looks_raw(payee: str) -> bool:
        """A bank alert's payee that a receipt's payee would improve on — a
        VPA, an account number, or an all-caps card-network descriptor."""
        p = payee or ""
        return "@" in p or p.startswith("a/c") or (p.isupper() and len(p) > 3)

    async def _declared_chart(self) -> frozenset[str]:
        """The declared chart, or empty when the books are unreachable.

        Reading it costs an hledger run, and the index write only needs it to
        canonicalise an instrument spelling, so a failure degrades that one
        convenience rather than stopping the row being indexed — the half that
        is designed to work with no checkout at all (`books_disabled`).
        `books._spawn` turns a missing binary or working copy into a
        `BooksError`, so that is the whole failure surface.
        """
        if self.books_cfg is None:
            return frozenset()
        try:
            return frozenset(
                await asyncio.to_thread(books._declared_accounts_sync, self.books_cfg)
            )
        except books.BooksError as exc:
            activity.logger.warning("declared_chart_unavailable error=%s", exc)
            return frozenset()

    @activity.defn
    async def post_money_event(
        self,
        receipt_id: str,
        mailbox: str,
        message_id: str,
        event: dict,
        todoist_ref: str | None = None,
    ) -> dict:
        """Route one event (spec §2 step 4, §5.4, §7.1). Transactions are
        posted or linked; everything else is indexed only."""
        ev = MoneyEvent(**{k: v for k, v in event.items() if not k.startswith("_")})
        msgid = ji.msgid_for(mailbox, message_id)
        # Only read the chart when there is an instrument to canonicalise --
        # most events have none (199 of 245 live rows), and each read spawns
        # hledger.
        declared = await self._declared_chart() if ev.instrument else frozenset()
        result: dict = {
            "msgid": msgid,
            "status": "indexed",
            "journal_file": None,
            "linked": None,
            "closed_due": None,
        }
        if ev.kind != "transaction" or ev.entity == "none":
            await ji.upsert(
                self.db_pool, msgid, mailbox, ev, todoist_ref=todoist_ref, declared=declared
            )
            return result

        # Idempotency for the whole transaction branch, because `post_event`'s
        # own msgid guard cannot cover the linked path. `find_match` excludes
        # already-linked rows, so a retry of the second-arriving email finds no
        # match and falls through to `post_event` — which looks for a
        # `; msgid: <msgid>` line that a linked email never has, since its id
        # lives only in the counterpart's `receipt:`/`bank:` tag. The result is
        # a second block for the same payment. Two reachable retries: Temporal
        # re-running after `ji.link` succeeded but the due-close below raised,
        # and the stuck sweep re-driving a row whose `store_money_result` never
        # landed. `journal_file` wins over `linked_message_id` so a first-
        # arriving email re-run *after* its counterpart linked still reports
        # the `posted` it reported the first time — it does own a block.
        existing = await ji.get(self.db_pool, msgid)
        if existing is not None and (existing["journal_file"] or existing["linked_message_id"]):
            if existing["journal_file"]:
                result.update(status="posted", journal_file=existing["journal_file"])
            else:
                result.update(status="linked", linked=existing["linked_message_id"])
            activity.logger.info(
                "money_event_already_routed receipt=%s msgid=%s status=%s",
                receipt_id,
                msgid,
                result["status"],
            )
            return result

        # §9.3's ordering rule (spec, "reconciled through" watermark, §15.10
        # item 2): once a period is reconciled for an account, a transaction
        # dated inside it is already in the statement that proved it — post
        # it again and the balance counts the money twice. Only an event WITH
        # an instrument has a watermark to check: 199 of 245 live index rows
        # carry none (#408), so gating on a missing one would gate almost
        # nothing or almost everything, and it is deliberately never gated.
        #
        # The gate fails open. A watermark that cannot be read posts as
        # normal — a check about bookkeeping ORDER must never decide whether
        # the money gets recorded at all, the same shape #449 already burned
        # once with a check about noise deciding what the record said.
        if ev.instrument and ev.occurred_on is not None:
            canon_instrument = books.canonical_instrument(ev.instrument, declared)
            try:
                watermark = await reconciled.reconciled_through(self.db_pool, canon_instrument)
            except Exception as exc:  # noqa: BLE001 — fail open, see above
                activity.logger.warning(
                    "reconciled_watermark_unreadable instrument=%s error=%s — posting as normal",
                    canon_instrument,
                    exc,
                )
                watermark = None
            if watermark is not None and ev.occurred_on <= watermark:
                await ji.upsert(
                    self.db_pool, msgid, mailbox, ev, todoist_ref=todoist_ref, declared=declared
                )
                result["status"] = "reconciled"
                activity.logger.info(
                    "money_event_reconciled_period msgid=%s instrument=%s occurred_on=%s "
                    "watermark=%s — indexed, not posted",
                    msgid,
                    canon_instrument,
                    ev.occurred_on,
                    watermark,
                )
                return result

        cfg = self.books_cfg
        # The bank alert and the vendor receipt for one payment are two emails
        # (spec §5.4). Whichever arrives second enriches the block the first
        # posted rather than posting a duplicate.
        match = await ji.find_match(self.db_pool, ev, msgid)
        try:
            if cfg is None:
                raise books.BooksDisabled("no books config")
            linked_to: str | None = None
            if match is not None:
                other = match["message_id"]
                # EVERY books failure here falls back to posting this event as
                # its own block. `find_match` proves the row HAS a journal_file,
                # not that the block is still in the journal: a lost unpushed
                # commit, a re-clone or a human revert leaves the index pointing
                # at nothing, and `rewrite_event` then raises a plain BooksError.
                # Uncaught, the activity retried that forever and the row stuck.
                # An extra block is recoverable by hand; a stuck activity is not.
                try:
                    if ev.source_class == "receipt":
                        kwargs: dict = {"add_tags": {"receipt": msgid}}
                        if self._looks_raw(match["payee"] or "") and ev.payee:
                            kwargs["payee"] = ev.payee
                        if (match["account"] or "").endswith(":unknown") and (
                            ev.account and not ev.account.endswith(":unknown")
                        ):
                            kwargs["account"] = ev.account
                        try:
                            await books.rewrite_event(other, cfg, **kwargs)
                        except books.BooksCheckError:
                            # The receipt's account is not in the chart. Keep the
                            # cross-reference rather than losing the whole link.
                            kwargs = {"add_tags": {"receipt": msgid}}
                            await books.rewrite_event(other, cfg, **kwargs)
                        fixed_kwargs = {
                            k: v for k, v in kwargs.items() if k in ("payee", "account")
                        }
                        fixed = MoneyEvent(**{**match_to_event(match), **fixed_kwargs})
                        if "payee" in fixed_kwargs:
                            fixed.payee_key = payee_key(fixed.payee)
                        await ji.upsert(
                            self.db_pool, other, match["mailbox"], fixed, declared=declared
                        )
                    else:
                        # Reuse the chart the index read, but fall back to a
                        # read that RAISES: an empty chart here would write
                        # `assets:unknown` over the counterpart's instrument
                        # posting, where an unreadable chart is meant to drop
                        # to the tag-only rewrite below.
                        chart = declared or await asyncio.to_thread(
                            books._declared_accounts_sync, cfg
                        )
                        inst = instrument_account(ev.instrument, chart)
                        try:
                            await books.rewrite_event(
                                other, cfg, instrument_account=inst, add_tags={"bank": msgid}
                            )
                        except books.BooksCheckError:
                            await books.rewrite_event(other, cfg, add_tags={"bank": msgid})
                    await ji.upsert(
                        self.db_pool, msgid, mailbox, ev, linked=other, declared=declared
                    )
                    await ji.link(self.db_pool, msgid, other)
                    linked_to = other
                except books.BooksDisabled:
                    raise  # the checkout is gone entirely — index only, below
                except books.BooksError as exc:
                    activity.logger.warning(
                        "money_enrich_failed msgid=%s match=%s error=%s — posting its own "
                        "block instead; if the matched block does still exist this payment "
                        "is now a DUPLICATE in the journal, remove one with remove_event",
                        msgid,
                        other,
                        exc,
                    )
            if linked_to is not None:
                result.update(status="linked", linked=linked_to)
            else:
                # Also the no-match path. Either way the index records what
                # actually happened — `posted`, with the block it wrote.
                rel = await books.post_event(ev, msgid, cfg)
                await ji.upsert(
                    self.db_pool, msgid, mailbox, ev, journal_file=rel, declared=declared
                )
                result.update(status="posted", journal_file=rel)
        except books.BooksDisabled:
            # The index is the cheap half and stays useful without a checkout:
            # the admin page, dues dedupe and matching all still work, and a
            # later backfill has the rows to post from.
            await ji.upsert(self.db_pool, msgid, mailbox, ev, declared=declared)
            result["status"] = "books_disabled"
            return result
        except books.BooksError as exc:
            # The write was refused and reverted — usually `check --strict` on
            # a chart mismatch (an undeclared account or commodity). Retrying
            # cannot fix that, so an uncaught error burned all three attempts,
            # failed the workflow, and left the row below version 2 for the
            # weekly sweep to re-drive and fail again, forever. Same stuck-row
            # class as the enrichment path above.
            #
            # Index it with NO journal_file: the row exists for the admin page
            # and dues dedupe, `find_match` will not offer a counterpart a
            # block that was never written, and the sweep re-posts it once the
            # chart is fixed. Deliberately NOT stamped as posted — the caller
            # must leave it below version 2 (`UNSTAMPED_STATUSES`).
            activity.logger.warning(
                "money_post_failed msgid=%s error=%s: %s — indexed only, not posted",
                msgid,
                type(exc).__name__,
                exc,
            )
            await ji.upsert(self.db_pool, msgid, mailbox, ev, declared=declared)
            result["status"] = "post_failed"
            return result

        if ev.amount is not None and ev.currency and ev.occurred_on is not None:
            due = await ji.find_open_due(
                self.db_pool, ev.payee_key, ev.amount, ev.currency, ev.occurred_on
            )
            if due is not None:
                # No task ref means `capture_due` indexed the due and withheld
                # the Todoist task — a zero invoice, a twin under another
                # payee's name, or an autopay notice. There is nothing to
                # close, so closing is not a precondition for marking it paid;
                # requiring one is what kept those dues open forever.
                #
                # The same is true of a close that FAILS, which is what this
                # used to gate on (#449). The Todoist task is a projection of
                # the due, not the due itself, so whether Todoist accepted the
                # close is not evidence about whether the money moved — and
                # both failure modes here are permanent, because
                # `mark_due_paid` has one caller and nothing re-drives it: an
                # `item-…` ref queues no completion at all, and a task the user
                # deleted is a permanent 4xx. (A *retryable* failure is fine —
                # `complete_captured_task` queues it to the outbox and returns
                # True.) The comment this replaces promised a retry after the
                # drain that does not exist, so each of the two left a paid
                # bill sitting in every "dues open" count forever, clearable
                # only by hand.
                #
                # An unclosed task is the recoverable failure of the two: it is
                # visible to the user, who can close it, and it is logged here.
                # `mark_due_paid`, not `link`: this payment may already be
                # linked to its own bank/receipt counterpart, and `link` writes
                # both sides, which would overwrite that.
                if (
                    self.capture is not None
                    and due["todoist_ref"]
                    and not await self.capture.complete_captured_task(due["todoist_ref"])
                ):
                    activity.logger.warning(
                        "money_due_task_unclosed due=%s task=%s — the due is marked "
                        "paid regardless; close the Todoist task by hand",
                        due["message_id"],
                        due["todoist_ref"],
                    )
                await ji.mark_due_paid(self.db_pool, due["message_id"], msgid)
                result["closed_due"] = due["message_id"]
        activity.logger.info(
            "money_event_routed receipt=%s msgid=%s status=%s linked=%s closed_due=%s",
            receipt_id,
            msgid,
            result["status"],
            result["linked"],
            result["closed_due"],
        )
        return result

    @activity.defn
    async def store_money_result(
        self, receipt_id: str, event: dict, journal_file: str | None
    ) -> None:
        """Stamp the row as v2-processed (spec §2 step 5)."""
        async with self.db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE finance.receipt_email "
                "SET parsed = COALESCE(parsed, '{}'::jsonb) || $2 WHERE id = $1::uuid",
                receipt_id,
                {
                    "version": 2,
                    "event": {k: v for k, v in event.items() if not k.startswith("_")},
                    "journal_file": journal_file,
                },
            )

    # ------------------------------------------------------------------ books

    @activity.defn
    async def books_write(self, op: str, payload: dict) -> dict:
        """One books write on behalf of a chat tool (issue #388).

        `BooksWriteFlow` calls this; the write itself is
        `aegis.services.ledger_write`, the same module the tool validated
        against, so the tool's refusals and the writer's behaviour cannot
        drift. Returns `{"ok", "message"}` — a books-level refusal comes back
        as `ok: False` with the sentence the model should relay, never as a
        raise, because a raise here is a Temporal retry of a write that was
        deliberately turned down.

        Books unconfigured on the WORKER is worth saying out loud: the tool
        reached its own checkout well enough to validate the call and dispatch
        it, so this is a split configuration, not an idle lane.
        """
        if self.books_cfg is None:
            return {
                "ok": False,
                "message": (
                    "error: the books are not configured on the worker, so the write "
                    "could not run. Nothing was written."
                ),
            }
        return await ledger_write.perform_write(op, payload, self.db_pool, self.books_cfg)

    # Yahoo's FX pair → the hledger commodity symbol its rate prices.
    _FX_SYMBOLS = {"USDINR=X": "$", "GBPINR=X": "£", "EURINR=X": "€"}

    @activity.defn
    async def refresh_fx_prices(self) -> dict:
        """Weekly P lines from the keyless quote provider (spec §7.2 step 1).

        Never raises. The brief that calls this is worth sending with stale
        rates, a quote provider is the least reliable thing in the lane, and
        the books write can fail OUTSIDE the errors `books.py` names: the
        flock is taken before `_write_sync`'s try block, so a permission or
        disk error on `.aegis.lock` escapes as a bare `OSError`. Both the
        fetch and the write therefore catch `Exception`, not `BooksError` —
        only `BaseException` (cancellation, interrupt) still propagates.
        """
        if self.finance is None or self.books_cfg is None:
            return {"written": 0, "errors": ["disabled"]}
        today = datetime.now(ZoneInfo(self.home_tz)).date().isoformat()
        lines: list[str] = []
        errors: list[str] = []
        try:
            quotes = await self.finance.get_quotes(list(self._FX_SYMBOLS))
        except Exception as exc:  # noqa: BLE001 — a dead provider is not a flow failure
            return {"written": 0, "errors": [f"quotes: {str(exc)[:120]}"]}
        for q in quotes or []:
            sym = self._FX_SYMBOLS.get(str(q.get("symbol")))
            price = q.get("price")
            if sym and isinstance(price, int | float) and not isinstance(price, bool) and price > 0:
                lines.append(f"P {today} {sym} ₹{Decimal(str(price)).quantize(Decimal('0.01'))}")
            else:
                errors.append(f"{q.get('symbol')}: {q.get('error') or 'no price'}")
        if lines:
            try:
                await books.append_prices(lines, self.books_cfg)
            except Exception as exc:  # noqa: BLE001 — see the docstring
                return {"written": 0, "errors": [*errors, f"books: {str(exc)[:120]}"]}
        return {"written": len(lines), "errors": errors}

    async def _hl(self, args: list[str], fmt: str = "text") -> str:
        """One read-only hledger call through the allowlisted runner."""
        return await books.run_hledger(args, self.books_cfg, output_format=fmt)

    @activity.defn
    async def build_money_brief(self, days: int = 7) -> dict:
        """Everything the weekly money brief renders (spec §7.2).

        Two independent sources: hledger over the journal (the record) and the
        Postgres index (the only place that knows what was never posted). The
        journal half is wrapped as a unit — with the books unreachable the
        brief still ships the index half rather than nothing, which is what
        keeps an unconfigured or mid-clone checkout from silencing the lane.
        """
        today = datetime.now(ZoneInfo(self.home_tz)).date()
        since = today - timedelta(days=days)
        end = (today + timedelta(days=1)).isoformat()
        brief: dict = {
            "as_of": today.isoformat(),
            "since": since.isoformat(),
            "books_ok": True,
            "entities": {
                "personal": {"income": "0", "expenses": "0"},
                "hikmah": {"income": "0", "expenses": "0"},
            },
            "by_account": [],
            "top_payees": [],
            "forecast": [],
            "bal_text": "",
            "unpushed": 0,
            # Non-empty ⇒ some figure above is in the wrong ballpark because
            # `prices.journal` has no rate for that commodity. The renderer
            # must say so; a confident wrong headline is worse than a caveat.
            "fx_unconverted": [],
            "fx_stale": False,
        }
        unconverted: set[str] = set()
        try:
            if self.books_cfg is None:
                raise books.BooksDisabled("books_cfg is not set on MoneyActivities")
            bal_args = [
                "bal", "-X", HOME_SYMBOL, "-b", since.isoformat(), "-e", end,
                "income", "expenses", "--depth", "2",
            ]
            rows = parse_hledger_csv(await self._hl(bal_args, "csv"))
            for row in rows[1:]:
                if len(row) < 2 or not _is_account_cell(row[0]):
                    continue
                account, balance = row[0], row[1]
                unconverted.update(unconverted_commodities(balance))
                ent = (
                    "hikmah"
                    if account.startswith(("expenses:hikmah", "income:hikmah"))
                    else "personal"
                )
                side = "income" if account.startswith("income") else "expenses"
                brief["entities"][ent][side] = str(
                    Decimal(brief["entities"][ent][side]) + amount_from_cell(balance)
                )
                brief["by_account"].append({"account": account, "balance": balance})
            payees = parse_hledger_csv(await self._hl([
                "bal", "-X", HOME_SYMBOL, "-b", since.isoformat(), "-e", end,
                "expenses", "--pivot", "payee", "--flat", "--sort-amount",
            ], "csv"))
            brief["top_payees"] = [
                {"payee": r[0], "amount": r[1]}
                for r in payees[1:]
                if len(r) >= 2 and r[0].lower() != "total:"
            ][:10]
            # No sweep over `top_payees`: it is the same expense postings the
            # `bal` above already reported, pivoted by payee instead of by
            # account, so it can never carry a commodity that one missed.
            # `--forecast=A..B` excludes B, so the window ends the day AFTER the
            # last day the brief covers — otherwise a charge exactly a
            # fortnight out is missing from the fortnight it is meant to warn
            # about.
            fc = parse_hledger_csv(await self._hl([
                "reg", "-X", HOME_SYMBOL,
                f"--forecast={today.isoformat()}..{(today + timedelta(days=15)).isoformat()}",
                "-b", today.isoformat(), "-e", (today + timedelta(days=15)).isoformat(),
                "expenses", "tag:generated-transaction",
            ], "csv"))
            # reg -O csv: txnidx, date, code, description, account, amount, total
            # `txnidx` is carried because these are POSTINGS: a rule splitting
            # one charge across two expense accounts writes two rows, and only
            # this column says they are the same predicted transaction.
            brief["forecast"] = [
                {"txnidx": r[0], "date": r[1], "description": r[3], "amount": r[5]}
                for r in fc[1:] if len(r) >= 6
            ]
            for row in brief["forecast"]:
                unconverted.update(unconverted_commodities(row["amount"]))
            brief["bal_text"] = await self._hl(bal_args)
            brief["unpushed"] = await books.unpushed_commits(self.books_cfg)
        except books.BooksError as exc:
            logger.warning("money_brief_books_unavailable", error=str(exc)[:200])
            brief["books_ok"] = False
            brief["bal_text"] = ""
        if unconverted:
            logger.warning("money_brief_fx_stale", commodities=sorted(unconverted))
        brief["fx_unconverted"] = sorted(unconverted)
        brief["fx_stale"] = bool(unconverted)
        # `amount IS NOT NULL` is load-bearing, not tidiness: a transaction the
        # writer refused (no amount) is still indexed by `post_money_event`
        # with `account='expenses:unknown'` and an `occurred_on`, so it matches
        # every other clause here. `str(None)` then made `Decimal(u["amount"])`
        # in `large_unexplained` raise `InvalidOperation`, which is not a
        # `BooksError`, so the whole brief died and Temporal retried it forever
        # with nothing saying why.
        unknowns = await self.db_pool.fetch(
            "SELECT message_id, payee, amount, currency, occurred_on, channel "
            "FROM finance.journal_index "
            "WHERE kind = 'transaction' AND account LIKE '%:unknown' AND occurred_on >= $1 "
            "  AND amount IS NOT NULL "
            "ORDER BY amount DESC LIMIT 15",
            since,
        )
        brief["unknowns"] = [
            {
                "msgid": r["message_id"], "payee": r["payee"], "amount": str(r["amount"]),
                "currency": r["currency"], "occurred_on": r["occurred_on"].isoformat(),
                "channel": r["channel"],
            }
            for r in unknowns
        ]
        # Its own aggregate, NOT a filter over `unknowns` (issue #391). That
        # list is `ORDER BY amount DESC LIMIT 15`, so a week with more than 15
        # unexplained rows would have made this the total of the top 15 and
        # called it the total — the one number in the brief whose whole job is
        # to be the real one. The renderer never sees the rows, only this.
        #
        # `abs()` on both sides because `journal_index.amount` is a magnitude
        # with the sign carried by `direction`, and a stray negative would
        # otherwise cancel against a real one instead of adding to it.
        large = await self.db_pool.fetchrow(
            "SELECT count(*) AS n, coalesce(sum(abs(amount)), 0) AS total "
            "FROM finance.journal_index "
            "WHERE kind = 'transaction' AND account LIKE '%:unknown' AND occurred_on >= $1 "
            "  AND amount IS NOT NULL AND currency = $2 AND abs(amount) >= $3",
            since,
            LARGE_UNEXPLAINED_CURRENCY,
            LARGE_UNEXPLAINED_MIN,
        )
        brief["large_unexplained"] = {
            "count": int(large["n"] or 0),
            "total": str(large["total"] or 0),
        }
        dues = await self.db_pool.fetch(
            "SELECT message_id, payee, payee_key, amount, currency, due_on, kind, todoist_ref "
            "FROM finance.journal_index "
            "WHERE kind IN ('due','failed') AND linked_message_id IS NULL "
            "  AND due_on BETWEEN $1 AND $2 ORDER BY due_on",
            today - timedelta(days=7), today + timedelta(days=14),
        )
        brief["dues"] = [
            {
                "msgid": r["message_id"], "payee": r["payee"], "amount": str(r["amount"]),
                "currency": r["currency"], "due_on": r["due_on"].isoformat(), "kind": r["kind"],
                "todoist_ref": r["todoist_ref"],
            }
            for r in dues
        ]
        closed = await self.db_pool.fetch(
            "SELECT message_id, payee, payee_key, amount, currency, due_on "
            "FROM finance.journal_index "
            "WHERE kind IN ('due','failed') AND linked_message_id IS NOT NULL "
            "  AND updated_at >= $1 ORDER BY due_on",
            since,
        )
        brief["closed_dues"] = [
            {
                "msgid": r["message_id"], "payee": r["payee"], "amount": str(r["amount"]),
                "currency": r["currency"],
                "due_on": r["due_on"].isoformat() if r["due_on"] else None,
            }
            for r in closed
        ]
        # The forecast warns about money leaving that the books have not
        # otherwise seen, so anything they HAVE seen is struck off it (issue
        # #393): the dues and closed dues just fetched, plus the payments that
        # already posted in this brief's window. Only home-commodity rows take
        # part — the forecast is reported `-X ₹`, so a foreign amount there is
        # a converted number that no index row can be compared against.
        #
        # `journal_file IS NOT NULL` on the payments is what makes "the books
        # have seen it" true. A transaction the writer REFUSED is indexed with
        # no block, so it is in none of the other sections of this brief and in
        # none of hledger's totals: letting it retire a forecast row would put
        # that money nowhere the reader can see it.
        if brief["forecast"]:
            settled = await self.db_pool.fetch(
                "SELECT payee_key, amount, currency, occurred_on FROM finance.journal_index "
                "WHERE kind = 'transaction' AND amount IS NOT NULL "
                "  AND journal_file IS NOT NULL "
                "  AND occurred_on IS NOT NULL AND occurred_on >= $1",
                since,
            )
            obligations = [
                (r["payee_key"], abs(Decimal(r["amount"])), when)
                for rows, day in ((dues, "due_on"), (closed, "due_on"), (settled, "occurred_on"))
                for r in rows
                if (when := r[day]) is not None
                and r["amount"] is not None
                and r["payee_key"]
                and (r["currency"] or "").upper() == self.home_currency.upper()
            ]
            brief["forecast"] = drop_forecast_duplicates(brief["forecast"], obligations)
        # Postings, not rows (issue #394). The first live brief said "53
        # low-confidence LLM postings" when exactly one low-confidence row had
        # reached the journal: over half of the 53 was the extractor's doubt
        # about mail that is not a money event at all, and the rest never
        # posted. `journal_file IS NOT NULL` is the whole difference between a
        # categorisation the user could go and fix and a number that means
        # nothing.
        brief["low_confidence"] = int(await self.db_pool.fetchval(
            "SELECT count(*) FROM finance.journal_index "
            "WHERE parser = 'llm' AND confidence < 0.8 AND journal_file IS NOT NULL "
            "  AND created_at >= $1",
            since,
        ))
        return brief

    @activity.defn
    async def build_month_close(self) -> dict:
        """The previous calendar month's close (spec §7.3).

        The window is computed here rather than borrowed: the month being
        closed is the one BEFORE today's, and the income statement carries the
        month before that as its comparison column.
        """
        today = datetime.now(ZoneInfo(self.home_tz)).date()
        this_first = today.replace(day=1)
        last = this_first - timedelta(days=1)
        month_first = last.replace(day=1)
        prev_first = (month_first - timedelta(days=1)).replace(day=1)
        close: dict = {
            "month": last.strftime("%Y-%m"),
            "books_ok": True,
            "is_text": "",
            "bs_text": "",
            "is_rows": [],
            "recurring_total": "0",
            # Same contract as the brief: non-empty means `is_rows` and
            # `recurring_total` understate the month because those commodities
            # have no rate in `prices.journal`.
            "fx_unconverted": [],
            "fx_stale": False,
        }
        unconverted: set[str] = set()
        try:
            if self.books_cfg is None:
                raise books.BooksDisabled("books_cfg is not set on MoneyActivities")
            is_args = [
                "is", "-X", HOME_SYMBOL, "-M", "-b", prev_first.isoformat(), "-e", this_first.isoformat(),
                "--depth", "2",
            ]
            close["is_text"] = await self._hl(is_args)
            close["bs_text"] = await self._hl([
                "bs", "-X", HOME_SYMBOL, "-e", this_first.isoformat(), "--depth", "2",
            ])
            rows = parse_hledger_csv(await self._hl(is_args, "csv"))
            # is -M -O csv: a title line, then Account/<month>/<month>, then the
            # account rows interleaved with Revenues/Expenses/Total:/Net: labels.
            close["is_rows"] = [
                {"account": r[0], "prev": r[1], "month": r[2]}
                for r in rows
                if len(r) >= 3 and _is_account_cell(r[0])
            ]
            for row in close["is_rows"]:
                unconverted.update(unconverted_commodities(row["prev"]))
                unconverted.update(unconverted_commodities(row["month"]))
            # Exclusive end again: `..this_first` is what covers the month's
            # own last day, which is exactly when a month-end charge lands.
            fc = parse_hledger_csv(await self._hl([
                "bal", "-X", HOME_SYMBOL,
                f"--forecast={month_first.isoformat()}..{this_first.isoformat()}",
                "-b", month_first.isoformat(), "-e", this_first.isoformat(),
                "expenses", "tag:generated-transaction", "--depth", "1",
            ], "csv"))
            recurring = [r for r in fc[1:] if len(r) >= 2 and r[0].lower() != "total:"]
            for r in recurring:
                unconverted.update(unconverted_commodities(r[1]))
            total = sum((amount_from_cell(r[1]) for r in recurring), Decimal("0"))
            close["recurring_total"] = str(total.quantize(Decimal("0.01")))
        except books.BooksError as exc:
            logger.warning("month_close_books_unavailable", error=str(exc)[:200])
            close["books_ok"] = False
        if unconverted:
            logger.warning("month_close_fx_stale", commodities=sorted(unconverted))
        close["fx_unconverted"] = sorted(unconverted)
        close["fx_stale"] = bool(unconverted)
        close["unknown_count"] = int(await self.db_pool.fetchval(
            "SELECT count(*) FROM finance.journal_index "
            "WHERE kind = 'transaction' AND account LIKE '%:unknown' "
            "  AND occurred_on BETWEEN $1 AND $2",
            month_first, last,
        ))
        close["dues_paid"] = int(await self.db_pool.fetchval(
            "SELECT count(*) FROM finance.journal_index "
            "WHERE kind IN ('due','failed') AND linked_message_id IS NOT NULL "
            "  AND due_on BETWEEN $1 AND $2",
            month_first, last,
        ))
        # `ji.OPEN_DUE_SQL` is the same predicate `/api/admin/money/state`
        # carries: a ₹0 due is not an obligation and can never close, so
        # "still open: 1" for a month in which nothing was owed is a number
        # that only rises (issue #385).
        close["dues_open"] = int(await self.db_pool.fetchval(
            "SELECT count(*) FROM finance.journal_index "
            "WHERE kind IN ('due','failed') AND linked_message_id IS NULL "
            f"  AND due_on BETWEEN $1 AND $2 AND {ji.OPEN_DUE_SQL}",
            month_first, last,
        ))
        return close

    # --------------------------------------------------------- brief output

    # Rendering is a pure function in `money_render`; these thin activities
    # exist so a workflow can reach it without importing it into the sandbox.
    #
    # `HOME_SYMBOL`, not a symbol derived from `self.home_currency`: it is the
    # commodity `build_money_brief` hands to `hledger -X`, so it is the one
    # `unconverted_commodities` measured against. Naming a different currency
    # in the "no exchange rate for …" caveat would describe a conversion that
    # never ran.

    @activity.defn
    async def render_money_brief(self, brief: dict) -> dict:
        return money_render.render_money_brief(brief, HOME_SYMBOL)

    @activity.defn
    async def render_month_close(self, close: dict) -> dict:
        return money_render.render_month_close(close, HOME_SYMBOL)

    @activity.defn
    async def notify_money_message(self, html: str, log_event: str) -> bool:
        """Push one already-rendered message to the agent's channel.

        The caller renders; this only delivers. `safe_send_message` never
        raises, so a dead comms server costs the message, not the run — and it
        returns whether the message actually landed, which is what the flow
        reports as `sent`. Reporting True on the attempt would put `sent: true`
        in `workflow_runs` for a week the user never heard from Maou.
        """
        return await safe_send_message(
            self.delivery, agent_id=self.agent_id, message=html, log_event=log_event
        )

    @activity.defn
    async def write_money_report(self, rel_path: str, text: str) -> None:
        """File a rendered report in the books repo. Best effort.

        No checkout configured (`books_cfg is None`) means the lane is running
        index-only, and a `BooksError` means the write was refused and already
        reverted — neither is worth failing the brief that was just delivered.
        """
        if self.books_cfg is None:
            logger.info("money_report_skipped_no_books", path=rel_path)
            return
        try:
            await books.write_report(rel_path, text, self.books_cfg)
        except books.BooksError as exc:
            logger.warning("money_report_write_failed", path=rel_path, error=str(exc)[:200])
