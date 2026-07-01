# AEGIS Leanness Audit — 2026-06-21

Full read-only sweep of the LLM subsystem, Temporal flows, worker activities, connectors, the Todoist sync layer, the core chat service, and comms. Every "dead" claim was reachability-verified (codegraph callers + grep across all packages incl. tests, plus `__main__.py` registration, `activities.yaml` schedules, child-workflow spawns, and route mounting) before being listed.

**Codebase:** 33,509 source LOC across `aegis-core` / `aegis-worker` / `aegis-comms`.

## TL;DR — what's actually worth doing

| Bucket | Lines | Risk | Verdict |
|---|---|---|---|
| **A. One real bug** (`_retry_via_llm` silently dead) | ~3 fix | low | **Fix now** — a whole feature never fires |
| **B. Delete-outright dead code** (5 dead activities, dead Gate-1 block, dead config fields, 3 dead connector methods, …) | ~480 src + ~300 test | low | **Do** — pure subtraction, no live path |
| **C. Duplication consolidation** (one LLM-JSON parser, LLM success-recording in client, Todoist status-ladder, Gmail-reauth trio, retry constants) | ~400 | low–med | **Do** — fewer foot-guns |
| **D. Telegram teardown** (commit to Slack-only) | ~1,400 src + ~1,100 test | med (1 decision) | **Owner call** — biggest single win |
| **E. `chat.py` decomposition** (4025 → ~600 + focused modules) | restructure | low | **Do when convenient** — readability |
| **F. Cosmetic / optional** | ~120 | trivial | batch into adjacent work |

The system is **not bloated with dead flows or dead tools** — all 28 flows and all 43 chat tools are live and correctly wired. The fat is concentrated in: (1) the Telegram channel that prod no longer uses, (2) a handful of orphaned activities/blocks left behind when their drivers were deleted, (3) the same LLM-JSON / outbox / retry idioms hand-rolled 3–9× instead of shared, and (4) two undecomposed mega-files (`chat.py` 4025, `clarify.py`/`alerts.py` ~2000 each — though the latter two are genuinely all-live logic).

---

## A. Correctness bug found during the scan (not leanness, but fix it)

### A1. `_retry_via_llm` is silently non-functional — the entire arg-correction retry feature is dead
- **`core/src/aegis/services/chat.py:2630-2645`** — category: **latent bug**
- `_retry_via_llm` reads the tool call in the *nested* OpenAI shape `tc["function"]["name"]` / `tc["function"]["arguments"]`, but `LLMClient.chat` returns the *flat* shape `{"id","name","arguments"}` (`core/src/aegis/llm/__init__.py:335-337`). The nested `"function"` key only exists on the *outbound* assistant message built at `chat.py:3703` — never on what the client returns. So the match loop at `:2640` never fires and the function always `return {}`. On any JSONSchema validation failure the "model self-corrects" retry (the whole 128-line `:2520-2647` validation/retry block + `_schema_hint`) hands back empty args → second validation fails → `ChatToolValidationError`.
- No test catches it — every `test_chat_tool_validation.py` case injects a deterministic `retry_args_provider` stub; the real `_retry_via_llm` is never exercised.
- **Fix:** `tc.get("name")` / `tc["arguments"]` (flat shape) + one test driving the real path against a mocked `llm_client.chat`. ~3 lines.

---

## B. Dead code — delete outright (verified zero live callers)

### Worker activities (5 orphans — drivers were deleted, activities left behind)
| What | Location | Evidence | Lines |
|---|---|---|---|
| **B1. `gmail.py::bootstrap_triage_cache`** | `gmail.py:743` + reg `__main__.py:455` | Its flow `GmailTriageBootstrapFlow` was deleted (no file, no seed row). Orphaned remnant. Largest single dead activity. | ~100 |
| **B2. `content.py::check_semantic_similarity`** | `content.py:412` + reg `__main__.py:429` | Zero callers — not in `process_content`, not in any flow. Tests only. | ~45 |
| **B3. `delivery.py::send_telegram_document`** | `delivery.py:80` + reg `__main__.py:425` | Zero prod callers. Pairs with the **dead comms route** `POST /api/deliver/telegram/document` (`comms/__main__.py:330`), also uncalled. Delete both. | ~30 + route |
| **B4. `knowledge.py::ingest_outcome`** | `knowledge.py:51` + reg `__main__.py:430` | Zero prod callers; near-clone of live sibling `ingest_claims`. | ~28 |
| **B5. `clarify.py::_RuleSet.auto_priority` + `_PRIORITY_RULES`** | `clarify.py:72,87` | Referenced only by its own def + the class docstring. The other 3 `_RuleSet` methods are live; this one isn't. | ~10 |

Each: delete activity + its registration line in `__main__.py` + its test(s).

### Flows (dead blocks inside live flows — no dead flow files)
| What | Location | Evidence | Lines |
|---|---|---|---|
| **B6. Gate-1 `requires_approval` approval block** | `alert_investigation.py:263-340` | **Verified dead in prod:** all three producers hardcode `requires_approval=False` (`clarify.py:1015`, `clarify.py:1058`, `chat.py:1459`); only `True` is in a test + docstring. The approve/skip/mute_24h InteractionFlow child never fires for a real alert. *Largest single deletion in the audit.* | ~78 |
| **B7. `activity_name` config field** | 12 flow-config dataclasses + 13 `schedule_sync.py` assignments | Populated by the mapper, **read by nothing** — no flow body, no interceptor. | ~25 |
| **B8. Impossible-state empty-`mute_key` branches** | `alert_investigation.py:321-338` (inside B6) + `:1134-1146` | `mute_key` is empty only if `service` is missing, but every caller sets `service`. Both arms log "couldn't mute" for a state that never occurs. | ~25 |
| **B9. Step-1 "skip if resolved on arrival"** | `alert_investigation.py:230-234` | No caller sets `alert["resolved"]`; webhook pre-filters non-firing, and the real self-resolution check is the reachable step 3. Low-confidence (cheap insurance). | ~5 |

### Connectors (test-only methods — kept alive only by their own tests)
| What | Location | Evidence | Lines |
|---|---|---|---|
| **B10. `remote_script.py::fetch_kimi_session_log` + `kimi_project_hash` + `run_script_path`** | `:664-696`, `:380-388`, `:291-301` | Zero production callers; the live path is `fetch_kimi_run_output`. The `~/.kimi/sessions/<md5>/` fetch was superseded and never re-wired. | ~55 + 3 tests |

### Core config / routes (dead Telegram-era leftovers, independent of the full teardown)
| What | Location | Evidence | Lines |
|---|---|---|---|
| **B11. `telegram_webhook` route + `telegram_webhook_secret`** | `routes/webhooks.py:258-267`, `config.py:177` | Docstring: "becomes primary in Phase 5" — Phase 5 never happened. Route validates a secret and returns `{accepted}`, does nothing. No inbound. | ~15 |
| **B12. `check_active_agents_have_topics` + boot loop** | `api/app.py:28-39` + `:76-78` | Queries `agents WHERE telegram_topic_id IS NULL` and WARNs at every Core boot — its own docstring says it's "the Telegram bot's job." On Slack it just spams a WARN per agent. | ~15 |
| **B13. `telegram_topic_id` / `telegram_chat_id` override plumbing** | `interaction.py:42-43`, threaded through gmail/calendar/receipt ingest + `delivery.send_interaction_card` + card models + `schedule_sync.py:125,142` | **No caller ever sets these non-zero** — every assignment is `=0`; SlackAdapter ignores `chat_id`/`topic_id` entirely (routes by `target.channel`). Pure dead plumbing across ~5 flows. | ~40 (many files) |

**B11–B13 are deletable today without committing to single-channel** (they're already dead on Slack). B-section total: **~480 src + ~300 test lines.**

---

## C. Duplication — consolidate to one source of truth

### C1. LLM-JSON "strip ```json fence + json.loads" — 4 divergent implementations across 8 sites ⭐ (flagged independently by 3 audits)
- `clarify.py:400 _parse_llm_json` (the *good* tolerant one — regex fence + first-`{...}` fallback + returns `None`), vs `gmail.py:460,726` + `chat.py:2994` (`split("```")[1]`), vs `alerts.py:1040,1686` + `intelligence.py:91` (`re.sub` fence strip), vs `llm/__init__.py:427` (array regex).
- They diverge on edge cases — some return `{}`, some raise, some fall back differently.
- **Fix:** promote one `parse_llm_json(text) -> Any | None` into `aegis.llm` (lift+generalize `clarify._parse_llm_json`), replace all 8. ~40–50 lines, one parser to fix when a model emits a new fence quirk.

### C2. LLM success-path `record_llm_call` boilerplate copy-pasted across 7 call sites ⭐
- Identical `start=monotonic()` → `think(...)` → `record_llm_call(model=result.get(...), prompt_tokens=..., latency_ms=int((monotonic()-start)*1000), ...)` at `gmail.py:445,717`, `intelligence.py:82`, `alerts.py:723,1029,1674`, `chat.py:3678`.
- `think()`/`chat()` already accept `db_pool`/`purpose`/`agent_id` and already record the *failure* row internally — successes are recorded by the caller, an asymmetric foot-gun. `think()` even computes `latency_ms` at `__init__.py:197` but doesn't return it, forcing every caller to re-time.
- **Fix:** move `record_llm_call(..., status="success")` inside the client, gated on `db_pool and purpose`. Delete all 7 caller blocks. ~70–90 lines + makes recording impossible to forget on new call sites.

### C3. Gmail-OAuth reauth trio — extract the one high-value ingest dedup ⭐
- `_is_auth_expired` is byte-for-byte identical in 3 files (`gmail.py:34`, `calendar.py:36`, `receipt.py:44`); `_fetch_with_reauth` is ~95% identical across the same 3 (same reauth-card InteractionFlow, `timeout_seconds=86400`, `"hold"` policy) — only the fetch-activity name + prompt differ.
- **Fix:** extract `shared/gmail_auth.py` parametrized by fetch activity + prompt. ~90–110 lines, and a security-sensitive reauth flow fixed in one place.

### C4. Todoist outbox-staging duplicated across 3–4 paths
- `chat.py:2298 _stage_chat_tool_outbox` vs `clarify.py:~1204` (inline in `apply_outcome`) vs `_capture_to_inbox_impl` vs `CaptureActivities.capture_to_inbox` — same `INSERT INTO todoist_outbox ... ON CONFLICT (temp_id) DO NOTHING` + retryable/permanent branching. The chat-side docstring itself names the other three.
- **Fix:** one `stage_outbox_commands(conn, commands, status, op)` on `TodoistConnector`. ~40 lines, single retry-semantics source.

### C5. Todoist connector status-code ladder repeated 3×
- `todoist.py` `sync()` (`:99`), `commands()` (`:144`), `upload_file()` (`:201`) each have an identical 14-line 200/401-403/5xx/429 → envelope ladder, differing only in the action label.
- **Fix:** `self._classify_response(r, action, elapsed)`. ~28 lines.

### C6. `drain_outbox` re-implements `check_sync_status` classification + reaches into a private attr
- `activities/todoist.py:529-548` hand-rolls the permanent-vs-retryable decision and reaches into `TodoistConnector._NON_RETRYABLE_ERROR_TAGS` (private). The 14 other sites correctly use `check_sync_status`.
- **Fix:** extract `_is_permanent_rejection(st)` staticmethod on the connector, reuse in both. ~12 lines + removes the private-API coupling.

### C7. `_ACT_RETRY = RetryPolicy(maximum_attempts=3)` redeclared in 9 flows + 18 inline `maximum_attempts=1`
- 9 flows redeclare `_ACT_RETRY` (= `shared.retry.FAST` minus `initial_interval`) plus duplicate `_ACT_TIMEOUT`/`_FETCH_TIMEOUT`; 18 inline `RetryPolicy(maximum_attempts=1)` where the already-imported `NO_RETRY` would do.
- **Fix:** add one `ACT_RETRY` to `shared/retry.py`, import everywhere; replace the 18 inline ones with `NO_RETRY`. ~30 lines + matches the CLAUDE.md "use the shared constants" convention.

### C8. Duplicated inline JSONB-decode helper
- `_meta(row)` defined byte-identically at `alerts.py:1261` and `:1333`; `channels.py:17 _decode_config` does the same for another table.
- **Fix:** one module-level `_decode_metadata(row)`. ~14 lines.

### C9. Envelope shape defined twice
- `homelab.py:25-38` free function vs `todoist.py:53-67 _envelope` method — identical bodies; both subclass `_base.HTTPConnector` which doesn't own the envelope.
- **Fix (marginal):** hoist `make_envelope()` into `_base.py`. ~13 lines — only if touching `_base` anyway.

---

## D. Telegram teardown — the biggest single win (one owner decision)

Slack is hard-set in prod (`AEGIS_CHANNEL=slack`); Telegram is blocked in India and declared retired. Everything below is reachable **only** via `AEGIS_CHANNEL=telegram` — a dead config. Removing it = hard-committing to Slack and deleting the `telegram` branch of the channel-adapter seam.

| What | Lines |
|---|---|
| `comms/bot.py` — the entire `AegisTelegramBot` + aiogram machinery (the shared delivery server is **not** here; it's the channel-neutral `__main__.py::create_delivery_app`) | 963 |
| `comms/adapters/telegram.py` `TelegramAdapter` | 112 |
| `comms/__main__.py` telegram branch (`_run_telegram_api_probe`, `_TelegramProbeState`, the `channel=="telegram"` arms of `run()`/`health()`/`_startup_error`) | ~110 |
| `comms/cards.py::render_telegram_keyboard` | 65 |
| `comms/config.py` `telegram_bot_token`/`telegram_chat_id`; `comms/handlers/` (vestigial empty package — dead regardless) | ~13 |
| `worker cleanup.py:179-192` legacy `telegram_message_id` fallback branch | ~14 |
| **Telegram-only tests** (`test_telegram_adapter`, `test_send_chunking`, `test_topic_routing_self_heal`, `test_interaction_callback`, `test_cards_telegram`, `test_setup_topics`, + Telegram halves of others) | **~1,100** |

**Total: ~1,400 src + ~1,100 test lines.**

**Keep (intentionally channel-neutral — do NOT delete):** the `base.py` `ChannelAdapter`/`DeliveryRef`/`SendResult`/`CardSpec` seam, the channel-neutral delivery app, `format.py`, `slack_*.py`. If Telegram is removed, also collapse `get_adapter`/`run()`/`health()` to single-branch.

**Naming debt (live + correct, just misleadingly named `telegram` — rename, don't delete):** `delivery.py::send_telegram`/`send_telegram_document`, the `/api/deliver/telegram` route, `config.telegram_service_url`, `homelab.check_telegram_polling_health`, `chat.py:3560` `"[Sent to you on Telegram]"` prefix, `interactions.telegram_message_id` column. These all route through the *active* (Slack) adapter. A `telegram→comms`/`send_message` rename sweep is honesty-only; the column is a cheap NULL on Slack — leave it.

> If you do **not** want to commit to single-channel yet, B11–B13 (dead core webhook/topic plumbing) are still removable today, and the naming-debt rename can wait.

---

## E. Structural — decompose the mega-file

### E1. `services/chat.py` (4025 lines) — four concerns glued into one module
Measured breakdown:
- **926 lines (23%)** — `CHAT_TOOLS` JSON-schema dicts (`:153-1078`), pure data.
- **~1490 lines (37%)** — 43 `_exec_*` executors, already clustered by domain (infra/k8s `:1101`, knowledge `:1498`, market `:1630`, research/triage `:1868`, GTD `:2121`, vercel `:2648`).
- **~525 lines** — knowledge-context machinery (`:2965-3488`).
- **~540 lines** — orchestration (`send_message` `:3489`, `synthesize_agent_reply` `:3952`).

**Fix:** mechanical split into `chat_tools_schema.py` (the dicts), `chat_tool_executors/` (by domain), `chat_knowledge_context.py`, and a ~600-line `chat.py` orchestrator. `TOOL_EXECUTORS`/`AGENT_TOOL_SETS` stay as the registry; the boot-time `_validate_agent_tool_sets` already enforces consistency. Near-zero risk, big reviewability win.

### E2. `_truncate_result` 6-pass progressive shrink (`chat.py:64-149`, ~110 lines)
Used (not dead), but a single "keep first N items + hard-cap each string" pass covers the same need; the 6 graduated passes are speculative tuning for a budget the LLM tolerates loosely. ~60 lines if simplified. Low priority.

> `clarify.py` (2041) and `alerts.py` (1900) were checked for the same treatment — **verdict: leave them.** Their size is all-live logic (`classify_one` 457, `apply_outcome` 346, the Tier-1/1.5/2 `resolve_alert_resource` cascade 351) with the documented sacred `@pandora`/`APP-` branch ordering. No dead branches; only the small dups already listed (B5, C8).

---

## F. Cosmetic / optional (batch into adjacent work — not worth a dedicated PR)

- **F1.** `IntelligenceScanInput.significance_threshold` dataclass default is `5`; the `schedule_sync` mapper always passes config (default `4`) → the `5` is a dead fallback. Align to `4`. (cmemory-flagged.)
- **F2.** `tier_to_model` in `llm/__init__.py:490 __all__` + re-export `:483` has zero external callers — trim from `__all__` (keep the function). ~2 lines.
- **F3.** `config/models.yaml:10-16` `litellm:` block is read by nothing (`load_model_tiers` reads only `tiers`); its stale `request_timeout: 120` doesn't even match the client default `300`. Delete. ~7 lines.
- **F4.** `active_work_lookback_hours` comment (`core/config.py:76`) still says "in-flight signal" — removed in PR #322. Update comment.
- **F5.** `intelligence.py:76 score_significance` comment says "gpt-oss:20b" though it now runs `model_light`/gemma4. Update comment.
- **F6.** Repeated multi-line "gpt-oss reasoning-budget tax" rationale comments (`gmail.py:436`, `intelligence.py:75`) → one-liner pointing at the `LLMTruncationError` docstring. (Do **not** collapse the per-call `max_tokens` numbers themselves — each was live-validated.)
- **F7.** `alert_investigation.py` `if track_task_id and not track_task_id.startswith("item-")` guard repeated ~11× before `_safe_post_note` → push the `item-` check inside `_safe_post_note`. ~10 lines.
- **F8.** `alert_investigation.py:1307-1324` + `:1335-1348` rebuild the verdict heading twice (Todoist comment + Slack ping, only `_html_escape` differs) → `_verdict_heading(...)` helper. ~12 lines.
- **F9.** `alert_investigation.py` inline Gate-2 prompt (~37 lines) + infra-context prose (~13 lines) in the `@workflow.run` body → extract to module helpers (Gate-0's already is). Readability only.

---

## Verified clean — recorded so future scans don't re-chase

- **No dead flows** — all 28 registered + reachable (23 scheduled, `GitHubAlertFlow` via webhook, 4 via child spawn).
- **No dead chat tools / routes** — `CHAT_TOOLS`/`TOOL_EXECUTORS`/`AGENT_TOOL_SETS` are exactly 43/43/43 aligned; all 21 routers mounted, all ~80 endpoints consumed. (Project doc's "~38 tools" is stale — it's 43.)
- **`intelligence.py` vs `intel_scan.py` are NOT duplicates** — complementary halves of one pipeline (`intel_scan.search_source` = searxng HTTP; `intelligence.{dedup,score,ingest}` = KG/LLM/KS), both live + registered. *False lead, closed.*
- **`clickhouse.py` + `vercel.py` are LIVE** — back 5 maou + 4 pandoras chat tools respectively, config-gated.
- **`remote_script.py` dormant kimi-host machinery is justified** — dormant by *config* (`kimi_host=""` fail-closes), not dead branches; gated by one `if`. The only dead bits are the 3 test-only methods (B10).
- **`runs_v3.py`, `alert_governance.py`, `channels.py`, `core_client.py`, `capture.py`, `active_work.py`** — all live.
- **`_ssh.py`/`_subprocess.py`/`_base.py`** — thin, justified abstractions (each with a documented incident origin), not over-abstraction. `check_sync_status` is correctly shared (5 callers).
- **The 7 ingest flows do NOT share a collapsible base** — only `intelligence_scan` fits the literal `fetch→dedup→process→ingest` shape; the others spawn different children / route differently. A shared base would be a leaky abstraction. The only real ingest dedup is the Gmail-reauth trio (C3).
- **Knowledge-feedback / decay machinery, semaphore throttle, `LLMTruncationError`** — all wired and used, not speculative.

---

## Suggested PR batching (respects CI path filters + batch-before-deploy)

CI path filters: `core/**`→core.yml, `worker/**`+`core/**`→worker.yml, `comms/**`→comms.yml, `migrations/**`→core. Group so each deploy is one logical unit:

1. **PR-1 `fix(chat): repair dead LLM arg-correction retry`** — A1 + its test. Core-only. *Ship first — it's a bug.*
2. **PR-2 `chore(worker): delete orphaned activities + dead alert blocks`** — B1–B9 + B10. Worker-only, pure deletion. (Touches `__main__.py` registration; verify `f1 migration-integrity` style checks stay green.)
3. **PR-3 `refactor(llm): one JSON parser + fold success-recording into client`** — C1 + C2. Touches core (`llm/`) + worker call sites → worker.yml + core.yml both fire; that's fine, it's one logical change.
4. **PR-4 `refactor(worker): extract gmail-reauth trio + share retry constants`** — C3 + C7. Worker-only.
5. **PR-5 `refactor(todoist): one outbox-stager + status classifier`** — C4 + C5 + C6. Core + worker.
6. **PR-6 `refactor(core): decompose chat.py`** — E1 (+ E2). Core-only, mechanical.
7. **PR-7 (owner decision) `chore: remove Telegram channel`** — D, if committing to Slack-only. Comms + worker + core + delete tests. Big but mechanical; do it as its own PR.
8. **Cosmetics (F1–F9)** — fold into whichever of the above touches the same file; never a standalone deploy.

**Realistic net reduction:** ~880 lines deletable without the Telegram decision (B + C + F), ~2,500 more with it (D), plus `chat.py` going 4025 → ~600 in the orchestrator (E, restructure not deletion). All of B/C/E/F is behavior-preserving; A is a fix; D is the one judgment call.
