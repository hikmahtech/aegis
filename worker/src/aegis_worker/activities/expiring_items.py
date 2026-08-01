"""ExpiringItemsActivities — the DB half of the expiry radar (C6).

`life.expiring_items` (migration 018) is the registry: anything with a
renewal/expiry date. This module turns "what is due" into "what has not been
alerted about yet", and claims each alert atomically so a threshold fires
exactly once per expiry cycle.

The dedup ledger is `life.expiring_item_alerts`, unique on
`(item_id, threshold_days, expires_on)`. `claim_due_alerts` does NOT
read-then-write — it inserts with `ON CONFLICT DO NOTHING ... RETURNING`
and treats "no row came back" as "already alerted". That makes the unique
index, not a Python check, the arbiter: two runs racing on the same item
serialise on the index and exactly one of them gets rows back.

Because the ledger is keyed on the item's `expires_on` at fire time,
renewing an item (editing `expires_on` forward) re-arms every threshold for
free, and the ledger is never pruned — pruning would re-arm already-fired
thresholds, which is exactly the #113 bug migration 013 closed.

At-most-once, deliberately: an alert row is written BEFORE the card is
dispatched, so a crash between claim and dispatch drops that alert forever
rather than risking a duplicate. Same tradeoff `safe_send_message` documents
for the drift / cert / backup / renewal paths.
"""

from __future__ import annotations

import html as _html
from dataclasses import dataclass
from typing import Any

import structlog
from temporalio import activity

_logger = structlog.get_logger()


def _days(n: int) -> str:
    return "1 day" if n == 1 else f"{n} days"


def format_expiry_prompt(
    *, title: str, kind: str, days_left: int, expires_on: str, notes: str = ""
) -> str:
    """Card body for one expiring item. HTML — the Slack renderer converts it.

    Every interpolated value is operator-entered free text, so it is escaped:
    `html_to_mrkdwn` treats raw `<`/`&` as markup.
    """
    if days_left < 0:
        when = f"expired <b>{_days(abs(days_left))} ago</b>"
    elif days_left == 0:
        when = "expires <b>today</b>"
    else:
        when = f"expires in <b>{_days(days_left)}</b>"
    line = (
        f"⏳ <b>{_html.escape(title)}</b> ({_html.escape(kind)}) "
        f"{when} — {_html.escape(expires_on)}"
    )
    if notes:
        line += f"\n{_html.escape(notes)}"
    return line + "\nRenewed it? Tap Acknowledge. Otherwise update the date in AEGIS."


@dataclass
class ExpiringItemsActivities:
    """Expiry-radar activities. `db_pool` is the only dependency."""

    db_pool: Any

    @activity.defn
    async def claim_due_alerts(self, lookahead_days: int, max_alerts: int) -> list[dict]:
        """Claim (and return) at most `max_alerts` not-yet-fired expiry alerts.

        For each item due inside `lookahead_days`, the crossed thresholds are
        those `lead_days` entries `>= days_left` (`days_left` is computed by
        the DB in `due_within`, so the workflow never needs `date.today()` —
        banned under Temporal replay). ALL crossed-and-unclaimed thresholds are
        written to the ledger in one statement, but only ONE alert is returned,
        for the tightest of them: an item added when it is already 5 days from
        expiry with `lead_days={30,7,1}` produces a single "7 days" card, not a
        burst of two, and its 30-day threshold is retired rather than firing
        tomorrow.

        Thresholds are inserted in ascending order so two concurrent runs
        acquire the index locks in the same order — one waits, then gets zero
        rows back, so it sends nothing. No deadlock, no read-then-write window.

        `max_alerts` is the notification gate: interaction cards bypass
        `safe_send_message` and therefore the daily notification budget, so a
        registry that suddenly goes 50-items-overdue must not fire 50 cards.
        Surplus items are simply not claimed, so they alert on the next run.
        """
        from aegis.services.expiring_items import due_within

        alerts: list[dict] = []
        items = await due_within(self.db_pool, lookahead_days)
        for item in items:
            if len(alerts) >= max_alerts:
                break
            days_left = int(item["days_left"])
            crossed = sorted(int(t) for t in (item["lead_days"] or []) if days_left <= int(t))
            if not crossed:
                continue
            rows = await self.db_pool.fetch(
                "INSERT INTO life.expiring_item_alerts "
                "(item_id, threshold_days, expires_on) "
                "SELECT $1::uuid, t, $2::date FROM unnest($3::int[]) AS t "
                "ON CONFLICT (item_id, threshold_days, expires_on) DO NOTHING "
                "RETURNING threshold_days",
                item["id"],
                item["expires_on"],
                crossed,
            )
            if not rows:
                # Every crossed threshold already fired for this expiry cycle.
                continue
            threshold = min(int(r["threshold_days"]) for r in rows)
            expires_on = item["expires_on"].isoformat()
            alerts.append(
                {
                    "item_id": str(item["id"]),
                    "kind": item["kind"],
                    "title": item["title"],
                    "expires_on": expires_on,
                    "days_left": days_left,
                    "threshold_days": threshold,
                    "prompt": format_expiry_prompt(
                        title=item["title"],
                        kind=item["kind"],
                        days_left=days_left,
                        expires_on=expires_on,
                        notes=item["notes"] or "",
                    ),
                }
            )
        _logger.info("expiry_alerts_claimed", count=len(alerts), scanned=len(items))
        return alerts

    @activity.defn
    async def record_expiry_cards(self, agent_id: str, sent: int, failed: int) -> None:
        """Count dispatched expiry cards against the daily notification budget.

        `send_interaction_card` posts straight to the comms delivery endpoint
        and never passes through `safe_send_message`, so nothing records these
        pushes otherwise and the budget under-counts. Best-effort: a failure
        here must not fail a run whose cards already went out.
        """
        if not (sent or failed):
            return
        try:
            from aegis.services.notifications import record_notification

            for _ in range(max(0, int(sent))):
                await record_notification(self.db_pool, agent_id, "expiry_card", sent=True)
            for _ in range(max(0, int(failed))):
                await record_notification(self.db_pool, agent_id, "expiry_card", sent=False)
        except Exception as exc:  # noqa: BLE001 — accounting is best-effort
            _logger.warning("expiry_card_accounting_failed", error=str(exc)[:200])
