# Improvement observations — AEGIS as an extension of life

*Investigation date: 2026-07-31. Question explored: how can AEGIS gain bigger
context from its owner's life, and how do we build a knowledge base about the
mundane things in life? Findings were produced by a four-agent codebase audit
(core platform, flows/connectors, memory/knowledge, frontend/comms/deploy) and
then broken into themed, independently-shippable development tasks (Part 3).*

---

## Part 1 — Where the system stands today

### What AEGIS currently ingests about its owner

| Sense | Mechanism | Depth |
|---|---|---|
| Email | `GmailIngestFlow` hourly, multi-account | triage, receipts, references |
| Calendar | `CalendarIngestFlow` daily | 30-day horizon → knowledge chunks |
| Tasks | Todoist mirror + clarify | full GTD layer |
| Money | receipt emails → `finance.recurring_charge` | subscriptions/renewals only |
| Bookmarks | Raindrop 2-hourly | → knowledge store |
| Reading | RSS hourly, intelligence scans | → knowledge store |
| Documents | one watched Drive folder, uploads, URLs | → knowledge store |
| Infra | heartbeats, alerts, Sentry, certs | deep |

### What is entirely absent (verified — no code, stub, or TODO)

Location, health/wearables, sleep, photos, messaging, contacts/people,
journaling, habits, browsing, nutrition, banking aggregation. The only
reserved-but-empty integration seam is MCP (`core/src/aegis/mcp_manager.py` —
an honest 501 stub with a pinned route contract).

### The memory system, honestly assessed

- `agent_memory` is a single plain-text table fed by exactly two signals: the
  "Why?" note on interaction cards, and Gmail triage corrections. Injection is
  top-8 rows per prompt. Nightly `MemoryReflectionFlow` **only prunes to 50
  rows** — its own docstring admits the LLM consolidation pass is unbuilt.
- The persona `USER.md` / `MEMORY.md` docs (the designated "who is this
  human" slot, injected into every prompt) ship as **blank templates** and
  nothing ever writes to them.
- There is **no people/contacts entity** anywhere — calendar attendees are
  flattened into event text.
- There is **no observations/time-series store** — `recurring_charge` is the
  only structured life data.
- The daily brief re-ingests itself as `source_type='briefing'` (a good
  feedback-loop precedent), but nothing records what actually *happened* in a
  day — AEGIS has no episodic/autobiographical memory.

**The one-line diagnosis: AEGIS is a superb *operations* nervous system with
almost no *life* sensory organs and no long-term autobiographical memory.**
The blog post says learning "the shape of one person's life" was the point all
along — the plumbing below shows the gap is closable with mostly-existing
machinery.

### Extension points the audit confirmed (leverage ranked)

1. **`channels` kind + ingest flow** — the uniform pollable-source pattern
   (Gmail/RSS/Raindrop/Calendar all follow it). ~5 registration touch points;
   retention, run-recording, connector health all come free.
2. **Webhook router** (`core/src/aegis/api/routes/webhooks.py`) — HMAC verify +
   start-workflow-by-string-name; a push endpoint is ~30 lines.
3. **Knowledge store `source_type`** — free-form string all the way down; new
   life-data types need zero schema work, and per-type decay windows already
   exist in `_apply_knowledge_decay`.
4. **Interactions primitive** — `draft_review` and `input` card kinds +
   `post_resolve_activity` hook + notification budget: everything a
   human-approved learning loop needs already exists.
5. **`CONFIG_REGISTRY`** — one `ConfigKey` line = encrypted storage + admin UI
   field for any new integration secret.
6. **Structured-data precedents** — per-domain Postgres schemas (`finance`,
   `pandoras_actor`), the `infra` registry pattern (entities + JSONB metadata +
   encrypted credentials + admin CRUD), and `CertRadarFlow` (registry of dated
   things → daily check → card) all generalize directly to life data.

### Friction to fix before adding several sources

Flow registration is five hand-edited lists plus seed YAML; a half-wired flow
silently never schedules. Connector "registration" is a hardcoded dict in
`worker/src/aegis_worker/bootstrap.py`. A small declarative registry pays for
itself before life-data source #3 (see Theme D).

---

## Part 2 — The ideas (three layers)

### Layer 1 — Capture: more senses

1. **Voice-first frictionless capture.** Capture friction is why nobody has a
   knowledge base of where the spare keys are. STT already lives in comms
   (ElevenLabs), capture endpoints already exist (`POST /api/admin/capture`,
   `/capture` slash command). Missing: voice note → transcript → tiny
   classifier → *task* (Todoist Inbox) vs *life fact* (knowledge store).
   "The water filter is the 3-month one, bought April" should take 5 seconds
   to say and be retrievable forever.
2. **New pollable senses via the channels pattern** — in order of
   context-per-effort: location (OwnTracks/Home Assistant), health/sleep
   (Health Auto Export / Oura / Strava), curated messaging (self-signals only:
   starred messages, note-to-self chat), photo *metadata* (EXIF/places — a
   life-event index, not the images).
3. **Implement the MCP client — the sensor bus.** Turns the whole MCP server
   ecosystem into life-data sources without a connector per source. Highest
   infrastructure leverage on the list.

### Layer 2 — Structure: the mundane-life knowledge base

4. **`people` entity model** — the biggest structural gap. Passive enrichment
   from email/calendar co-occurrence unlocks the most human features:
   "when did I last talk to X", birthday radar in the weekly review,
   "you're seeing Sarah Tuesday — last time you discussed Y".
5. **Life "expiry radar"** — generalize `CertRadarFlow` to passports, visas,
   licences, insurance, warranties, medication refills, service dates.
6. **Household/asset registry** — generalize the `infra` table pattern from
   "SSH hosts and k8s clusters" to "boiler, car, appliances", manuals and
   receipts linked in the knowledge store by tag.
7. **`observations` table** — a home for sleep, weight, steps, mood, places.
   RAG chunks are the wrong shape for "how did I sleep in March"; this enables
   Maou-style trend/anomaly flows for health the way they exist for money.
8. **Episodic diary (DayLog)** — nightly distillation of what actually
   happened (mail, events, tasks done, places, card decisions) into a dated
   knowledge entry, with weekly/monthly rollups. Makes "what was going on in
   my life last November" an answerable query.

### Layer 3 — The loop: learning and giving back

9. **The self-writing user profile** — a weekly reflection flow that proposes
   a diff to the blank `USER` persona docs via a `draft_review` card. Highest
   leverage-per-line in the whole list: every agent gets smarter
   simultaneously and all machinery exists.
10. **A curiosity budget** — one `input`-kind card per day (budget-gated):
    "You meet 'S. Chen' every Tuesday — who is that to you?" Active
    interviewing beats passive inference, and the budget keeps it from
    becoming noise. This is what makes it feel like a relationship rather
    than surveillance.
11. **Memory lifecycle** — upgrade `agent_memory` from append-and-prune to a
    real nightly ADD/UPDATE/DELETE/NOOP consolidation, promoting
    generalizations into the profile doc.

### Recommended sequencing

Start where infrastructure exists and payoff compounds:
**profile reflection → voice capture → day log → curiosity cards** (no new
external integrations; transforms the felt experience). Then add senses in
order of signal: **people model, expiry radar, location/health channels, MCP
client** — with the declarative-registration cleanup (Theme D) landing before
the third new source.

---

## Part 3 — Development themes & task breakdown

Tasks are sized S (≲half day), M (1–2 days), L (multi-day). Each is intended
to be independently shippable; dependencies are explicit. Detailed sections
follow.

<!-- THEME SECTIONS INSERTED BELOW -->

---

## Part 4 — Bugs & hygiene found along the way

Found incidentally during the audit; tracked as Theme D tasks above.

1. **Stale tool-capability guard (likely live bug on the main chat path).**
   `_TOOL_INCAPABLE_MODELS` in `core/src/aegis/services/chat.py` matches by
   exact equality (`"claude-sonnet"`), but `config/models.yaml` now maps the
   smart tier to `"claude-sonnet-5"` — so the substitution guard never fires
   for smart-tier agents (all four seed agents), and its fallback model
   (`gpt-oss:20b`) points at a host models.yaml describes as down
   indefinitely.
2. **`cryptography` is an undeclared dependency** of `aegis-core`
   (`crypto.py` imports `cryptography.fernet`; arrives transitively today).
3. **CORS**: `allow_origins=["*"]` with `allow_credentials=True` — browsers
   reject the combination; should be an explicit origin list.
4. **SPA catch-all** returns HTTP 200 `null` for unknown `/api/*` paths
   instead of 404.
5. **Duplicate migration number** `006_` (two files); needs a lint guard that
   grandfathers the existing pair.
