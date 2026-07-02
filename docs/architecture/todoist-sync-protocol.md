# Todoist Sync Protocol — Architecture Notes

Operational protocol every Todoist Sync API caller in AEGIS must follow. This document describes the invariants that hold across the whole GTD layer — see [`overview.md`](overview.md#todoist-comment-channel) for the higher-level routing flow.

> **GTD-model update (2026-07):** the sync/envelope invariants below are unchanged, but the GTD *shape* has moved to a **label-first** model. **Next** and **Someday/Later** are no longer managed projects — they are the `@next` / `@someday` **labels** (alongside `@waiting` / `@reference`). AEGIS creates **no** managed projects: `bootstrap_if_empty` adopts only the native Todoist **Inbox** (seed `managed_projects: []`), and areas/work-streams are **real, user-owned nested projects**, not `@area/*` / `project/*` labels. Clarify is label-only — it never `item_move`s a task between projects (filing into a project is manual). Sections that still refer to Next/Someday as projects, or to `project/*`/`@area/*` labels as the work-stream taxonomy, describe the pre-2026-07 model.

## 1. Envelope vs per-command status

`TodoistConnector.commands(commands)` wraps Todoist's `/api/v1/sync` response in the standard AEGIS envelope `{ok, data, error, retryable, external_ref}`. `ok=True` only means the HTTP call returned 200; individual commands inside the batch can still be rejected. Per-command status lives at `data.sync_status[uuid]` — either the literal string `"ok"` or a dict like `{"error": "Item not found", "error_code": 22, "error_tag": "ITEM_NOT_FOUND", "http_code": 404}`.

**Every caller MUST use `TodoistConnector.check_sync_status(envelope, command_uuids)`.** It returns:

```python
{
    "ok": bool,             # envelope-ok AND every uuid sync_status == "ok"
    "envelope_error": str | None,
    "retryable": bool,      # envelope-fail with retryable=True (5xx, timeout, rate-limit)
    "rejected": dict,       # uuid -> sync_status dict for the failures
    "rejected_retryable": bool,  # True iff every rejection is in the
                                 # retryable class (5xx-style HTTP / unknown).
                                 # False if any is in _NON_RETRYABLE_ERROR_TAGS
                                 # (ITEM_NOT_FOUND, INVALID_ARGUMENT, etc.).
}
```

The split between retryable and non-retryable rejections is load-bearing for the outbox (see §3). Reference impls:

- `worker/.../activities/alerts.py::post_task_note`
- `worker/.../activities/clarify.py::apply_outcome`
- `worker/.../activities/capture.py::capture_to_inbox`
- `core/.../services/chat.py::_exec_*` (the user-facing chat tools)

**Anti-pattern**: `if not result.get("ok"):` followed by treating any non-ok envelope as a transient failure. This silently outbox-queues permanent rejections, burning five wasted retries per call before they hit `attempt_count >= 5` and get marked failed.

### Lenient empty-status fallback

`check_sync_status` has a backward-compatibility branch that returns `ok=True` when `sync_status={}` and `command_uuids` is non-empty (for tests that mock the connector with `{ok: True, data: {sync_status: {}}}`). It logs a `todoist_check_sync_status_empty_lenient_fallback` warning when this fires so we can spot if it ever triggers in production (which would indicate a degraded Todoist response silently reported as success).

## 2. Bootstrap (`TodoistActivities.bootstrap_if_empty`)

Runs as step 1 of `TodoistSyncFlow` (every 5 min). Three branches:

1. **`already_done`**: `settings.todoist_managed_project_ids` exists and contains every key in `config/seed/todoist.yaml::managed_projects`.
2. **Self-heal**: settings row exists but is missing keys (e.g. a later phase added `reference`). `_create_missing_managed_projects` creates only the missing projects and patches settings.
3. **Recovery from partial state**: settings row missing but `todoist_projects.is_managed=true` rows present. Recovery matches the projection rows to seed entries by name. **If any `is_managed=true` row has a name not in the seed AND there's more than one such row, recovery refuses** — the user likely renamed a managed project in the Todoist UI; falling through to a normal bootstrap would create duplicate `📚 Reference` / `Reference` projects. Operator must rename the project back or clear the `is_managed` flag manually.

The adopted-Inbox slot is special: Todoist's default Inbox project (the one with `inbox_project=true`) is adopted as our `inbox` slot rather than created. This is the single name-unknown row that recovery tolerates.

## 3. Outbox replay (`TodoistActivities.drain_outbox`)

Runs as step 4 of `TodoistSyncFlow` every 5 min. Pulls up to 50 pending rows from `todoist_outbox` (the Sync API batch limit), submits as one `commands` batch, and updates each row:

| Per-cmd `sync_status` | Action |
|---|---|
| `"ok"` | mark `committed`, record `committed_id` from `temp_id_mapping` |
| dict with `error_tag` in `_NON_RETRYABLE_ERROR_TAGS` (`ITEM_NOT_FOUND`, `INVALID_ARGUMENT`, …) | mark `failed` immediately (permanent rejection — replay would just fail again) |
| dict with `http_code` in 400-499 | mark `failed` immediately |
| dict with `http_code` >= 500 / no `http_code` / unknown tag | increment `attempt_count`; mark `failed` when `attempt_count >= 5` |
| missing (Todoist didn't ack the uuid) | mark `failed` (avoid infinite re-queue) |

Envelope-level failure (HTTP 5xx, timeout, rate-limit): bump `attempt_count` for every row; mark `failed` when `attempt_count >= 5`.

Producers queue to the outbox only on **retryable** failures (envelope `retryable=True` OR `rejected_retryable=True`). Permanent rejections never enter the outbox — they're logged and dropped by the producer. This is the rule that prevents poison loops.

## 4. Comment-loop guard

ClarifyFlow posts machine-generated notes back to Todoist tasks to record its classification decisions. Those notes MUST be distinguishable from user-authored comments because three downstream consumers filter on them:

1. **`apply_sync_diff`** — bumps `todoist_tasks.last_note_at` only for non-AEGIS notes. Bumping on our own output would loop the clarifier forever.
2. **`webhooks.py::todoist_webhook`** — receives `note:added` and triggers an immediate ClarifyFlow re-run; must skip its own notes.
3. **`ClarifyActivities.find_unclassified_items`** — reads `latest_user_note` to give the LLM user supervision; filters out machine notes via SQL `NOT LIKE`.

The prefix is centralised at **`core/src/aegis/clarify_note.py::CLARIFY_NOTE_PREFIX`** (`"[ClarifyFlow @ "`) so a producer-side rename can't drift from the consumer filters. Producers in `worker/.../activities/clarify.py::_format_apply_note` + `_format_review_note`. Sub-suffix patterns (`ref-complete`, `ref-demote`, `pass N`, `NEEDS REVIEW`) use the same prefix.

Pandora's `AlertInvestigationFlow` comments use a different convention — they include `Workflow run: <id>` as a stable footer marker. `apply_sync_diff` and `find_unclassified_items` filter both prefixes.

## 5. Watermark invariant

`ClarifyActivities.log_classification(bump_watermark: bool = True)` controls when `todoist_tasks.last_clarified_at` advances. The flow at `worker/.../flows/clarify.py` computes `bump_watermark` per task:

```python
payload = outcome.get("interaction_payload") or {}
spawn_kind = payload.get("spawn_kind")
interaction_will_spawn = bool(
    outcome.get("interaction_spawned")
    and (spawn_kind != "pandora_investigation" or outcome.get("applied"))
)
bump_watermark = bool(
    outcome.get("applied")
    or outcome.get("outbox_queued", 0) > 0
    or interaction_will_spawn
)
```

The pandora carve-out: when `spawn_kind == "pandora_investigation"` and `applied=False` (typically a non-retryable rejection from a stale projection), the spawn is skipped in `flows/clarify.py:147-151`. Bumping the watermark there would silently consume the user's followup comment and never re-investigate. Migration 016 was added to repair watermarks poisoned by the original unconditional bump.

## 6. Settings invariants

These settings rows are seeded by migrations 011 + 012 and are read by the GTD pipeline:

| Key | Migration | Default | Purpose |
|---|---|---|---|
| `todoist_capture_enabled` | 011 | `true` | Kill switch for every `capture_to_inbox` call |
| `todoist_managed_project_ids` | runtime | `{}` | Created by `bootstrap_if_empty`; maps `inbox / reference / someday / projects / ...` keys to Todoist project ids |
| `gtd_clarify_enabled` | 012 | `true` | Kill switch for `ClarifyFlow.find_unclassified_items` |
| `gtd_2min_rule_enabled` | 012 | `true` | Gate for the 2-min in-window comms card spawn (Slack/Telegram) |
| `user_timezone` | 012 | `UTC` | Used by the in-window check for the 2-min rule |

The worker bootstrap at `worker/.../__main__.py` logs a `todoist_settings_missing` warning if any of these keys are absent at boot — flags a failed migration before the silent defaults engage.

## 7. Label projection

The `todoist_labels` projection had a `UNIQUE(name)` index until migration 018. The unique constraint was a sync-token poison pill: a Todoist delta containing a rename collision (label A id=X name="Old" already in our projection; label B id=Y name="Old" arrives in the same diff) made the `INSERT ... ON CONFLICT (id) DO UPDATE` violate the name-unique index because the INSERT path matches by id, not name. The transaction aborted, sync_token never advanced, and every subsequent tick re-polled the same poison diff.

No runtime code reads `todoist_labels` by name (the seed loader uses ids; chat-tool reads come off the `todoist_tasks.labels` text-array column), so dropping the index is safe. Migration 018 drops it. The defensive empty-name skip in `apply_sync_diff:194` remains in place.
