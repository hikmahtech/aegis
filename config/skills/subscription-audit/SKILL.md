---
name: subscription-audit
description: Review recurring spend — gather charges from the mounted money/observation/knowledge tools, cluster by service, flag overlaps, zombies and price creep, and output a decision table. Reports and recommends; never cancels anything.
---

# Recurring-spend audit

Find what is being paid for every month, what is being paid for **twice**, and
what is being paid for and not used. Produce a table the owner can act on in
one sitting.

You cannot cancel anything and must not try. The deliverable is a decision
table plus, at most, captured follow-up tasks — a cancellation is the owner's
call and often has consequences (shared plans, annual discounts, data loss)
that no unattended run can see.

## 1. Gather

Use whichever of these the run has mounted, and say which you used:

- **Observation/metric tools** (e.g. `query_observations`) for recorded amounts
  over a window — these give you trend, not just a snapshot.
- **Knowledge search** (`search_knowledge`, `ask_knowledge`, `find_reference`)
  for receipts, invoices and confirmation mail that were filed automatically.
  Query by vendor name *and* by generic terms ("subscription", "renewal",
  "invoice", "your receipt").
- **Task tools** (`list_next_actions`) for renewal reminders and prior
  cancel-this decisions already in flight. Do not re-raise something already
  queued.

If none of these are mounted, stop and say so. An audit assembled from memory
is worse than no audit — invented numbers get acted on.

Normalize as you gather: amount, currency, cadence (monthly/annual/unknown),
last seen date, source of the evidence. Keep the source per row; a
recommendation without provenance is unreviewable.

## 2. Cluster by service, not by charge

The same service often appears under several billing descriptors (a vendor
name, a payment processor, an app-store intermediary). Group them, and state
when you are unsure that two rows are the same service rather than silently
merging them.

Annualize everything to one comparable number (monthly × 12, annual as-is) so
a $9/mo item and a $99/yr item can be ranked honestly.

## 3. What to flag

- **Overlap** — two or more services solving the same job (two cloud drives,
  two note apps, two VPNs). This is where the real money is.
- **Price creep** — the amount for a given service rising across periods. A
  15% annual increase is invisible per-charge and material per-year.
- **Zombies** — a charge that keeps landing while nothing references the
  service anywhere else (no tasks, no recent receipts other than the bill, no
  mention in knowledge). Flag as *suspected* unused; you cannot prove non-use.
- **Cadence surprises** — a monthly item that just billed annually, a trial
  that converted, a "free" tier that started charging.
- **Unknown cadence** — rows you could not pin down. List them; do not drop
  them, and do not guess "monthly" to make the total look complete.

## 4. Output

A single table, sorted by annualized cost descending:

| Service | Amount | Cadence | Annualized | Last seen | Flag | Recommendation |
|---|---|---|---|---|---|---|

Recommendations use a closed vocabulary so they can be actioned quickly:
`keep` / `review` / `likely cancel` / `consolidate with <service>` /
`verify — evidence thin`.

Close with three lines: total annualized spend, total flagged as reviewable,
and the single largest saving available. If the ask included capturing
follow-ups, create **one** task per `likely cancel` / `consolidate` row with
the evidence in the description — and list what you captured.
