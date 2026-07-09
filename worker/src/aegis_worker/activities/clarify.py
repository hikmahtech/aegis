"""ClarifyActivities — Phase 3 GTD Inbox clarification.

ClarifyFlow walks Inbox tasks tagged with Phase 2 source tags
('#email', '#alert', '#receipt', '#research', '#calendar', '#manual',
'#chat') and classifies them into one of:

    trash | reference | someday | 2_min | next_action

Rule lookups (skip_inbox / default_assignee / default_contexts) live in
the `_RuleSet` Python class below, keyed on source_tag (where a task came
from). A second, complementary axis routes by task CONTENT: `content_routes`
(aegis.services.content_routes) is an admin-configured, ordered list of
regex/prefix/contains rules — the old hardcoded Acme `^APP-\\d+:` → @pandora
investigation is now just one such row. First match wins; ships empty.

Content-route classifications:

- `pandora_gate` — a *fresh* task matching a `gate: true` content route (no
  @pandora yet). Doesn't auto-fire: classify_one returns `pandora_gate` →
  apply_outcome spawns a two-option choice card ("🔍 investigate" / "🙋 I've
  got it"). apply_clarify_resolution then either stamps the route's assignee
  (@pandora) + clears the watermark — so the next tick's retry branch fires
  the real AlertInvestigationFlow, scoped by the route's service/resource_tags
  — or stamps @me.

- `pandora_investigation` — a @pandora task matching a content route with no
  completed investigation (the retry surface, or the gate's approved path).
  apply_outcome stamps the route's assignee + area_label and returns a spawn
  flag; ClarifyFlow fires AlertInvestigationFlow with the existing task_id.

- `route_apply` — a task matching a `gate: false` content route: apply the
  route's assignee + contexts (+ area_label) directly, no card, no agent run.

- `pandora_owned` — task already carries the @pandora label (claimed by a
  prior AlertInvestigationFlow run). apply_outcome is a no-op; log_classification
  still bumps last_clarified_at so the task drops out of find_unclassified_items.

Any task carrying @me — set on a gate card or by hand — is skipped by
find_unclassified_items entirely: the user's "hands off, I'm on it" signal.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

from aegis.llm import parse_llm_json
from aegis.services.content_routes import (
    active_patterns,
    match_route,
)
from aegis.services.content_routes import get_content_routes as _get_content_routes_db
from aegis.services.gtd_rules import (
    DEFAULT_ASSIGNEE,
    DEFAULT_CONTEXTS,
    DEFAULT_SKIP_INBOX,
    get_gtd_rules,
)


class _RuleSet:
    """Source-tag → defaults lookup (assignee / contexts / skip-inbox). Rules are
    DB-overridable from the admin UI; the maps default to the shipped defaults in
    aegis.services.gtd_rules. Methods accept a possibly-None tag and return a
    sensible fallback so callers don't have to branch.
    """

    def __init__(
        self,
        assignee: dict | None = None,
        contexts: dict | None = None,
        skip_inbox: dict | None = None,
    ):
        self._ASSIGNEE = DEFAULT_ASSIGNEE if assignee is None else assignee
        self._CONTEXTS = DEFAULT_CONTEXTS if contexts is None else contexts
        self._SKIP_INBOX = DEFAULT_SKIP_INBOX if skip_inbox is None else skip_inbox

    def default_assignee(self, source_tag: str | None) -> str:
        return self._ASSIGNEE.get(source_tag or "", "@me")

    def default_contexts(self, source_tag: str | None) -> list[str]:
        return self._CONTEXTS.get(source_tag or "", ["@deep"])

    def skip_inbox(self, source_tag: str | None) -> str | None:
        return self._SKIP_INBOX.get(source_tag or "")


_RULES = _RuleSet()  # shipped defaults — used as the no-pool sync fallback

# 30s-cached resolve of the effective (DB-overridable) ruleset.
_gtd_cache: dict = {"rs": None, "ts": 0.0}


async def get_gtd_ruleset(pool) -> _RuleSet:
    """Effective ruleset, DB-first (merged over defaults), 30s cache. Falls back
    to the shipped defaults when there's no pool or the read fails."""
    if pool is None:
        return _RULES
    import time

    now = time.monotonic()
    if _gtd_cache["rs"] is not None and now - _gtd_cache["ts"] < 30.0:
        return _gtd_cache["rs"]
    try:
        r = await get_gtd_rules(pool)
        rs = _RuleSet(r["assignee"], r["contexts"], r["skip_inbox"])
    except Exception:  # noqa: BLE001 — never let a config read break classification
        rs = _RULES
    _gtd_cache.update(rs=rs, ts=now)
    return rs


# 30s-cached content routes (regex/prefix/contains on task title → assignee /
# labels / gate). Ships empty; each deployment configures its own from the admin
# UI. Empty list without a pool or on read failure — routing must never break
# classification.
_routes_cache: dict = {"routes": None, "ts": 0.0}


async def get_content_routes(pool) -> list[dict]:
    """Effective content routes, 30s cache. [] without a pool or on read failure."""
    if pool is None:
        return []
    import time

    now = time.monotonic()
    if _routes_cache["routes"] is not None and now - _routes_cache["ts"] < 30.0:
        return _routes_cache["routes"]
    routes = await _get_content_routes_db(pool)
    _routes_cache.update(routes=routes, ts=now)
    return routes


# Per-agent addressable labels. classify_one's per-agent short-circuit
# (added 2026-05-26) routes user comments on @<agent>-labelled tasks to
# the matching personality. Iteration order is documented + deterministic:
# @sebas wins co-occurrence with @raphael or @maou. @pandora-bearing
# tasks bypass this block — the Jira route stays sacred (Branch 2 at
# the `if "@pandora" in existing_labels` block owns all @pandora
# routing, including the non-APP `pandora_chat_followup` branch added
# 2026-05-27 so user comments on manual @pandora-labelled tasks reach
# pandoras-actor instead of dead-ending in pandora_owned).
#
# The addressable list + assignee vocabulary + context-hook gating are all
# DERIVED from the active agents (issue #36): mention_aliases give the labels,
# capabilities give the behavior tag that picks the context pre-fetch. The
# literals below are the shipped-seed fallback when there's no DB / the read
# fails — behavior stays identical for the default 4-agent set.
_DEFAULT_AGENT_REG: dict[str, dict] = {
    "sebas": {"aliases": ["@sebas"], "caps": {"gtd"}},
    "raphael": {"aliases": ["@raphael"], "caps": {"research"}},
    "maou": {"aliases": ["@maou"], "caps": {"finance"}},
    "pandoras-actor": {"aliases": ["@pandora"], "caps": {"infra"}},
}

_agent_reg_cache: dict = {"reg": None, "ts": 0.0}


def _decode_jsonish(value, empty):
    """asyncpg returns jsonb as a Python object when the codec is registered,
    else a raw string. Accept both; fall back to `empty` on anything odd."""
    if value is None:
        return empty
    if isinstance(value, (dict, list)):
        return value
    try:
        import json

        return json.loads(value)
    except Exception:  # noqa: BLE001
        return empty


async def get_agent_registry(pool) -> dict[str, dict]:
    """Active agents as {id: {"aliases": [@label...], "caps": {tag...}}}, 30s
    cached. Aliases come from metadata.mention_aliases (default [id]); caps from
    the capabilities column. Falls back to the shipped defaults without a pool
    or on read failure — routing must never break."""
    if pool is None:
        return _DEFAULT_AGENT_REG
    import time

    now = time.monotonic()
    if _agent_reg_cache["reg"] is not None and now - _agent_reg_cache["ts"] < 30.0:
        return _agent_reg_cache["reg"]
    try:
        rows = await pool.fetch("SELECT id, capabilities, metadata FROM agents WHERE active = TRUE")
        reg: dict[str, dict] = {}
        for r in rows:
            md = _decode_jsonish(r["metadata"], {})
            caps = set(_decode_jsonish(r["capabilities"], []))
            raw_aliases = md.get("mention_aliases") or [r["id"]]
            aliases = [f"@{str(a).lstrip('@')}" for a in raw_aliases]
            reg[r["id"]] = {"aliases": aliases, "caps": caps}
        reg = reg or _DEFAULT_AGENT_REG
    except Exception:  # noqa: BLE001 — never let a config read break classification
        reg = _DEFAULT_AGENT_REG
    _agent_reg_cache.update(reg=reg, ts=now)
    return reg


def _addressable_agents(reg: dict[str, dict]) -> list[tuple[str, str]]:
    """(@label, "<id>_followup") pairs from the registry. Ordered gtd-owner
    first, then by id, so the GTD owner wins co-occurrence (preserves the old
    '@sebas wins' guarantee). @pandora-labelled tasks bypass this list upstream,
    so including the infra agent here is inert."""
    out: list[tuple[str, str]] = []
    for aid in sorted(reg, key=lambda a: (0 if "gtd" in reg[a]["caps"] else 1, a)):
        for label in reg[aid]["aliases"]:
            out.append((label, f"{aid}_followup"))
    return out


def _assignee_labels(reg: dict[str, dict]) -> list[str]:
    """Valid classifier assignee labels: @me plus every agent alias."""
    labels = ["@me"]
    for info in reg.values():
        labels.extend(info["aliases"])
    return labels


# GTD state labels (Todoist restructure, 2026-07): Someday/Later and Next
# used to be managed projects; both are now labels applied in apply_outcome
# instead of an item_move. Mirrors aegis_worker.activities.review._STATE_LABELS
# / _LABEL_SOMEDAY / _LABEL_NEXT (cross-package; keep in sync).
_LABEL_SOMEDAY = "@someday"
_LABEL_NEXT = "@next"


import asyncpg  # noqa: E402
from aegis.clarify_note import (  # noqa: E402
    AGENT_REPLY_ERROR_SQL_LIKE,
    AGENT_REPLY_PREFIX,
    AGENT_REPLY_SQL_LIKE,
    CLARIFY_NOTE_PREFIX,
    CLARIFY_NOTE_SQL_LIKE,
)
from temporalio import activity  # noqa: E402, F401  # used by appended methods in Tasks 8-11

from aegis_worker.activities.delivery import safe_send_message  # noqa: E402


@dataclass
class ClarifyActivities:
    """Phase 3 clarify activities. find_unclassified_items / classify_one /
    apply_outcome / log_classification are appended by Tasks 8-11.
    """

    db_pool: asyncpg.Pool | None
    todoist_connector: object | None = None
    llm_client: object | None = None
    # Phase 5 reference store: knowledge-service connector, wired by
    # worker boot. None in unit tests that don't exercise ingest.
    knowledge_connector: object | None = None
    # references-as-knowledge: raphael's per-message chat path uses
    # DeliveryActivities to talk to the comms delivery server. Wired
    # in worker boot; None in unit tests that don't exercise notifications.
    delivery_connector: object | None = None
    sonnet_model: str = "claude-sonnet"
    # primary_model defaults to qwen3:14b but the worker boot wires
    # settings.model_balanced into it so operators can flip via
    # AEGIS_MODEL_BALANCED=gemma4:e2b (etc.) without a code change.
    primary_model: str = "qwen3:14b"
    escalation_threshold: float = 0.7

    @activity.defn
    async def find_unclassified_items(self, max_items: int = 20) -> list[dict]:
        """Return Inbox tasks that need (re)classification.

        A task is unclassified if last_clarified_at IS NULL, or if a
        USER-authored note has been posted since the last classification
        (MAX(posted_at) over notes that don't match `[ClarifyFlow @`,
        `[Agent reply @`, or `%Workflow run:%` — the same AEGIS-author
        exclusions the latest_user_note subquery uses). Raw last_note_at
        is not used for eligibility — apply_sync_diff bumps it on every
        note including agent replies, which would create a 15-min reply
        loop (loop fix 2026-05-27, commit cb7fce6e).

        When gtd_clarify_enabled=false this returns [] so no downstream
        activity runs — flipping the switch must NOT post NEEDS REVIEW
        comments or bump last_clarified_at (which would poison the
        watermark and prevent re-clarify when the switch flips back on).
        """
        if self.db_pool is None:
            return []
        if not await self._settings_bool("gtd_clarify_enabled", True):
            return []
        # Content-route patterns for the eligibility filter ($6). Fetched outside
        # the connection block; empty when no routes are configured, in which
        # case `t.content ~ ANY('{}')` admits nothing extra (only source-tagged +
        # agent-labelled tasks reach the classifier — preserving old behavior).
        patterns = active_patterns(await get_content_routes(self.db_pool))
        async with self.db_pool.acquire() as conn:
            inbox_id = await conn.fetchval(
                "SELECT value->>'inbox' FROM settings WHERE key = 'todoist_managed_project_ids'"
            )
            if not inbox_id:
                return []
            # Source-tagged tasks are the standard clarify input. Tasks whose
            # title matches a content route (e.g. issue-tracker tickets synced by
            # Todoist's native integration, not AEGIS capture) arrive without any
            # source_tag — the `t.content ~ ANY($6)` branch below lets the
            # content-route classifier see them so routing still fires.
            rows = await conn.fetch(
                """
                SELECT
                    t.id,
                    t.content,
                    t.description,
                    t.labels,
                    t.source_tag,
                    t.project_id,
                    t.last_note_at,
                    (
                        -- Filter AEGIS-authored notes from this "latest
                        -- user note" lookup. Two patterns:
                        --   * ClarifyFlow tags its own comments with
                        --     `[ClarifyFlow @ `.
                        --   * Every AlertInvestigationFlow comment carries
                        --     `Workflow run: ` as a footer marker (start,
                        --     verdict, PR-opened, fix-discarded, etc.).
                        -- Without the second filter, Pandora's own progress
                        -- comments re-trigger pandora_followup every tick
                        -- (caught 2026-05-21 — 5 tasks looped at 12:00
                        -- because 11:45 spawn-comments bumped last_note_at).
                        SELECT content FROM todoist_notes
                        WHERE item_id = t.id
                          AND content NOT LIKE $3
                          AND content NOT LIKE $4
                          AND content NOT LIKE '%Workflow run:%'
                        ORDER BY posted_at DESC
                        LIMIT 1
                    ) AS latest_user_note
                FROM todoist_tasks t
                WHERE t.project_id = $1
                  AND NOT t.is_completed
                  AND (
                      t.last_clarified_at IS NULL
                      -- Eligibility filter (2026-05-27): compare against the
                      -- latest USER-authored note's posted_at, not raw
                      -- last_note_at — otherwise AgentChatReplyFlow's own
                      -- reply (success OR error) re-eligibles the task and
                      -- the next clarify tick spawns ANOTHER reply, in a
                      -- 15-min loop. Caught in prod with 3 @pandora tasks
                      -- 2026-05-27 — pandora burned ~9 redundant
                      -- claude-sonnet runs / hour each. Mirror the same
                      -- AEGIS-author exclusion the latest_user_note
                      -- subquery already uses.
                      OR (
                          SELECT MAX(posted_at) FROM todoist_notes
                          WHERE item_id = t.id
                            AND content NOT LIKE $3
                            AND content NOT LIKE $4
                            AND content NOT LIKE '%Workflow run:%'
                      ) > t.last_clarified_at
                      -- Pandora retry surface (2026-05-21): @pandora APP-<n>:
                      -- tasks whose prior AlertInvestigationFlow never
                      -- completed get re-surfaced so classify_one's
                      -- retry branch can fire. Throttled to 1h between
                      -- attempts via last_clarified_at so a chronically-
                      -- failing investigation doesn't loop every tick.
                      OR (
                          '@pandora' = ANY(t.labels)
                          AND t.content ~ ANY($6)
                          AND COALESCE(t.last_clarified_at, 'epoch'::timestamptz)
                              < NOW() - INTERVAL '1 hour'
                          AND NOT EXISTS (
                              SELECT 1 FROM workflow_runs wr
                              WHERE wr.workflow_type = 'AlertInvestigationFlow'
                                AND wr.status = 'completed'
                                AND wr.workflow_id LIKE '%' || t.id || '%'
                          )
                      )
                  )
                  AND (
                      t.source_tag IS NOT NULL
                      OR t.content ~ ANY($6)
                      -- Comment-channel (2026-05-26): user-created Todoist
                      -- tasks labelled with an addressable agent need to be
                      -- eligible too, otherwise the @sebas/@raphael/@maou
                      -- followup short-circuit in classify_one never runs
                      -- on them and the user's comment is silently dropped.
                      -- @pandora is already covered by APP- but listed for
                      -- symmetry — and to support manual @pandora-labelled
                      -- tasks (rare but possible).
                      OR t.labels && ARRAY['@sebas','@raphael','@maou','@pandora']
                  )
                  -- Pandora cooldown (2026-05-22): independent of the
                  -- content-based bump filters in todoist.py +
                  -- latest_user_note subquery above, force a 30-min gap
                  -- between investigation spawns per @pandora APP- task.
                  -- Defence-in-depth: if any AEGIS comment shape ever
                  -- bypasses the `Workflow run:` filter and bumps
                  -- last_note_at, this still blocks the runaway. Counts
                  -- any AlertInvestigationFlow row (running OR completed)
                  -- so a still-in-flight investigation doesn't get a
                  -- concurrent sibling.
                  AND NOT (
                      '@pandora' = ANY(t.labels)
                      AND t.content ~ ANY($6)
                      AND EXISTS (
                          SELECT 1 FROM workflow_runs wr
                          WHERE wr.workflow_type = 'AlertInvestigationFlow'
                            AND wr.workflow_id LIKE '%' || t.id || '%'
                            AND wr.started_at > NOW() - INTERVAL '30 minutes'
                      )
                  )
                  -- Chat-reply error cooldown (2026-06-04): when the most
                  -- recent agent reply on a comment-channel task ERRORED, the
                  -- parent rolls back the watermark, so the original user note
                  -- keeps re-qualifying the task and AgentChatReplyFlow
                  -- re-spawns (and re-errors) every tick, posting an
                  -- `[Agent reply @ ... ERROR]` note each time. Suppress the
                  -- task for 30 min after an error — UNLESS a newer user note
                  -- has arrived since (genuine new input must still be
                  -- processed, so we compare against the latest user note).
                  AND NOT EXISTS (
                      SELECT 1 FROM todoist_notes n
                      WHERE n.item_id = t.id
                        AND n.content LIKE $5
                        AND n.posted_at > NOW() - INTERVAL '30 minutes'
                        AND n.posted_at >= COALESCE((
                            SELECT MAX(posted_at) FROM todoist_notes
                            WHERE item_id = t.id
                              AND content NOT LIKE $3
                              AND content NOT LIKE $4
                              AND content NOT LIKE '%Workflow run:%'
                        ), 'epoch'::timestamptz)
                  )
                  -- Hands-off signal (inbox gate): a task the user has claimed
                  -- with @me — and hasn't addressed to an agent — is theirs to
                  -- handle. Clarify ignores it entirely so aegis won't
                  -- auto-classify or auto-investigate. This is the escape
                  -- hatch: label a task @me in Todoist (or click "I've got it"
                  -- on the gate card) and it drops out here. Agent-addressed
                  -- tasks (@sebas/@raphael/@maou/@pandora) still pass so the
                  -- comment-channel reply path keeps firing even if @me
                  -- co-occurs. @me is never auto-applied by Jira sync (labels
                  -- come verbatim from Todoist), so fresh APP-<n>: tickets have
                  -- none and still reach the gate.
                  AND NOT (
                      '@me' = ANY(t.labels)
                      AND NOT (t.labels && ARRAY['@sebas','@raphael','@maou','@pandora'])
                  )
                ORDER BY t.last_note_at DESC NULLS LAST, t.updated_at
                LIMIT $2
                """,
                inbox_id,
                max_items,
                CLARIFY_NOTE_SQL_LIKE,
                AGENT_REPLY_SQL_LIKE,
                AGENT_REPLY_ERROR_SQL_LIKE,
                patterns,
            )
        return [dict(r) for r in rows]

    async def _has_completed_pandora_investigation(self, task_id: str) -> bool:
        """Return True iff at least one AlertInvestigationFlow run for
        this Todoist task id has completed cleanly.

        Used by classify_one to break the pandora_owned dead-end when a
        prior investigation crashed (e.g. assess_investigation LLM timeout).
        Returns True (conservative — no retry) on any DB error so we never
        spin a tight loop when the DB itself is the cause.
        """
        if self.db_pool is None or not task_id:
            return True
        try:
            async with self.db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT 1
                    FROM workflow_runs
                    WHERE workflow_type = 'AlertInvestigationFlow'
                      AND status = 'completed'
                      AND workflow_id LIKE $1
                    LIMIT 1
                    """,
                    # Match both the legacy `pandora-jira-<id>-…` and the new
                    # `investigation-<id>-…` workflow-id schemes by task id.
                    f"%{task_id}%",
                )
            return row is not None
        except Exception:
            return True

    async def _settings_bool(self, key: str, default: bool) -> bool:
        if self.db_pool is None:
            return default
        async with self.db_pool.acquire() as conn:
            raw = await conn.fetchval("SELECT value FROM settings WHERE key=$1", key)
        if raw is None:
            return default
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, dict):
            v = raw.get("value")
            if isinstance(v, bool):
                return v
        return default

    def _build_classify_prompt(
        self, task: dict, rules: _RuleSet, assignees: list[str] | None = None
    ) -> str:
        """Compose the JSON-output prompt for qwen3:14b / Sonnet."""
        source_tag = task.get("source_tag")
        rule_assignee = rules.default_assignee(source_tag)
        rule_contexts = rules.default_contexts(source_tag)
        title = task.get("content") or ""
        description = task.get("description") or ""
        labels = task.get("labels") or []
        user_note = task.get("latest_user_note")
        hint_block = ""
        if user_note:
            hint_block = (
                f"\nThe user added this comment on a prior classification:\n"
                f"> {user_note}\n"
                f"Take this as authoritative supervision.\n"
            )
        return (
            "You're a GTD clarify assistant. Classify this Inbox task and "
            "return JSON ONLY (no prose, no markdown).\n\n"
            f"Title: {title}\n"
            f"Description: {description}\n"
            f"Source: {source_tag}\n"
            f"Existing labels: {labels}\n"
            f"Rule suggestions:\n"
            f"  - assignee: {rule_assignee}\n"
            f"  - contexts: {rule_contexts}\n"
            f"{hint_block}"
            "\nOutput JSON: "
            '{"classification": str, "confidence": float, '
            '"assignee": str, "contexts": [str], "reason": str}\n'
            "classification ∈ {trash, reference, someday, 2_min, "
            "next_action}\n"
            "confidence ∈ [0.0, 1.0]\n"
            f"assignee ∈ {{{', '.join(assignees or ['@me', '@sebas', '@raphael', '@maou', '@pandora'])}}}\n"
            "contexts ⊆ {@5min, @deep, @email, @phone, @code, @errand, "
            "@home, @office, @reading, @waiting, @reference}"
        )

    def _unpack_think_result(self, result) -> tuple[dict, int | None, int | None]:
        """Normalize an LLMClient.think() result.

        think() returns a dict {response, model, prompt_tokens,
        completion_tokens}, but unit tests mock it as a bare string — accept
        both shapes. Returns (parsed_classification_json_or_{}, prompt_tokens,
        completion_tokens); token counts are None for the bare-string shape.
        """
        if isinstance(result, dict):
            raw = result.get("response", "")
            prompt_tokens = result.get("prompt_tokens")
            completion_tokens = result.get("completion_tokens")
        else:
            raw = result
            prompt_tokens = None
            completion_tokens = None
        return parse_llm_json(raw) or {}, prompt_tokens, completion_tokens

    @activity.defn
    async def classify_one(self, task: dict) -> dict:
        """Run the clarify decision for a single task.

        Returns a dict ready for log_classification + apply_outcome:
            {classification, confidence, assignee, contexts, reason,
             llm_model, prompt_tokens, completion_tokens, latency_ms}
        """
        if not await self._settings_bool("gtd_clarify_enabled", True):
            return {
                "classification": "skipped",
                "confidence": 0.0,
                "assignee": None,
                "contexts": [],
                "reason": "kill_switch_off",
                "llm_model": "none",
            }

        source_tag = task.get("source_tag")
        rules = await get_gtd_ruleset(self.db_pool)
        reg = await get_agent_registry(self.db_pool)
        routes = await get_content_routes(self.db_pool)

        # Pandora ownership short-circuit. AlertInvestigationFlow may
        # create inbox tasks with @pandora pre-applied; clarify must not
        # re-classify them. log_classification still fires so
        # last_clarified_at gets bumped and the task exits the
        # find_unclassified_items watermark.
        #
        # Followup branch (2026-05-21): if the user posted a comment AFTER
        # the @pandora label landed (latest_user_note is set, which means
        # find_unclassified_items saw a user-authored note newer than
        # last_clarified_at — post-2026-05-27 the eligibility filter
        # ignores agent replies, so this signal is genuinely a user
        # follow-up), the user is adding context — fire a fresh
        # investigation that includes the comment instead of silently
        # dropping the signal. Watermark-poisoning is prevented in
        # apply_outcome by gating the spawn on a new fingerprint per
        # comment.
        existing_labels = list(task.get("labels") or [])
        content_for_branch = task.get("content") or ""

        # Per-agent comment-channel short-circuit (2026-05-26).
        # Fires only when:
        #   - a fresh user comment is present (latest_user_note non-empty),
        #   - @pandora is NOT in labels (pandora keeps priority for its
        #     content-route workflow + own pipeline),
        #   - content does NOT match a content route (content routing wins).
        # The classification result spawns AgentChatReplyFlow downstream.
        latest_note_for_branch = (task.get("latest_user_note") or "").strip()
        if (
            latest_note_for_branch
            and "@pandora" not in existing_labels
            and match_route(content_for_branch, routes) is None
        ):
            for label, branch in _addressable_agents(reg):
                if label in existing_labels:
                    return {
                        "classification": branch,
                        "confidence": 1.0,
                        "assignee": label,
                        "contexts": ["@deep"],
                        "reason": f"user comment on {label}-addressed task",
                        "llm_model": "rules",
                    }

        if "@pandora" in existing_labels:
            latest_note = (task.get("latest_user_note") or "").strip()
            content = task.get("content") or ""
            if latest_note and match_route(content, routes) is not None:
                return {
                    "classification": "pandora_followup",
                    "confidence": 1.0,
                    "assignee": "@pandora",
                    "contexts": ["@deep", "@code"],
                    "reason": "user comment on @pandora content-route task",
                    "llm_model": "rules",
                }
            # Retry branch (2026-05-21): a content-route task labelled @pandora
            # with NO successful AlertInvestigationFlow completion in its
            # history means a prior investigation crashed (most commonly
            # qwen3:14b LLM timeouts during assess_investigation). Without
            # this branch the task stays pandora_owned forever — the watermark
            # bumps on the no-op, find_unclassified_items skips it, and only
            # a user comment can trigger a fresh run. Re-route to
            # pandora_investigation so the spawn fires again; AlertInvestigation
            # dedup keys on `audit_log.alert_investigated` which is only
            # written on success, so re-running a failed investigation isn't
            # blocked.
            if match_route(content, routes) is not None:
                investigated = await self._has_completed_pandora_investigation(task["id"])
                if not investigated:
                    return {
                        "classification": "pandora_investigation",
                        "confidence": 1.0,
                        "assignee": "@pandora",
                        "contexts": ["@deep", "@code"],
                        "reason": "@pandora content-route task with no successful prior investigation — retrying",
                        "llm_model": "rules",
                    }
            # Comment-channel branch (2026-05-27): a manual @pandora-labelled
            # inbox task (no APP-<n>: prefix) with a fresh user comment.
            # Before this branch the task fell through to pandora_owned and
            # the comment was silently dropped — find_unclassified_items
            # admitted the task (PR #262 added @pandora to the labels
            # filter) but no spawn fired. Route to AgentChatReplyFlow via
            # the shared `agent_chat_reply` spawn so pandoras-actor handles
            # the reply with its own tool set.
            if latest_note:
                return {
                    "classification": "pandora_chat_followup",
                    "confidence": 1.0,
                    "assignee": "@pandora",
                    "contexts": ["@deep"],
                    "reason": "user comment on @pandora non-APP task",
                    "llm_model": "rules",
                }
            return {
                "classification": "pandora_owned",
                "confidence": 1.0,
                "assignee": "@pandora",
                "contexts": [],
                "reason": "task already labelled @pandora",
                "llm_model": "rules",
            }

        # Content-route branch. First encounter (no @pandora label yet — that
        # case returned in the @pandora block above). A `gate: true` route
        # DOESN'T auto-fire an investigation: ask first via a choice card
        # (pandora_gate). Picking "investigate" applies the route's assignee,
        # which re-enters the @pandora retry branch above on the next tick and
        # fires the real AlertInvestigationFlow; "I've got it" applies @me and
        # clarify leaves the task alone. A `gate: false` route just applies the
        # route's assignee + contexts directly (route_apply) — plain label
        # routing, no card, no agent run. (inbox gate — replaces the old
        # hardcoded Acme APP-<n>: auto-dispatch.)
        content = task.get("content") or ""
        matched = match_route(content, routes)
        if matched is not None:
            if matched.get("gate", True):
                return {
                    "classification": "pandora_gate",
                    "confidence": 1.0,
                    "assignee": matched.get("assignee") or "@pandora",
                    "contexts": list(matched.get("contexts") or ["@deep", "@code"]),
                    "reason": f"content route {matched.get('key')!r} — ask before investigating",
                    "llm_model": "rules",
                }
            return {
                "classification": "route_apply",
                "confidence": 1.0,
                "assignee": matched.get("assignee") or "@me",
                "contexts": list(matched.get("contexts") or []),
                "reason": f"content route {matched.get('key')!r} — apply labels",
                "llm_model": "rules",
            }

        # Rule short-circuit: deterministic routes (e.g. #research → reference)
        rule_outcome = rules.skip_inbox(source_tag)
        if rule_outcome is not None:
            return {
                "classification": rule_outcome,
                "confidence": 1.0,
                "assignee": rules.default_assignee(source_tag),
                "contexts": rules.default_contexts(source_tag),
                "reason": f"rule skip_inbox({source_tag!r}) → {rule_outcome}",
                "llm_model": "rules",
            }

        if self.llm_client is None:
            return {
                "classification": "skipped",
                "confidence": 0.0,
                "assignee": None,
                "contexts": [],
                "reason": "no_llm_client",
                "llm_model": "none",
            }

        prompt = self._build_classify_prompt(task, rules, _assignee_labels(reg))

        # Primary classifier. LLMClient.think() returns a dict
        # {response, model, prompt_tokens, completion_tokens} — extract
        # the raw text from .response. Unit tests mocked think() to
        # return a bare string, so accept both shapes defensively.
        primary_result = await self.llm_client.think(prompt, model=self.primary_model)
        primary, primary_pt, primary_ct = self._unpack_think_result(primary_result)
        primary_conf = float(primary.get("confidence") or 0.0)
        best = {
            "classification": primary.get("classification") or "unknown",
            "confidence": primary_conf,
            "assignee": primary.get("assignee") or rules.default_assignee(source_tag),
            "contexts": primary.get("contexts") or rules.default_contexts(source_tag),
            "reason": primary.get("reason") or "",
            "llm_model": self.primary_model,
            "prompt_tokens": primary_pt,
            "completion_tokens": primary_ct,
        }

        # Escalate to Sonnet on low confidence; keep whichever wins on confidence.
        if primary_conf < self.escalation_threshold:
            sonnet_result = await self.llm_client.think(prompt, model=self.sonnet_model)
            sonnet, sonnet_pt, sonnet_ct = self._unpack_think_result(sonnet_result)
            sonnet_conf = float(sonnet.get("confidence") or 0.0)
            if sonnet_conf >= primary_conf:
                best = {
                    "classification": sonnet.get("classification") or best["classification"],
                    "confidence": sonnet_conf,
                    "assignee": sonnet.get("assignee") or best["assignee"],
                    "contexts": sonnet.get("contexts") or best["contexts"],
                    "reason": sonnet.get("reason") or "",
                    "llm_model": self.sonnet_model,
                    "prompt_tokens": sonnet_pt,
                    "completion_tokens": sonnet_ct,
                }

        return best

    async def _settings_str(self, key: str, default: str) -> str:
        if self.db_pool is None:
            return default
        async with self.db_pool.acquire() as conn:
            raw = await conn.fetchval("SELECT value FROM settings WHERE key=$1", key)
        if isinstance(raw, str):
            return raw
        return default

    def _in_time_window(self, tz_name: str, now: object | None) -> bool:
        ref = now if now is not None else datetime.now(ZoneInfo("UTC"))
        local = ref.astimezone(ZoneInfo(tz_name))
        return time(8) <= local.time() < time(22)

    def _format_apply_note(self, decision: dict, pass_n: int, now) -> str:
        ref = now if now is not None else datetime.now(ZoneInfo("UTC"))
        ts = ref.astimezone(ZoneInfo("UTC")).strftime("%H:%M UTC")
        contexts = " ".join(decision.get("contexts") or [])
        confidence = float(decision.get("confidence") or 0.0)
        assignee = decision.get("assignee") or "@me"
        classification = decision.get("classification") or "unknown"
        reason = decision.get("reason") or "(none)"
        # Machine prefix from CLARIFY_NOTE_PREFIX. The comment-loop guards
        # in apply_sync_diff + webhook receiver + find_unclassified_items
        # all key on this prefix to suppress self-bumps of last_note_at.
        # Sebas's voice lives inside the body so the user sees the
        # butler addressing them, not a bare log line.
        return (
            f"{CLARIFY_NOTE_PREFIX}{ts} · pass {pass_n}]\n"
            f"🎩 Sebas to Master — classified as **{classification}** "
            f"for {assignee} ({contexts}).\n"
            f"Reason: {reason}\n"
            f"Model: {decision.get('llm_model')} (confidence {confidence:.2f})"
        )

    def _format_review_note(self, decision: dict, pass_n: int, now) -> str:
        ref = now if now is not None else datetime.now(ZoneInfo("UTC"))
        ts = ref.astimezone(ZoneInfo("UTC")).strftime("%H:%M UTC")
        contexts = " ".join(decision.get("contexts") or [])
        confidence = float(decision.get("confidence") or 0.0)
        classification = decision.get("classification") or "unknown"
        return (
            f"{CLARIFY_NOTE_PREFIX}{ts} · pass {pass_n} · NEEDS REVIEW]\n"
            f"🎩 Sebas to Master — I am uncertain about this one. "
            f"Best guess: **{classification}** ({contexts}), "
            f"confidence {confidence:.2f}.\n"
            f"A choice card awaits in chat; alternatively, reply here.\n"
            f"Model: {decision.get('llm_model')}"
        )

    def _build_2min_interaction_payload(self, task: dict, decision: dict, pass_n: int) -> dict:
        title = (task.get("content") or "").strip()[:120]
        source_tag = decision.get("source_tag") or task.get("source_tag") or ""
        confidence = float(decision.get("confidence") or 0.0)
        return {
            "flavor": "2_min",
            "prompt": (f"⏱ 2-min: {title}\nSource: {source_tag}   Conf: {confidence:.2f}"),
            "options": {
                "do_now": "✅ Do now",
                "defer_1d": "📅 Defer 1d",
                "trash": "🗑 Trash",
            },
            "decision": decision,
            "pass_n": pass_n,
        }

    @staticmethod
    def _pandora_alert_payload(
        content: str,
        description: str,
        fingerprint: str,
        item_id: str,
        *,
        service: str | None = None,
        resource_tags: list[str] | None = None,
    ) -> dict:
        """Build the AlertInvestigationFlow spawn payload for a content-route
        investigation. Shared by the pandora_investigation and pandora_followup
        branches — they differ only in `description`/`fingerprint`; `service`
        and `resource_tags` come from the matched content route (both optional —
        omit to let the investigation repo-match unscoped).

        `source` stays "todoist-jira": AlertInvestigationFlow treats that value
        as a scoping-only contract (investigate + comment, never an autonomous
        PR) — the right default for a gated inbox work ticket.
        """
        labels: dict = {"alertname": content[:100]}
        alert: dict = {
            "title": content[:200],
            "description": description[:2000],
            "source": "todoist-jira",
            "severity": "normal",
            "fingerprint": fingerprint,
            "labels": labels,
            "requires_approval": False,
            "todoist_task_id": item_id,
        }
        if service:
            alert["service"] = service
            labels["service"] = service
        if resource_tags:
            alert["resource_tag_filter"] = list(resource_tags)
        return {"spawn_kind": "pandora_investigation", "alert": alert}

    def _build_agent_synthetic_input(
        self, task: dict, agent_id: str, recent_notes: list[dict] | None = None
    ) -> str:
        """Compose the synthetic user message for AgentChatReplyFlow.

        Format is stable: send_message picks up tool selection from
        AGENT_TOOL_SETS[agent_id] and personality from the SOUL/USER/MEMORY
        files; the synthetic body just frames what the user wants.

        `recent_notes` (oldest-first) is the recent comment thread on
        the task — both user comments AND prior agent replies (every
        AEGIS-authored note already carries its `[Agent reply @ ...
        agent=NAME]` / `[ClarifyFlow @ ...]` prefix or `Workflow run:`
        footer, so the agent sees who said what). Including this
        transcript prevents the agent from re-answering a question
        it already addressed in a prior tick — important when a task
        cycles through clarify multiple times before settling
        (genuine multi-turn user followups, or any case where the
        chat_history thread on `todoist-task-<id>` isn't authoritative).
        """
        title = (task.get("content") or "").strip()
        description = (task.get("description") or "").strip()
        comment = (task.get("latest_user_note") or "").strip()

        transcript = ""
        if recent_notes:
            lines = []
            for n in recent_notes:
                posted = n.get("posted_at")
                ts = posted.strftime("%Y-%m-%d %H:%M UTC") if posted is not None else "?"
                body = (n.get("content") or "")[:800]
                lines.append(f"[{ts}] {body}")
            transcript = (
                "\n\nRecent comment thread on this task (oldest first — "
                "your own prior replies appear with `agent=<name>` in their "
                "header, so do NOT repeat advice you already gave):\n" + "\n".join(lines)
            )
            transcript += (
                "\n\nThe bracketed turn markers above (e.g. `[Agent reply @ ...]`, "
                "`[ClarifyFlow @ ...]`, `Workflow run: ...`) are internal context "
                "only — do NOT reproduce or echo them in your reply. Write only your "
                "new response."
            )

        return (
            f"User commented on Todoist task {task['id']}:\n"
            f"Title: {title}\n"
            f"Description: {description}"
            f"{transcript}\n\n"
            f"Their latest comment:\n> {comment}"
        )

    async def _fetch_recent_task_notes(self, task_id: str, limit: int = 15) -> list[dict]:
        """Return up to `limit` most-recent notes on a Todoist task,
        oldest-first. Includes user notes AND prior agent-reply notes
        so the agent can see what it already said (each `[Agent reply @
        ...]` note carries its own author marker). Excludes
        `[ClarifyFlow @ ...]` machine notes and `Workflow run:` digests
        — those are noise the agent doesn't need to see and used to
        dominate the prompt for tasks that cycled through clarify
        multiple times. The filter happens in SQL so `LIMIT` returns
        `limit` USEFUL rows (pre-2026-05-28 the filter ran in app
        layer AFTER LIMIT, which often left a transcript of only
        machine notes). Best-effort: returns [] on DB failure so
        synthetic input still works without history.
        """
        if self.db_pool is None or not task_id:
            return []
        try:
            async with self.db_pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT posted_at, content
                    FROM (
                        SELECT posted_at, content
                        FROM todoist_notes
                        WHERE item_id = $1
                          AND content NOT LIKE $3
                          AND content NOT LIKE '%Workflow run:%'
                        ORDER BY posted_at DESC
                        LIMIT $2
                    ) s
                    ORDER BY posted_at ASC
                    """,
                    task_id,
                    limit,
                    CLARIFY_NOTE_SQL_LIKE,
                )
                return [dict(r) for r in rows]
        except Exception as exc:  # noqa: BLE001 — best-effort
            activity.logger.warning(
                "agent_chat_recent_notes_fetch_failed task_id=%s err=%s",
                task_id,
                str(exc)[:200],
            )
            return []

    async def _maybe_attach_ks_context(self, synthetic_input: str, task: dict) -> str:
        """Raphael pre-fetch hook. Best-effort: failures fall through to
        the bare synthetic input (logged at WARN, never raised).
        """
        if self.knowledge_connector is None:
            return synthetic_input
        title = (task.get("content") or "").strip()
        if not title:
            return synthetic_input
        try:
            results = await self.knowledge_connector.search(query=title, limit=3)
        except Exception as exc:  # noqa: BLE001 — best-effort
            activity.logger.warning(
                "agent_chat_ks_prefetch_failed task_id=%s err=%s",
                task.get("id"),
                str(exc)[:200],
            )
            return synthetic_input
        if not results:
            return synthetic_input
        rendered = "\n".join(
            f"- {r.get('title') or r.get('url') or '<untitled>'}" for r in results[:3]
        )
        return synthetic_input + f"\n\nExisting knowledge:\n{rendered}\n"

    async def _maybe_attach_transaction_context(self, synthetic_input: str, task: dict) -> str:
        """Maou pre-fetch hook. Best-effort: failures fall through to the
        bare synthetic input. Fires on '#receipt' source_tag tasks; pulls
        the 5 most recent receipts from maou.receipt_email so the agent
        can correlate the new task with what's already in the receipt
        history (vendor, amount, date).
        """
        if (task.get("source_tag") or "") != "#receipt":
            return synthetic_input
        if self.db_pool is None:
            return synthetic_input
        try:
            async with self.db_pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT received_at,
                           sender,
                           COALESCE(parsed->>'amount', '?') AS amount,
                           COALESCE(parsed->>'currency', '?') AS currency
                    FROM maou.receipt_email
                    ORDER BY received_at DESC
                    LIMIT 5
                    """
                )
        except Exception as exc:  # noqa: BLE001 — best-effort
            activity.logger.warning(
                "agent_chat_tx_prefetch_failed task_id=%s err=%s",
                task.get("id"),
                str(exc)[:200],
            )
            return synthetic_input
        if not rows:
            return synthetic_input
        rendered = "\n".join(
            f"- {r['received_at'].date()} {r['sender']} {r['amount']} {r['currency']}" for r in rows
        )
        return synthetic_input + f"\n\nRecent receipts:\n{rendered}\n"

    def _build_low_conf_interaction_payload(self, task: dict, decision: dict, pass_n: int) -> dict:
        title = (task.get("content") or "").strip()[:120]
        source_tag = decision.get("source_tag") or task.get("source_tag") or ""
        confidence = float(decision.get("confidence") or 0.0)
        classification = decision.get("classification") or "unknown"
        contexts = " ".join(decision.get("contexts") or [])
        return {
            "flavor": "low_conf",
            "prompt": (
                f"❓ Review: {title}\n"
                f"Source: {source_tag}   Conf: {confidence:.2f}\n"
                f"Best guess: {classification} {contexts}"
            ),
            "options": {
                "confirm": "✅ Confirm best guess",
                "trash": "🗑 Trash",
                "leave": "💤 Leave for later",
            },
            "decision": decision,
            "pass_n": pass_n,
        }

    def _build_gate_interaction_payload(self, task: dict, decision: dict, pass_n: int) -> dict:
        """Choice card shown before any agent runs on an Inbox work ticket
        (APP-<n>: Jira). Two options — hand it to Pandora, or claim it
        yourself. Ignoring the card (24h timeout → archive) leaves the task
        untouched in Inbox. No spawn_kind, so ClarifyFlow routes this through
        InteractionFlow (not AlertInvestigationFlow). (inbox gate)
        """
        title = (task.get("content") or "").strip()[:120]
        # Name the investigating agent from the matched route's assignee
        # (@pandora → "Pandora"), so the card isn't hardcoded to one deployment.
        handle = (decision.get("assignee") or "@pandora").lstrip("@")
        who = (handle[:1].upper() + handle[1:]) if handle else "the agent"
        return {
            "flavor": "pandora_gate",
            "prompt": (
                f"🎫 New ticket in Inbox:\n{title}\n\n"
                f"Want {who} to investigate, or are you on it?"
            ),
            "options": {
                "investigate": f"🔍 {who}, investigate",
                "mine": "🙋 I've got it",
            },
            "decision": decision,
            "pass_n": pass_n,
        }

    @activity.defn
    async def apply_outcome(
        self,
        task: dict,
        decision: dict,
        pass_n: int = 1,
        _now=None,
        force_apply: bool = False,
    ) -> dict:
        """Translate a classify_one decision into a Todoist command batch.

        Returns {applied, interaction_spawned, interaction_payload?,
        commands_sent, outbox_queued}.

        When `force_apply=True`, low-confidence + 2-min in-window paths
        skip the interaction spawn and apply the chosen action directly
        (used by apply_clarify_resolution after the user picks an option).
        """
        from aegis.connectors.todoist import TodoistConnector

        item_id = task["id"]
        classification = decision.get("classification") or "unknown"
        confidence = float(decision.get("confidence") or 0.0)
        assignee = decision.get("assignee") or "@me"
        contexts = list(decision.get("contexts") or [])

        # Low-confidence → NEEDS REVIEW note + signal interaction.
        # The note is best-effort: if Todoist rejects it (envelope-ok +
        # per-command ITEM_NOT_FOUND on a stale projection row), we still
        # spawn the user interaction since the chat card is the real
        # signal. Log the rejection so it surfaces in observability.
        if confidence < self.escalation_threshold and not force_apply:
            note_cmd = TodoistConnector.build_note_add_command(
                item_id, self._format_review_note(decision, pass_n, _now)
            )
            note_result = await self.todoist_connector.commands([note_cmd])
            note_status = TodoistConnector.check_sync_status(note_result, [note_cmd["uuid"]])
            if not note_status["ok"]:
                activity.logger.warning(
                    "clarify_low_conf_note_rejected task_id=%s envelope_err=%s rejected=%s",
                    item_id,
                    note_status["envelope_error"],
                    str(note_status["rejected"])[:200],
                )
            return {
                "applied": False,
                "interaction_spawned": True,
                "interaction_payload": self._build_low_conf_interaction_payload(
                    task, decision, pass_n
                ),
                "commands_sent": 1,
                "outbox_queued": 0,
            }

        commands: list[dict] = []
        existing_labels: list[str] = list(task.get("labels") or [])
        # Merge label set: keep #source_tag + add assignee + contexts
        merged_labels = list({*existing_labels, assignee, *contexts})

        interaction_spawned = False
        interaction_payload: dict | None = None

        # Active-agent registry (aliases + behavior tags) for the per-agent
        # follow-up branch below — derived, not hardcoded (issue #36).
        reg = await get_agent_registry(self.db_pool)
        followup_classifications = {f"{aid}_followup" for aid in reg} | {"pandora_chat_followup"}
        # Content route matched by this task's title — drives the label set +
        # investigation scoping for the route classifications below. None when
        # no route matches (e.g. the pandora_investigation retry raced a config
        # edit); the branches fall back to sane defaults.
        matched_route = match_route(
            task.get("content") or "", await get_content_routes(self.db_pool)
        )

        # Pandora-owned task — no side effects at all. classify_one
        # already identified this case; we only need log_classification
        # to bump last_clarified_at so the watermark advances.
        if classification == "pandora_owned":
            return {
                "applied": True,
                "interaction_spawned": False,
                "interaction_payload": None,
                "commands_sent": 0,
                "outbox_queued": 0,
            }

        if classification == "trash":
            commands.append(
                TodoistConnector.build_item_update_command(
                    item_id, labels=[*existing_labels, "#trash"]
                )
            )
            commands.append(
                {
                    "type": "item_complete",
                    "uuid": uuid.uuid4().hex,
                    "args": {"id": item_id},
                }
            )

        elif classification == "pandora_gate":
            # Ask-before-acting gate for Inbox work tickets (inbox gate).
            # Apply NO labels/commands — just spawn the choice card. The flow
            # bumps the watermark (an interaction spawned) so we don't re-card
            # every tick; apply_clarify_resolution applies the chosen decision.
            # No spawn_kind → ClarifyFlow routes this to InteractionFlow.
            return {
                "applied": False,
                "interaction_spawned": True,
                "interaction_payload": self._build_gate_interaction_payload(
                    task, decision, pass_n
                ),
                "commands_sent": 0,
                "outbox_queued": 0,
            }

        elif classification == "pandora_investigation":
            # Content-route investigation. Stamp @pandora ownership (the label
            # the investigation machinery — pandora_owned short-circuit, retry,
            # cooldown SQL — keys on) plus the route's assignee + optional
            # area_label, and signal the caller to spawn AlertInvestigationFlow
            # as an abandoned child, scoped by the route's service/resource_tags.
            route = matched_route or {}
            label_set = {*existing_labels, "@pandora", route.get("assignee") or "@pandora", *contexts}
            area = route.get("area_label")
            if area:
                label_set.add(area)
            commands.append(
                TodoistConnector.build_item_update_command(item_id, labels=list(label_set))
            )
            content = task.get("content") or ""
            description = task.get("description") or ""
            fingerprint = f"route-{item_id}"
            interaction_payload = self._pandora_alert_payload(
                content,
                description,
                fingerprint,
                item_id,
                service=route.get("service"),
                resource_tags=route.get("resource_tags"),
            )
            interaction_spawned = True

        elif classification == "route_apply":
            # Content route with gate:false — apply the route's assignee +
            # contexts (+ optional area_label) directly. No card, no agent run.
            route = matched_route or {}
            label_set = {*existing_labels, assignee, *contexts}
            area = route.get("area_label")
            if area:
                label_set.add(area)
            commands.append(
                TodoistConnector.build_item_update_command(item_id, labels=list(label_set))
            )

        elif classification == "pandora_followup":
            # User commented on an existing @pandora task — fire a fresh
            # investigation that includes the user's comment as context.
            # No label changes (the task already has @pandora). Use a
            # fingerprint keyed on last_note_at so the alert flow's 24h
            # dedup doesn't block re-investigations for genuinely new
            # comments. The latest_user_note is appended to the alert
            # description so kimi sees the additional context.
            content = task.get("content") or ""
            description = task.get("description") or ""
            latest_note = (task.get("latest_user_note") or "").strip()
            last_note_at = task.get("last_note_at")
            # Stable per-comment fingerprint: use an ISO timestamp slice if
            # available, else hash of the note text.
            note_token = ""
            if last_note_at:
                note_token = str(last_note_at)[:19].replace(":", "").replace(" ", "T")
            elif latest_note:
                note_token = str(abs(hash(latest_note)))[:12]
            fingerprint = f"route-{item_id}-followup-{note_token}"
            followup_desc = (
                f"{description[:1500]}\n\n--- User followup comment ---\n{latest_note[:1500]}"
            )
            _route = matched_route or {}
            interaction_payload = self._pandora_alert_payload(
                content,
                followup_desc,
                fingerprint,
                item_id,
                service=_route.get("service"),
                resource_tags=_route.get("resource_tags"),
            )
            interaction_spawned = True
            # No commands to send — labels already include @pandora. We
            # explicitly synthesize an empty success below so apply_outcome
            # returns applied=True and the ClarifyFlow spawn gate passes.

        elif classification in followup_classifications:
            # Per-agent comment-channel branch (2026-05-26; pandora added
            # 2026-05-27). Builds the synthetic chat turn for
            # AgentChatReplyFlow and returns a spawn payload. No Todoist
            # commands are sent here — the spawned workflow does all
            # writes (chat + Todoist comment).
            #
            # Agent id mapping is mostly classification.replace("_followup", "")
            # except for pandora — the personality directory + agents.id
            # is "pandoras-actor", not "pandora".
            if classification == "pandora_chat_followup":
                target_agent = "pandoras-actor"
            else:
                target_agent = classification.replace("_followup", "")
            # Fetch the recent comment thread so the agent sees its own
            # prior replies (and other agents' replies) and doesn't
            # repeat itself. Best-effort: empty list on DB failure.
            recent_notes = await self._fetch_recent_task_notes(item_id, limit=15)
            synthetic_input = self._build_agent_synthetic_input(
                task, target_agent, recent_notes=recent_notes
            )
            # Per-agent pre-fetch hooks, gated on the target's behavior tag
            # (issue #36) rather than its id: a `research` agent gets the
            # knowledge context, a `finance` agent gets the transaction
            # context. gtd/infra agents have no hook — their context IS the
            # task and their tool sets fetch what they need.
            target_caps = reg.get(target_agent, {}).get("caps", set())
            if "research" in target_caps:
                synthetic_input = await self._maybe_attach_ks_context(synthetic_input, task)
            elif "finance" in target_caps:
                synthetic_input = await self._maybe_attach_transaction_context(
                    synthetic_input, task
                )
            return {
                "applied": True,
                "interaction_spawned": True,
                "interaction_payload": {
                    "spawn_kind": "agent_chat_reply",
                    "target_agent": target_agent,
                    "task_id": item_id,
                    "synthetic_input": synthetic_input,
                    "thread_id": f"todoist-task-{item_id}",
                },
                "commands_sent": 0,
                "outbox_queued": 0,
            }

        elif classification == "reference":
            labels_with_state = list({*merged_labels, "@reference"})
            commands.append(
                TodoistConnector.build_item_update_command(item_id, labels=labels_with_state)
            )
            # No item_move — @reference is the permanent home signal.

        elif classification == "someday":
            # Todoist GTD restructure (2026-07): Someday/Later is now the
            # @someday LABEL, not a managed project (the project is being
            # retired). Label-only, mirrors the `reference` branch above —
            # no item_move.
            labels_with_state = list({*merged_labels, _LABEL_SOMEDAY})
            commands.append(
                TodoistConnector.build_item_update_command(item_id, labels=labels_with_state)
            )
            # No item_move — @someday is the permanent home signal now.

        elif classification == "2_min":
            tz_name = await self._settings_str("user_timezone", "UTC")
            in_window = self._in_time_window(tz_name, _now)
            gate_on = await self._settings_bool("gtd_2min_rule_enabled", True)
            if gate_on and in_window and not force_apply:
                # In window AND not a forced-from-resolution apply →
                # spawn the user-facing card. Do NOT apply commands here;
                # the resolution activity will do that with force_apply=True.
                return {
                    "applied": False,
                    "interaction_spawned": True,
                    "interaction_payload": self._build_2min_interaction_payload(
                        task, decision, pass_n
                    ),
                    "commands_sent": 0,
                    "outbox_queued": 0,
                }
            else:
                # Out of window OR force_apply → label-only update (+@5min).
                demoted = list({*merged_labels, "@5min"})
                commands.append(TodoistConnector.build_item_update_command(item_id, labels=demoted))

        elif classification == "next_action":
            # Todoist GTD restructure (2026-07): Next is now the @next
            # LABEL, not a managed project — this branch never moved
            # projects, so the only change is adding the state label.
            labels_with_state = list({*merged_labels, _LABEL_NEXT})
            # Optional due-date passthrough (used by 'defer_1d' resolution).
            due_string = decision.get("_due_string")
            if due_string:
                commands.append(
                    TodoistConnector.build_item_update_command(
                        item_id, labels=labels_with_state, due={"string": due_string}
                    )
                )
            else:
                commands.append(
                    TodoistConnector.build_item_update_command(item_id, labels=labels_with_state)
                )

        elif classification == "mine":
            # Hands-off resolution: user claimed the task with @me (via the
            # gate card's "I've got it", or manually). find_unclassified_items
            # excludes @me tasks, so stamping the label is terminal. (inbox gate)
            mine_labels = list({*existing_labels, "@me"})
            commands.append(
                TodoistConnector.build_item_update_command(item_id, labels=mine_labels)
            )

        elif classification == "leave":
            # Low-conf 'Leave for later' resolution: add @review label,
            # bump last_clarified_at (via log_classification) so the next
            # tick doesn't re-spawn; user comment retriggers.
            review_labels = list({*existing_labels, "@review"})
            commands.append(
                TodoistConnector.build_item_update_command(item_id, labels=review_labels)
            )

        else:
            # Unknown classification → label-only update; flow caller logs.
            commands.append(
                TodoistConnector.build_item_update_command(item_id, labels=merged_labels)
            )

        # Always append the reasoning note
        commands.append(
            TodoistConnector.build_note_add_command(
                item_id, self._format_apply_note(decision, pass_n, _now)
            )
        )

        result = await self.todoist_connector.commands(commands)
        cmd_uuids = [c["uuid"] for c in commands]
        status = TodoistConnector.check_sync_status(result, cmd_uuids)
        applied = status["ok"]
        outbox_queued = 0
        # Outbox compensation only when the failure is recoverable:
        #   - envelope-failure with retryable=True (5xx, timeout, rate-limit)
        #   - envelope-ok but per-cmd status was a transient class (5xx-ish)
        # Non-retryable per-cmd rejections (ITEM_NOT_FOUND, INVALID_ARGUMENT)
        # would just poison the outbox — log and drop instead.
        envelope_retryable = status["retryable"]
        per_cmd_retryable = status["rejected_retryable"]
        if not applied and (envelope_retryable or per_cmd_retryable):
            import uuid as _uuid

            async with self.db_pool.acquire() as conn:
                for cmd in commands:
                    temp_id = cmd.get("temp_id") or f"apply-{_uuid.uuid4()}"
                    await conn.execute(
                        "INSERT INTO todoist_outbox (temp_id, command, status) "
                        "VALUES ($1, $2, 'pending') ON CONFLICT (temp_id) DO NOTHING",
                        temp_id,
                        cmd,
                    )
                    outbox_queued += 1
            activity.logger.warning(
                "clarify_apply_outcome_outbox_queued task_id=%s queued=%d error=%s",
                item_id,
                outbox_queued,
                status["envelope_error"] or str(status["rejected"])[:200],
            )
        elif not applied:
            # Permanent rejection — task likely gone from Todoist or args
            # malformed. Log and let the caller skip the watermark bump
            # (so the task re-enters find_unclassified_items only after
            # the projection catches up).
            activity.logger.warning(
                "clarify_apply_outcome_rejected_nonretryable task_id=%s envelope_err=%s rejected=%s",
                item_id,
                status["envelope_error"],
                str(status["rejected"])[:300],
            )
        return {
            "applied": applied,
            "interaction_spawned": interaction_spawned,
            "interaction_payload": interaction_payload,
            "commands_sent": len(commands),
            "outbox_queued": outbox_queued,
        }

    @activity.defn
    async def log_classification(
        self,
        task_id: str,
        decision: dict,
        applied: bool,
        pass_n: int = 1,
        user_hint: str | None = None,
        bump_watermark: bool = True,
    ) -> None:
        """Insert a gtd_clarify_log row and (optionally) bump
        todoist_tasks.last_clarified_at.

        Always called from ClarifyFlow regardless of applied=True/False so
        that low-confidence cycles are visible in the audit trail.

        **Watermark invariant** (added 2026-05-21): the audit row writes
        unconditionally, but `last_clarified_at` only bumps when
        `bump_watermark=True`. The caller is responsible for setting this
        False when the task should re-enter `find_unclassified_items` on
        the next tick — e.g. a Todoist update was rejected non-retryably,
        or a low-confidence interaction was spawned and the user hasn't
        resolved it yet (poisoning the watermark would silently abandon
        the task forever).
        """
        if self.db_pool is None:
            return
        contexts = list(decision.get("contexts") or [])
        async with self.db_pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """
                INSERT INTO gtd_clarify_log
                  (todoist_task_id, pass, source_tag, classification,
                   confidence, assignee, contexts, reason, user_hint,
                   llm_model, prompt_tokens, completion_tokens, latency_ms,
                   applied)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
                """,
                task_id,
                pass_n,
                decision.get("source_tag"),
                decision.get("classification") or "unknown",
                float(decision.get("confidence") or 0.0),
                decision.get("assignee"),
                contexts,
                decision.get("reason"),
                user_hint,
                decision.get("llm_model") or "unknown",
                decision.get("prompt_tokens"),
                decision.get("completion_tokens"),
                decision.get("latency_ms"),
                bool(applied),
            )
            if bump_watermark:
                await conn.execute(
                    "UPDATE todoist_tasks SET last_clarified_at = now() WHERE id = $1",
                    task_id,
                )

    @activity.defn
    async def apply_clarify_resolution(
        self,
        interaction_id: str,
        response: dict,
        metadata: dict,
    ) -> dict:
        """Translate a resolved Phase 4 choice interaction into Todoist actions.

        Dispatches based on metadata.flavor + response.value:
            2_min:    do_now / defer_1d / trash
            low_conf: confirm / trash / leave
        Then re-runs apply_outcome with force_apply=True so the confidence
        floor doesn't re-spawn the interaction, and writes a follow-up
        gtd_clarify_log row with pass_n incremented + llm_model='user_resolution'.
        """
        flavor = metadata.get("flavor")
        choice = (response.get("value") or "").strip()
        task_id = metadata.get("task_id")
        original_decision = dict(metadata.get("decision") or {})
        pass_n = int(metadata.get("pass_n") or 1) + 1

        if not task_id or not flavor:
            activity.logger.warning(
                "clarify_resolution_missing_metadata interaction=%s flavor=%s task_id=%s",
                interaction_id,
                flavor,
                task_id,
            )
            return {"applied": False, "reason": "missing_metadata"}

        # Fetch the live task labels so the merged-label calculation in
        # apply_outcome is correct (decision payload may be stale).
        existing_labels: list[str] = []
        task_content = ""
        if self.db_pool is not None:
            async with self.db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT content, labels FROM todoist_tasks WHERE id = $1",
                    task_id,
                )
            if row:
                existing_labels = list(row["labels"] or [])
                task_content = row["content"] or ""

        resolved_decision = dict(original_decision)
        resolved_decision["llm_model"] = "user_resolution"
        resolved_decision["reason"] = f"resolved via chat: flavor={flavor} choice={choice}"

        if flavor == "2_min":
            if choice == "do_now":
                resolved_decision["classification"] = "next_action"
                resolved_decision["contexts"] = list(
                    {"@5min", *(original_decision.get("contexts") or [])}
                )
            elif choice == "defer_1d":
                resolved_decision["classification"] = "next_action"
                resolved_decision["_due_string"] = "tomorrow"
            elif choice == "trash":
                resolved_decision["classification"] = "trash"
            else:
                return {"applied": False, "reason": f"unknown_2min_choice:{choice}"}

        elif flavor == "low_conf":
            if choice == "confirm":
                # Use original LLM classification verbatim — confidence floor
                # bypassed via force_apply below.
                pass
            elif choice == "trash":
                resolved_decision["classification"] = "trash"
            elif choice == "leave":
                resolved_decision["classification"] = "leave"
            else:
                return {"applied": False, "reason": f"unknown_low_conf_choice:{choice}"}

        elif flavor == "pandora_gate":
            # Inbox work-ticket gate resolution (inbox gate).
            if choice == "investigate":
                # Approve investigation. Route through pandora_investigation so
                # apply_outcome stamps @pandora + the route's assignee/area; its
                # spawn payload is ignored here (activities can't start
                # workflows). We clear the watermark AFTER logging (below) so the
                # next ClarifyFlow tick re-surfaces the task, hits the @pandora
                # retry branch, and fires AlertInvestigationFlow from the flow.
                # ponytail: ~15-min latency to investigation start — fine for
                # async triage; upgrade path is a Temporal client on this
                # activity if instant start is ever needed.
                _route = match_route(task_content, await get_content_routes(self.db_pool)) or {}
                resolved_decision["classification"] = "pandora_investigation"
                resolved_decision["assignee"] = _route.get("assignee") or "@pandora"
                resolved_decision["contexts"] = list(_route.get("contexts") or ["@deep", "@code"])
            elif choice == "mine":
                resolved_decision["classification"] = "mine"
            else:
                return {"applied": False, "reason": f"unknown_gate_choice:{choice}"}
        else:
            return {"applied": False, "reason": f"unknown_flavor:{flavor}"}

        pseudo_task = {
            "id": task_id,
            "content": task_content,
            "labels": existing_labels,
            "source_tag": original_decision.get("source_tag"),
        }
        outcome = await self.apply_outcome(
            pseudo_task,
            resolved_decision,
            pass_n=pass_n,
            force_apply=True,
        )
        await self.log_classification(
            task_id=task_id,
            decision={**resolved_decision, "source_tag": original_decision.get("source_tag")},
            applied=outcome.get("applied", False),
            pass_n=pass_n,
            user_hint=f"chat:{choice}",
        )
        # Gate "investigate" needs the task to re-enter find_unclassified_items
        # so the @pandora retry branch fires AlertInvestigationFlow from the
        # flow. log_classification just bumped the watermark — undo it. (inbox gate)
        if flavor == "pandora_gate" and choice == "investigate":
            await self.clear_clarify_watermark(task_id)
        # references-as-knowledge: if the resolution lands on 'reference'
        # and Todoist accepted the labels, inline-ingest + dispatch the
        # verdict (no Temporal scheduling — we're inside an activity).
        # Transient errors are RAISED by ingest_reference_to_ks. The flow
        # path gets 5 retries from Temporal's STANDARD policy; the inline
        # path (this one) ran with zero retries pre-2026-05-28, so a
        # single 5xx demoted the user-chosen reference. We now align with
        # the flow's budget via an inline 3-attempt exponential backoff
        # (1s, 2s, 4s = ~7s worst case) before treating a transient
        # failure as permanent and demoting the task.
        if outcome.get("applied") and resolved_decision.get("classification") == "reference":
            source_tag = original_decision.get("source_tag") or ""
            task_content = pseudo_task.get("content") or ""
            verdict: dict | None = None
            last_transient_exc: Exception | None = None
            ingest_max_attempts = 3
            for attempt in range(ingest_max_attempts):
                try:
                    verdict = await self.ingest_reference_to_ks(
                        task_id=task_id,
                        task_content=task_content,
                        task_description="",
                        source_tag=source_tag,
                        latest_user_note=None,
                    )
                    break
                except Exception as ingest_exc:  # noqa: BLE001
                    last_transient_exc = ingest_exc
                    activity.logger.warning(
                        "clarify_resolution_ks_ingest_transient task=%s attempt=%s err=%s",
                        task_id,
                        attempt + 1,
                        str(ingest_exc)[:200],
                    )
                    if attempt + 1 < ingest_max_attempts:
                        await asyncio.sleep(2**attempt)
            if verdict is None:
                # All inline attempts exhausted with transient errors —
                # treat as permanent so we demote the task and unblock
                # the user. Reason includes the final exception string.
                verdict = {
                    "status": "permanent_error",
                    "reason": (
                        f"inline_retries_exhausted: {str(last_transient_exc)[:120]}"
                        if last_transient_exc is not None
                        else "inline_retries_exhausted"
                    ),
                }
            v_status = verdict.get("status")
            if v_status == "ok":
                complete_result = await self.complete_reference_task(
                    task_id=task_id,
                    title=task_content,
                    source_tag=source_tag,
                    content_id=verdict.get("content_id"),
                    url=verdict.get("url"),
                )
                # If Todoist rejected the completion (transient or permanent),
                # fall through to the demotion so the user is told. This is
                # the inline (no Temporal retry) sibling of the flow-level
                # _dispatch_reference_verdict — see worker/flows/clarify.py.
                if not complete_result.get("completed"):
                    reason = complete_result.get("reason") or "unknown"
                    activity.logger.warning(
                        "apply_clarify_resolution_complete_failed task=%s reason=%s retryable=%s",
                        task_id,
                        reason,
                        complete_result.get("retryable"),
                    )
                    await self.reclassify_reference_to_reading(
                        task_id=task_id,
                        title=task_content,
                        source_tag=source_tag,
                        existing_labels=list(pseudo_task.get("labels") or []),
                        reason=f"todoist rejected completion: {reason}",
                    )
            elif v_status == "permanent_error":
                await self.reclassify_reference_to_reading(
                    task_id=task_id,
                    title=task_content,
                    source_tag=source_tag,
                    existing_labels=list(pseudo_task.get("labels") or []),
                    reason=verdict.get("reason") or "unknown",
                )
        activity.logger.info(
            "clarify_resolution_applied interaction=%s task=%s flavor=%s choice=%s applied=%s",
            interaction_id,
            task_id,
            flavor,
            choice,
            outcome.get("applied"),
        )
        return {
            "applied": outcome.get("applied", False),
            "choice": choice,
            "flavor": flavor,
            "commands_sent": outcome.get("commands_sent", 0),
            "outbox_queued": outcome.get("outbox_queued", 0),
        }

    @staticmethod
    def _extract_first_url(text: str) -> str | None:
        """Return the first http(s) URL in `text`, or None.

        Used by ingest_reference_to_ks to decide: pass URL to KS (which
        offloads scraping) vs. send raw_text (for #manual / #chat / email-
        subject references with no clickable source).
        """
        if not text:
            return None
        m = re.search(r"https?://[^\s)<>\"']+", text)
        if not m:
            return None
        # Trim trailing punctuation that's commonly attached to URLs in prose
        url = m.group(0)
        return url.rstrip(".,;:")

    @staticmethod
    def _url_is_unscrapable(url: str) -> bool:
        """URLs whose body KS can't fetch from its own network — Gmail
        deeplinks need OAuth, HN item pages need the Firebase API, etc.
        Letting KS see one of these means the row lands with raw_text=""
        and 0 useful triples. Caller falls through to the raw_text path
        for these.
        """
        if not url:
            return False
        lowered = url.lower()
        return "mail.google.com" in lowered or "news.ycombinator.com" in lowered

    @staticmethod
    def _hn_item_id(url: str) -> str | None:
        """Extract the HN item id from a Hacker News URL.

        ``https://news.ycombinator.com/item?id=47911524`` → ``"47911524"``.
        Returns None for anything else.
        """
        m = re.search(r"news\.ycombinator\.com/item\?id=(\d+)", url or "")
        return m.group(1) if m else None

    @staticmethod
    async def _fetch_hn_body(item_id: str) -> str | None:
        """Fetch the HN item's title + text + top-level comments via the
        Firebase API. Returns a single string suitable for raw_text, or
        None on failure. Network errors are swallowed — caller falls back
        to title-only ingestion.
        """
        import httpx

        base = "https://hacker-news.firebaseio.com/v0"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
                item_resp = await client.get(f"{base}/item/{item_id}.json")
                item_resp.raise_for_status()
                item = item_resp.json() or {}
                parts: list[str] = []
                if item.get("title"):
                    parts.append(item["title"])
                if item.get("text"):
                    parts.append(item["text"])
                # Pull up to 10 top-level comments so KS has something to
                # extract knowledge from for link-only submissions where
                # the discussion is the substance.
                kid_ids = (item.get("kids") or [])[:10]
                for kid in kid_ids:
                    try:
                        kid_resp = await client.get(f"{base}/item/{kid}.json")
                        kid_data = kid_resp.json() or {}
                        if kid_data.get("text") and not kid_data.get("deleted"):
                            parts.append(kid_data["text"])
                    except Exception:  # noqa: BLE001 — best-effort per comment
                        continue
                body = "\n\n".join(parts).strip()
                return body or None
        except Exception:  # noqa: BLE001 — best-effort fetch
            return None

    @staticmethod
    def _classify_ks_exception(exc: Exception) -> tuple[str, str]:
        """Return (verdict, reason) for a KS ingest exception.

        verdict ∈ {"transient", "permanent"}. 4xx → permanent (URL bad,
        scraper can't parse, body rejected). 5xx / connect / timeout →
        transient (KS hiccup; Temporal retries the activity).
        """
        try:
            import httpx
        except ImportError:  # pragma: no cover
            return ("transient", str(exc)[:200])
        if isinstance(exc, httpx.HTTPStatusError):
            code = exc.response.status_code
            if 400 <= code < 500:
                reason = f"http_{code}"
                # Try to capture KS's error body when present
                try:
                    body = exc.response.json()
                    detail = body.get("detail") or body.get("error")
                    if detail:
                        reason = f"http_{code}: {str(detail)[:120]}"
                except Exception:  # noqa: BLE001
                    pass
                return ("permanent", reason)
            return ("transient", f"http_{code}")
        if isinstance(exc, (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError)):
            return ("transient", type(exc).__name__)
        return ("transient", str(exc)[:200])

    @activity.defn
    async def ingest_reference_to_ks(
        self,
        task_id: str,
        task_content: str,
        task_description: str,
        source_tag: str,
        latest_user_note: str | None,
    ) -> dict:
        """Push a reference task into knowledge-service.

        Returns a verdict the caller dispatches on:

        - ``{"status": "ok", "content_id": ..., ...}`` — KS confirmed. Caller
          should complete the Todoist task; KS is the home now.
        - ``{"status": "permanent_error", "reason": ..., ...}`` — 4xx, scraper
          failure, body rejected. Caller should reclassify the task to a
          reading lane; it won't ever land in KS without intervention.
        - ``{"status": "skipped", "reason": ...}`` — no connector / empty
          payload. Caller leaves the task alone.

        Transient failures (5xx, connect, timeout) **raise** so Temporal's
        activity retry policy kicks in. The caller never observes a
        ``transient_error`` status.
        """
        if self.knowledge_connector is None:
            return {"status": "skipped", "reason": "no_knowledge_connector"}
        if not task_id or not task_content:
            return {"status": "skipped", "reason": "empty_task"}

        url = self._extract_first_url(
            " ".join(filter(None, [task_content, task_description, latest_user_note]))
        )
        title = task_content[:200]
        tags = ["gtd:reference"]
        if source_tag:
            tags.append(source_tag)
        metadata = {
            "todoist_task_id": task_id,
            "source_tag": source_tag or "",
            "captured_via": "gtd_clarify",
        }

        # For URLs KS can't scrape (Gmail deeplinks, HN items), fetch the
        # body ourselves and send it as raw_text. Without this, KS stores
        # the URL with raw_text="" and zero useful triples come out.
        synthesized_body: str | None = None
        if url and self._url_is_unscrapable(url):
            hn_id = self._hn_item_id(url)
            if hn_id:
                synthesized_body = await self._fetch_hn_body(hn_id)
            url = None  # fall through to the raw_text path below

        try:
            if url:
                result = await self.knowledge_connector.ingest_content(
                    url=url,
                    title=title,
                    source_type="reference",
                    tags=tags,
                    metadata=metadata,
                )
            else:
                raw_text = "\n\n".join(
                    filter(
                        None,
                        [
                            task_content,
                            task_description,
                            latest_user_note,
                            synthesized_body,
                        ],
                    )
                )
                result = await self.knowledge_connector.ingest_content(
                    url=f"aegis://reference/{task_id}",
                    title=title,
                    source_type="reference",
                    raw_text=raw_text[:50_000],
                    tags=tags,
                    metadata=metadata,
                )
        except Exception as exc:  # noqa: BLE001
            verdict, reason = self._classify_ks_exception(exc)
            if verdict == "transient":
                activity.logger.warning(
                    "ingest_reference_to_ks_transient task_id=%s reason=%s",
                    task_id,
                    reason,
                )
                # Raise so Temporal retries the activity per its policy.
                raise
            activity.logger.warning(
                "ingest_reference_to_ks_permanent task_id=%s reason=%s",
                task_id,
                reason,
            )
            return {
                "status": "permanent_error",
                "reason": reason,
                "url": url,
            }

        activity.logger.info(
            "ingest_reference_to_ks_ok task_id=%s url=%s content_id=%s status=%s",
            task_id,
            url,
            result.get("content_id"),
            result.get("status"),
        )
        return {
            "status": "ok",
            "url": url,
            "content_id": result.get("content_id"),
            "job_id": result.get("job_id"),
        }

    # ---- references-as-knowledge: completion & demotion paths ----

    # Source tags considered "automated" (raphael surfaces these via the
    # daily briefing digest, not per-message). Everything else is treated
    # as user-initiated and gets an immediate chat confirmation.
    _AUTOMATED_REFERENCE_SOURCES = {"#research", "#email"}

    @activity.defn
    async def complete_reference_task(
        self,
        task_id: str,
        title: str,
        source_tag: str,
        content_id: str | None,
        url: str | None,
    ) -> dict:
        """Complete a Todoist task whose reference body landed in KS.

        Posts a short ClarifyFlow note ("filed in knowledge service") so
        the audit trail explains why the task closed without user action,
        then issues item_complete. For user-initiated sources (chat /
        manual) raphael sends a per-message chat confirmation; for
        automated sources (raindrop / RSS / intel-scan / email) the
        confirmation lands in the daily briefing instead.
        """
        from aegis.connectors.todoist import TodoistConnector

        if self.todoist_connector is None:
            return {"completed": False, "reason": "no_todoist_connector"}
        if not task_id:
            return {"completed": False, "reason": "empty_task_id"}

        ref_hint = content_id or url or "(no-id)"
        note_text = (
            f"{CLARIFY_NOTE_PREFIX}ref-complete] filed in knowledge service (ref={ref_hint})"
        )
        commands = [
            TodoistConnector.build_note_add_command(task_id, note_text),
            TodoistConnector.build_item_complete_command(task_id),
        ]
        result = await self.todoist_connector.commands(commands)
        cmd_uuids = [c["uuid"] for c in commands]
        status = TodoistConnector.check_sync_status(result, cmd_uuids)
        if not status["ok"]:
            activity.logger.warning(
                "complete_reference_task_failed task_id=%s err=%s rejected=%s",
                task_id,
                status["envelope_error"],
                str(status["rejected"])[:200],
            )
            return {
                "completed": False,
                "reason": status["envelope_error"] or "rejected",
                "retryable": status["retryable"] or status["rejected_retryable"],
            }

        notify_sent = False
        if source_tag not in self._AUTOMATED_REFERENCE_SOURCES:
            notify_sent = await self._notify_reference_filed(
                title=title, content_id=content_id, url=url
            )
        return {
            "completed": True,
            "notify_sent": notify_sent,
            "automated_source": source_tag in self._AUTOMATED_REFERENCE_SOURCES,
        }

    @activity.defn
    async def reclassify_reference_to_reading(
        self,
        task_id: str,
        title: str,
        source_tag: str,
        existing_labels: list[str],
        reason: str,
    ) -> dict:
        """Demote a reference task to a reading task after KS gave up.

        Strips ``@reference`` from the label set, adds ``@to-read``, posts
        a comment with the failure reason, and sends an immediate chat
        from raphael (regardless of source — the user needs to act).
        """
        from aegis.connectors.todoist import TodoistConnector

        if self.todoist_connector is None:
            return {"reclassified": False, "reason": "no_todoist_connector"}
        if not task_id:
            return {"reclassified": False, "reason": "empty_task_id"}

        new_labels = sorted(
            {label for label in (existing_labels or []) if label != "@reference"} | {"@to-read"}
        )
        note_text = (
            f"{CLARIFY_NOTE_PREFIX}ref-demote] couldn't file in knowledge service — "
            f"{reason}. Demoted to @to-read."
        )
        commands = [
            TodoistConnector.build_item_update_command(task_id, labels=new_labels),
            TodoistConnector.build_note_add_command(task_id, note_text),
        ]
        result = await self.todoist_connector.commands(commands)
        cmd_uuids = [c["uuid"] for c in commands]
        status = TodoistConnector.check_sync_status(result, cmd_uuids)
        if not status["ok"]:
            activity.logger.warning(
                "reclassify_reference_to_reading_failed task_id=%s err=%s rejected=%s",
                task_id,
                status["envelope_error"],
                str(status["rejected"])[:200],
            )
            return {
                "reclassified": False,
                "reason": status["envelope_error"] or "rejected",
                "retryable": status["retryable"] or status["rejected_retryable"],
            }

        notify_sent = await self._notify_reference_demoted(title=title, reason=reason)
        return {
            "reclassified": True,
            "labels": new_labels,
            "notify_sent": notify_sent,
        }

    @activity.defn
    async def post_agent_reply_comment(
        self,
        task_id: str,
        agent_id: str,
        reply_text: str,
        tool_trace_summary: str,
        message_id: int | None,
    ) -> dict:
        """Mirror an AgentChatReplyFlow reply as a Todoist comment.

        Prefix MUST start with AGENT_REPLY_PREFIX so:
          - the webhook receiver's is_clarify_own check trips,
          - find_unclassified_items' SQL filter excludes it from
            latest_user_note extraction.
        Failure does NOT raise — chat is the primary surface, and a
        Todoist-side rejection here just leaves the user without the
        in-task anchor (acceptable degradation).
        """
        from aegis.connectors.todoist import TodoistConnector

        if self.todoist_connector is None or not task_id:
            return {"posted": False, "reason": "no_connector_or_task_id"}

        ts = datetime.now(ZoneInfo("UTC")).strftime("%H:%M UTC")
        trailer_parts: list[str] = []
        if tool_trace_summary:
            trailer_parts.append(f"(tools: {tool_trace_summary})")
        if message_id is not None:
            trailer_parts.append(f"(chat message_id={message_id})")
        trailer = "\n\n" + "\n".join(trailer_parts) if trailer_parts else ""
        body = f"{AGENT_REPLY_PREFIX}{ts} agent={agent_id}]\n{reply_text}{trailer}"

        cmd = TodoistConnector.build_note_add_command(task_id, body)
        result = await self.todoist_connector.commands([cmd])
        status = TodoistConnector.check_sync_status(result, [cmd["uuid"]])

        if status["ok"]:
            return {"posted": True, "outbox_queued": 0}

        # Retryable → outbox; permanent → log + drop.
        outbox_queued = 0
        if (status["retryable"] or status["rejected_retryable"]) and self.db_pool is not None:
            import uuid as _uuid

            async with self.db_pool.acquire() as conn:
                temp_id = cmd.get("temp_id") or f"agent-reply-{_uuid.uuid4()}"
                await conn.execute(
                    "INSERT INTO todoist_outbox (temp_id, command, status) "
                    "VALUES ($1, $2, 'pending') ON CONFLICT (temp_id) DO NOTHING",
                    temp_id,
                    cmd,
                )
                outbox_queued = 1
            activity.logger.warning(
                "post_agent_reply_comment_outbox_queued task_id=%s err=%s",
                task_id,
                status["envelope_error"] or str(status["rejected"])[:200],
            )
        else:
            activity.logger.warning(
                "post_agent_reply_comment_rejected_nonretryable task_id=%s err=%s",
                task_id,
                status["envelope_error"] or str(status["rejected"])[:200],
            )

        return {
            "posted": False,
            "outbox_queued": outbox_queued,
            "reason": status["envelope_error"] or "rejected",
        }

    @activity.defn
    async def post_agent_reply_error_comment(
        self,
        task_id: str,
        agent_id: str,
        reason: str,
    ) -> dict:
        """Comment posted when AgentChatReplyFlow dropped the reply.

        Same AGENT_REPLY_PREFIX family so it does not self-loop. If the
        post itself fails, raise so Temporal records the workflow failure
        (the failure handler called us; we need that signal upstream).
        """
        from aegis.connectors.todoist import TodoistConnector

        if self.todoist_connector is None or not task_id:
            raise RuntimeError("post_agent_reply_error_comment_unconfigured")

        ts = datetime.now(ZoneInfo("UTC")).strftime("%H:%M UTC")
        body = f"{AGENT_REPLY_PREFIX}{ts} agent={agent_id} ERROR]\nDropped the reply: {reason}"

        cmd = TodoistConnector.build_note_add_command(task_id, body)
        result = await self.todoist_connector.commands([cmd])
        status = TodoistConnector.check_sync_status(result, [cmd["uuid"]])

        if not status["ok"]:
            activity.logger.warning(
                "post_agent_reply_error_comment_failed task_id=%s err=%s",
                task_id,
                status["envelope_error"] or str(status["rejected"])[:200],
            )
            raise RuntimeError(
                f"error_comment_failed: {status['envelope_error'] or str(status['rejected'])[:200]}"
            )

        return {"posted": True}

    @activity.defn
    async def clear_clarify_watermark(self, task_id: str) -> dict:
        """Compensating action: reset last_clarified_at = NULL so the task
        re-enters find_unclassified_items on the next tick.

        Used by ClarifyFlow when start_child_workflow raises for the
        agent_chat_reply spawn — without this, the watermark was already
        bumped in log_classification and the user's comment would be
        silently consumed.
        """
        if self.db_pool is None or not task_id:
            return {"cleared": False, "reason": "no_pool_or_task_id"}
        async with self.db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE todoist_tasks SET last_clarified_at = NULL WHERE id = $1",
                task_id,
            )
        return {"cleared": True}

    async def _notify_reference_filed(
        self, title: str, content_id: str | None, url: str | None
    ) -> bool:
        """Send a raphael-voiced chat message confirming a filed reference.

        Returns True when the send was dispatched via `safe_send_message`
        (failures are logged via the helper under
        `reference_filed_notify_failed`, never raised). Returns False only
        when there's no delivery connector wired (test path).
        """
        delivery = getattr(self, "delivery_connector", None)
        if delivery is None:
            return False
        from html import escape as _esc

        safe_title = _esc(title or "untitled")[:200]
        link_html = ""
        if content_id:
            link_html = f'\n<a href="/references/{_esc(content_id)}">Open in library</a>'
        elif url:
            link_html = f'\n<a href="{_esc(url)}">{_esc(url)[:120]}</a>'
        message = (
            f"📚 Filed: <b>{safe_title}</b>\n<i>Added to your reference library.</i>{link_html}"
        )
        await safe_send_message(
            delivery,
            agent_id="raphael",
            message=message,
            log_event="reference_filed_notify_failed",
        )
        return True

    async def _notify_reference_demoted(self, title: str, reason: str) -> bool:
        """Send a raphael-voiced chat message that a reference couldn't be filed.

        Always per-message — the user has a new reading task to act on.
        Returns True when the send was dispatched via `safe_send_message`
        (failures are logged via the helper under
        `reference_demoted_notify_failed`, never raised). Returns False only
        when there's no delivery connector wired (test path).
        """
        delivery = getattr(self, "delivery_connector", None)
        if delivery is None:
            return False
        from html import escape as _esc

        safe_title = _esc(title or "untitled")[:200]
        safe_reason = _esc(reason or "unknown")[:200]
        message = (
            f"📖 Couldn't file <b>{safe_title}</b>\n"
            f"<i>Reason: {safe_reason}</i>\n"
            f"Added to your reading list."
        )
        await safe_send_message(
            delivery,
            agent_id="raphael",
            message=message,
            log_event="reference_demoted_notify_failed",
        )
        return True
