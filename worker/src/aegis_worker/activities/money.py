"""Money Hygiene activities — receipt parse, charge upsert, alerts, audit."""

from __future__ import annotations

import asyncio
import csv
import html as _html
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from aegis.api.models.money import MoneyEvent, payee_key
from aegis.services import books
from aegis.services import journal_index as ji
from aegis.services.bank_parsers import has_money_shape, is_autopay, parse_any
from aegis.services.books import UNKNOWN, account_for, instrument_account
from aegis.services.fx import to_monthly_home
from aegis.services.money_format import fmt_money
from temporalio import activity

from aegis_worker.activities import money_render
from aegis_worker.activities.delivery import safe_send_message

_ONE_DAY = timedelta(days=1)

# Display symbol for digest rendering, keyed by ISO currency code. Unknown
# codes fall back to "<CODE> " (e.g. "CHF ") via _symbol() below.
_CURRENCY_SYMBOL = {
    "INR": "₹",
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "JPY": "¥",
    "SGD": "S$",
    "AUD": "A$",
    "CAD": "C$",
}


def _symbol(code: str) -> str:
    """Digest currency symbol for `code`, or "<CODE> " if unmapped."""
    return _CURRENCY_SYMBOL.get(code, f"{code} ")


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


def _previous_month_window(today: date) -> tuple[date, date]:
    """Return (period_start, period_end) for the calendar month BEFORE `today`.

    period_start = first day of previous month.
    period_end = last day of previous month (= first-of-this-month minus 1 day).

    Pure stdlib so we don't need python-dateutil.
    """
    first_of_this = today.replace(day=1)
    period_end = first_of_this - _ONE_DAY
    period_start = period_end.replace(day=1)
    return period_start, period_end


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


# Bank / card-alert sender domains. Mail from these addresses is transactional
# *alerts* (autopay reminders, failed-payment notices, credit-card statements),
# NOT vendor receipts — yet they quote figures the extractor can mistake for a
# recurring charge (verified prod offenders: an autopay reminder or card
# statement minting a fake recurring charge, a failed payment-gateway notice).
# The LLM prompt hardening is best-effort; this is the belt-and-suspenders
# deterministic guard. Match is case-insensitive substring. Configured via
# Settings.bank_alert_senders (admin Integrations page; AEGIS_BANK_ALERT_SENDERS
# env fallback), injected into MoneyActivities at worker bootstrap — default
# empty means the guard is a clean no-op until a self-hoster adds their own
# bank's domains.
def parse_bank_alert_senders(raw: str) -> frozenset[str]:
    """Comma-separated domains -> normalized frozenset (lowercased, stripped)."""
    return frozenset(s.strip().lower() for s in (raw or "").split(",") if s.strip())


def match_to_event(row: dict) -> dict:
    """journal_index row → MoneyEvent kwargs (for re-upserting an enriched row)."""
    keys = ("kind", "direction", "amount", "currency", "payee", "payee_key", "channel",
            "instrument", "occurred_on", "due_on", "entity", "account", "parser",
            "confidence", "source_class")
    return {k: row[k] for k in keys if k in row and row[k] is not None}


def _is_bank_alert_sender(*candidates: str, senders: frozenset[str]) -> bool:
    """True if any candidate sender string contains a known bank-alert domain."""
    for cand in candidates:
        if not cand:
            continue
        low = cand.lower()
        if any(domain in low for domain in senders):
            return True
    return False


@dataclass
class MoneyActivities:
    db_pool: Any
    llm: Any  # LLMClient (for Haiku batch extraction)
    delivery: Any  # DeliveryActivities
    fx_rates: dict[str, float]
    agent_id: str = "maou"
    home_currency: str = "INR"
    # Receipt extraction needs reliable structured JSON. The local fast model
    # (gemma4:e2b) parse-failed ~81% of receipt-shaped mail in prod — wire the
    # smart tier here (worker __main__) so money data stops silently dropping.
    extract_model: str = "gemma4:e2b"
    # Bank/card alert sender domains — deterministic guard; see
    # parse_bank_alert_senders. Injected from Settings.bank_alert_senders
    # (admin Integrations page, env AEGIS_BANK_ALERT_SENDERS fallback).
    bank_alert_senders: frozenset[str] = frozenset()
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

        v3 schema has no body_plain column — snippet is stored in parsed jsonb.
        Aliased as body_plain so classify_and_extract callers remain unchanged.

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

    @activity.defn
    async def classify_and_extract(
        self, receipts: list[dict], agent_id: str = ""
    ) -> list[dict]:
        """Single LLM batch call → one extraction per receipt.

        `agent_id` — when set, loads the agent's persona (soul + user
        kinds, DB-first via aegis.services.personalities) and passes it
        as system context so the extractor reflects that agent's
        voice/policy (e.g. maou for subscription classification).

        Returns list of dicts with the receipt's `id` echoed as
        `receipt_id` so upsert_charges can correlate the extraction
        back to its source row.
        """
        if not receipts:
            return []
        system_prompt = None
        if agent_id:
            from aegis.services.personalities import get_personality

            persona = await get_personality(self.db_pool, agent_id)
            system_prompt = _format_agent_persona(persona)
        extractions = await self.llm.extract_receipts_batch(
            receipts,
            model=self.extract_model,
            system_prompt=system_prompt,
            db_pool=self.db_pool,
            agent_id=agent_id or None,
        )
        for r, e in zip(receipts, extractions, strict=False):
            e["receipt_id"] = r["id"]
            # Echo the real email sender so upsert_charges can deterministically
            # skip bank/card-alert senders (the LLM's sender_label is best-effort
            # and on autopay reminders names the merchant, not the sender).
            e["sender"] = r.get("sender", "")
        return extractions

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
        rule = books.apply_rules(self._rules(), sender, ev.payee)
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
        result: dict = {
            "msgid": msgid,
            "status": "indexed",
            "journal_file": None,
            "linked": None,
            "closed_due": None,
        }
        if ev.kind != "transaction" or ev.entity == "none":
            await ji.upsert(self.db_pool, msgid, mailbox, ev, todoist_ref=todoist_ref)
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
                        await ji.upsert(self.db_pool, other, match["mailbox"], fixed)
                    else:
                        declared = await asyncio.to_thread(books._declared_accounts_sync, cfg)
                        inst = instrument_account(ev.instrument, declared)
                        try:
                            await books.rewrite_event(
                                other, cfg, instrument_account=inst, add_tags={"bank": msgid}
                            )
                        except books.BooksCheckError:
                            await books.rewrite_event(other, cfg, add_tags={"bank": msgid})
                    await ji.upsert(self.db_pool, msgid, mailbox, ev, linked=other)
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
                await ji.upsert(self.db_pool, msgid, mailbox, ev, journal_file=rel)
                result.update(status="posted", journal_file=rel)
        except books.BooksDisabled:
            # The index is the cheap half and stays useful without a checkout:
            # the admin page, dues dedupe and matching all still work, and a
            # later backfill has the rows to post from.
            await ji.upsert(self.db_pool, msgid, mailbox, ev)
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
            await ji.upsert(self.db_pool, msgid, mailbox, ev)
            result["status"] = "post_failed"
            return result

        if ev.amount is not None and ev.currency and ev.occurred_on is not None:
            due = await ji.find_open_due(
                self.db_pool, ev.payee_key, ev.amount, ev.currency, ev.occurred_on
            )
            if due is not None:
                closed = True
                if self.capture is not None:
                    closed = await self.capture.complete_captured_task(due["todoist_ref"])
                # Only mark it paid once the task is actually closed — that is
                # what takes the due out of find_open_due, so marking on a
                # failed close would strand an open Todoist task nothing
                # revisits. `mark_due_paid`, not `link`: this payment may
                # already be linked to its own bank/receipt counterpart, and
                # `link` writes both sides, which would overwrite that.
                if closed:
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

    @activity.defn
    async def upsert_charges(self, account: str, extractions: list[dict]) -> int:
        """For each extraction, link `finance.receipt_email` and (when
        is_receipt=True and is_recurring is not False) upsert
        `finance.recurring_charge` keyed on
        (account, sender_label, amount_cents, currency).

        Cadence is upgrade-only ('unknown' may be replaced; explicit
        cadence is preserved). A previously-cancelled charge flips back
        to 'active' the moment a fresh receipt arrives. Returns the
        total number of receipts processed (receipts + non-receipts).
        """
        processed = 0
        async with self.db_pool.acquire() as conn:
            for e in extractions:
                receipt_id = e.get("receipt_id")
                if not receipt_id:
                    continue

                if not e.get("is_receipt"):
                    # Mark as parsed so we don't re-LLM it.
                    # v3 schema: no is_receipt/parsed_at columns; use parsed jsonb only.
                    await conn.execute(
                        "UPDATE finance.receipt_email "
                        "SET parsed = COALESCE(parsed, '{}'::jsonb) || $2 WHERE id=$1::uuid",
                        receipt_id,
                        e,
                    )
                    processed += 1
                    continue

                # Deterministic bank/card-alert guard: never mint a recurring
                # charge from a bank/card alert sender (autopay reminders,
                # failed-payment notices, card statements). Belt-and-suspenders
                # behind the LLM prompt hardening. Mark parsed so we don't re-LLM.
                if _is_bank_alert_sender(
                    e.get("sender", ""), e.get("sender_label", ""), senders=self.bank_alert_senders
                ):
                    logger.info(
                        "money_skip_bank_alert_sender",
                        receipt_id=receipt_id,
                        sender=e.get("sender", ""),
                        sender_label=e.get("sender_label", ""),
                        vendor_name=e.get("vendor_name", ""),
                    )
                    await conn.execute(
                        "UPDATE finance.receipt_email "
                        "SET parsed = COALESCE(parsed, '{}'::jsonb) || $2 WHERE id=$1::uuid",
                        receipt_id,
                        e,
                    )
                    processed += 1
                    continue

                # One-off purchase, not a subscription/utility (#113): mint
                # no recurring_charge row so it doesn't sit as a fake
                # "active subscription" forever. The receipt itself is
                # still stored/marked parsed. A missing/uncertain flag
                # (None — model didn't answer, or pre-fix extractions that
                # predate this field) is treated conservatively as
                # recurring, preserving prior behaviour for ambiguous
                # cases. Existing prod one-off rows are NOT reclassified
                # here — see PR description for the manual prune.
                if e.get("is_recurring") is False:
                    logger.info(
                        "money_skip_one_off",
                        receipt_id=receipt_id,
                        vendor_name=e.get("vendor_name", ""),
                    )
                    await conn.execute(
                        "UPDATE finance.receipt_email "
                        "SET parsed = COALESCE(parsed, '{}'::jsonb) || $2 WHERE id=$1::uuid",
                        receipt_id,
                        e,
                    )
                    processed += 1
                    continue

                amount = e.get("amount") or 0
                amount_cents = int(round(amount * 100))
                currency = (e.get("currency") or self.home_currency).upper()
                cadence = e.get("cadence") or "unknown"
                monthly_home = to_monthly_home(
                    amount,
                    currency,
                    cadence,
                    self.fx_rates,
                    self.home_currency,
                )

                next_due_raw = e.get("next_due_at")
                next_due_at: datetime | None = None
                if next_due_raw:
                    try:
                        next_due_at = datetime.fromisoformat(next_due_raw)
                    except (TypeError, ValueError):
                        next_due_at = None

                # Cadence merge: prefer the more-specific cadence whenever
                # known. If the stored row is 'unknown' and the new extraction
                # has a real cadence, upgrade. If the stored row has a real
                # cadence, keep it (don't let a later 'unknown' extraction
                # erase what we know). Symmetric form so a real→unknown
                # sequence preserves the real cadence the same way unknown→
                # real upgrades it.
                charge_row = await conn.fetchrow(
                    "INSERT INTO finance.recurring_charge "
                    "(account, sender_label, vendor_name, category, amount_cents, "
                    " currency, monthly_home_equivalent, cadence, "
                    " first_seen_at, last_seen_at, next_due_at) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,NOW(),NOW(),$9) "
                    "ON CONFLICT (account, sender_label, amount_cents, currency) "
                    "DO UPDATE SET "
                    "  last_seen_at = NOW(), "
                    "  next_due_at = COALESCE(EXCLUDED.next_due_at, "
                    "                          finance.recurring_charge.next_due_at), "
                    "  cadence = CASE "
                    "    WHEN finance.recurring_charge.cadence='unknown' "
                    "         AND EXCLUDED.cadence != 'unknown' THEN EXCLUDED.cadence "
                    "    WHEN finance.recurring_charge.cadence != 'unknown' "
                    "         THEN finance.recurring_charge.cadence "
                    "    ELSE EXCLUDED.cadence "
                    "  END, "
                    "  monthly_home_equivalent = EXCLUDED.monthly_home_equivalent, "
                    "  status = CASE WHEN finance.recurring_charge.status='cancelled' "
                    "                THEN 'active' "
                    "                ELSE finance.recurring_charge.status END, "
                    "  updated_at = NOW() "
                    "RETURNING id",
                    account,
                    e.get("sender_label", ""),
                    e.get("vendor_name", ""),
                    e.get("category", "other"),
                    amount_cents,
                    currency,
                    monthly_home,
                    cadence,
                    next_due_at,
                )

                await conn.execute(
                    "UPDATE finance.receipt_email "
                    "SET parsed = COALESCE(parsed, '{}'::jsonb) || $2, charge_id=$3 "
                    "WHERE id=$1::uuid",
                    receipt_id,
                    e,
                    charge_row["id"],
                )
                processed += 1
        return processed

    @activity.defn
    async def detect_cancellations(
        self, threshold_multiplier: float = 2.0
    ) -> list[dict]:
        """Mark active charges as cancelled when no receipt seen for
        threshold_multiplier × cadence_interval. Skips cadence='unknown'.

        Returns a list of the newly-cancelled charge rows (id, vendor_name,
        amount_cents, currency, cadence, last_seen_at, account) so callers
        can capture or notify per subscription.
        """
        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                UPDATE finance.recurring_charge
                SET status = 'cancelled', updated_at = NOW()
                WHERE status = 'active'
                  AND cadence IN ('monthly', 'quarterly', 'yearly')
                  AND last_seen_at < NOW() - (
                    CASE cadence
                      WHEN 'monthly'   THEN INTERVAL '1 month'
                      WHEN 'quarterly' THEN INTERVAL '3 months'
                      WHEN 'yearly'    THEN INTERVAL '12 months'
                      ELSE NULL
                    END * $1
                  )
                RETURNING id, vendor_name, amount_cents, currency,
                          cadence, last_seen_at, account
                """,
                threshold_multiplier,
            )
        return [dict(r) for r in rows]

    @activity.defn
    async def evaluate_renewal_alerts(self, thresholds: list[int]) -> list[dict]:
        """Insert one renewal_alert row for the lowest not-yet-alerted
        threshold each active, due-soon charge has crossed. Returns the NEW
        alert payloads (for notification).

        Fix #113: dedup used to be scoped to a UTC day (partial unique
        index on charge_id/threshold_days/day(fired_at)) so an
        already-crossed threshold re-fired every single day forever (166
        rows in 3 weeks from only 7 charges). Dedup is now scoped to the
        renewal cycle instead — (charge_id, threshold_days, next_due_at),
        checked across ALL time, not just today — so a threshold, once
        alerted for a given next_due_at, never re-fires for that same
        cycle. Only the single lowest never-alerted crossed threshold
        fires per call: a charge that jumps past several thresholds at
        once (freshly imported, already overdue) gets one alert, not a
        burst, matching the "at most one alert" cadence the downstream
        Slack 7-day guard and Todoist charge_id+next_due_at dedupe already
        assume. The day-scoped ON CONFLICT clause stays as a
        race-condition backstop; it must still match the original index
        expression exactly.
        """
        new_alerts: list[dict] = []
        async with self.db_pool.acquire() as conn:
            # Past-due window guard (14 days): a charge whose next_due_at
            # slipped weeks ago should NOT keep firing the 0-day alert
            # forever. The 14-day floor gives one final pass after the
            # due date and then drops the row out of the eligible set.
            charges = await conn.fetch(
                "SELECT id, account, vendor_name, category, amount_cents, "
                "currency, monthly_home_equivalent, next_due_at, "
                "EXTRACT(EPOCH FROM (next_due_at - NOW())) / 86400 AS days_left "
                "FROM finance.recurring_charge "
                "WHERE status = 'active' AND next_due_at IS NOT NULL "
                "  AND next_due_at >= NOW() - INTERVAL '14 days'"
            )
            for c in charges:
                days_left = float(c["days_left"])
                crossed = sorted(t for t in thresholds if days_left <= t)
                if not crossed:
                    continue

                fired_rows = await conn.fetch(
                    "SELECT threshold_days FROM finance.renewal_alert "
                    "WHERE charge_id = $1 AND next_due_at = $2 "
                    "  AND threshold_days = ANY($3::int[])",
                    c["id"],
                    c["next_due_at"],
                    crossed,
                )
                fired = {r["threshold_days"] for r in fired_rows}
                not_yet_fired = [t for t in crossed if t not in fired]
                if not not_yet_fired:
                    continue
                t = min(not_yet_fired)

                row = await conn.fetchrow(
                    "INSERT INTO finance.renewal_alert "
                    "(charge_id, threshold_days, next_due_at) VALUES ($1, $2, $3) "
                    "ON CONFLICT (charge_id, threshold_days, "
                    "             ((fired_at AT TIME ZONE 'UTC')::date)) "
                    "DO NOTHING RETURNING id",
                    c["id"],
                    t,
                    c["next_due_at"],
                )
                if row is not None:
                    new_alerts.append(
                        {
                            "alert_id": str(row["id"]),
                            "charge_id": str(c["id"]),
                            "threshold_days": t,
                            "vendor_name": c["vendor_name"],
                            "category": c["category"],
                            "amount_cents": c["amount_cents"],
                            "currency": c["currency"],
                            "monthly_home_equivalent": float(c["monthly_home_equivalent"]),
                            "days_left": round(days_left, 1),
                            "next_due_at": c["next_due_at"].isoformat(),
                            "account": c["account"],
                        }
                    )
        return new_alerts

    @activity.defn
    async def notify_renewal_alert(self, alert: dict) -> None:
        """Send chat card to Maou's channel. Best-effort — every user-controlled
        string is HTML-escaped because parse_mode=HTML treats raw <,>,& as
        markup and a single bad char fails the send.

        Send-level dedup: skip the send if the same
        (charge_id, threshold_days) was notified within the last 7 days.
        The DB-level partial unique index already dedups the Inbox capture
        side per UTC day; this 7-day window is the send-only guard so
        the user doesn't get pinged for the same upcoming renewal multiple
        days in a row when evaluate_renewal_alerts re-inserts the row for
        a new threshold band or after a past-due slip.
        """
        charge_id = alert.get("charge_id")
        threshold = alert["threshold_days"]
        alert_id = alert.get("alert_id")
        if charge_id and self.db_pool is not None:
            async with self.db_pool.acquire() as conn:
                recent = await conn.fetchval(
                    "SELECT 1 FROM finance.renewal_alert "
                    "WHERE charge_id = $1::uuid AND threshold_days = $2 "
                    "  AND last_notified_at IS NOT NULL "
                    "  AND last_notified_at > NOW() - INTERVAL '7 days' "
                    "LIMIT 1",
                    str(charge_id),
                    int(threshold),
                )
            if recent:
                return

        vendor = _html.escape(str(alert.get("vendor_name", "")))
        category = _html.escape(str(alert.get("category", "")))
        account = _html.escape(str(alert.get("account", "")))
        # Escape the rendered amount, not just the fields: fmt_money's ISO-suffix
        # branch carries the LLM-extracted currency code into the body verbatim.
        amount = _html.escape(
            fmt_money(Decimal(alert["amount_cents"]) / 100, alert.get("currency") or "")
        )
        title = f"[RENEWAL][{threshold}d] {vendor}"
        body = (
            f"<b>{vendor}</b> ({category})\n"
            f"Amount: {amount}\n"
            f"Monthly {self.home_currency} equiv: "
            f"{_symbol(self.home_currency)}{alert['monthly_home_equivalent']:.0f}\n"
            f"Renews in: <b>{alert['days_left']:.0f} days</b> "
            f"({alert['next_due_at'][:10]})\n"
            f"Account: {account}"
        )
        # Title is internal-only ("[RENEWAL][30d] vendor") so it joins the
        # already-escaped body without further escaping. Body content is
        # vendor-supplied — escaping happens above where each field is built.
        await safe_send_message(
            self.delivery,
            agent_id=self.agent_id,
            message=f"<b>{title}</b>\n{body}",
            log_event="renewal_notify_failed",
        )

        # Stamp the row so the next 7d window of evaluate runs short-circuits.
        # Best-effort: an unsuccessful send still benefits from this
        # stamp because safe_send_message swallows failures — the caller's
        # capture-to-inbox path is the durable record either way.
        if alert_id and self.db_pool is not None:
            async with self.db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE finance.renewal_alert SET last_notified_at = NOW() "
                    "WHERE id = $1::uuid",
                    str(alert_id),
                )

    @activity.defn
    async def notify_cancellation(self, cancellation: dict) -> None:
        """Send chat card to Maou's channel for a silently-cancelled charge.
        Best-effort; all vendor-supplied fields HTML-escaped before
        interpolation since parse_mode=HTML treats raw <,>,& as markup."""
        vendor = _html.escape(str(cancellation.get("vendor_name") or "subscription"))
        cadence = _html.escape(str(cancellation.get("cadence") or ""))
        account = _html.escape(str(cancellation.get("account") or ""))
        # Escape the rendered amount, not just the fields: fmt_money's ISO-suffix
        # branch carries the LLM-extracted currency code into the body verbatim.
        amount = _html.escape(
            fmt_money(
                Decimal(cancellation.get("amount_cents") or 0) / 100,
                cancellation.get("currency") or "",
            )
        )
        last_seen = cancellation.get("last_seen_at")
        last_date = str(last_seen)[:10] if last_seen else "unknown"
        title = f"[CANCEL] {vendor}"
        body = (
            f"<b>{vendor}</b>\n"
            f"Amount: {amount} ({cadence})\n"
            f"Last seen: {last_date}\n"
            f"Account: {account}"
        )
        await safe_send_message(
            self.delivery,
            agent_id=self.agent_id,
            message=f"<b>{title}</b>\n{body}",
            log_event="cancellation_notify_failed",
        )

    @activity.defn
    async def build_subscription_digest(self) -> dict:
        """Aggregate active charges into a monthly digest, persist + return.

        Period covered = the calendar month BEFORE today. Idempotent on
        (period_start, period_end) — re-running the same month UPDATES
        the existing row instead of inserting a duplicate.
        """
        today = date.today()
        period_start, period_end = _previous_month_window(today)

        async with self.db_pool.acquire() as conn:
            active = await conn.fetch(
                "SELECT vendor_name, category, currency, amount_cents, "
                "       monthly_home_equivalent, last_seen_at, first_seen_at, "
                "       status "
                "FROM finance.recurring_charge WHERE status = 'active'"
            )
            new_this = await conn.fetch(
                "SELECT vendor_name, monthly_home_equivalent "
                "FROM finance.recurring_charge "
                "WHERE first_seen_at >= $1 AND first_seen_at < $2",
                period_start,
                period_end,
            )
            cancelled_this = await conn.fetch(
                "SELECT vendor_name, monthly_home_equivalent "
                "FROM finance.recurring_charge "
                "WHERE status='cancelled' "
                "  AND updated_at >= $1 AND updated_at < $2",
                period_start,
                period_end,
            )

        by_category: dict[str, dict] = {}
        total = 0.0
        for r in active:
            inr = float(r["monthly_home_equivalent"])
            total += inr
            cat = r["category"] or "other"
            slot = by_category.setdefault(cat, {"total_inr": 0.0, "count": 0})
            slot["total_inr"] += inr
            slot["count"] += 1

        top_spenders = sorted(
            (
                {
                    "vendor_name": r["vendor_name"],
                    "monthly_home_equivalent": float(r["monthly_home_equivalent"]),
                }
                for r in active
            ),
            key=lambda x: x["monthly_home_equivalent"],
            reverse=True,
        )[:10]

        digest = {
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "total_monthly_inr": round(total, 2),
            "active_count": len(active),
            "by_category": {
                k: {"total_inr": round(v["total_inr"], 2), "count": v["count"]}
                for k, v in by_category.items()
            },
            "new_this_month": [
                {
                    "vendor_name": r["vendor_name"],
                    "monthly_home_equivalent": float(r["monthly_home_equivalent"]),
                }
                for r in new_this
            ],
            "cancelled_this_month": [
                {
                    "vendor_name": r["vendor_name"],
                    "monthly_home_equivalent": float(r["monthly_home_equivalent"]),
                }
                for r in cancelled_this
            ],
            "top_spenders": top_spenders,
        }

        async with self.db_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO finance.subscription_digest "
                "(period_start, period_end, summary) VALUES ($1,$2,$3) "
                "ON CONFLICT (period_start, period_end) DO UPDATE SET "
                "  summary = EXCLUDED.summary, sent_at = NOW()",
                period_start,
                period_end,
                digest,
            )
        return digest

    @activity.defn
    async def notify_subscription_digest(self, digest: dict) -> None:
        """Send chat digest to Maou's channel. Best-effort.

        Every user-controlled string (vendor names, categories) is
        HTML-escaped because parse_mode=HTML treats raw <,>,& as markup.
        """
        period_start = _html.escape(str(digest.get("period_start", "")))
        period_end = _html.escape(str(digest.get("period_end", "")))
        total = float(digest.get("total_monthly_inr", 0.0))
        active_count = int(digest.get("active_count", 0))
        sym = _symbol(self.home_currency)

        lines = [
            f"<b>Monthly subscription audit</b> ({period_start} → {period_end})",
            f"Active charges: <b>{active_count}</b>",
            f"Total monthly burn: <b>{sym}{total:.0f}</b>",
            "",
            "<b>By category:</b>",
        ]
        by_category = digest.get("by_category") or {}
        for cat, info in sorted(
            by_category.items(),
            key=lambda kv: kv[1]["total_inr"],
            reverse=True,
        ):
            lines.append(
                f"  {_html.escape(str(cat))}: {sym}{info['total_inr']:.0f} ({info['count']} charges)"
            )

        top = digest.get("top_spenders") or []
        if top:
            lines.append("")
            lines.append("<b>Top 10 spenders:</b>")
            for s in top[:10]:
                lines.append(
                    f"  {_html.escape(str(s['vendor_name']))}: "
                    f"{sym}{float(s['monthly_home_equivalent']):.0f}"
                )

        new_this = digest.get("new_this_month") or []
        cancelled_this = digest.get("cancelled_this_month") or []
        if new_this:
            lines.append("")
            lines.append(f"<b>New this month:</b> {len(new_this)}")
        if cancelled_this:
            lines.append(f"<b>Cancelled this month:</b> {len(cancelled_this)}")

        body = "\n".join(lines)
        await safe_send_message(
            self.delivery,
            agent_id=self.agent_id,
            message=f"<b>Monthly money digest</b>\n{body}",
            log_event="subscription_digest_notify_failed",
        )

    # ------------------------------------------------------------------ books

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
            brief["forecast"] = [
                {"date": r[1], "description": r[3], "amount": r[5]} for r in fc[1:] if len(r) >= 6
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
        brief["large_unexplained"] = [
            u for u in brief["unknowns"]
            if u["currency"] == "INR" and Decimal(u["amount"]) >= 5000
        ]
        dues = await self.db_pool.fetch(
            "SELECT message_id, payee, amount, currency, due_on, kind, todoist_ref "
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
            "SELECT message_id, payee, amount, currency, due_on FROM finance.journal_index "
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
        brief["low_confidence"] = int(await self.db_pool.fetchval(
            "SELECT count(*) FROM finance.journal_index "
            "WHERE parser = 'llm' AND confidence < 0.8 AND created_at >= $1",
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
        close["dues_open"] = int(await self.db_pool.fetchval(
            "SELECT count(*) FROM finance.journal_index "
            "WHERE kind IN ('due','failed') AND linked_message_id IS NULL "
            "  AND due_on BETWEEN $1 AND $2",
            month_first, last,
        ))
        return close

    # --------------------------------------------------------- brief output

    # Rendering is a pure function in `money_render`; these thin activities
    # exist so a workflow can reach it without importing it into the sandbox.
    #
    # `HOME_SYMBOL`, not `_symbol(self.home_currency)`: it is the commodity
    # `build_money_brief` hands to `hledger -X`, so it is the one
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
    async def notify_money_message(self, html: str, log_event: str) -> None:
        """Push one already-rendered message to the agent's channel.

        The caller renders; this only delivers. `safe_send_message` never
        raises, so a dead comms server costs the message, not the run.
        """
        await safe_send_message(
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
