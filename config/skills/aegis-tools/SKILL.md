---
name: aegis-tools
description: How to use the mounted AEGIS MCP tools correctly — GTD label semantics, capture vs complete vs handoff, reading before writing, and what a null/duplicate return actually means. Load this before calling any aegis_* tool in a headless run.
---

# Working with the mounted AEGIS tools

This run has an MCP server named `aegis` mounted. Its tools are the *same*
tools the dispatching agent may use in chat — no more. If a tool you expect is
absent, that agent was not granted it; say so instead of working around it.

Every call reaches a live personal system: tasks land in a real task manager,
observations land in a real datastore, and the owner reads the result. Treat
writes as you would a production change.

## Read before you write

The single most common failure in an unattended run is creating a duplicate.

1. `whats_next` / `list_next_actions` before `capture_to_inbox` — the item you
   are about to capture very often already exists, sometimes with a different
   wording.
2. `search_knowledge` / `find_reference` before `remember_this` — the knowledge
   store is deduplicated by nobody.
3. When a read tool returns nothing, that is an answer. Report "no matching
   items" rather than inventing a plausible list.

## GTD state lives in labels, not in projects

AEGIS is GTD-first and the task store's *labels* carry the state. A task with
none of these is in no state at all and is invisible to every "what's next"
view — so anything you touch must leave exactly one:

| Label | Meaning | When it applies |
|---|---|---|
| `@next` | Actionable now, no date needed | The default for a real next action |
| `@someday` | An idea, deliberately not actionable yet | Maybe-later, review later |
| `@waiting` | Blocked on someone or something external | Always name *who* you're waiting on |
| `@reference` | Information, not a to-do | Never surfaces as work |

A task that carries a due date does not also need `@next` — the date is what
surfaces it. Adding both is drift.

## Write tools and what they actually mean

- **`capture_to_inbox`** — inbox capture, not triage. It creates an *unclarified*
  item. Do not try to make it fully-formed by stuffing context into the title;
  put the ask in the title and the context in the description.
- **`complete_task`** — the work is genuinely done. Do not complete a task to
  "clear" it; that destroys the signal that it was never done. If it should not
  have existed, say so in your report and leave it.
- **`handoff_task`** — reassign to another personality by its `@label`. This is
  delegation *within* the fleet, not a way to silence something. An invalid
  label is rejected with the valid list — read it rather than guessing again.
- **`mark_waiting`** — you are blocked. Always record who/what you are waiting
  on, and an expected-by date when one exists, or it will never be chased.
- **`defer_task`** — a new due date. Deferring repeatedly is a signal worth
  reporting, not a solution.

## Nulls, duplicates and idempotency

A `None` / empty / "already recorded" return from an ingest-style tool means
**already ingested**, not failed. Observation writes dedupe on
`(source, metric, external_id)` by design, and windows are deliberately re-read
with overlap. Do not retry, do not re-word the payload to force a second row,
and do not report it as an error.

Keep observation identity normalized: `metric` and `source` are stored stripped
and lowercased, so `Weight` and `weight` are the same series — but only if you
send them the same way. Mixed casing silently splits a series in two, and
nothing will tell you.

## Reporting

You are headless: nobody can answer a question mid-run, and only the tail of
your output is delivered. So:

- State assumptions instead of asking.
- Put the conclusion at the **end**, in the final message.
- List every write you made (tool, target, what changed) so the owner can undo
  it. An unlisted write is an unauditable one.
- If a tool errored, report the error verbatim; do not paper over it with a
  summary that implies success.
