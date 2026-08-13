---
name: investigation
description: Structured incident investigation — gather evidence with the read-only status/log/inspect tools, form competing hypotheses, verify each against evidence, and report claim → evidence → confidence with the smallest safe fix. Never restarts or mutates anything unasked.
---

# Investigating an incident

The job is a **diagnosis**, not a repair. You gather evidence, you rank
explanations, you propose the smallest change that would fix it. You do not
restart, redeploy, scale, delete or edit anything unless the ask explicitly
told you to — a mutation also destroys the evidence for the next person, and an
unattended fix to a misunderstood problem is how a small incident becomes a
large one.

## 1. Establish the symptom before you explain it

Write down, in one sentence each: what is observed, when it started, and how it
was noticed. If you cannot state the symptom precisely, everything after is
guesswork dressed as analysis.

Then bound it: is this one component or several? Started at a deploy, a
schedule boundary, or gradually? Still happening, or already recovered?

## 2. Gather — cheapest and broadest first

- `system_status` — the aggregate view: what ran, what failed, what completed
  but encoded an error, what is pending. Start here; it often reframes the
  symptom entirely.
- `list_services` / `list_nodes` — is the thing even running, and where?
- `inspect_service` — task state, restart counts, update state, placement.
  Restart counts and a recent update timestamp are the two highest-signal
  fields in an incident.
- `get_service_logs` — last, and narrowly. Logs are the most expensive to read
  and the easiest to over-interpret.
- `search_knowledge` / `find_reference` — a runbook or a past incident for the
  same symptom. Check this *early*: the cheapest investigation is the one
  somebody already did.

Record what you looked at even when it was clean. "Node count normal, no
restarts in 24h" narrows the space as much as an error does.

## 3. Hypotheses, plural

Write down at least **two** candidate explanations before testing any of them.
A single hypothesis is a conclusion you have already reached, and every
subsequent tool call will be read as confirming it.

For each, state in advance what evidence would confirm it and — more
importantly — what would *rule it out*. Then go look for the ruling-out
evidence first.

Common families worth considering explicitly: a recent change (deploy, config,
credential rotation), a resource limit (disk, memory, connections, quota), a
dependency (upstream service, DNS, certificate expiry), a schedule/time
boundary, and "it was always broken and only now visible".

## 4. Report as claim → evidence → confidence

Every assertion gets all three:

> **Claim:** the service has been crash-looping since ~14:20.
> **Evidence:** `inspect_service` shows 38 restarts, newest task 4m old, image
> unchanged since the previous day.
> **Confidence:** high — restart count and task age agree.

Confidence is `high` / `medium` / `low`, and a `low` is a legitimate,
useful answer. Say plainly when the evidence does not distinguish between two
hypotheses; a forced pick reads as certainty and gets acted on as certainty.

Keep what you ruled out, and why. That is half the value of the report.

## 5. Propose the smallest safe fix

One paragraph: the minimal change that would address the leading hypothesis,
what it would prove if it works, and what it risks. Prefer a reversible action
over a durable one, and a narrow one over a broad one. If the leading
hypothesis is only `medium` confidence, propose the *next diagnostic step*
instead of a fix — and say which observation would settle it.

Finish with the open questions a human needs to answer. Do not close them
yourself by assumption.
