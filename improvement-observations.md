# Improvement observations — AEGIS as an extension of life

*Investigation date: 2026-07-31. Question explored: how can AEGIS gain bigger
context from its owner's life, and how do we build a knowledge base about the
mundane things in life? Findings were produced by a four-agent codebase audit
(core platform, flows/connectors, memory/knowledge, frontend/comms/deploy) and
then broken into themed, independently-shippable development tasks (Part 3).*

---

## STATUS (2026-08-02) — COMPLETE. This is a record, not a plan.

**The entire 33-task queue below (A1–A9, B1–B9, C1–C8, D1–D7) has been
implemented, merged to `main`, deployed and independently reviewed.** Every task
shipped as its own squash-merged PR. Five separate code-review rounds produced
follow-up fix PRs (#181, #185, #191, #214, #222, and — after this status block was
first written — #229, #231, #233), plus a CI-flake fix (#196) and a production
incident fix (#224). The last round's fixes were: curiosity no longer surfaces
A4-retired memories and the health importer refuses an unknown unit instead of
assuming one (#229); a person or asset can be unlinked from an expiring item, and a
connector that fails to initialise raises instead of degrading to `None` (#231); and
A2 refuses a drifted persona-draft approval (#233, below).

**Read everything below as a record of intent and reasoning — NOT as a to-do
list, and NOT as an accurate description of the code.** It was written on
2026-07-31 against a tree that has since moved substantially (D6 and D7 rewrote
the very registration and `chat.py` structure the sketches cite). **43 of its
specific claims are catalogued below as false or stale** — some already false
when written, some made false by the queue's own execution — and several of
them, followed literally, would have shipped silent failures. They are in
**Known-stale claims** immediately below, which is the section to read first.

The per-task sketches are retained **unedited**: their reasoning is the point of
keeping this document. Each now carries a one-line outcome marker naming its PR
and where the implementation deliberately deviated from the sketch.

**Three tasks ship deliberately inert** and stay inert until an operator acts:

- **A4** (memory consolidation) will not mutate `agent_memory` until *two
  independent keys* are turned — the DB-side config flag and an environment kill
  switch. Shipping it did not arm it.
- **A2** and **A5** write nothing to a persona doc until a human approves the
  `draft_review` card. As of #233, an approval whose acknowledged base fingerprint
  no longer matches the live doc is refused with **409** rather than clobbering the
  newer content — the human is shown the conflict and must resubmit with a
  `base_ack` equal to the current fingerprint.

---

## Known-stale claims — do NOT trust this document for mechanics

Each entry cites the PR that found it. Numbers are for reference only.

### Migration numbers — every single one in this document is wrong

Slots were reallocated as the queue executed. `migrations/` is at `021`, not the
`014` the Theme C preamble claims.

| # | Doc says | Actually shipped as | Found by |
|---|---|---|---|
| 1 | A4 `016_agent_memory_provenance.sql` | `020_agent_memory_consolidation.sql` | #218 |
| 2 | C1 `015_life_people.sql` | `016_life_people.sql` | #189 |
| 3 | C4 `016_life_observations.sql` | `017_life_observations.sql` | #195 |
| 4 | C5 `017_life_expiring_items.sql` | `018_life_expiring_items.sql` | #199 |
| 5 | C7 `018_life_assets.sql` | `019_life_assets.sql` | #203 |
| 6 | Theme C preamble: "`migrations/` is up to `014`" | stale by seven slots; B7 took `021` | #207 |

A1's `015` is the only number in the document that held.

### Line numbers and file structure

7. **Every `chat.py` line number in this document is stale**, and the file has
   since been *partially decomposed* — the infra and Vercel executors now live in
   `core/src/aegis/services/tools/`, so some cited symbols are no longer in
   `chat.py` at all. (#216, #193, #195, #213)
8. **`chat.py`'s size and shape were both misreported.** D7 claims "~4,537 lines,
   confirmed" (it was 5,169 at the time) and "~104 `_exec_*` functions" (there
   were 52 definitions / 50 registry entries). (#216)
9. **Other stale citations, all cosmetic but all wrong:** `knowledge.py:301`/`:187`
   → `:304`/`:190` (#192); `alert_webhook_secret` `config.py:206` → `:234`, and
   `integrations_config.py` lives under `services/`, not `api/routes/` (#206);
   `Settings.mcp_servers` `config.py:209` → `:242`, `app.py:166`/`:207` →
   `:168`/`:209` (#211); `prune_memories` `:115` → `:145` (#218);
   `prune_old_records` `:227` → `:232` (#195); `slack_owner_member_id` `:82` →
   `:83` and the `file_shared` handler `:493` → `:500` (#186);
   `KnowledgeStore.ingest_content` `:94` → `:96` (#179); `_ACTIVITY_TYPE_MAP`
   `:57` → `:59`/`:60`, `WORKFLOWS` `~:111` → `:118`/`:120`, runtime `workflows`
   `~:668` → `~:724`/`:731` (#199, #193, #204).

### Wiring checklists — the registration model changed underneath them

10. **The six hand-edited `__main__.py` lists named all over Theme A/B/C
    checklists were DELETED by D6** (`c6b9f8f`). Registration is now a single
    `FlowSpec` in `worker/src/aegis_worker/registry.py` plus a seed row, enforced
    by a `check_registration()` boot check; `base_workflows()` /
    `all_activity_methods()` derive the rest. **Activity methods need no
    registration edit at all** — they are collected from `@activity.defn` on the
    instances `main()` builds. Every checklist saying "both `__main__.py` lists"
    or "the activities list ~:560" is obsolete. (#207, #209, #210, #217, #219)
11. **D6's own fan-out count was wrong** — it lists five places; there were
    **seven**. It misses `_FEATURE_FLAGGED_TYPES` (`schedule_sync.py`) and treats
    the flow-class import as free. (#204)
12. **A8's "both `__main__.py` lists" was really three**, and its activity list
    was short by one (`commit_daylog_state`). (#187)
13. **C3's "No new flow registration" omitted the activity registration** its own
    sketch requires — following it verbatim gives a runtime "activity type not
    registered" failure that no existing unit test catches. (#193)
14. **C6's five-point checklist contains no activity-registration point at
    all.** Followed verbatim it ships a flow that boots fine and dies at
    runtime. Six points were needed. (#199)
15. **C5 and C7's checklists both omit the SPA wiring** (`App.tsx` route,
    `Layout.tsx` nav, `api/client.ts`). (#203)
16. **B7: `CHANNEL_KINDS` exists twice.** The doc names only
    `core/src/aegis/api/routes/channels.py`; `admin-panel/frontend/src/pages/Channels.tsx`
    has its own copy that drives rendering, so a wearable row would have been
    invisible and uneditable in the admin panel. (#207)

### Behavioural claims that were simply false

These are the dangerous ones — correct about what to call, wrong about what that
call does. CI cannot catch any of them.

17. **"Pass `db_pool` + `purpose` to `LLMClient.think()` so the call lands in
    `llm_calls`" is FALSE.** `think()` calls `_record_failure` only from its
    `except` block, and raises `LLMTruncationError` *outside* that block — so
    both success and truncation go unrecorded. Repeated in A2, A3 and A6.
    Implementations use an explicit `record_llm_call` on success plus an explicit
    error row on truncation. (#217, #208)
18. **`KnowledgeStore.list_content_items` cannot return the body A9 assumes.**
    It selects the `summary` *column*, and `knowledge_content` has no raw-text
    column at all — the body exists only as `knowledge_chunks` rows. `search` is
    a semantic top-k and can neither bound a window nor guarantee every date in
    one. Following the sketch would have filed a contentless rollup with green
    tests. (#192)
19. **`format_weekly_preview` is only a FALLBACK.** In production `frame_review`
    returns an LLM narrative that *replaces* it, so C3's key-date block added
    there would have rendered in tests and never reached the user. (#193)
20. **Core `ConfigKey`s do not reach the comms process.** `CONFIG_REGISTRY`
    values live in core's `settings` table overlaid onto core's `Settings`; comms
    has no DB access and `CommsSettings` is env-only. Only the Slack subset
    crosses, via `resolve_slack_config` / `GET /api/internal/slack-config`. B2's
    checklist as written would have made the feature permanently inert with
    nothing logged; B3's requested `ConfigKey("voice_ingest_secret", …)` was
    dropped for the same reason. (#186, #208)
21. **`ConfigKey("mcp_enabled", "…", "Features", boolean=True)` is a `TypeError`
    as written** — `secret` is a required positional. (#211)
22. **The `settings` `calendar_events_%` KV rows have NO writer in this repo.**
    Cited as an evidence source by A2 and A8; they are legacy n8n leftovers with
    exactly one reference anywhere, a *read*. Live calendar data is
    `knowledge_content` + `knowledge_chunks` at `source_type='calendar'`.
    (#180, #187)
23. **Retention entries keyed by SOURCE silently no-op.** `prune_old_records`
    prunes by TABLE: `_ALLOWED_TABLES` is derived from `_TIMESTAMP_COLUMNS`, and
    anything outside it is skipped with a `cleanup_table_not_allowed` log. So B5's
    `"location"` key would have looked configured and pruned nothing, and C4's
    "no code change is needed there beyond the new dict key" is wrong — a
    `_DEFAULT_RETENTIONS` entry alone never deletes a row. A1 hit the same trap
    and had to add the `_TIMESTAMP_COLUMNS` entry. (#195, #209, #210, #178)
24. **`seed.py` does not overwrite `activities.config` once the DB owns it**, so
    a seed-YAML config edit never reaches an existing deployment. A3's "ship the
    seed row with `dry_run: true` so the first production week is observe-only"
    silently assumed propagation; it applies to a fresh install only. (New slugs
    are an INSERT, so *those* land.) (#194, #198, #204)
25. **A6's detector SQL named a column that no longer exists.**
    `finance.recurring_charge.monthly_inr_equivalent` was renamed to
    `monthly_home_equivalent` by `migrations/009_money_home_currency.sql`. As
    written it would raise `UndefinedColumnError` on every run — swallowed by the
    per-detector try/except into a permanently, silently dead detector. (#180)
26. **Both of C2's named call sites are wrong.** `calendar_event_to_content` is a
    pure converter in core with no pool (and `CalendarActivities` carries no
    `db_pool`); `ingest_email_to_kg` is only called for
    `important_action`/`important_read` and early-returns on an unset connector
    or empty body, so enrichment hung off it would silently cover a fraction of
    mail. (#202)
27. **Calendar cannot set `last_contact`.** `fetch_events` uses `timeMin=now`
    with a forward horizon — every event it returns is in the *future*, so
    bumping `last_contact` from it would answer "when did I last talk to X?"
    with a meeting that has not happened. Calendar also flattens attendees to
    bare email strings, so it cannot supply display names either. (#202)
28. **B9's "executor calls `request.app.state.mcp_manager`" is impossible as
    written** — executors are `(pool, args, ctx)`; there is no `request`.
    Required adding `ToolContext.mcp_manager`. Its "truncates the result to the
    same size ceiling other executors use" is also false: **no** executor
    truncates; the `send_message` loop does. And `metadata.mcp_servers` had to
    be an object, not the list the doc specifies, or the acceptance criteria
    themselves are unsatisfiable. (#213)
29. **C7's acceptance criterion 3 is not implementable.** Tagging a `resources`
    row `asset:<slug>` and finding it "via the existing knowledge search path"
    does not work — `search_knowledge` does pgvector search over
    `knowledge_content`, `references.py` lists `knowledge_content`, and
    `list_resources` filters only by `kind`. **Nothing reads `public.resources`
    by tag.** Storable, not queryable. (#203)
30. **A2's UI instruction was not implementable as written** —
    `GET /api/interactions/{id}` did not return `metadata`, so the editor could
    not be initialised from `metadata.proposed_doc`. A2 added it. (#217)
31. **An `input` card with no options renders as a dead card.** `cards.py` emits
    a URL button for `input` only when `options.aegis_ui_url` is present, so A7's
    card needed that threaded from settings or there would have been no way to
    answer it; C6 sidestepped it by using `ack`. (#198, #199)
32. **A7's `should_send` gate alone cannot produce `status="budget"`.**
    `notification_budget_enabled` is `False` by default and in production, and
    `should_send` *always allows* when disabled. The per-day count is what
    actually enforces `max_per_day`. (#198)
33. **B4's per-source secret override cannot be generic.** `Settings` is a
    pydantic model, so `getattr(settings, f"life_webhook_secret_{source}")`
    resolves to nothing unless the field is *declared*. Also: an **optional**
    `X-Aegis-Timestamp` is not replay protection — it shipped mandatory. (#206)
34. **C1's requested GIN index would have been dead.** A plain GIN index over a
    mixed-case `text[]` cannot serve the case-insensitive alias probe C2 needs;
    aliases are normalized on write instead. (#189)
35. **A3's acceptance criterion 1 is unreachable as written** — "one row
    remains, count drops by exactly one" requires applying the plan, which A3's
    own hard gate forbids. Internally inconsistent with criteria 3 and 4 in the
    same list. (#194)
36. **C6's "insert the alert row only after a successful `safe_send_message`" is
    not possible on a card path** (dispatch happens in a child workflow after the
    activity returns) and is not race-safe. (#199)
37. **A5's "`superseded_by = NULL, source = 'promoted'`" is incoherent and
    unsafe** — `superseded_by = NULL` is a no-op on a column that does not exist
    until A4, and rewriting `source` would clobber `source='gmail_triage_correction'`,
    the exact marker A4's own section says must be protected. Relatedly, A4's
    `superseded_by` cannot be the retirement marker at all; `superseded_at` was
    added instead. (#219, #218)

### Forward references and things that did not exist

38. **`capture_classify` did not exist when A2, A3 and A6 cite it as "the
    correct template" for LLM-call wiring.** It appears only in this document —
    as B3's own `purpose` string, and in sections that reference B3. It was a
    forward reference; **B3 created it** (#208). A3 had to substitute
    `IntelligenceActivities.score_significance` / `DayLogActivities.distil_daylog`
    (#194).
39. **B7's "reuse B6's metric vocabulary and normalization helpers" — B6 had not
    landed.** There was nothing to reuse. (#207)
40. **B7's `source_types.py` registration is inert.** That registry is the
    `knowledge_content.source_type` vocabulary; the wearable feature writes to
    `life.observations.source`, a different column, and nothing calls
    `warn_if_unknown` on it. (#207)
41. **`channels:history` was already in the Slack manifest** — only
    `reactions:read` and the `reaction_added` subscription were new. (#186)
42. **B5's `metadata={place, lat, lon, accuracy, trigger}` contradicts its own
    "coarse coords only" instruction** in the same bullet. Shipped storing no
    coordinates at all. (#209)
43. **Two proposed crons land on crowded minutes.** A2's `0 2 * * 0` sits on
    minute `:00`, the busiest minute in the schedule file (two hourly flows plus
    the `*/2`, `*/5`, `*/15` and `*/30` steppers) — the very thing its own
    justification rules out; shipped `23 2 * * 0`. C6's `15 7 * * *` is free
    among daily rows but is the `*/15` sweep minute; shipped `25 7 * * *`.
    (#217, #199)

---

## Follow-on issues this work opened

Open:

- **#182** — Postiz channel sync is manual-only; new channels stay invisible to the publish pipeline
- **#183** — Task labelled for a platform with no connected account stays open forever
- **#184** — No tool answers "which social channels can you post to?"
- **#200** — No API path to unlink a person or asset from an expiring item (C5/C7)
- **#205** — Connector construction failures degrade silently to `None` (the hazard D6 declined to fix in scope)
- **#212** — Health import: unknown unit falls back silently, risking a 60x or 4x error (B6)
- **#215** — Briefing narrative still carries the rendered health block into Temporal history (B6)
- **#220** — `gather_profile_evidence` reads two global tables, so any test leak breaks every "quiet week" assertion (A2)
- **#221** — A2: a drifted persona-doc approval is silent to the approver
- **#223** — Curiosity `_known_text` does not filter A4 soft-retired memories (A6/A4 interaction)
- **#225** — Social posts silently stuck: Postiz orchestrator stopped polling Temporal since Jul 29, with no QUEUE-past-due alert
- **#226** — A scheduled flow can fail every run indefinitely with no alert

Closed by their fixing PRs:

- **#188** — Flow registration tests checked the wrong list; a removal from the Worker's actual list would slip past → closed by **#204** (D6)
- **#190** — `test_alert_escalation` leaks a temporal-test-server, hanging the next suite run at ~99% → closed by **#196**
- **#201** — A4 review checklist: the consolidation SQL spy does not cover MERGE/TRUNCATE → closed by **#218**

---

## What the queue actually taught us

Two findings worth carrying forward.

**1. The document's errors had one shape: correct about what it calls, wrong
about what that call does.** Symbol names and file paths were nearly always
right. What was wrong was the *behaviour* behind them — `think()` does not log
the call, `list_content_items` does not return the body, `format_weekly_preview`
is not the delivery path, a source-keyed retention entry prunes nothing, a
`ConfigKey` does not reach comms, `seed.py` does not overwrite config. Every one
of these compiles, type-checks, lints clean and passes a call-shape test. CI
cannot catch this class of error; only reading the callee can. Every task in the
queue was therefore run with an explicit "verify each cited claim against the
code before implementing, and report deviations" step — which is where this
entire stale-claims list came from.

**2. Break-and-revert falsifiability caught vacuous tests in most rounds.** The
practice — deliberately break the implementation, confirm the test fails, revert
— repeatedly exposed tests that passed for the wrong reason, *including several
where an agent's own first-draft test passed against a deliberately broken
implementation*. Concrete examples: A9's rollup test passed with the day-log body
dropped entirely, until break-and-revert showed the filed document read
`"Weekly log 2026-W31 — 7 day(s) recorded."` and nothing else (#192); C4's
`float8` write cast turned out to be doing nothing and was removed (#195); B2
dropped a duplicate guard because removing either copy alone changed no test
(#186). A test that has never been observed to fail is not evidence.

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

**Cross-theme coordination notes**

- **Suggested overall order:** D1+D2 (quick wins) → C8 (source_type registry) →
  A1→A2 (profile loop) → B1→B2→B3 (capture) → A8→A9 (day log + rollups) →
  D6 (declarative registry — must land here: A2 and A8 are the first two new
  scheduled flows, and D6's own constraint is "before the third") →
  A6→A7 (curiosity) → C1→C2→C3 (people + enrichment + payoff features) /
  C4 (observations) → C5→C6→C7 (expiry/assets) → B4→B5/B6 (push senses) →
  A3→A4→A5 (memory lifecycle) → B7 → B8→B9 (MCP) → D7 (chat.py split) →
  D3/D4/D5 anytime.
  (New cron-scheduled flows in this order: A2 #1, A8 #2 — D6 — A7 #3,
  C6 #4, B7 #5. The previous revision of this order both violated the D6
  constraint and omitted A9, B2, C2 and C3 entirely.)
- **Migration numbers collide across themes** (A1 and C1 both assume `015_`,
  etc.). Numbers below are relative — always take the next free slot at
  implementation time, and never reuse `006`.
- **Theme B's observation writers (B5/B6/B7) depend on C4** (the
  `life.observations` table); Theme A and Theme D have no cross-theme
  dependencies.

## Theme A — Memory & learning loop

> **STALE — the "6-point registration rule" below no longer exists.** D6 (PR #204,
> `c6b9f8f`) deleted all six hand-edited lists. A new scheduled flow is now one
> `FlowSpec` in `worker/src/aegis_worker/registry.py` plus a seed row in
> `config/seed/activities.yaml`, enforced by `check_registration()` at worker boot.
> Activity methods need no registration edit at all. Every per-task "all 5 points"
> checklist in Themes A, B and C is obsolete for the same reason.

Goal: AEGIS stops being stateless-with-notes and starts accumulating a durable, human-auditable model of its owner. Four features, split into nine independently shippable tasks. All flow work follows the 6-point registration rule (flow class import + module-level `WORKFLOWS` at `worker/src/aegis_worker/__main__.py:111` + the local `workflows` list in `main()` ~line 670 + the `activities` list ~line 555 + `_ACTIVITY_TYPE_MAP` at `worker/src/aegis_worker/schedule_sync.py:57` + a seed row in `config/seed/activities.yaml` — Part 1's "five hand-edited lists plus seed YAML"); every flow config dataclass keeps `agent_id` as its first field. Per-task "all 5 points" checklists below fold the flow-class import into point 1.

### A1 — Profile write path + revision log

> **SHIPPED — PR #178 (`0abb939`).** Migration `015` as sketched (the only correct number in the doc). Deviations: a `_TIMESTAMP_COLUMNS` entry was also required or the retention would never have pruned; no FK on `interaction_id` (a revision must outlive the interaction); a patch is recorded even when content is unchanged.

**Outcome** A safe, auditable server-side path for programmatically patching an agent's `user` personality doc, with every revision recorded and revertible. Nothing writes to it yet — this is the substrate A2/A5/A7 land on.

**Implementation sketch**
- Add `migrations/015_agent_profile_revisions.sql`: `agent_profile_revisions(id bigserial, agent_id text, kind text, before_content text, after_content text, source text, interaction_id uuid null, created_at timestamptz)`. Mirrors the `agent_personalities` shape in `migrations/007_agent_personalities.sql`.
- In `core/src/aegis/services/personalities.py`, add `apply_profile_patch(pool, agent_id, kind, new_content, *, source, interaction_id=None)` — reads current via `get_personality(..., use_cache=False)`, writes the revision row, delegates the upsert to the existing `set_personality` (which already calls `invalidate`), and refuses writes >N% shrink without an explicit `allow_shrink` flag.
- Add `list_profile_revisions` / `revert_profile_revision` helpers; expose read-only at `GET /api/admin/agents/{agent_id}/personality/revisions` in `core/src/aegis/api/routes/agents.py` (next to the existing `GET`/`PUT /{agent_id}/personality` at lines 25–46). Mark the route docstring "intentionally curl/ops-only, no UI consumer" — issue #101's convention for endpoints that are deliberately not wired to the admin panel.
- Retention: add an `agent_profile_revisions` entry (365 days) to `_DEFAULT_RETENTIONS` in `worker/src/aegis_worker/flows/cleanup.py:14` — every patch writes a row forever otherwise.
- New worker activity module `worker/src/aegis_worker/activities/profile.py` with `ProfileActivities(db_pool)` exposing `read_profile_context` and `apply_profile_patch` (thin wrappers, `@activity.defn`), so flows never import the FastAPI layer.

**Wiring checklist** migration only + activities list in `__main__.py` (no flow, no schedule, no `_ACTIVITY_TYPE_MAP` entry yet).

**Acceptance criteria**
- `tests/core/services/` (real `db_pool` fixture from `tests/core/conftest.py`): patching writes exactly one `agent_profile_revisions` row with `before_content` equal to the prior value; `get_personality` returns the new content on the next call with `use_cache=False`.
- A shrink >50% without `allow_shrink` raises and leaves both tables untouched.
- `revert_profile_revision` restores byte-identical prior content and itself logs a revision.
- Registration test in `tests/worker/` asserting `ProfileActivities.apply_profile_patch` carries `__temporal_activity_definition` (pattern: `tests/worker/test_briefing_registration.py`).

**Size** M **Depends on** —

### A2 — ProfileReflectionFlow (weekly draft_review)

> **SHIPPED — PR #217 (`793d4db`). Ships INERT: nothing reaches a persona doc until a human approves the card.** Deviations: registration is a `FlowSpec` (D6), not the listed lists; the `think()` logging claim is false, so `record_llm_call` is explicit on both success and `LLMTruncationError`; `metadata` had to be added to `GET /api/interactions/{id}` for the UI panel; cron `23 2 * * 0`, not `0 2 * * 0`. Follow-ups: #220, #221; refined by #222.

**Outcome** Every week each agent proposes a concrete diff to its own `user` personality doc, delivered as a `draft_review` interaction card; the human approves or edits before anything is written.

**Implementation sketch**
- `worker/src/aegis_worker/activities/profile.py`: add `gather_profile_evidence(agent_id, since)` — one isolated try/except per source so a dead source degrades to empty, exactly like `BriefingActivities.gather_briefing_changes` (`worker/src/aegis_worker/activities/briefing.py:205`). Sources: `chat_history` (`migrations/001_baseline.sql:255`), `agent_memory` (:158), resolved `interactions` (:339) with a correction reason, `finance.receipt_email` (:31) / `finance.recurring_charge` (:45), calendar events from the `calendar_events_%` settings KV rows read by `BriefingActivities.gather_calendar_events`.
- Add `propose_profile_patch(agent_id, evidence, current_user_doc)` — one LLM call via the injected `llm_client.think` on the balanced tier, passing `db_pool` and `purpose="profile_reflection"` so the call lands in `llm_calls` (do NOT copy `frame_briefing`'s wiring verbatim — it is one of issue #106's named unlogged call sites; B3's `capture_classify` wiring is the correct template). Returns `{proposed_doc, rationale, changed_lines}`. Any LLM failure returns `{}` → flow exits `status="skipped"`, never a failed run.
- New flow `worker/src/aegis_worker/flows/profile_reflection.py` with `ProfileReflectionConfig(agent_id: str = "sebas", lookback_days: int = 7)`. Spawns `InteractionFlow` (`worker/src/aegis_worker/flows/interaction.py`) as an ABANDONED child with `kind="draft_review"`, `timeout_policy="archive"`, `timeout_seconds=7*86400`, `metadata={agent_id, kind:"user", proposed_doc, revision_of}`, `post_resolve_activity="apply_profile_reflection"` — copy the spawn shape from `worker/src/aegis_worker/flows/social_publish.py:60-90`.
- `apply_profile_reflection(interaction_id, response, metadata)` in `ProfileActivities`: writes `response["edited_doc"]` if present, else `metadata["proposed_doc"]`, via A1's `apply_profile_patch(source="profile_reflection", interaction_id=...)`. Any non-approve response is a no-op (but the existing `record_correction_from_interaction` in `core/src/aegis/api/routes/interactions.py:106` still banks the reason as a memory).
- **UI work (required, not a verification step):** `admin-panel/frontend/src/pages/InteractionDetail.tsx` currently shares one textarea+submit branch between `input` and `draft_review`, posting `resolveInteraction(id, {value: draft})` — there is no `action` or `edited_doc` key and no reject button, so `apply_profile_reflection`'s contract cannot fire from this screen today. Add a `draft_review`-specific panel: proposed-doc preview, editable text initialised to `metadata.proposed_doc`, and explicit Approve / Reject buttons submitting `{action: "approve", edited_doc}` / `{action: "reject", reason}`. (`comms/src/aegis_comms/cards.py:87` already deep-links here, so no comms change.)
- Budget gate: `send_interaction_card` bypasses `safe_send_message` and therefore the notification budget (`delivery.py:163` posts straight to `/api/deliver/card`). Gate the weekly card exactly as A7 does — call `aegis.services.notifications.should_send` before spawning and `record_notification(pool, agent_id, "profile_reflection", sent)` after, so the card consumes the same daily budget.
- Seed: `slug: profile-reflection-weekly`, `schedule_cron: "0 2 * * 0"` (07:30 IST Sunday — `0 4` belongs to `cleanup-daily` and sits 30 min before the 04:30 briefing, the busiest minute-cluster in the schedule), `config: {lookback_days: 7}`, one row per agent that should self-reflect.

**Wiring checklist** all 5 points (flow + `ProfileActivities` methods + both `__main__.py` lists + `_ACTIVITY_TYPE_MAP` builder reading `lookback_days` + seed rows).

**Acceptance criteria**
- Flow test under `tests/worker/` using the Temporal test environment (pattern: `tests/worker/test_clarify_flow.py`): mocked activities → exactly one `InteractionFlow` child spawned with `kind="draft_review"` and `post_resolve_activity="apply_profile_reflection"`.
- Empty evidence bundle → no card spawned, `status="skipped"`.
- `apply_profile_reflection` with `{"action":"approve","edited_doc":"..."}` on a real `db_pool` writes the edited text (not the proposed text) and one revision row; with `{"action":"reject","reason":"..."}` writes nothing.
- **End-to-end payload-shape test (falsifiability guard):** resolve a seeded `draft_review` interaction through the real resolve route with the exact JSON the UI submits, and assert the profile changed — this test MUST fail if the UI payload shape and `apply_profile_reflection`'s contract drift apart. A direct call with a hand-built dict does not satisfy this criterion.
- Second run in the same day with the notification budget exhausted → no card spawned, `status="budget"`, one `notification_log` row from the first run.
- Registration test asserting `ProfileReflectionFlow` is in `WORKFLOWS` and has an `_ACTIVITY_TYPE_MAP` entry.

**Size** L **Depends on** A1

### A3 — Memory consolidation pass (ADD/UPDATE/DELETE/NOOP)

> **SHIPPED — PR #194 (`6d768ba`), planner only.** Deviations: `apply_consolidation` was deliberately NOT written (not writing the destructive SQL makes safety structural rather than a flag); `dry_run=False` refuses *unconditionally* rather than probing for A4's table; acceptance criterion 1 was unreachable as written; the seed `config` change does not reach an existing deployment.

**Outcome** The nightly `MemoryReflectionFlow` stops being a row-cap and actually consolidates: duplicate lessons merge, contradictions resolve in favour of the newer signal, stale rows retire.

**Implementation sketch**
- `core/src/aegis/services/memory.py`: add `all_memories(pool, agent_id)` (id + content + importance + source + created_at — `recent_memories` at line 36 returns content only) and `apply_consolidation(pool, agent_id, ops)` executing a validated `[{op, id?, content?, importance?}]` list in one transaction.
- `worker/src/aegis_worker/activities/memory.py`: add `consolidate_agent_memories(agent_id, dry_run=True)` to `MemoryActivities` — loads the agent's rows, one LLM call (balanced tier, with `db_pool` and `purpose="memory_consolidation"` so it lands in `llm_calls` — not `frame_briefing`'s unlogged wiring, which issue #106 names as the offending pattern) returning strict JSON ops, validates every referenced id belongs to that agent, returns `{ops, applied, skipped}`. `MemoryActivities` currently takes only `db_pool`, so add an `llm_client: Any = None` field and pass `deps.llm` at `worker/src/aegis_worker/__main__.py:350`; a null client makes the activity a documented no-op.
- **Hard gate, in code, not config:** DELETE here is a hard delete of human-authored corrections driven by LLM JSON, and `dry_run: true` as a seed default is not a safety mechanism — a config edit could flip it before the rails exist. A3 ships with `dry_run=False` REFUSED (`ApplicationError`, non-retryable) whenever the A4 provenance machinery (`agent_memory_ops_log` table / soft-retire path) is absent; A4 removes the refusal. Until then the activity is observe-only by construction.
- `worker/src/aegis_worker/flows/memory_reflection.py`: run consolidation **before** `prune_agent_memories` (consolidating first means the cap deletes fewer real lessons), each step in its own try/except so consolidation failure still prunes. Extend `MemoryReflectionInput` with `consolidate: bool = False` and `dry_run: bool = True`; update the docstring, which currently advertises this pass as unbuilt.
- `_ACTIVITY_TYPE_MAP["MemoryReflectionFlow"]` (`schedule_sync.py:168`) reads the two new config keys; ship the seed row (`config/seed/activities.yaml:144`) with `consolidate: true, dry_run: true` so the first production week is observe-only.

**Wiring checklist** activities list (`memory_act.consolidate_agent_memories`), `MemoryActivities` construction (add `llm_client`), `_ACTIVITY_TYPE_MAP` config keys, seed config. No new flow class.

**Acceptance criteria**
- `tests/worker/activities/test_memory_consolidation.py` on the real `db_pool`: two near-duplicate rows + a stubbed LLM returning a merge op → one row remains, content is the merged text, `agent_memory` count drops by exactly one.
- An op referencing another agent's memory id is rejected and counted in `skipped`; the foreign row survives.
- `dry_run=True` returns the ops but leaves the table byte-identical.
- `dry_run=False` without A4's `agent_memory_ops_log` present → the activity refuses (non-retryable error), zero rows changed — proven by a test that creates the condition, not by the config default.
- LLM raising → activity returns `{"status":"llm_failed"}`, flow still returns the prune result.

**Size** M **Depends on** — (dry-run only; non-dry-run execution is hard-blocked until A4 lands)

### A4 — Consolidation safety rails

> **SHIPPED — PR #218 (`407afc3`). Ships INERT: two independent keys (DB config flag + environment kill switch) are required before it mutates `agent_memory`.** Deviations: migration `020`, not `016`; `superseded_at` is the retirement marker, not `superseded_by`; `agent_memory_ops_log` deliberately left OUT of `_DEFAULT_RETENTIONS` (the janitor has no predicate support and would delete the applied-op record) with a tripwire test; protection keyed on the `[gmail:` content marker rather than `source`. Closed #201.

**Outcome** The consolidation pass can be trusted to run with `dry_run: false`: deletions are bounded, provenance survives, and a bad night is reversible.

**Implementation sketch**
- `migrations/016_agent_memory_provenance.sql`: add `agent_memory.superseded_by bigint NULL`, `agent_memory.last_consolidated_at timestamptz NULL`, and `agent_memory_ops_log(id, agent_id, op, memory_id, before_content, after_content, dry_run bool, created_at)`.
- Make DELETE a soft retire in `apply_consolidation` (set `superseded_by`, exclude retired rows from `recent_memories`) so `format_memories` output changes but nothing is lost; `prune_memories` (`core/src/aegis/services/memory.py:115`) becomes the only hard delete.
- Quotas in `consolidate_agent_memories`: refuse a batch that deletes/merges more than `max_ops_pct` (default 25%) of an agent's rows; never touch rows younger than `min_age_hours` (default 24) or with `importance >= 0.9`; protect `source='gmail_triage_correction'` rows from silent merge (they carry the `[gmail:<id>]` dedupe marker that `record_gmail_triage_correction` relies on at line 91).
- Log every proposed op to `agent_memory_ops_log` including in dry-run — that log is how we decide when to flip `dry_run: false`. Because dry-run is the intended long-running default, this table grows nightly forever: add an `agent_memory_ops_log` entry (90 days) to `_DEFAULT_RETENTIONS` in `worker/src/aegis_worker/flows/cleanup.py:14` in the same task.
- Removing A3's hard refusal of `dry_run=False` is part of THIS task — the gate and the rails swap in one commit.
- Surface a read-only `GET /api/admin/agents/{agent_id}/memory/ops` in `core/src/aegis/api/routes/` for the flip decision, with an "intentionally curl/ops-only, no UI consumer" docstring note (issue #101's convention).

**Wiring checklist** migration + config keys (`max_ops_pct`, `min_age_hours`) threaded through `_ACTIVITY_TYPE_MAP` and the seed row. No new flow.

**Acceptance criteria**
- A 40-row agent with an LLM proposing 20 deletes → batch rejected wholesale, zero rows changed, one `agent_memory_ops_log` row per proposed op with `dry_run` reflecting the mode.
- A merge op targeting a row containing `[gmail:...]` is skipped; `record_gmail_triage_correction` remains idempotent for that email id afterwards.
- Soft-retired rows disappear from `recent_memories` but remain `SELECT`-able with `superseded_by` set.
- Rows created <24h ago are never in the op set.

**Size** M **Depends on** A3

### A5 — Generalization promotion (memory → profile)

> **SHIPPED — PR #219 (`39f9801`). Ships INERT behind A2's human-approved card.** Deviations: `propose_generalizations` lives on `ProfileActivities`, not `MemoryActivities` (avoids racing A4's concurrent rewrite of that file); the sketch's `superseded_by = NULL, source = 'promoted'` was not implemented — incoherent and would clobber the gmail dedupe marker — so nothing in `agent_memory` is mutated and the ledger is `promoted_memory_ids` + the revision row; zero registration edits needed. Refined by #222.

**Outcome** Repeated lessons stop living forever as one-line memories and get promoted into the durable `user` profile doc — through the same human-approved card as A2, never silently.

**Implementation sketch**
- Add `propose_generalizations(agent_id)` to `MemoryActivities`: clusters an agent's memories (LLM, same client as A3) and emits candidates of the form `{claim, supporting_memory_ids, confidence}` for anything backed by ≥3 rows.
- `ProfileReflectionFlow` calls it after `gather_profile_evidence` and passes the candidates into `propose_profile_patch` as a distinct `generalizations` key so the LLM prompt can weight them above raw evidence.
- On approval, `apply_profile_reflection` additionally marks the supporting memory ids `superseded_by = NULL, source = 'promoted'` (or soft-retires them once A4 lands) so the same claim doesn't re-promote every week.
- Off-week safety: candidates below `confidence` threshold are logged, not carded.

**Wiring checklist** activities list (`memory_act.propose_generalizations`); no new flow, no schedule, no seed change.

**Acceptance criteria**
- Three memories expressing the same preference produce exactly one candidate carrying all three ids; two memories produce none.
- After an approved promotion, a second run over the same data produces zero candidates (idempotence over weeks).
- Rejection of the card leaves all supporting memories untouched and un-retired.

**Size** M **Depends on** A1, A2, A3

### A6 — Curiosity gap-finder activity

> **SHIPPED — PR #180 (`e7b68cd`), plus review fix #181 (`b9db109`: exclude the calendar owner from gaps, count only open tasks).** Deviations: the sketch's `finance.recurring_charge` column was renamed by migration `009`, so the detector as specced would have been permanently and silently dead; attendees are read from `knowledge_content`/`knowledge_chunks` at `source_type='calendar'`, not the writerless `calendar_events_%` KV rows. Open follow-up: #223.

**Outcome** A deterministic, testable detector that turns data gaps into at most a handful of ranked candidate questions per day — no delivery, no LLM required for the detection itself.

**Implementation sketch**
- New `worker/src/aegis_worker/activities/curiosity.py`, `CuriosityActivities(db_pool, llm_client=None)`, activity `find_curiosity_gaps(agent_id, limit=5)`.
- Detectors, each independently try/excepted: (a) recurring calendar attendee never mentioned in `chat_history` or `agent_memory` — attendee emails are already carried through `CalendarActivities.fetch_events` (`worker/src/aegis_worker/activities/calendar.py:109`) and into KS text via `calendar_event_to_content` (`core/src/aegis/services/claims.py:10`); (b) `finance.recurring_charge` rows with `status='active'` and no matching memory/profile mention; (c) a frequently-hit `todoist_tasks` project (`migrations/001_baseline.sql:607`) with no profile context.
- Each candidate: `{gap_type, subject, question, evidence, novelty_key}`. `novelty_key` is a stable hash (e.g. `attendee:<email>`) used for the never-ask-twice check against previously-created `interactions` rows (`metadata->>'novelty_key'`).
- Optional single LLM pass phrases the question; deterministic template is the fallback (structure like `frame_briefing`, `activities/briefing.py:374`, but pass `db_pool` + `purpose="curiosity_phrasing"` to `think()` — `frame_briefing` itself is an unlogged call site named by issue #106; don't copy that part).

**Wiring checklist** activities list in `__main__.py` + construction with `db_pool` and `deps.llm`.

**Acceptance criteria**
- Real-Postgres test: an active `finance.recurring_charge` with no matching `agent_memory` row yields a candidate; adding a memory naming that vendor removes it.
- A `novelty_key` already present on any non-archived `interactions` row is excluded.
- Zero detectors firing returns `[]` — never a synthetic filler question.
- LLM absent/failing → deterministic question text, still one candidate.

**Size** M **Depends on** —

### A7 — CuriosityCardFlow (one question per day, budget-aware)

> **SHIPPED — PR #198 (`7b54879`).** Deviations: the daily cap is checked *before* the shared budget (otherwise a pending card shadows `status="budget"`), and the shared-budget rejection is reported as `global_budget`; `should_send` alone cannot enforce the cap since the budget is disabled by default; three activities, not two; the card must thread `aegis_ui_url` or an `input` card renders with no way to answer.

**Outcome** At most one `input`-kind card per day asks the owner a real question about their life, and the answer is banked as durable memory.

**Implementation sketch**
- New `worker/src/aegis_worker/flows/curiosity.py`, `CuriosityConfig(agent_id: str = "sebas", max_per_day: int = 1)`. Daily cron `"30 9 * * *"` (15:00 IST) — not adjacent to the 04:30 briefing, and NOT `0 9`, which `money-hygiene-daily` already holds.
- Gate before spawning: interaction cards go out via `send_interaction_card`, which does **not** pass through `safe_send_message` (`worker/src/aegis_worker/activities/delivery.py:15`) and therefore is not currently covered by the notification budget. So the gate is explicit: call `aegis.services.notifications.should_send` (`core/src/aegis/services/notifications.py:32`) from a small `check_curiosity_budget` activity, and on send call `record_notification(pool, agent_id, "curiosity_card", sent)` so curiosity consumes the same daily budget as proactive FYIs. Additionally hard-cap at one *pending* curiosity interaction at a time (`SELECT ... FROM interactions WHERE origin='curiosity' AND status='pending'`).
- Spawn `InteractionFlow` ABANDONED with `kind="input"`, `origin="curiosity"`, `timeout_policy="archive"`, `timeout_seconds=2*86400`, `metadata={novelty_key, gap_type, subject}`, `post_resolve_activity="apply_curiosity_answer"`.
- `apply_curiosity_answer` writes the answer via `record_memory(pool, agent_id, ..., importance=0.8, source="curiosity")`; high-signal answers additionally show up as evidence to A2 next Sunday (no direct profile write from this path).
- Agent selection uses `AgentRegistryActivities.resolve_agents` (`worker/src/aegis_worker/activities/agent_registry.py:41`) so a finance-gap question is asked by the `finance`-tagged agent, a calendar gap by `gtd`.

**Wiring checklist** all 5 points (flow + `CuriosityActivities.check_curiosity_budget` / `apply_curiosity_answer` + both `__main__.py` lists + `_ACTIVITY_TYPE_MAP` + seed row `curiosity-daily`).

**Acceptance criteria**
- Two runs on the same day → second spawns no card and returns `status="budget"`; `notification_log` shows one `curiosity_card` row with `sent=true`.
- With an already-pending `origin='curiosity'` interaction, the flow spawns nothing.
- `apply_curiosity_answer` with a non-empty `value` inserts exactly one `agent_memory` row with `source='curiosity'`; an empty/skipped answer inserts none.
- Card renders as `input` (`comms/src/aegis_comms/cards.py:87` deep-link) — assert the card spec in `tests/comms/`.

**Size** M **Depends on** A6

### A8 — DayLogFlow (nightly episodic diary)

> **SHIPPED — PR #187 (`b261c56`), plus review fix #191 (`300c6ca`: exclude archived cards).** Deviations: `calendar_events_%` settings rows are not a source (they have no writer) — meetings come from `knowledge_chunks` filtered on the `Start: <date>` text; `lookback_hours` → `day_offset` (a 24h window at the 19:00 UTC cron lands on the wrong date); three registration lists, not two, and a third activity (`commit_daylog_state`).

**Outcome** Each night, the day's actual events become one dated knowledge-store entry with `source_type='daylog'`, giving future retrieval an episodic spine ("what happened on 2026-07-14") the briefing archive can't provide.

**Implementation sketch**
- New `worker/src/aegis_worker/activities/daylog.py`, `DayLogActivities(db_pool, llm_client, knowledge_connector)`.
- `gather_day_events(date)` — per-source try/except: Gmail highlights from `knowledge_content` (`migrations/001_baseline.sql:387`) filtered to the day, calendar events attended (settings KV `calendar_events_%` / KS calendar items), `todoist_tasks` completed that day, captures from `gtd_clarify_log` (:297), resolved `interactions` (:339) and how they were decided, and workflow failures from `workflow_runs` (:670).
- `distil_daylog(events, date)` — one LLM call → 200-400 word narrative; deterministic bullet fallback (mirror `_format_changes_fallback`).
- Ingest by reusing the already-registered generic activity `ContentActivities.ingest_content` (`worker/src/aegis_worker/activities/content.py:301`) with `{url: f"aegis://daylog/{date}", title: f"Day Log {date}", source_type: "daylog", raw_text: narrative, tags: ["daylog"]}` — the direct analogue of `BriefingActivities.ingest_briefing` (`activities/briefing.py:469`) but without a second bespoke ingest activity.
- New flow `worker/src/aegis_worker/flows/daylog.py`, `DayLogConfig(agent_id: str = "raphael", lookback_hours: int = 24)`. Cursor state in the `settings` table (`:521`) under key `daylog_state`, committed only after a successful ingest — same discipline as `commit_briefing_state` (`activities/briefing.py:458`). Cron `"0 19 * * *"` (00:30 IST).
- Idempotency: the `aegis://daylog/<date>` URL is the natural key; re-running the same date must update, not duplicate.

**Wiring checklist** all 5 points (flow + `DayLogActivities.gather_day_events` / `distil_daylog` + both `__main__.py` lists + `_ACTIVITY_TYPE_MAP` + seed row `daylog-nightly`).

**Acceptance criteria**
- Real-Postgres test seeding one completed task, one resolved interaction and one calendar event for a date → `gather_day_events` returns all three bucketed by kind; a failing source (bad table/permission) degrades that bucket to empty without failing the activity.
- Stubbed knowledge connector receives exactly one `ingest_content` call with `source_type='daylog'` and `url='aegis://daylog/<date>'` — AND the `raw_text` it carries mentions the seeded task/interaction/event content (a call-shape assertion alone passes with `distil_daylog` returning `""`; asserting the narrative reflects the inputs is what makes this falsifiable).
- Running the flow twice for the same date issues the same URL (no second entry) and does not double-advance `daylog_state`.
- An empty day still writes an entry (a quiet day is data), marked `quiet: true` in metadata.

**Size** M **Depends on** —

### A9 — DayLog weekly + monthly rollups

> **SHIPPED — PR #192 (`3cd781a`).** Deviation (load-bearing): `KnowledgeStore.list_content_items` / `search` cannot supply the day-log body — the activity reads `knowledge_content JOIN knowledge_chunks` directly and stitches the 200-char chunk overlap. Flow count is unchanged by design (`activities` 160→162, `flows` 33→33).

**Outcome** Weekly and monthly summaries built from the daylog entries, so retrieval over "last quarter" doesn't have to read 90 daily documents.

**Implementation sketch**
- `DayLogActivities.gather_daylogs(start, end)` — pulls `source_type='daylog'` entries via `KnowledgeStore.list_content_items` / `search` (`core/src/aegis/services/knowledge.py:301`, `:187`), same access pattern as `BriefingActivities.gather_references_filed`.
- `distil_rollup(entries, period, label)` — one LLM call producing themes/decisions/open threads; deterministic concatenation fallback.
- Extend `DayLogConfig` with `mode: str = "daily"` (`daily` | `weekly` | `monthly`) rather than adding two more flow classes; the flow branches on mode. Two extra seed rows: `daylog-weekly` `"0 20 * * 0"`, `daylog-monthly` `"0 21 28-31 * *"` with a last-day-of-month guard inside the flow.
- Ingest as `source_type='daylog_rollup'` with `url=aegis://daylog/week/<iso-week>` / `aegis://daylog/month/<yyyy-mm>` and `metadata={period, covers: [dates]}`.

**Wiring checklist** `_ACTIVITY_TYPE_MAP["DayLogFlow"]` reads `mode`; two additional seed rows; activities list gains `gather_daylogs` / `distil_rollup`. No new flow class, no new `WORKFLOWS` entry.

**Acceptance criteria**
- Seven stubbed daylog entries → one `ingest_content` call with `source_type='daylog_rollup'`, `metadata.covers` listing all seven dates, and non-empty `raw_text` that references content from the entries (not just their existence).
- Fewer than 2 entries in the window → no rollup written, `status="insufficient"`.
- The monthly run fires only on the actual last day of the month (test February and a 31-day month).
- `mode="daily"` behaviour is byte-identical to A8 (regression test over the same fixture).

**Size** S **Depends on** A8

---

## Theme B — Capture & ingestion

New life-data senses. Every task below reuses one of three existing shapes: the signed-webhook handler in `core/src/aegis/api/routes/webhooks.py`, the pollable-`channels`-row pattern (`core/src/aegis/api/routes/channels.py` + `worker/src/aegis_worker/activities/channels.py`), or the knowledge pipeline (`KnowledgeStore.ingest_content`). Nothing here designs the time-series table — B5/B6/B7 write to `observations(source, metric, value, observed_at, metadata)` delivered by **C4**.

### B1 — `life_fact` capture lane

> **SHIPPED — PR #179 (`da653b5`), plus review fix #181 (`b9db109`: reject an empty `life_fact`).** The most accurate section in the document — every cited claim held except `ingest_content` being at `:96`, not `:94`. Implementation deviation: the new DB-touching route tests use httpx `ASGITransport`, not the file's existing `TestClient` (which drives its own event loop and dies on the asyncpg pool).

**Outcome:** The existing capture surfaces can drop a *fact about my life* into the knowledge store instead of a Todoist task. Everything downstream (B2, B3) routes into this lane.

**Implementation sketch**
- `core/src/aegis/api/routes/capture.py`: add `kind: Literal["task","life_fact"] = "task"` to `CaptureRequest`. `task` keeps the current `_capture_to_inbox_impl` path verbatim; `life_fact` calls the knowledge connector (`aegis.api.deps.get_knowledge_connector`) → `KnowledgeStore.ingest_content` (`core/src/aegis/services/knowledge.py:94`) with `source_type="life_fact"`, `tags=["life_fact", body.source]`.
- Use a synthetic stable URL `aegis://life_fact/{ext_id}` — `_content_id_for(url)` hashes the url, so re-posting the same text is an idempotent upsert with no extra table.
- `CaptureResponse` gains `content_id: str | None`; `task_ref` is `None` for life facts.
- `comms/src/aegis_comms/slack_inbound.py:349` `SlackCoreClient.capture()` gains `kind`; `:397` `knowledge_ingest()` currently hardcodes `source_type="document"` — parameterize it.
- New `/remember` slash command: `on_remember` beside `on_capture` (`slack_inbound.py:649`), registered next to `@app.command("/capture")` in `comms/src/aegis_comms/adapters/slack.py:480`.

**Wiring checklist:** route model + branch · comms client `kind` param · `on_remember` handler · Slack command registration · Slack app manifest slash-command entry.

**Acceptance criteria** (`tests/core/test_capture_route.py`, real Postgres)
- `POST /api/admin/capture {kind:"life_fact"}` → 200, and a `knowledge_content` row exists with `source_type='life_fact'` and the returned `content_id`.
- Re-posting identical text returns the same `content_id` and leaves exactly one `knowledge_content` row.
- All existing `kind` omitted / `kind="task"` tests stay green unchanged (no Todoist behaviour drift).

**Size:** S · **Depends on:** —

### B2 — Curated self-signal ingest (Slack reaction / note-to-self)

> **SHIPPED — PR #186 (`1f09540`), plus review fix #191 (`300c6ca`: unblock note-channel mentions).** Deviation (load-bearing): the wiring checklist would have shipped a permanent silent no-op — core `ConfigKey`s never reach comms, so both guards would have read `""` forever. Config is threaded through `resolve_slack_config` / `/api/internal/slack-config` instead. `channels:history` was already in the manifest; only `reactions:read` is new.

**Outcome:** Marking my own message with a designated emoji (or posting in a note-to-self channel) files it as a `life_fact`. Only self-signals — never other people's messages.

**Implementation sketch**
- `comms/src/aegis_comms/adapters/slack.py`: add `@app.event("reaction_added")` beside the existing `@app.event("file_shared")` (`:493`), delegating to a new `SlackInbound.on_reaction`.
- Two hard filters before any work: `event["user"]` must equal the configured owner member id (`slack_owner_member_id` already exists in `CONFIG_REGISTRY`, `core/src/aegis/services/integrations_config.py:82`), and `event["item_user"]` must be the same person. Reaction must be in a configured set (default `{"brain"}`).
- Fetch the message body with `conversations.history(latest=ts, oldest=ts, inclusive=True, limit=1)` using the existing bolt client.
- Ingest via `SlackCoreClient.knowledge_ingest(source_type="life_fact", url=f"slack://{channel}/{ts}", tags=["life_fact","slack"])` — the `channel/ts` URL is the dedupe key (B1's content-id derivation).
- Note-to-self mode: a configured channel id where every owner message ingests without a reaction; shares the same ingest call.

**Wiring checklist:** two `ConfigKey` lines (`slack_saveit_emoji`, `slack_note_to_self_channel`) in `integrations_config.py` · matching fields in `comms/src/aegis_comms/config.py` · event handler + registration · Slack app scopes (`reactions:read`, `channels:history`).

**Acceptance criteria** (`tests/comms/test_slack_reaction_ingest.py`)
- Reaction from a non-owner user id → no core call at all.
- Reaction on someone else's message by the owner → no core call.
- Owner reacting with the configured emoji on their own message → exactly one `knowledge_ingest` with `source_type="life_fact"` and the `slack://…` url.
- Duplicate `reaction_added` for the same `ts` produces the same url (idempotent at the knowledge layer).

**Size:** S · **Depends on:** B1

### B3 — Voice-first capture with intent classifier

> **SHIPPED — PR #208 (`e627a70`).** This task **created** `capture_classify`, which A2/A3/A6 cite as an existing template — it was a forward reference. Deviations: `balanced` tier, not `fast`; `think()` records neither successes nor truncations, so both are recorded explicitly; no core `ConfigKey` for the voice secret (ConfigKeys do not reach comms) — it is a comms env var, unset ⇒ 503 fail-closed; raw body rather than multipart (`python-multipart` is not a comms dependency); low-confidence and unknown labels also fall back to `task`.

**Outcome:** A voice note (iOS Shortcut or Slack) becomes either a Todoist Inbox task or a `life_fact`, without me deciding which.

**Implementation sketch**
- Classifier lives in **core** (comms deliberately has no `aegis-core` dependency — see the `comms/src/aegis_comms/elevenlabs.py` docstring). Extend `core/src/aegis/api/routes/capture.py` with `kind="auto"`: call `LLMClient.think()` (`core/src/aegis/llm/__init__.py:246`) on the `fast` tier (`tier_to_model("fast")` → `gemma4:e2b` per `config/models.yaml`) with a strict two-label prompt and JSON-ish parse; anything unparseable falls back to `task` (a mis-filed task is recoverable, a lost one is not).
- Pass `db_pool` + `purpose="capture_classify"` to `think()` so failures land in `llm_calls` like every other classifier.
- Slack path: `comms/src/aegis_comms/slack_inbound.py:789` `_handle_audio_file` already downloads + transcribes via `elevenlabs.transcribe`; today it always calls `_route_and_dispatch`. Add a capture-intent prefix check ("capture", "remember", "note to self") that instead calls `SlackCoreClient.capture(kind="auto", text=transcript)` and echoes the resolved lane back to the channel.
- iOS Shortcut path: new `POST /api/ingest/voice` (multipart audio) on the comms FastAPI router that already hosts `/api/deliver/voice` (`comms/src/aegis_comms/__main__.py:296`); shared-secret header, transcribe, then the same `capture(kind="auto")` call. Returns the transcript + lane so the Shortcut can show a confirmation.

**Wiring checklist:** `kind="auto"` branch + classifier prompt · `ConfigKey("voice_ingest_secret", …)` in `integrations_config.py` + comms config field · inbound route in comms `__main__.py` · `_handle_audio_file` branch.

**Acceptance criteria**
- `tests/core/test_capture_route.py`: with a stubbed `LLMClient` returning `life_fact`, the row lands in `knowledge_content` with `source_type='life_fact'`; returning garbage or raising → task lane, and the request still returns 200.
- `tests/comms/test_voice_ingest.py`: missing/bad shared secret → 401; valid audio with `transcribe` stubbed → exactly one core `capture` call carrying the transcript. Comms tests may stop at the client boundary, but pair this with one core-side test (real Postgres) asserting that `capture(kind="auto")` with that transcript actually lands a `knowledge_content` or Todoist-outbox row — a mock-was-called assertion alone passes with the core route broken (B1's own criteria assert the real row; reuse that fixture).
- `tests/comms/test_slack_voice_capture.py`: transcript starting "remember …" hits `capture`, not `_route_and_dispatch`; a normal transcript still routes to chat (existing behaviour unchanged).

**Size:** M · **Depends on:** B1

### B4 — Generic signed life-data webhook

> **SHIPPED — PR #206 (`16b6384`), plus review fix #214 (`22894a0`: opaque 401 for isdigit-but-unparseable timestamps, release idempotency claims on failure).** Deviations: `X-Aegis-Timestamp` is **mandatory**, not optional (an optional one is not replay protection); the per-source secret override was dropped for v1 (pydantic `Settings` cannot resolve an undeclared dynamic field); unknown-slug 404 moved *after* verification so the endpoint cannot be enumerated; `LIFE_SOURCES` values are `str | None`, with `None` meaning "record inline", or the endpoint would have 404'd every request until B5.

**Outcome:** One authenticated push endpoint that phone/watch/home-automation clients can POST to, dispatching to a per-source Temporal flow. The substrate for B5 and B6.

**Implementation sketch**
- `core/src/aegis/api/routes/webhooks.py`: add `POST /api/webhooks/ingest/{source}`. Reuse the module's own `verify_hmac()` helper with `prefix=""` against an `X-Aegis-Signature` header; secret from a new `life_webhook_secret` field in `core/src/aegis/config.py` (beside `alert_webhook_secret:206`). Missing secret → 503, matching the github/sentry handlers.
- Module-level `LIFE_SOURCES: dict[str, str]` mapping source slug → workflow name; unknown slug → 404. Dispatch via the existing start-workflow-by-string-name pattern (`temporal.start_workflow("<Name>", payload, id=…, task_queue="aegis-main")`).
- Idempotency: claim `ingest_idempotency (source_type=f"life:{source}", external_id=…)` with the same `INSERT … ON CONFLICT DO NOTHING RETURNING` used by `github_webhook`; external id from a payload `id`/`_id` field, else `sha256(body)`.
- Body size cap (reject > ~1 MB) and return `202 {"accepted": true, "workflow_id": …}` immediately — never await flow completion.
- `log_audit` (`core/src/aegis/observability.py:100`) each accepted push so a chatty device is visible.
- **Security notes:** (1) one shared secret covers every life source — a leak compromises location AND health ingestion at once. Acceptable for v1 (one owner, few devices), but support an optional per-source override (`life_webhook_secret_<source>` config lookup falling back to the shared key) so a single leaked device credential can be rotated without touching the others. (2) `ingest_idempotency` dedupes *processing* but does not reject a captured-and-replayed signed request — accept an optional `X-Aegis-Timestamp` header folded into the HMAC input with a ±5 min window; clients that send it get replay protection, ones that don't still work. TLS in front is assumed and required.

**Wiring checklist:** route + `LIFE_SOURCES` · `Settings.life_webhook_secret` · `ConfigKey("life_webhook_secret", "Webhook secret", "Life data", True)` in `integrations_config.py` · docs note on computing the HMAC from an iOS Shortcut.

**Acceptance criteria** (`tests/core/test_life_webhook.py`, real Postgres, fake Temporal client)
- No secret configured → 503; bad signature → 401; unknown source slug → 404.
- Valid push → 202 and exactly one `start_workflow` call with the mapped workflow name and `task_queue="aegis-main"`.
- Replaying the identical body → `{"duplicate": true}`, zero additional `start_workflow` calls, one `ingest_idempotency` row.

**Size:** M · **Depends on:** —

### B5 — Location channel with place inference

> **SHIPPED — PR #209 (`3c96e47`), plus review fix #214.** Deviations: **no `LocationIngestFlow`** — a workflow argument is persisted verbatim in Temporal history, i.e. a second copy of exactly the coordinates this feature exists to discard, at ~290 executions/day for one haversine and one INSERT; ingest is inline. The suggested source-keyed retention entry is a silent no-op and was not added. No coordinates are stored at all — `metadata` is `{place, trigger}`. Migration slot `020` was not needed.

**Outcome:** OwnTracks / Home Assistant pushes resolve to a named place ('home', 'office', …), recorded as observations and surfaced in the daily briefing.

**Implementation sketch**
- Register `"location"` in `LIFE_SOURCES` → `LocationIngestFlow` (`worker/src/aegis_worker/flows/location_ingest.py`) with `worker/src/aegis_worker/activities/location.py`.
- Named places are DB-owned as `channels` rows: `kind="place"`, `identifier=<place name>`, `config={lat, lon, radius_m}`. Add `"place"` to `CHANNEL_KINDS` in `core/src/aegis/api/routes/channels.py` so the admin Channels page (`admin-panel/frontend/src/pages/Channels.tsx`) manages them for free; read them with the existing `ChannelActivities.list_active_channels("place")`.
- Inference is plain haversine in the activity against those radii — no PostGIS. First match wins by smallest radius; no match → `place="elsewhere"`.
- Write one `observations` row per push (`source='location'`, `metric='place'`, `metadata={place, lat, lon, accuracy, trigger}`); coarse coords only in metadata, and add a `location` retention entry to the `CleanupFlow` config in `config/seed/activities.yaml` so raw coordinates age out.
- Maintain a `current_place` key in `settings` (the same KV `gather_briefing_changes` uses for `briefing_state`, `worker/src/aegis_worker/activities/briefing.py:205`) and add an isolated place dimension to that gather, in its existing per-source try/except style.
- No proactive arrival/departure pings in v1. If added later they must go through `safe_send_message` (`worker/src/aegis_worker/activities/delivery.py:15`) so the notification budget applies.

**Wiring checklist:** flow + activities modules · `WORKFLOWS` list (`worker/src/aegis_worker/__main__.py:110`) and the worker-run list (`:670`) · activity instance + methods in the activities list (~`:560`) · `LIFE_SOURCES` entry · `CHANNEL_KINDS` · briefing gather. **No** `schedule_sync` / `activities.yaml` entry — this flow is push-triggered, so only 3 of the 5 registration points apply.

**Acceptance criteria** (`tests/worker/test_location_ingest.py` via `ActivityEnvironment`, real Postgres)
- A point inside a seeded `place` channel radius resolves to that place; a point 10 km away resolves to `elsewhere`; overlapping radii resolve to the tighter one.
- One `observations` row per accepted push with `source='location'`.
- Re-pushing the same payload does not double-write (idempotency claim from B4 holds).
- `tests/worker/test_briefing_changes.py` extension: a stale/absent `current_place` degrades the briefing to no place line, never an exception.

**Size:** M · **Depends on:** B4, C4

### B6 — Health push channel (Apple Health Auto Export)

> **SHIPPED — PR #210 (`bb5a267`), plus review fix #214 (keep health out of workflow history).** Deviations: **no `HealthIngestFlow`** — same Temporal-history argument as B5, stronger for body data; ingest is inline, boot delta zero. No source-keyed retention entry (silent no-op); malformed payload → 422 plus claim release, not a `{"status": …}` result; no new `ConfigKey`, `Settings` field or migration. Added beyond the sketch: timestamp plausibility bounds. Open follow-ups: #212, #215.

**Outcome:** Sleep, HRV, resting HR, steps and active energy land as observations from a Health Auto Export automation, ready for the briefing and for correlation work in Theme C.

**Implementation sketch**
- Register `"health"` in `LIFE_SOURCES` → `HealthIngestFlow` (`worker/src/aegis_worker/flows/health_ingest.py`) + `worker/src/aegis_worker/activities/health.py`.
- Health Auto Export posts `{data: {metrics: [{name, units, data: [{date, qty|Avg|…}]}]}}` — a fan-out of many samples per push. Normalize in the activity to a fixed metric vocabulary (`sleep_minutes`, `hrv_ms`, `resting_hr`, `steps`, `active_energy_kcal`); unknown metric names are counted and dropped, not stored, so the vocabulary stays curated.
- Batch-insert observations in one transaction; dedupe per sample via `ingest_idempotency` claim keyed `f"health:{metric}:{observed_at}"` (backfills and re-exports overlap heavily — this is the load-bearing bit).
- Runaway guard mirroring `activities/raindrop.py`'s `_MAX_PAGES`: cap samples processed per push and log the truncation.
- Briefing: one health line in `gather_briefing_changes`, same isolated-try style as B5.

**Wiring checklist:** flow + activities modules · both `__main__.py` lists (`:110`, `:670`) · activity registration (~`:560`) · `LIFE_SOURCES` entry · briefing gather. Push-triggered, so no `schedule_sync` / `activities.yaml` row.

**Acceptance criteria** (`tests/worker/test_health_ingest.py`, real Postgres)
- A realistic multi-metric export yields the expected `observations` rows with correct `metric`/`observed_at`/`value`; units are normalized (hours → minutes for sleep).
- Re-posting an overlapping export adds zero rows.
- An export containing an unrecognized metric name ingests the known ones and returns a non-zero `skipped` count instead of raising.
- A malformed payload (`metrics` not a list) returns a `status` result, never an unhandled exception in the flow.

**Size:** M · **Depends on:** B4, C4

### B7 — Wearables poll channel (Oura / Whoop-style API)

> **SHIPPED — PR #207 (`bba2529`), migration `021`.** Deviations: the wiring checklist is obsolete — D6 deleted all six lists an hour earlier, so registration is one `FlowSpec` plus a seed row; the frontend `Channels.tsx` keeps its own `CHANNEL_KINDS` copy that the checklist omits (the row would have been invisible in the admin panel); cron `50 */6`, not `45 */2` (the sketch's own justification argues against 12 polls a day); B6 had not landed, so there was no metric vocabulary to reuse; the `source_types.py` entry is inert (wrong column).

**Outcome:** A scheduled cursor-based poll for wearable vendors that offer an API but no webhook — the same data shape as B6, different transport.

**Implementation sketch**
- `channels` rows with `kind="wearable"`, `identifier=<vendor>` (e.g. `oura`), `config={last_cursor, agent_id}`. Add `"wearable"` to `CHANNEL_KINDS` in `core/src/aegis/api/routes/channels.py`; optional starter row in `config/seed/channels.yaml` with `active: false`.
- `worker/src/aegis_worker/activities/wearable.py` modeled on `worker/src/aegis_worker/activities/raindrop.py` (~150-line template): dataclass in/out, token from settings, `httpx` client injectable for tests, `_MAX_PAGES` runaway guard, returns records + `latest_cursor`.
- `worker/src/aegis_worker/flows/wearable_ingest.py` modeled on `worker/src/aegis_worker/flows/rss_ingest.py` (183 lines — budget for that, not a toy): `list_active_channels("wearable")` → poll → per-record `ingest_idempotency_claim` → write observations → `update_channel_config_key(kind, identifier, "last_cursor", …)`. Advance the cursor **only** past records with a definite outcome, exactly as `rss_ingest` does — this is the bug-avoidance the RSS flow already documents.
- Reuse B6's metric vocabulary and normalization helpers so both channels produce identical `observations` rows.

**Wiring checklist:** all 5 registration points — flow + activities modules; `WORKFLOWS` (`__main__.py:110`) and the worker-run list (`:670`); activity instance (~`:351`) and its methods (~`:560`); `_ACTIVITY_TYPE_MAP` in `worker/src/aegis_worker/schedule_sync.py:57`; seed row in `config/seed/activities.yaml` (follow the `rss-ingest-hourly` shape at `:123` but with its OWN cron — `schedule_cron: "45 */2 * * *"`; copying `30 * * * *` verbatim would stack it on the RSS run every hour, and wearable APIs update far less often than feeds). Plus `ConfigKey("oura_api_token", "API token", "Wearables", True)` in `integrations_config.py` and the matching `Settings` field.

**Acceptance criteria**
- `tests/worker/test_wearable_ingest.py` (real Postgres, stubbed `httpx`): first poll with no cursor ingests N records and writes `last_cursor`; second poll with the vendor returning the same records ingests 0 and leaves the cursor unchanged.
- A record whose observation write fails does **not** advance the cursor past it.
- Missing API token → the activity returns an empty result with a warning, no exception (matches `poll_bookmarks`'s `raindrop_token_missing` behaviour).
- `tests/worker/test_schedule_registration.py`-style check: `WearableIngestFlow` is present in both `__main__.py` lists and in `_ACTIVITY_TYPE_MAP`.

**Size:** M · **Depends on:** C4

### B8 — Real MCP client transport

> **SHIPPED — PR #211 (`1588f8f`), streamable-HTTP only.** Deviations: **stdio was deliberately not implemented** — it is config-driven local process execution and is *explicitly rejected* with a message rather than silently ignored, which also voids the sketch's stdio-fixture acceptance criteria (replaced with respx-mocked HTTP equivalents); `ConfigKey(…, boolean=True)` as written is a `TypeError`; `mcp>=1.2` was not added; an unknown transport is a per-entry rejection with an ERROR log, not a boot-time raise that would take Core down over one typo.

**Outcome:** `MCPManager` actually connects. `POST /api/mcp/{server}/{tool}` executes against a configured MCP server instead of returning 501, and servers can be listed/introspected.

**Implementation sketch**
- Add `mcp>=1.2` to `core/pyproject.toml` dependencies (it is not currently a dependency of any package).
- Rewrite `core/src/aegis/mcp_manager.py` (today: `call_tool` raises `NotImplementedError`, `close()` is a documented no-op). Support two transports — `stdio` (spawn `command`) and `streamable-http` (`url`) — with lazy per-server connect held in an `AsyncExitStack`, a `list_tools()` result cache, a per-call timeout, and a real `close()` that tears sessions down. `core/src/aegis/api/app.py` already constructs the manager at `:166` and awaits `close()` at `:207`, so no lifespan changes are needed.
- Validate `Settings.mcp_servers` (`core/src/aegis/config.py:209`) entry shape at construction — `{transport, command|url, env, enabled, timeout_s}` — and raise on an unknown transport so a typo fails at boot rather than at first tool call.
- Safety: stdio spawns local processes, so gate the whole subsystem behind a `ConfigKey("mcp_enabled", "MCP client (external tool servers)", "Features", boolean=True)` in `integrations_config.py`, default off; pass only the explicit per-server `env`, never the process environment or `Settings` secrets.
- `core/src/aegis/api/routes/mcp.py`: add `GET /api/mcp` (configured servers + health) and `GET /api/mcp/{server}/tools`. Keep the 404-unknown-server and auth behaviour byte-identical.

**Wiring checklist:** dependency · manager rewrite · config validation · feature flag ConfigKey · two new read routes · `docs/` note on configuring `mcp_servers`.

**Acceptance criteria** (`tests/core/test_mcp_endpoint.py` — replace the 501 test with a live-transport happy path, not a mock)
- A minimal in-repo stdio MCP server fixture (`tests/core/fixtures/mcp_echo_server.py`, spawned for real, no network) is reachable: `GET /api/mcp/echo/tools` lists its tools and `POST /api/mcp/echo/echo` returns the echoed payload with `{"ok": true}`.
- Unknown server → 404 and unauthenticated → 401/403 still hold (existing assertions preserved verbatim).
- A server whose command exits immediately → 500 with a bounded error message, and the failure does not wedge subsequent calls to a healthy server.
- `close()` terminates the spawned process — asserted by checking the child is gone after app shutdown.
- Feature flag off → 503 on all `/api/mcp` calls.

**Size:** L · **Depends on:** —

### B9 — MCP tool surface for agents

> **SHIPPED — PR #213 (`593c118`).** Deviations: `request.app.state.mcp_manager` is unreachable from an executor (signature is `(pool, args, ctx)`) — added `ToolContext.mcp_manager`, wired for both `send_message` and `synthesize_agent_reply`; `metadata.mcp_servers` is an **object**, not a list, and a list is now explicitly refused; truncation applied in the executor because no other executor truncates; the `channels`-row stretch bullet was not shipped, per the sketch's own instruction.

**Outcome:** Agents can call whitelisted MCP tools from chat, so an MCP server becomes both a tool provider and a life-data source.

**Implementation sketch**
- `CHAT_TOOLS` (`core/src/aegis/services/chat.py:301`) and `TOOL_EXECUTORS` (`:3315`) are static module-level structures, and `_validate_agent_tool_sets` (`:3503`) raises at boot on any tool name without an executor. So do **not** splice per-server tools into those dicts. Instead add **one** passthrough tool `call_mcp_tool(server, tool, args)` to both registries — a single entry that keeps boot validation and `_get_agent_tools` (`:3489`) working untouched.
- Per-agent authorization: the tool is opt-in via `agents.metadata.tool_set` (admin Behavior tab), and the executor additionally checks `agents.metadata.mcp_servers` so an agent granted the tool still cannot reach every configured server. Empty/absent list → deny.
- Executor calls `request.app.state.mcp_manager.call_tool(...)`, truncates the result to the same size ceiling other executors use, and writes a `log_audit` row (`core/src/aegis/observability.py:100`) per call — MCP tools reach outside AEGIS, so every call needs a trail.
- Tool description injected into the prompt is built from the live `list_tools()` cache for the agent's allowed servers, so the model sees real tool names without registry mutation.
- Life-data source (stretch, one bullet): a `channels` row `kind="mcp"` naming a read-only tool to poll on a schedule, feeding `ContentActivities.process_content` (`worker/src/aegis_worker/activities/content.py:325`) or observations. Ship only after B8 has run in production for a while.

**Wiring checklist:** `CHAT_TOOLS` entry + `TOOL_EXECUTORS` entry · `AGENT_TOOL_SETS` opt-in for one agent (not `_FALLBACK_TOOL_SET`) · admin Behavior-tab docs for `metadata.mcp_servers` · audit logging.

**Acceptance criteria** (`tests/core/test_chat_tool_mcp.py`, real Postgres)
- An agent without `call_mcp_tool` in its tool set never sees it in `_get_agent_tools` output.
- An agent with the tool but an empty `metadata.mcp_servers` gets a denial result string, and no `MCPManager.call_tool` invocation happens.
- An allowed agent + allowed server returns the tool result and writes exactly one `audit_log` row naming the server and tool.
- Boot-time `_validate_agent_tool_sets()` still passes (guards against the orphan-tool `RuntimeError`).

**Size:** M · **Depends on:** B8

---

## Theme C — Life entities & structured data

**Schema strategy (applies to C1–C7):** follow the existing per-domain schema precedent (`finance`, `pandoras_actor`, created by `migrations/001_baseline.sql` and renamed/extended in `migrations/010_rename_maou_schema.sql` / `008_infra_cloud.sql`). Introduce a new `life` schema for the tables this theme adds (`life.people`, `life.observations`, `life.expiring_items`, `life.assets`). Keep them out of `public` so `\dt` / backups / retention sweeps stay organized by domain the same way finance and homelab data already are. `core/src/aegis/db/pool.py::run_migrations` runs plain numbered SQL with no schema-awareness required — a `CREATE SCHEMA IF NOT EXISTS life;` at the top of the first migration that touches it is sufficient. Household/asset data (C7) and life-document expiry (C5/C6) both live in `life` rather than extending `resources` or `infra`, because those two tables carry infra-specific columns (`ssh_user`, `docker_context`, encrypted `credentials`) that don't fit a car or a passport; manuals/receipts stay in `knowledge_content`/`resources` and are cross-referenced by tag/slug instead of by foreign key, consistent with how `resources.infra_id` is the only existing cross-table link of this kind.

> **STALE — every migration number in this theme is wrong**, and `migrations/` is
> now at `021`, not `014`. Actual slots: C1 `016_life_people`, C4
> `017_life_observations`, C5 `018_life_expiring_items`, C7 `019_life_assets`
> (A4 took `020`, B7 `021`). The paragraph's own advice — "verify the highest
> existing number again immediately before creating each migration file" — is the
> one instruction here that held, and D5's CI guard now enforces it.

Migration numbers below assume the tasks ship in the listed order starting from the next free slot; `migrations/` is up to `014_delete_vercel_project_sync.sql` (note two `006_*.sql` files already exist — `006_infra_coding.sql` and `006_social_metrics.sql` — so that collision is historical, not a slot to reuse). **Verify the highest existing number again immediately before creating each migration file**, since other themes may land first.

### C1 — `people` schema + service + admin CRUD

> **SHIPPED — PR #189 (`86c740d`), migration `016` not `015`.** The most accurate Theme C section — every structural claim held. Deviations: aliases are normalized to lowercase on write, because a plain GIN index over a mixed-case `text[]` cannot serve the case-insensitive probe C2 needs (it would have been a dead index); added `find_people` + a `?q=` list param, which the section's own acceptance criteria require.

**Outcome:** a queryable `life.people` table with basic CRUD, usable standalone (manual entry) before any enrichment exists.

**Implementation sketch:**
- New migration `migrations/015_life_people.sql`: `CREATE SCHEMA IF NOT EXISTS life;` then `life.people(id uuid pk, name text not null, aliases text[] default '{}', relationship text, key_dates jsonb default '{}', notes text, last_contact timestamptz, metadata jsonb default '{}', created_at, updated_at)`. Index on `lower(name)` and a GIN index on `aliases` for lookup-by-alias.
- New service module `core/src/aegis/services/people.py`, modeled on `core/src/aegis/services/infra.py`'s shape (`_SELECT_COLS`, `_EDITABLE_FIELDS`, `list_/get_/create_/update_/delete_infra`) — plain dicts in/out over an `asyncpg.Pool`, no ORM.
- New admin routes `core/src/aegis/api/routes/people_admin.py` mirroring `core/src/aegis/api/routes/resources.py` (list/get/create/update/delete under `/api/admin/people`, `Depends(verify_auth)`).
- New admin page `admin-panel/frontend/src/pages/People.tsx` + client methods in `admin-panel/frontend/src/api/client.ts` (mirror the `listInfra`/`getInfra` pair at lines 192–193).

**Wiring checklist:**
- `app.include_router(people_admin.router)` in `core/src/aegis/api/app.py` next to the existing `infra`/`infra_admin`/`resources` includes (~line 295–298).
- Route added to the frontend router/nav alongside the other `pages/*.tsx` entries.
- No chat tool yet (deferred to C3) and no Temporal registration needed — this task is pure CRUD.

**Acceptance criteria:**
- `tests/core/services/test_people.py` (new, real Postgres via the `db_pool` fixture in `tests/core/conftest.py`): create/list/get/update/delete round-trip; alias search returns a person by alias, not just exact name; deleting a nonexistent id is a no-op, not an error.
- `tests/core/test_people_admin_route.py` (new): CRUD through the FastAPI routes, auth-gated (401 without `verify_auth`).
- Migration applies cleanly against a fresh DB and is idempotent-safe on re-run per the `schema_migrations` tracking in `pool.py`.

**Size:** M
**Depends on:** none

### C2 — Passive people enrichment from email/calendar co-occurrence

> **SHIPPED — PR #202 (`c78b2fa`).** Deviations: **both named call sites were wrong** — `calendar_event_to_content` is a pure converter with no pool, and `ingest_email_to_kg` runs for only a fraction of mail; enrichment is a new `PeopleActivities` called from the flow and from `_route`. Calendar deliberately does **not** set `last_contact` (`fetch_events` is forward-looking, so it would answer "when did I last talk to X?" with a meeting that has not happened) and cannot supply display names. The kill switch is a `CONFIG_REGISTRY` boolean, not a settings row + migration.

**Outcome:** `life.people` rows get created/updated automatically from who the user emails and meets, without manual entry, laying the groundwork for "last time I talked to X" and birthday radar.

**Implementation sketch:**
- Calendar side: `core/src/aegis/services/claims.py::calendar_event_to_content` currently only flattens `attendees` into the `raw_text` string for knowledge ingestion (lines ~30–39) — it does not touch `life.people` today, so this is new logic, not a refactor. Add a new helper, e.g. `core/src/aegis/services/people.py::upsert_from_attendees(pool, attendees, event_time)`, called from `worker/src/aegis_worker/activities/calendar.py::events_to_content` (or a new sibling activity) after the existing `calendar_event_to_content` call.
- Email side: `worker/src/aegis_worker/activities/gmail.py::ingest_email_to_kg` (~line 674) is the natural call site — add a co-occurrence upsert keyed on sender/recipient email, using `_normalize_sender` (line 210) for consistent matching.
- Matching strategy: match on email address stored in `aliases` (case-insensitive); if no existing row matches, create one with `name` derived from the display name and `aliases = [email]`; on every match, bump `last_contact`. Keep this conservative — no fuzzy name matching in this pass (avoid merging distinct people).
- Feature-flag via a `settings` row (e.g. `people_enrichment_enabled`, default off) so it can ship dark and be turned on after review, same kill-switch convention as `todoist_capture_enabled` (see `migrations/005_social.sql` comment for the pattern).

**Wiring checklist:**
- No new Temporal registration points — enrichment hangs off existing `GmailIngestFlow`/`CalendarIngestFlow` activities, not a new flow.
- Add the `people_enrichment_enabled` row via a migration `INSERT INTO settings (key, value) VALUES (...)` guarded by `ON CONFLICT DO NOTHING`.

**Acceptance criteria:**
- `tests/worker/activities/test_gmail_people_enrichment.py` and `test_calendar_people_enrichment.py` (new, real Postgres): first contact creates a row with correct alias; repeat contact updates `last_contact` and does not duplicate; enrichment is a no-op when the setting is off.
- Existing tests for `calendar_event_to_content` continue to pass unchanged — the raw_text flattening behavior is not touched, only supplemented.

**Size:** M
**Depends on:** C1

### C3 — People downstream: birthday/anniversary radar + "last time I talked to X" chat tool

> **SHIPPED — PR #193 (`6f35c43`).** Deviations: `format_weekly_preview` is only a **fallback** — in production `frame_review`'s LLM narrative replaces it, so key dates are appended in the flow *after* `frame_review` and survive both paths; the "no new flow registration" checklist omitted the new `ReviewActivities` method, which would have failed at runtime, so a guard test now asserts every `@activity.defn` on that class is registered; `_spawn_review_interaction` belongs to `DailyReviewFlow`, not `WeeklyReviewFlow`.

**Outcome:** `key_dates` on a person surface proactively, and any chat agent can answer contact-recency questions.

**Implementation sketch:**
- Birthday/anniversary radar: extend `worker/src/aegis_worker/flows/review.py::WeeklyReviewFlow` (config at line 34 `WeeklyReviewConfig`, flow body from line 182). Add a new `ReviewActivities` method, e.g. `check_upcoming_key_dates`, that queries `life.people.key_dates` for entries in the next N days and folds them into the existing weekly digest/preview path (`_spawn_review_interaction` at line 38). Prefer extending the existing flow over adding a new one — it already runs weekly and already has a delivery path.
- Chat tool: add a schema entry to `CHAT_TOOLS` (`core/src/aegis/services/chat.py`, list starts line 301) named e.g. `last_contact_with_person`, and an executor in `TOOL_EXECUTORS` (dict starts line 3315) that queries `life.people` by name/alias and returns `last_contact` plus `relationship`/`notes`.
- Grant the tool to relevant agents via `agents.metadata.tool_set` (per-agent, admin Behavior tab) — do not add it to `_FALLBACK_TOOL_SET` (line ~3484); it's a narrow, opt-in capability. Add it to `AGENT_TOOL_SETS["sebas"]` at minimum.

**Wiring checklist:**
- No new flow registration — reuses `WeeklyReviewFlow`, already in both `WORKFLOWS` (line ~111) and the runtime `workflows` list (line ~668) in `worker/src/aegis_worker/__main__.py`.
- `_validate_agent_tool_sets()` (chat.py line ~3503) will fail startup if the new tool name is referenced in `AGENT_TOOL_SETS` without a matching `TOOL_EXECUTORS` entry — add both in the same change.

**Acceptance criteria:**
- `tests/worker/test_review_flows.py` (existing file — extend it): weekly review includes an upcoming-birthday entry when a person's `key_dates` falls inside the lead window, and omits it otherwise.
- `tests/core/test_chat_tool_last_contact.py` (new, mirrors `tests/core/test_chat_tool_list_interactions.py`'s structure): tool returns the right person on exact name, on alias, and a clear "not found" on no match; tool absent from an agent's tool list when not granted.

**Size:** S
**Depends on:** C1 (dates can be entered manually even without C2); C2 improves `last_contact` accuracy but is not required.

### C4 — `observations` table + query service + chat tool

> **SHIPPED — PR #195 (`febfdbd`), migration `017` not `016`.** Deviation (load-bearing): "no code change is needed there beyond the new dict key" is **wrong** — `_ALLOWED_TABLES` is derived from `_TIMESTAMP_COLUMNS`, so a `_DEFAULT_RETENTIONS` entry alone would have shipped a retention that never deleted a row; both maps were edited. Also: the read-side `value::float8` cast is load-bearing (asyncpg returns `Decimal`), the write-side one was not and was removed after break-and-revert; tool param is `window_days`.

**Outcome:** a generic life-metrics store (weight, sleep, location pings, home-sensor readings, etc.) that other themes (Theme B location/health channels) can write into immediately.

**Implementation sketch:**
- New migration `migrations/016_life_observations.sql`: `CREATE TABLE life.observations(id uuid pk, source text not null, metric text not null, value numeric, observed_at timestamptz not null, metadata jsonb default '{}', created_at timestamptz default now())`. Indexes: `(metric, observed_at)` for trend queries, `(source, observed_at)` for per-source sweeps.
- New service `core/src/aegis/services/observations.py`: `record_observation(pool, source, metric, value, observed_at, metadata)`, `query_trend(pool, metric, since, until)` (returns ordered series), `summarize(pool, metric, window_days)` (min/max/avg/latest — plain SQL aggregates, no pandas dependency, matching this codebase's asyncpg-first style).
- Chat tool: `CHAT_TOOLS`/`TOOL_EXECUTORS` entry `query_observations` (params: metric, window) in `core/src/aegis/services/chat.py`, returning the `summarize()` output plus a short natural-language trend line (up/down/flat vs previous window).
- Retention: add `"life.observations": 365` (or similar) to `_DEFAULT_RETENTIONS` in `worker/src/aegis_worker/flows/cleanup.py` (dict starts line 14) — same pattern as the existing `pandoras_actor.*` entries (lines 36–40). Note `prune_old_records` (`worker/src/aegis_worker/activities/cleanup.py`, line 227) takes the retention dict as config, so no code change is needed there beyond the new dict key.

**Wiring checklist:**
- No new Temporal flow/activities registration required — `record_observation` is called directly from whichever ingest path produces the metric (Theme B's job, out of scope here); this task only needs the table, service, chat tool, and retention entry to exist first.
- Retention key uses the `life.observations` (schema-qualified) form, matching how `pandoras_actor.*` keys are written, not a new normalization scheme.

**Acceptance criteria:**
- `tests/core/services/test_observations.py` (new, real Postgres): insert + trend query returns correctly ordered series; `summarize` matches hand-computed min/max/avg on a fixture series.
- `tests/worker/test_cleanup_activity.py` (existing — extend): `life.observations` rows older than the configured retention are pruned, newer ones survive.
- `tests/core/test_chat_tool_query_observations.py` (new): tool returns a summary for a metric with data, and a graceful "no data" response for one without.

**Size:** M
**Depends on:** none (independent of the people tasks; Theme B depends on this, not the reverse)

### C5 — Expiry radar: `life.expiring_items` registry + admin CRUD

> **SHIPPED — PR #197 (`a16c249`), migration `018` not `017`.** The section's core call — start with the `finance.renewal_alert` existence-check dedup shape rather than `cert_expiry`'s sticky column — was adopted as written. The wiring checklist's omission of the SPA wiring (`App.tsx` / `Layout.tsx` / `client.ts`) was noted by C7 (#203). Open follow-up: #200.

**Outcome:** a place to register anything with an expiry/renewal date (passport, visa, licence, insurance policy, warranty, medication refill, domain), ready for the daily flow in C6 to consume. Ships independently as manually-browsable data even before the flow exists.

**Implementation sketch:**
- New migration `migrations/017_life_expiring_items.sql`: `life.expiring_items(id uuid pk, kind text not null, title text not null, expires_on date not null, lead_days integer[] not null default '{30,7,1}', person_id uuid references life.people(id), asset_id uuid, notes text, metadata jsonb default '{}', created_at, updated_at)`. `kind` is an open string (`passport`, `visa`, `licence`, `insurance`, `warranty`, `medication`, `domain`, ...) — no enum, matching how `resources.kind` and `infra.kind` are plain text with convention, not a DB constraint.
- Alert dedup table in the same migration: `life.expiring_item_alerts(id uuid pk, item_id uuid references life.expiring_items(id), threshold_days integer not null, expires_on date not null, fired_at timestamptz default now())` with a unique index on `(item_id, threshold_days, expires_on)`. **Use this dedup shape, not `pandoras_actor.cert_expiry`'s single sticky `last_alert_threshold` column** — `migrations/013_renewal_alert_next_due_at.sql` documents exactly the bug that pattern caused (166 duplicate rows in 3 weeks) and fixed it in `finance.renewal_alert` with this existence-check-across-all-time approach; start C5 with the already-fixed pattern instead of reintroducing the older bug.
- Service `core/src/aegis/services/expiring_items.py`: CRUD + `due_within(pool, days)` query.
- Admin routes `core/src/aegis/api/routes/expiring_items_admin.py` (mirror `resources.py`) + admin page `admin-panel/frontend/src/pages/ExpiringItems.tsx` + client methods in `client.ts`.

**Wiring checklist:**
- `app.include_router(...)` in `core/src/aegis/api/app.py`.
- No Temporal registration in this task — the daily check is C6.

**Acceptance criteria:**
- `tests/core/services/test_expiring_items.py` (new, real Postgres): CRUD; `due_within(30)` returns items expiring inside the window and excludes ones outside it, across a multi-item fixture.
- `tests/core/test_expiring_items_admin_route.py` (new): CRUD via routes, auth-gated.
- Migration applies cleanly; unique index on `(item_id, threshold_days, expires_on)` rejects a duplicate insert in a direct SQL test.

**Size:** M
**Depends on:** C1 (optional `person_id` FK — nullable, so C5 does not hard-depend on C1 shipping first; omit the FK constraint if C1 hasn't landed yet and add it in a later migration)

### C6 — Expiry radar: daily flow + interaction cards

> **SHIPPED — PR #199 (`eb2170e`).** Deviations: the five-point checklist below has **no activity-registration point**, so following it verbatim ships a flow that boots and then dies at runtime — six points were needed; three activities collapsed into one atomic `claim_due_alerts` (splitting find from record reintroduces the read-then-write race the ledger exists to remove); "insert the alert row only after a successful `safe_send_message`" is impossible on a card path, so the sketch's own insert-first alternative was taken; cron `25 7`, not `15 7` (the `*/15` sweep minute); card kind `ack`, because `input` renders with no button unless `aegis_ui_url` is threaded.

**Outcome:** the registry from C5 actually produces proactive warnings, generalizing the `CertRadarFlow` pattern (`worker/src/aegis_worker/flows/cert_radar.py`) to life documents.

**Implementation sketch:**
- New flow `worker/src/aegis_worker/flows/expiry_radar.py` (`ExpiryRadarFlow`), modeled directly on `CertRadarFlow`: loop over due items via `ExpiringItemsActivities.due_within`, and for each crossed `lead_days` threshold not already in `life.expiring_item_alerts`, raise a card.
- New activities `worker/src/aegis_worker/activities/expiring_items.py` (`ExpiringItemsActivities`): `find_due_items`, `record_alert_and_get_card_payload`, `notify_expiry`. Use `safe_send_message` (`worker/src/aegis_worker/activities/delivery.py`, function starts line 15) for the notification — insert the alert row only after a successful `safe_send_message` call (or insert first and treat as "attempted", accepting the same at-most-once tradeoff `safe_send_message`'s docstring already describes for drift/cert/backup/renewal paths).
- Card format: reuse `worker/src/aegis_worker/flows/interaction.py`'s `InteractionFlow` for anything needing a user response (e.g. "renewed?" acknowledgement); a simple FYI can go straight through `safe_send_message` like cert alerts do. Note the asymmetry: `safe_send_message` respects the notification budget, but interaction cards bypass it (`send_interaction_card` posts straight to `/api/deliver/card`) — call `record_notification(pool, agent_id, "expiry_card", sent)` on the card path too, so expiry acks count against the same daily budget as everything else (A2/A7 set the pattern).

**Wiring checklist (the 5 registration points for a new scheduled flow):**
1. Flow + activities class created (files above).
2. Added to the module-level `WORKFLOWS` list in `worker/src/aegis_worker/__main__.py` (~line 111).
3. Added to the runtime `workflows` list inside the same file (~line 668) — default to always-on since it's not homelab-specific.
4. `_ACTIVITY_TYPE_MAP` entry in `worker/src/aegis_worker/schedule_sync.py` (dict starts line 57), mapping `"ExpiryRadarFlow"` to `(ExpiryRadarFlow, ExpiryRadarConfig(...))`.
5. Seed row in `config/seed/activities.yaml` (follow the `cert-radar-daily` entry at line ~245 as a template, but with its OWN cron): `slug: expiry-radar-daily`, `workflow_type: ExpiryRadarFlow`, `agent_id: sebas`, `schedule_cron: "15 7 * * *"` — `0 7` is already held by two existing rows (incl. cert-radar) and `30 7` by a third; copying the template's cron verbatim would stack a fourth flow on the same minute.

**Acceptance criteria:**
- `tests/worker/flows/test_expiry_radar.py` (new, workflow test harness matching existing flow tests e.g. `tests/worker/test_review_flows.py`): item crossing a threshold produces exactly one alert; a second run on the same day does not re-fire (dedup via `life.expiring_item_alerts`); an item with no crossed threshold produces none.
- `tests/worker/activities/test_expiring_items_activities.py` (new, real Postgres): `find_due_items` correctness; alert insert is unique-constrained as expected.
- Schedule-sync registration test extended to cover the new `_ACTIVITY_TYPE_MAP` entry.

**Size:** L
**Depends on:** C5

### C7 — Household/asset registry

> **SHIPPED — PR #203 (`4725e7a`), migration `019` not `018`.** Deviations: acceptance criterion 3 is **not implementable** — nothing in the codebase reads `public.resources` by tag, so an `asset:<slug>` tag is storable but not queryable; the limitation is documented in the `019` header instead, and wiring a tag-filtered read is a separate opt-in change. The sketch gives no upsert key and `life.expiring_items` has no unique constraint to support `ON CONFLICT`, so the service-due feed is UPDATE-then-INSERT. The wiring checklist omits the SPA wiring (same gap as C5). Open follow-up: #200.

**Outcome:** cars, appliances, and home systems get a structured home, generalizing the `infra` table pattern (`core/src/aegis/services/infra.py`, admin page `admin-panel/frontend/src/pages/Infra.tsx`) without inheriting infra's SSH/credentials machinery that doesn't apply to a washing machine.

**Implementation sketch:**
- New migration `migrations/018_life_assets.sql`: `life.assets(id uuid pk, slug text unique, name text not null, kind text not null, purchase_date date, warranty_until date, service_interval_days integer, last_serviced_at date, location text, metadata jsonb default '{}', created_at, updated_at)`. No encrypted-credentials column — that was infra-specific; if a future asset needs a secret (e.g. a smart-lock PIN), add it then rather than speculatively.
- Service `core/src/aegis/services/assets.py`, CRUD only (no provisioning/SSH equivalent — this is data, not an actuation target).
- Admin routes `core/src/aegis/api/routes/assets_admin.py` + page `admin-panel/frontend/src/pages/Assets.tsx`, structurally mirroring `Infra.tsx`/`infra_admin.py` minus the provisioning/status panels.
- Manuals/receipts: store in the existing `knowledge_content`/`resources` tables tagged `asset:<slug>` (convention, not a new FK) — searchable via the existing `search_knowledge` chat tool and `references.py` route without new plumbing.
- Service-due feed: on create/update, if `service_interval_days` and `last_serviced_at` are set, upsert a corresponding `life.expiring_items` row (`kind='asset_service'`, `asset_id` set) so C6's daily flow picks it up automatically — this is the one place C7 depends on C5's table shape.

**Wiring checklist:**
- `app.include_router(...)` in `core/src/aegis/api/app.py`.
- No Temporal registration — reuses `ExpiryRadarFlow` via the `life.expiring_items` upsert, not a new flow.

**Acceptance criteria:**
- `tests/core/services/test_assets.py` (new, real Postgres): CRUD; updating `last_serviced_at`/`service_interval_days` upserts the correct `life.expiring_items` row with the right `expires_on` (`last_serviced_at + service_interval_days`).
- `tests/core/test_assets_admin_route.py` (new): CRUD via routes, auth-gated.
- Tagging an existing `resources` row with `asset:<slug>` and searching for it returns the row via the existing knowledge search path (regression check that no new coupling was introduced).

**Size:** M
**Depends on:** C5 (for the `life.expiring_items` upsert — the CRUD half could ship without it, but do it as one task since the service-due feed is the point of the feature)

### C8 — `source_type` registry module

> **SHIPPED — PR #177 (`a2cc33c`).** Implemented as sketched; no deviations reported. Note the resulting registry governs `knowledge_content.source_type` only — B7 later found that registering a value there does nothing for `life.observations.source`, a different column (#207).

**Outcome:** the knowledge store's `source_type` stops being an implicit, scattered string vocabulary and becomes a single declared list with per-type decay windows, making it safe to add life-domain source types (`people`, `observation`, `expiring_item`) without another silent-drift risk.

**Implementation sketch:**
- Verified current call sites (confirmed by grep): `core/src/aegis/services/claims.py` (`"calendar"`), `core/src/aegis/services/drive.py` (`"drive"` default), `core/src/aegis/services/chat.py` (`"chat"`, `"research"`, `"runbook"`, `"reference"`, plus the `DECAY_WINDOWS` dict at line 3627: `chat`, `task_outcome`, `triage`, `content`, `manual`), `core/src/aegis/services/knowledge.py` (generic parameter, no literal), `core/src/aegis/api/routes/knowledge.py` (`"content"`, `"upload"`, `"drive"` defaults), `worker/src/aegis_worker/activities/content.py` (`"content"`, `"media"`, content-type-derived), `worker/src/aegis_worker/activities/gmail.py` (`"email"`), `worker/src/aegis_worker/activities/briefing.py` (`"intelligence"`, `"briefing"`), `worker/src/aegis_worker/activities/intelligence.py` (`"intelligence"`), `worker/src/aegis_worker/activities/clarify.py` (`"reference"`), `worker/src/aegis_worker/activities/alerts.py` (`"alert"`, `"alert_investigation"`), `worker/src/aegis_worker/flows/raindrop_ingest.py` (`"reference"`), `worker/src/aegis_worker/schedule_sync.py` (`"drive"` default) — roughly 15 distinct literal values across 13 files.
- New module `core/src/aegis/services/source_types.py`: a frozen registry, e.g. `SOURCE_TYPES: dict[str, SourceTypeInfo]` where `SourceTypeInfo` carries `description` and optional `decay_days`. Seed it with every value found above, migrating `DECAY_WINDOWS`/`DEFAULT_DECAY_WINDOW` (chat.py line 3627–3634) to read from this module instead of maintaining a separate dict — `_apply_knowledge_decay` (line 3637) becomes `SOURCE_TYPES.get(source_type, DEFAULT).decay_days`.
- Add `warn_if_unknown(source_type)` helper, called from `core/src/aegis/services/knowledge.py`'s ingest path (around line 98–181) — logs a `structlog` warning, does **not** reject the write (existing call sites pass free-form strings from user-facing forms like `knowledge.py`'s upload endpoint at line 92, `source_type: str = Form("upload")`, and rejecting would break that).
- Register the new life-domain types up front: `people`, `observation`, `expiring_item`, `asset`, `life_fact`, `daylog`, `daylog_rollup` — so Themes A/B/C features can cite them when they feed the knowledge store.

**Wiring checklist:**
- Migrate `chat.py`'s `DECAY_WINDOWS` usage to `source_types.py` in the same change (don't leave two competing registries).
- No migration/DB change — this is a pure-Python constants module.
- No flow/activity registration needed.

**Acceptance criteria:**
- `tests/core/services/test_source_types.py` (new): every literal enumerated above is present in the registry; `warn_if_unknown` logs (capture via `structlog`'s test capture or caplog) for a made-up type and is silent for a known one.
- A decay regression test confirms behavior is unchanged after the `DECAY_WINDOWS` → `source_types.py` migration.
- Optional nice-to-have: a test asserting no bare `source_type="..."` literal appears outside `source_types.py`.

**Size:** S
**Depends on:** none — safe to do first or last; ideally lands before A8/B1/C1–C7 start writing new type names to the knowledge store.

---

## Theme D — Platform fixes & hygiene

### D1 — Fix stale tool-capability fallback in chat.py (quick win)

> **SHIPPED — PR #173 (`04781c3`).** Implemented as sketched (post-correction); no further deviations reported.

**Outcome:** The tool-calling substitution's fallback model is routed via the live-configured `balanced` tier instead of a frozen, now-dead model name.

**Correction (2026-08-01, PR #173):** an earlier draft of this section wrongly claimed `_TOOL_INCAPABLE_MODELS` was stale and should become a `claude-` prefix check. Verified against the live LiteLLM config (`homelab-gitops/ansible/roles/ollama/templates/litellm-config.yaml.j2`): `claude-haiku`/`claude-sonnet`/`claude-opus` are bare max-proxy bridge aliases that strip the `tools` array, while `claude-sonnet-5` / `claude-haiku-4.5` are separate, real-Anthropic-API LiteLLM entries with `model_info.supports_function_calling: true` — fully tool-capable. `smart` resolving to `claude-sonnet-5` and NOT matching `_TOOL_INCAPABLE_MODELS` is correct, intentional behavior, not a bug — a prefix check would silently downgrade every tool-bearing smart-tier chat turn to `balanced` for no reason. The exact-match set is right as-is. The genuine defect below (the hardcoded fallback literal) stands.

**Implementation sketch:**
- `core/src/aegis/services/chat.py:187` — `_TOOL_INCAPABLE_MODELS = frozenset({"claude-haiku", "claude-sonnet", "claude-opus"})` is matched by exact string equality at `chat.py:4105` (`model in _TOOL_INCAPABLE_MODELS`). This is correct as an exact-match set — keep it unchanged; do not prefix-match it (see correction above).
- `chat.py:188` — `_TOOL_FALLBACK_MODEL = "gpt-oss:20b"` points at a model `config/models.yaml` documents as "down indefinitely" (its comment says `balanced` was moved to `"kimi-k2.5"` for exactly this reason). Confirmed live in the repo, not a stale claim — this is the actual defect.
- Fix: replace the hardcoded fallback literal with a lookup against the live `balanced` tier via `tier_to_model("balanced")` (`core/src/aegis/llm/tier.py`) instead of a string constant, so the fallback always tracks whatever `config/models.yaml` currently designates. Degrade safely (leave the model unchanged) if `balanced` isn't resolvable.
- Update the comment block (`chat.py:176-190`) to state the bridge-vs-real-API distinction explicitly (citing the litellm config path), so this doesn't get "fixed" into a prefix check again.

**Acceptance criteria:**
- New test in `tests/core/` asserts that a chat request with a bare bridge-alias model (e.g. `"claude-sonnet"`) and a non-empty tool list gets substituted to the `balanced` tier's model, resolved dynamically — not a literal `"gpt-oss:20b"`.
- Test asserts a chat request with `model = tier_to_model("smart")` (currently `"claude-sonnet-5"`) and a non-empty tool list is left **unchanged** (regression guard against prefix-matching this again).
- Existing `_exec_*` / chat pipeline tests continue to pass; `ruff` (line-length 100) clean.

**Size:** S
**Depends on:** none

### D2 — Declare the `cryptography` dependency (quick win)

> **SHIPPED — PR #173 (`04781c3`), together with D1.** Implemented as sketched; no deviations reported.

**Outcome:** `core`'s dependency manifest accurately reflects what its code imports, so `cryptography` isn't silently relying on another package's transitive pin.

**Implementation sketch:**
- `core/src/aegis/crypto.py:14` imports `from cryptography.fernet import Fernet` (used by `encrypt_secret`/`decrypt_secret`).
- `core/pyproject.toml:12-34` `dependencies = [...]` has no `cryptography` entry — confirmed absent.
- Add `"cryptography>=42.0.0"` (or the currently-resolved transitive version, pinned as a floor) to `core/pyproject.toml`'s `dependencies` list.

**Acceptance criteria:**
- `grep cryptography core/pyproject.toml` finds an explicit entry.
- `pip install -e core` (or equivalent lock/sync step used in CI) succeeds with `cryptography` resolved directly rather than only via another package's transitive requirement.
- Existing crypto tests (e.g. wherever `encrypt_secret`/`decrypt_secret` are exercised under `tests/core/`) still pass unchanged.

**Size:** S
**Depends on:** none

### D3 — Fix CORS misconfiguration in app.py

> **SHIPPED — PR #176 (`c73baca`).** Implemented as sketched; no deviations reported.

**Outcome:** CORS policy is a real, enforceable allowlist instead of a combination browsers reject outright (and that is meaningless for a same-origin-serving SPA + API).

**Implementation sketch:**
- `core/src/aegis/api/app.py:254-259` — confirmed: `CORSMiddleware` is added with `allow_origins=["*"]`, `allow_credentials=True`, `allow_methods=["*"]`, `allow_headers=["*"]`. Per the CORS spec, browsers refuse `Access-Control-Allow-Origin: *` together with `Access-Control-Allow-Credentials: true`, so this configuration is inert for credentialed cross-origin calls today (dead/misleading config) and a wildcard-methods/headers policy is unnecessarily permissive if it ever does take effect (e.g. behind a proxy that rewrites the origin header).
- Add a `cors_allowed_origins: list[str] = []` setting (`core/src/aegis/config.py`), defaulting to empty (no cross-origin allowed) for the self-hosted single-origin deployment, overridable via `AEGIS_CORS_ALLOWED_ORIGINS` (comma-separated).
- Pass that explicit list to `allow_origins=`; keep `allow_credentials=True` only when the list is non-empty; scope `allow_methods`/`allow_headers` to what the admin panel actually needs rather than `["*"]`.

**Acceptance criteria:**
- New test in `tests/core/` builds the app and asserts `allow_origins` is never `["*"]` when `allow_credentials=True` (i.e. asserts the invariant, not just a snapshot value).
- Test covers both the empty-origin-list default and a configured origin list.
- No regression in existing app-startup tests (`tests/core/` app/lifespan tests).

**Size:** S
**Depends on:** none

### D4 — SPA catch-all must 404 unknown `/api/*` paths, not return `200 null`

> **SHIPPED — PR #174 (`8dd122c`).** Implemented as sketched; no deviations reported.

**Outcome:** Requests to unregistered `/api/*` routes return a proper `404`, matching client/error-handling expectations instead of a deceptive `200` with a `null` body.

**Implementation sketch:**
- `core/src/aegis/api/app.py:325-331` — confirmed: `serve_spa(path: str)` does `if path.startswith("api/") or path == "health": return None`, and since the handler has no response model FastAPI serializes this as `200` with a JSON `null` body — any unmatched `/api/...` route (typo, retired endpoint, client-side bug) looks like a successful empty response instead of a 404.
- Fix: `raise HTTPException(status_code=404)` for the `path.startswith("api/")` branch instead of returning `None`. Keep the `path == "health"` short-circuit behavior as-is (health has its own router) or confirm whether it still needs special-casing once the api/ branch is fixed.
- Import `HTTPException` from `fastapi` at the top of `app.py` (not currently imported there).

**Acceptance criteria:**
- New test in `tests/core/` hits `GET /api/definitely-not-a-real-route` and asserts `404` (not `200`).
- Existing SPA-serving tests (index.html fallback for non-api, non-existent frontend paths) still pass.

**Size:** S
**Depends on:** none

### D5 — Guard against future duplicate migration numbers

> **SHIPPED — PR #175 (`00c4ccb`).** Implemented as sketched; no deviations reported. It earned its keep immediately — six of the queue's migrations landed in slots other than the ones this document assigns.

**Outcome:** A CI check fails fast if a new migration reuses an already-taken numeric prefix, while the existing duplicate pair (`006_infra_coding.sql` / `006_social_metrics.sql`) is left untouched since renaming applied migrations is unsafe (tracked by filename in `schema_migrations`).

**Implementation sketch:**
- Confirmed: `migrations/006_infra_coding.sql` and `migrations/006_social_metrics.sql` are both live in the repo (15 files total in `migrations/`, this is the only duplicate prefix). Both have presumably already run in some deployments and are tracked by exact filename in `schema_migrations`, so renaming either is out of scope/unsafe.
- Add a small check (either a `tests/core/test_migrations_no_duplicate_numbers.py` test, or a standalone `scripts/check_migration_numbers.py` invoked from CI) that: lists `migrations/*.sql`, extracts the leading numeric prefix from each filename, and asserts no number is reused — **except** an explicit grandfather allowlist containing `{"006": ["006_infra_coding.sql", "006_social_metrics.sql"]}`.
- Document the grandfather exception inline so a future contributor understands why `006` is duplicated and doesn't "fix" it by renaming.

**Acceptance criteria:**
- Test passes today (grandfathered pair allowed).
- Test fails if a third file is added with an already-used prefix (verify by temporarily adding a scratch `006_test.sql` in the test's own tmp fixture, not the real `migrations/` dir).
- Test fails if a **new** duplicate pair (not in the allowlist) is introduced anywhere else in the numbering sequence.

**Size:** S
**Depends on:** none

### D6 — Declarative flow/activity/connector registration + boot-time completeness check

> **SHIPPED — PR #204 (`c6b9f8f`). This task invalidated every wiring checklist in this document.** Registration is now one `FlowSpec` in `worker/src/aegis_worker/registry.py` plus a seed row, with `base_workflows()` / `all_activity_methods()` / `activity_type_map()` derived from it and a `check_registration()` boot check. Deviations: the fan-out was **seven** places, not five (`_FEATURE_FLAGGED_TYPES` was missed); no per-flow `activities:` list — activities are owned by classes, not flows, so they are derived from the instances and the list is removed rather than moved; `seed_defaults` omitted (one flow class backs several rows with different configs, and `activities.config` is DB-owned after first boot); the **connector registry was declined** — four connectors with genuinely different construction, no second consumer, no completeness invariant. Closed #188; the declined connector hazard is tracked as #205.

**Outcome:** Adding a new Temporal flow (or connector) requires touching one declarative registration point instead of five hand-maintained lists, and a half-wired flow fails loudly at worker boot instead of silently never scheduling.

**Implementation sketch:**
- Confirmed current fan-out for a new flow, all in `worker/src/aegis_worker/`:
  - `__main__.py:111-136` module-level `WORKFLOWS` list (stub-safe, import-time).
  - `__main__.py:138-166` module-level `ACTIVITIES` list (stub instances).
  - `__main__.py:503-629` + `669-694` — `main()` rebuilds `activities`/`workflows` again with live-wired instances, then overwrites the module globals at `__main__.py:715-718` for tests.
  - `schedule_sync.py:57` `_ACTIVITY_TYPE_MAP` — maps `activity_type` string → `(WorkflowClass, ConfigBuilder)` lambda, used at `schedule_sync.py:368`.
  - `config/seed/activities.yaml` — seed rows (`workflow_type`, `agent_id`, `schedule_cron`, `config`) upserted by slug.
- Design a single module, e.g. `worker/src/aegis_worker/registry.py`, where each flow declares itself once: a small dataclass bundling `{flow_cls, activities: list[bound-method-getter], config_cls, seed_defaults}`. `__main__.py` builds `WORKFLOWS`/`ACTIVITIES` by iterating the registry instead of hand-listing; `schedule_sync.py` builds `_ACTIVITY_TYPE_MAP` from the same registry instead of a separate hardcoded dict.
- Add a boot-time completeness check (in `main()`, alongside the existing `_validate_agent_tool_sets()`-style pattern in `core/src/aegis/api/app.py:56`) that fails loudly (raises, not logs) if a registered flow is missing any of: its workflow class, an activity_type mapping, or a seed entry — catching the "half-wired flow silently never schedules" failure mode named in the audit.
- Apply the same idea to connectors: `bootstrap.py:82-153` currently constructs each connector with its own imperative `if`/`try` block into a `connectors: dict[str, Any]`. Replace with connectors self-registering `{key, factory, required_settings}` in the same or a sibling registry module, consumed by `bootstrap()`.
- Explicitly out of scope: `docs/architecture/sdk-stubs/` (confirmed present, ports/capability/lifecycle abstraction) is the eventual full capability system — this task is a pragmatic refactor enabler for upcoming life-data flows, not that design.

**Acceptance criteria:**
- New test in `tests/worker/` (extending the existing pattern in `tests/worker/test_worker_registrations.py`, `test_activity_registration.py`) asserts every flow in the registry appears in `WORKFLOWS`, has an `_ACTIVITY_TYPE_MAP` entry, and (where expected) a `config/seed/activities.yaml` row — and that a deliberately incomplete fake registry entry raises at the boot-check call, not just logs a warning.
- All existing flows migrate to the new registry with zero behavior change; `tests/worker/test_worker_registrations.py`, `test_llm_spend_guard.py`, `test_review_registration.py`, `test_briefing_registration.py`, `test_agent_registry.py` continue to pass unmodified in intent (adjust only for the new import path).
- `ruff` line-length 100 clean; tests run against real Postgres per existing `tests/conftest.py` convention.

**Size:** L
**Depends on:** none (but should land before new life-data flows are added, per theme framing)

### D7 — Begin chat.py decomposition: extract tool executors into `services/tools/<domain>.py`

> **SHIPPED — PR #216 (`105bf19`), first pass only: `services/tools/{base,infra,vercel}.py`.** Every figure this section cites was wrong at HEAD — 5,169 lines not "~4,537", 52 `_exec_*` definitions / 50 registry entries not "~104", and all four line numbers stale. The "under ~2,000 lines" acceptance criterion was **not met and not attempted** (the section says *begin*). Deviations: per-module executor dicts rejected — the names must be imported into `chat.py` anyway for routes and tests, so a per-module dict would be a second source of truth; and `AGENT_TOOL_SETS` is agent-scoped and overlapping, so it does not induce a partition of the executors — implementation coupling was used instead.

**Outcome:** `services/chat.py` (currently ~4,537 lines, confirmed) shrinks by moving its ~104 `_exec_*` tool-executor functions into domain-scoped modules under `services/tools/`, with zero behavior change and all existing import paths preserved for tests.

**Implementation sketch:**
- Confirmed structure to preserve: `CHAT_TOOLS` (tool schema list, `chat.py:301`), `TOOL_EXECUTORS: dict[str, Any]` (name → callable, `chat.py:3315`), and `_validate_agent_tool_sets()` (`chat.py:3503`) which boot-checks every `AGENT_TOOL_SETS` reference has a `TOOL_EXECUTORS` entry — this validation is called from `app.py`'s `lifespan()` and must keep working unchanged.
- The existing `_INFRA_SPECS` data-table + `functools.partial` pattern (`chat.py:1456-1633`, e.g. `_exec_list_nodes = functools.partial(_exec_infra, "list_nodes")`) is the template for how data-described executors should be organized in their new module — keep it as-is when moved, don't rewrite the pattern.
- Group the ~104 `_exec_*` functions by domain into new modules, e.g. `core/src/aegis/services/tools/infra.py`, `tools/knowledge.py`, `tools/gtd.py` (capture/next-actions/defer/complete/etc.), `tools/finance.py` (quotes/market/subscriptions), `tools/vercel.py`, `tools/social.py`, `tools/media.py` (youtube/pdf transcript tools) — grouping boundaries should follow the existing `AGENT_TOOL_SETS` domains already in `chat.py` rather than being invented fresh.
- Each new module builds its own local dict of `{tool_name: executor}`; `chat.py` imports and merges them into the single `TOOL_EXECUTORS` dict, preserving every existing symbol name (tests import `_exec_*` names directly — re-export via `from aegis.services.tools.infra import _exec_list_nodes` etc. in `chat.py` if any test imports from `aegis.services.chat` specifically) so no test call site changes.
- `CHAT_TOOLS` tool-schema definitions can stay in `chat.py` for this first pass (or move alongside their executors) — scope this task to executor extraction only; explicitly no behavior change, no new abstraction layer beyond straight file moves + the registry-merge glue.

**Acceptance criteria:**
- `services/chat.py` line count drops substantially (target: under ~2,000 lines) with no changes to `TOOL_EXECUTORS` keys, `CHAT_TOOLS` contents, or any `_exec_*` function's behavior.
- All existing tests under `tests/core/` that import `_exec_*` names (19+ files confirmed referencing `_exec_` today) pass without modification to their import statements, or with a mechanical import-path update reviewed as zero-behavior-change.
- `_validate_agent_tool_sets()` still runs at app boot (`core/src/aegis/api/app.py` lifespan) and still raises on an orphaned tool reference — add a regression test that moving executors didn't silently drop one from `TOOL_EXECUTORS`.
- Equivalence proof beyond incumbent tests (which don't cover all ~104 executors): capture `sorted(TOOL_EXECUTORS)` and each value's `__wrapped__`/`func.__name__` identity BEFORE the split (one committed snapshot test), and assert the merged post-split registry is identical — a `functools.partial` rebinding a stale reference or a dropped key then fails the snapshot instead of passing on untested executors.
- `ruff` line-length 100 clean across new `services/tools/*.py` modules; tests run against real Postgres per `tests/conftest.py`.

**Size:** M
**Depends on:** D6 preferred to land first (same "registry" mindset), but not a hard blocker — can proceed independently.


---

## Part 4 — Bugs & hygiene found along the way

Found incidentally during the audit; tracked as Theme D tasks above.

> **All five are FIXED.** (1) and (2) → PR #173; (3) → PR #176; (4) → PR #174;
> (5) → PR #175. Note item 1's framing was already corrected in PR #172 before
> implementation — see the correction block inside D1: the `smart` tier
> *deliberately* does not match `_TOOL_INCAPABLE_MODELS`, and only the hardcoded
> `gpt-oss:20b` fallback was a real defect.

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
