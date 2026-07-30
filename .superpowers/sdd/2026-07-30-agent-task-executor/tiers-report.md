# Issue #158: Repo resolution tiers 2-3 (title matching + Gate-0 confirm card)

## What was built

### Tier 2 — title/description matching (`resolve_task_repo`, `worker/src/aegis_worker/activities/agent_task.py`)

When tier 1 (Todoist project name → `PROJECT_REPO_MAP`) misses, `resolve_task_repo` now
synthesises an alert-shaped dict —

```python
{
    "title": task["content"],
    "description": task["description"],
    "fingerprint": f"task:{task['id']}",
    "service": "",
}
```

— and calls `self.alert_act.resolve_alert_resource(...)` **directly as a plain method call**
(no new Temporal activity registration). This mirrors the existing
`triage_email` → `self.gmail_activities.apply_label(...)` pattern already in this file: both are
`@activity.defn`-decorated methods on a sibling Activities dataclass, called directly from
inside another already-registered activity. `AgentTaskActivities` gained one new plain field,
`alert_act: Any = None` (same convention as `infra_ops`/`gmail_activities`), wired in
`worker/__main__.py` right after `agent_task_act` is constructed — `alert_act` (an
`AlertActivities` instance) already exists earlier in `main()`, fully configured with its own
`db_pool`/`llm_client`/`knowledge_connector`. **No new activity needed stub/live-list
registration** — `resolve_task_repo` was already registered; only the field wiring is new.

`service=""` is deliberate: a Todoist task has no alertmanager service label, so
`resolve_alert_resource`'s otherwise-deterministic (confidence=1.0) `sentry_project`/
`service_match` tiers correctly sit out rather than false-matching on an empty string, and the
KG tier misses by construction (no graph claim is ever written against a synthetic
`task:<id>` fingerprint) — title/description-based `deterministic`/`llm`/`llm_unconfirmed`
carry the whole tier-2 match, exactly as the issue describes.

A confident pick resolves precisely like tier 1: `{"github_repo", "repo_path", "source":
"title_match", "candidates": []}`. Anything less confident returns `{"github_repo": "",
"repo_path": "", "source": "none", "candidates": [...]}`, where each candidate is pre-shaped as
`{"resource_title", "github_repo", "resource_path", "score"}` — the exact fields
`_build_repo_confirm_prompt` (reused, not reimplemented) expects, so the flow's tier-3 card can
consume the activity's output unchanged. `alert_act is None` (test doubles, or a future
deploy before `__main__.py` wires it) → tier 2/3 are a no-op; behavior is identical to the old
tier-1-only code. A `resolve_alert_resource` exception is caught and degrades to the empty
result — tier 2 is best-effort and must never crash the activity or fall back to a guess.

### Confidence threshold: 0.8, and why

`resolve_alert_resource` itself splits `"llm"` (auto-confident) from `"llm_unconfirmed"` at
0.5 — but that split is calibrated for the **alert-investigation** flow, which re-scores the
pick against the actual issue content at its own Gate-0 (`score_resource_relevance`) and
further guards with an active-work check before ever touching a repo. `resolve_task_repo` has
neither of those downstream checks — a confident tier-2 result proceeds straight into a real
kimi investigation run. Reusing the 0.5 bar verbatim would let a coin-flip LLM guess
(0.5-0.79) kick off a real coding run unsupervised, which is exactly the thing issue #158 says
must never happen. **0.8** was chosen as `_TIER2_CONFIDENCE_THRESHOLD` because:
- Deterministic free-text token overlap (Tier 1.6 inside `resolve_alert_resource`) is always
  confidence 1.0 — always clears 0.8, so tier 2 still gets real, cheap, non-LLM coverage.
- A genuinely confident LLM pick commonly scores ≥0.85 in the existing test fixtures
  (`tests/worker/test_alert_resource_resolution.py`) — 0.8 still passes those.
- Anything between 0.5 and 0.8 (a real, LLM-confident-by-`resolve_alert_resource`'s-own-bar
  pick that is still not overwhelmingly clear) routes to the tier-3 Gate-0 card instead of
  running unsupervised — "never guess" wins any tie.

### Tier 3 — Gate-0 confirm card (`_confirm_repo_gate0`, `worker/src/aegis_worker/flows/agent_task.py`)

`_investigate_coding_task` now does, right after `resolve_task_repo`:

```python
if not repo["github_repo"] and repo.get("candidates"):
    confirmed = await self._confirm_repo_gate0(input, task_id, repo["candidates"])
    if confirmed is not None:
        repo = confirmed
if not repo["github_repo"]:
    return await self._park_coding(task_id, "repo unresolved", ...)  # unchanged exit
```

`_confirm_repo_gate0` reuses `_build_repo_confirm_prompt` (imported from
`aegis_worker.flows.alert_investigation`, no logic duplicated) and mirrors
`alert_investigation.py`'s own Gate-0 repo-confirm card: a numbered options menu
(`{"0": "1. 📦 <title>", ..., "none": "❌ None of these / cancel"}`), an `InteractionFlow`
child workflow (`kind="choice"`, `origin="agent_task_repo_confirm"`,
`timeout_seconds=86400`, `timeout_policy="archive"`, deterministic id
`agent-task-repo-confirm-{task_id}`), **awaited** (not fire-and-forget) — safe because
`AgentTaskFlow` is already spawned `ABANDON`ed by the sweep, exactly the same reasoning already
used for the plan/PR cards in this file. A numeric pick within range resolves
`{"github_repo", "repo_path", "source": "user_confirmed", "candidates": []}`; "none",
an out-of-range/non-numeric value, or an archived timeout all return `None`, and the caller
falls through to the **existing** "repo unresolved" park/comment — no new exit path, no new
`_CASES` entry required in `test_agent_task_flow.py`'s 17-case enumeration (same return
statement, reached via a new precondition).

## Test commands and output

```
ruff check worker/src/ tests/worker/
→ All checks passed!

PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/activities tests/worker/test_worker_registrations.py
→ 292 passed in 16.0s

PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/flows -n 4 --dist loadfile --timeout=120
→ 146 passed in 64.4s (first run, clean)
→ second back-to-back run: 145 passed, 1 failed —
  tests/worker/flows/test_alert_escalation.py::test_gate2_self_resolve_race_closes_gate,
  a Timeout under xdist parallel load. Unrelated file (alert escalation, not agent_task), and
  re-run in isolation (no xdist) passes in 0.74s. This matches the team lead's flagged known
  issue (#152 residue, pre-existing flakiness on this box) — not touched, not investigated
  further per instructions.
```

New/changed test files:
- `tests/worker/activities/test_agent_task_repo.py` — 8 new tests (11 total, up from 3):
  confident tier-2 match + exact synthetic-alert-shape assertion, low-confidence candidates
  surfaced in `_build_repo_confirm_prompt` shape, threshold boundary (just-below /
  exactly-at), no-match hard-park, `alert_act=None` no-op, `resolve_alert_resource` exception
  safety, and tier-1-hit-skips-tier-2 (asserts the fake was never called).
- `tests/worker/flows/test_agent_task_coding.py` — 2 new tests: Gate-0 approved (picks
  candidate 0, asserts the picked candidate's `resource_path` actually reaches
  `run_task_investigation`) and Gate-0 declined (`"none"` → parks, zero investigation events).
  `_activities()` gained an optional `repo_result` param (default unchanged: `_REPO_OK`).

## Falsifiability results

Every new behavior was broken, confirmed the corresponding test failed, then reverted:

| Break | Test that failed |
|---|---|
| Removed the `>= _TIER2_CONFIDENCE_THRESHOLD` gate (`if tier2_repo:`) | `test_tier2_confidence_just_below_threshold_is_unconfirmed` |
| Made tier-1 hit fall through to tier 2 anyway (`if False and github_repo:`) | `test_project_name_resolves_to_repo`, `test_tier1_hit_skips_tier2_entirely` |
| Removed the `try/except` around `resolve_alert_resource` | `test_tier2_exception_is_caught_and_never_guesses` |
| Disabled the flow's Gate-0 call site (`if False and not repo[...]`) | both new `test_agent_task_coding.py` Gate-0 tests |
| Made an invalid/"none" pick silently resolve to candidate 0 anyway | `test_gate0_confirm_declined_parks_without_investigating` (timed out — flow proceeded into a real coding run instead of parking) |
| Dropped `resource_path` from the confirmed-pick's `repo_path` | `test_gate0_confirm_approved_investigates_the_picked_repo` |

One check was **not** falsifiable as originally framed: removing the explicit
`if self.alert_act is None: return empty` guard did *not* break
`test_tier2_skipped_without_alert_act_wired`, because `None.resolve_alert_resource(...)` raises
`AttributeError`, which the surrounding `try/except Exception` already catches and degrades to
`empty` — belt-and-suspenders. Kept the explicit guard anyway (avoids relying on an
`AttributeError` as control flow, and avoids a misleading `agent_task_repo_tier2_failed`
warning log when tier 2 was simply never configured), but noting it honestly rather than
claiming a falsifiable test that doesn't exist for that specific line.

## Registration

**No new activity or workflow registrations.** `resolve_task_repo` already existed in both the
module-level `ACTIVITIES` stub and `main()`'s live `activities=[...]` list (issue #153); this
change only adds behavior inside it plus one new plain dataclass field (`alert_act`) wired via
assignment in `main()`, not a list entry. Tier 3's confirm card is pure workflow orchestration
reusing the already-registered `InteractionFlow`. `test_agent_task_registrations_reach_mains_live_lists`
was re-run and still passes unchanged (confirms no drift was introduced).

## Left out / not done

- Did not touch `test_agent_task_flow.py`'s 17-case exit enumeration — the Gate-0-decline path
  terminates at the exact same `return await self._park_coding(task_id, "repo unresolved", ...)`
  call site as the existing "no repo, no candidates" case, so no new literal exit was added.
  New coverage for the Gate-0-specific precondition lives in `test_agent_task_coding.py` instead.
- Did not add a distinct park-reason/comment for "Gate-0 declined" vs "no signal at all" —
  reused the existing message, which already reads correctly for both ("I couldn't work out
  which repository this task is about, so I haven't touched anything").
- Did not investigate the `test_alert_escalation.py` xdist flake (pre-existing, explicitly
  out of scope per instructions).

## Files touched

- `worker/src/aegis_worker/activities/agent_task.py` — `_TIER2_CONFIDENCE_THRESHOLD`,
  `alert_act` field, rewritten `resolve_task_repo`.
- `worker/src/aegis_worker/flows/agent_task.py` — `_build_repo_confirm_prompt` import,
  `_confirm_repo_gate0`, wiring in `_investigate_coding_task`.
- `worker/src/aegis_worker/__main__.py` — `agent_task_act.alert_act = alert_act`.
- `tests/worker/activities/test_agent_task_repo.py`, `tests/worker/flows/test_agent_task_coding.py`
  — new tests (see above).
