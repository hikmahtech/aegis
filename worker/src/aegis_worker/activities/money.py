"""Money Hygiene activities — receipt parse, charge upsert, alerts, audit."""

from __future__ import annotations

import html as _html
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import structlog
from aegis.services.fx import to_monthly_home
from temporalio import activity

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


logger = structlog.get_logger()


# Bank / card-alert sender domains. Mail from these addresses is transactional
# *alerts* (autopay reminders, failed-payment notices, credit-card statements),
# NOT vendor receipts — yet they quote figures the extractor can mistake for a
# recurring charge (verified prod offenders: an autopay reminder or card
# statement minting a fake recurring charge, a failed payment-gateway notice).
# The LLM prompt hardening is best-effort; this is the belt-and-suspenders
# deterministic guard. Match is case-insensitive substring. Configured via
# AEGIS_BANK_ALERT_SENDERS (comma-separated domains); default empty means the
# guard is a clean no-op until a self-hoster adds their own bank's domains.
_BANK_ALERT_SENDERS = frozenset(
    s.strip().lower()
    for s in os.getenv("AEGIS_BANK_ALERT_SENDERS", "").split(",")
    if s.strip()
)


def _is_bank_alert_sender(*candidates: str) -> bool:
    """True if any candidate sender string contains a known bank-alert domain."""
    for cand in candidates:
        if not cand:
            continue
        low = cand.lower()
        if any(domain in low for domain in _BANK_ALERT_SENDERS):
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

    @activity.defn
    async def store_receipt_email(self, msg: dict, account: str) -> str:
        """Insert raw email into maou.receipt_email; return UUID id.

        Idempotent: ON CONFLICT (message_id) DO NOTHING. Returns empty string
        on conflict (caller treats as "already stored").

        `msg` is the Gmail dict from GmailActivities.fetch_emails:
        {id, thread_id, sender, subject, to, date, snippet, internal_date_ms}
        """
        if not self.db_pool:
            return ""
        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO maou.receipt_email
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
        return str(row["id"]) if row else ""

    @activity.defn
    async def load_receipts(self, receipt_ids: list[str]) -> list[dict]:
        """Read raw receipt rows for parsing. Returns plain dicts (not records).

        v3 schema has no body_plain column — snippet is stored in parsed jsonb.
        Aliased as body_plain so classify_and_extract callers remain unchanged.
        """
        if not receipt_ids:
            return []
        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, account, message_id, sender, subject, "
                "parsed->>'snippet' AS body_plain, received_at "
                "FROM maou.receipt_email WHERE id = ANY($1::uuid[])",
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
        )
        for r, e in zip(receipts, extractions, strict=False):
            e["receipt_id"] = r["id"]
            # Echo the real email sender so upsert_charges can deterministically
            # skip bank/card-alert senders (the LLM's sender_label is best-effort
            # and on autopay reminders names the merchant, not the sender).
            e["sender"] = r.get("sender", "")
        return extractions

    @activity.defn
    async def upsert_charges(self, account: str, extractions: list[dict]) -> int:
        """For each extraction, link `maou.receipt_email` and (when
        is_receipt=True) upsert `maou.recurring_charge` keyed on
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
                        "UPDATE maou.receipt_email SET parsed=$2 WHERE id=$1::uuid",
                        receipt_id,
                        e,
                    )
                    processed += 1
                    continue

                # Deterministic bank/card-alert guard: never mint a recurring
                # charge from a bank/card alert sender (autopay reminders,
                # failed-payment notices, card statements). Belt-and-suspenders
                # behind the LLM prompt hardening. Mark parsed so we don't re-LLM.
                if _is_bank_alert_sender(e.get("sender", ""), e.get("sender_label", "")):
                    logger.info(
                        "money_skip_bank_alert_sender",
                        receipt_id=receipt_id,
                        sender=e.get("sender", ""),
                        sender_label=e.get("sender_label", ""),
                        vendor_name=e.get("vendor_name", ""),
                    )
                    await conn.execute(
                        "UPDATE maou.receipt_email SET parsed=$2 WHERE id=$1::uuid",
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
                    "INSERT INTO maou.recurring_charge "
                    "(account, sender_label, vendor_name, category, amount_cents, "
                    " currency, monthly_home_equivalent, cadence, "
                    " first_seen_at, last_seen_at, next_due_at) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,NOW(),NOW(),$9) "
                    "ON CONFLICT (account, sender_label, amount_cents, currency) "
                    "DO UPDATE SET "
                    "  last_seen_at = NOW(), "
                    "  next_due_at = COALESCE(EXCLUDED.next_due_at, "
                    "                          maou.recurring_charge.next_due_at), "
                    "  cadence = CASE "
                    "    WHEN maou.recurring_charge.cadence='unknown' "
                    "         AND EXCLUDED.cadence != 'unknown' THEN EXCLUDED.cadence "
                    "    WHEN maou.recurring_charge.cadence != 'unknown' "
                    "         THEN maou.recurring_charge.cadence "
                    "    ELSE EXCLUDED.cadence "
                    "  END, "
                    "  monthly_home_equivalent = EXCLUDED.monthly_home_equivalent, "
                    "  status = CASE WHEN maou.recurring_charge.status='cancelled' "
                    "                THEN 'active' "
                    "                ELSE maou.recurring_charge.status END, "
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
                    "UPDATE maou.receipt_email SET parsed=$2, charge_id=$3 WHERE id=$1::uuid",
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
                UPDATE maou.recurring_charge
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
        """Insert renewal_alert rows for each threshold a charge has crossed
        today. Returns the NEW alert payloads (for notification).

        Idempotent — partial unique index on
        (charge_id, threshold_days, ((fired_at AT TIME ZONE 'UTC')::date))
        means multi-fire same UTC day is a no-op. The ON CONFLICT clause
        below MUST match that index expression exactly or dedup breaks.
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
                "FROM maou.recurring_charge "
                "WHERE status = 'active' AND next_due_at IS NOT NULL "
                "  AND next_due_at >= NOW() - INTERVAL '14 days'"
            )
            for c in charges:
                days_left = float(c["days_left"])
                for t in thresholds:
                    if days_left > t:
                        continue
                    row = await conn.fetchrow(
                        "INSERT INTO maou.renewal_alert "
                        "(charge_id, threshold_days) VALUES ($1, $2) "
                        "ON CONFLICT (charge_id, threshold_days, "
                        "             ((fired_at AT TIME ZONE 'UTC')::date)) "
                        "DO NOTHING RETURNING id",
                        c["id"],
                        t,
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
                    "SELECT 1 FROM maou.renewal_alert "
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
        currency = _html.escape(str(alert.get("currency", "")))
        account = _html.escape(str(alert.get("account", "")))
        amount = alert["amount_cents"] / 100
        title = f"[RENEWAL][{threshold}d] {vendor}"
        body = (
            f"<b>{vendor}</b> ({category})\n"
            f"Amount: {amount:.2f} {currency}\n"
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
                    "UPDATE maou.renewal_alert SET last_notified_at = NOW() "
                    "WHERE id = $1::uuid",
                    str(alert_id),
                )

    @activity.defn
    async def notify_cancellation(self, cancellation: dict) -> None:
        """Send chat card to Maou's channel for a silently-cancelled charge.
        Best-effort; all vendor-supplied fields HTML-escaped before
        interpolation since parse_mode=HTML treats raw <,>,& as markup."""
        vendor = _html.escape(str(cancellation.get("vendor_name") or "subscription"))
        currency = _html.escape(str(cancellation.get("currency") or ""))
        cadence = _html.escape(str(cancellation.get("cadence") or ""))
        account = _html.escape(str(cancellation.get("account") or ""))
        amount_cents = cancellation.get("amount_cents") or 0
        amount = amount_cents / 100
        last_seen = cancellation.get("last_seen_at")
        last_date = str(last_seen)[:10] if last_seen else "unknown"
        title = f"[CANCEL] {vendor}"
        body = (
            f"<b>{vendor}</b>\n"
            f"Amount: {amount:.2f} {currency} ({cadence})\n"
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
                "FROM maou.recurring_charge WHERE status = 'active'"
            )
            new_this = await conn.fetch(
                "SELECT vendor_name, monthly_home_equivalent "
                "FROM maou.recurring_charge "
                "WHERE first_seen_at >= $1 AND first_seen_at < $2",
                period_start,
                period_end,
            )
            cancelled_this = await conn.fetch(
                "SELECT vendor_name, monthly_home_equivalent "
                "FROM maou.recurring_charge "
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
                "INSERT INTO maou.subscription_digest "
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
