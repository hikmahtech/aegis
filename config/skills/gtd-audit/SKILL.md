---
name: gtd-audit
description: Weekly GTD hygiene sweep over the task system — inventory open work, find stale @waiting items, surface undated @next drift and stateless tasks, then report a prioritized summary. Read-only by default; never mutates unless explicitly asked.
---

# Weekly GTD audit

A hygiene pass over the task system. The output is a **report**, not a cleanup:
you inventory, you diagnose, you propose. You do not complete, delete, relabel
or reschedule anything unless the ask explicitly says to.

That constraint is the point. A GTD system that an unattended process quietly
edits stops being trusted, and an untrusted system stops being used.

## 1. Inventory

Start wide, then narrow:

- `list_next_actions` with no filters — the full actionable pool.
- `list_next_actions` per assignee label for anything delegated.
- `whats_next` — what the system *would* surface right now. If this comes back
  thin while the inventory is large, that gap is itself the headline finding.
- `list_projects` for the shape of the work areas.

Record the counts before you interpret anything. "47 open, 12 undated, 9
waiting" is a finding; "quite a lot" is not.

## 2. The four drift patterns

**Stale `@waiting`.** A `@waiting` item is a promise someone else made. Flag any
that are older than ~2 weeks with no expected-by date, and any whose expected-by
date has passed. For each, the useful output is one line: *who* is being waited
on and *how long it has been*. These are the highest-value findings in the whole
audit, because they are invisible to the owner by construction.

**Undated `@next` pile-up.** `@next` means "do it whenever there's a moment".
That is healthy at ten items and meaningless at eighty — the list stops being a
choice and becomes a wall. If the undated `@next` count is large, cluster the
items by area and suggest which cluster should move to `@someday`.

**Stateless tasks.** Any open task carrying none of `@next` / `@someday` /
`@waiting` / `@reference` is in no GTD state and will never surface in any view.
These are silent losses. List them individually.

**Label contradictions.** A task with both a due date and `@next` (the date
already surfaces it), or with `@waiting` and no named blocker, or `@someday`
with a near-term due date. Each one means the state was set by two different
decisions that never met.

## 3. Age and staleness

Sort by age within each bucket. The oldest item in each is worth naming
explicitly — an eight-month-old `@next` is not a task, it is a decision the
owner has been declining to make, and naming it is more useful than any count.

## 4. Report

Lead with a five-line summary: total open, per-state counts, and the single
worst pattern found. Then, in priority order:

1. **Stale waiting** — who, what, how long, suggested chase.
2. **Stateless** — the individual tasks, with a suggested state each.
3. **Contradictions** — the specific conflict per task.
4. **Volume** — clusters that want a `@someday` demotion.
5. **Nothing-to-do-here** — say so explicitly when a category is clean. A silent
   category reads as "not checked".

End with a short list of *proposed* actions phrased so the owner can approve
them in one message ("move these 6 to `@someday`; chase these 3"). If the ask
was explicitly "and fix it", make exactly those proposed changes, then list every
write you performed.
