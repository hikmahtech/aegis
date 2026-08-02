"""Profile activities — the auditable persona write path, for flows.

Thin wrappers over aegis.services.personalities so a flow never imports the
FastAPI layer. A1 shipped the write substrate (`read_profile_context`,
`apply_profile_patch`); A2 adds everything `ProfileReflectionFlow` needs to
propose a weekly edit to the agent's own `user` doc and land it only after a
human said yes:

  gather_profile_evidence   what changed in the owner's data this week
  propose_generalizations   (A5) repeated lessons in agent_memory → claims
  propose_profile_patch     one LLM call turning that into a whole new doc
  check_profile_budget      the gate (interaction cards bypass the budget)
  record_profile_card       what makes a delivered card consume that budget
  apply_profile_reflection  the InteractionFlow post-resolve hook

Two properties are load-bearing:

**Nothing is written without an explicit approve.** `apply_profile_reflection`
is a no-op for every response that is not `{"action": "approve"}` — a reject, a
timeout (which never reaches the hook at all), a malformed payload, an empty
doc. The `user` persona doc is the most sensitive write target in this
codebase; the default has to be "do nothing".

**Every applied patch is revertible.** The write goes through A1's
`apply_profile_patch` with `source="profile_reflection"` and the authorising
`interaction_id`, so `agent_profile_revisions` holds the exact before/after and
`revert_profile_revision` can undo it. A1's >50%-shrink guard is left in force
(`allow_shrink` is never passed) — an LLM that "summarises" the persona down to
a stub is refused rather than applied.

A5 adds `propose_generalizations`: the same weekly card additionally carries
claims that several *independent* memories agree on, so a repeated lesson can
graduate out of `agent_memory` into the durable doc. That is an inference about
the human, drawn by an LLM from rows another LLM wrote — compounding, which is
precisely why it rides the SAME human-approved `draft_review` card rather than
getting a write path of its own. Nothing here promotes anything; approval does.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any

from temporalio import activity

# The persona kind an automated writer is allowed to touch. `soul`/`agents` are
# the agent's own identity and boundaries — a reflection pass has no business
# rewriting either.
_DEFAULT_KIND = "user"

_PURPOSE = "profile_reflection"

# The reply is a whole persona document plus rationale, so the budget is large
# by the standards of a structured-extraction call. `LLMClient._reasoning_floor`
# raises it again for kimi-class models.
_MAX_TOKENS = 6000

_SYSTEM_PROMPT = (
    "You maintain the 'user context' document an AI assistant reads before every "
    "reply — the durable facts about its owner. You propose a revised document "
    "and answer with JSON only."
)

_PROMPT = """Here is the assistant's CURRENT user-context document:

\"\"\"
{current}
\"\"\"

Here is EVIDENCE gathered from the owner's data over the last {days} days:

\"\"\"
{evidence}
\"\"\"

Here are GENERALISATIONS drawn from the assistant's long-term memory — each one
is a claim that several *independent* memories agree on:

\"\"\"
{generalizations}
\"\"\"

Propose a revised version of the document.

Rules:
- Return the COMPLETE revised document, not a diff and not a summary.
- A GENERALISATION is backed by several independent observations, so weight it
  above a single mention in EVIDENCE. It is still an inference — write it as a
  plain statement and let the human reviewing this document judge it.
- Keep every existing fact that the evidence does not contradict. You are
  editing a durable record, not rewriting it — when in doubt, keep the line.
- Add only facts the evidence actually supports. Do not invent, and do not
  restate transient events (a single meeting, one receipt) as durable facts.
- Remove a line only when the evidence directly contradicts it.
- If the evidence supports no change at all, return the current document
  unchanged and say so in "rationale".
- "changed_lines" lists the lines you added, removed or rewrote, each prefixed
  with "+ ", "- " or "~ ". Keep it short — it is what the human reads first.

Return ONLY this JSON object — no prose, no code fence:
{{"proposed_doc": "...", "rationale": "...", "changed_lines": ["+ ...", "- ..."]}}
"""


# --------------------------------------------------------------- A5 constants

_GENERALIZATION_PURPOSE = "profile_generalization"

# A JSON array of short claims — nothing like the whole-document budget above.
_GENERALIZATION_MAX_TOKENS = 2500
_MAX_CLAIM_CHARS = 400

_GENERALIZATION_SYSTEM_PROMPT = (
    "You look for GENERALISATIONS in an AI assistant's long-term memory of its "
    "owner: standing facts or preferences that several independent memories "
    "agree on. You answer with JSON only."
)

_GENERALIZATION_PROMPT = """Here are the assistant's memories of its owner, as
JSON rows of {{id, content, importance, source}}:

{memories}

Find claims that at least {min_support} DIFFERENT rows independently support.

Rules:
- A claim is a durable fact or preference about the owner, in one sentence —
  not a summary of a single event.
- "supporting_memory_ids" lists the ids of the rows that support the claim. Use
  only ids from the list above, and never repeat an id within one claim.
- Do NOT emit a claim supported by fewer than {min_support} rows. Two rows
  saying a similar thing is a coincidence, not a pattern.
- "confidence" is 0.0-1.0: how sure you are this is a standing truth about the
  owner rather than something that was briefly true.
- If nothing is supported {min_support} times, return [].

Return ONLY a JSON array — no prose, no code fence:
[{{"claim": "...", "supporting_memory_ids": [1, 2, 3], "confidence": 0.0}}]
"""


def _as_memory_id(value: Any) -> int | None:
    """Strictly coerce an LLM-supplied memory id. Booleans are not ids.

    Same discipline as `MemoryActivities._as_id`: an id we cannot read as an
    integer is dropped, never guessed at.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _validate_generalizations(
    raw: Any,
    own_ids: set[int],
    *,
    min_support: int,
    min_confidence: float,
    limit: int,
) -> tuple[list[dict], int, int]:
    """LLM payload → ``(candidates, below_threshold, dropped)``.

    Three independent gates, each of which alone is enough to reject a claim:

    * **ownership** — every supporting id must be a row in the pool we actually
      sent. An invented or foreign id is discarded, and a claim can fall below
      `min_support` because of it;
    * **support** — fewer than `min_support` DISTINCT owned ids and the claim is
      dropped. "Backed by three memories" is the whole premise of promoting it;
    * **confidence** — below `min_confidence` the claim is counted and logged,
      never carded (the spec's off-week safety).

    A truncated, prose-wrapped or otherwise malformed response parses to
    ``None``/non-list and yields NO candidates — it can never be read as a
    promotion.
    """
    if isinstance(raw, dict):
        # Models like wrapping arrays in an object; accept the obvious key.
        raw = raw.get("generalizations") or raw.get("claims")
    if not isinstance(raw, list):
        return [], 0, 0

    candidates: list[dict] = []
    below = 0
    dropped = 0
    for item in raw[:50]:
        if not isinstance(item, dict):
            dropped += 1
            continue
        claim = str(item.get("claim") or "").strip()[:_MAX_CLAIM_CHARS]
        if not claim:
            dropped += 1
            continue
        ids: list[int] = []
        for value in item.get("supporting_memory_ids") or []:
            memory_id = _as_memory_id(value)
            if memory_id is not None and memory_id in own_ids and memory_id not in ids:
                ids.append(memory_id)
        if len(ids) < min_support:
            dropped += 1
            continue
        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence"))))
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence < min_confidence:
            below += 1
            continue
        candidates.append(
            {
                "claim": claim,
                "supporting_memory_ids": sorted(ids),
                "confidence": round(confidence, 3),
            }
        )
    candidates.sort(key=lambda c: c["confidence"], reverse=True)
    return candidates[: max(0, limit)], below, dropped


def _doc_fingerprint(text: str) -> str:
    """Stable handle for "the document this proposal was based on".

    Carried through the card's metadata so the applier can tell whether the doc
    moved under the human while the card was open.
    """
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


@dataclass
class ProfileActivities:
    db_pool: Any
    # A2: the reflection pass needs an LLM. None keeps every A1 activity working
    # and makes `propose_profile_patch` a documented no-op ({} → flow skips).
    llm_client: Any = None
    model: str = "gpt-oss:20b"
    # Notification budget (same knobs CuriosityActivities carries). A draft_review
    # card is a proactive push, but it goes out via `send_interaction_card`, which
    # never reaches `safe_send_message` — so the budget is consulted here instead
    # of being inherited.
    budget_enabled: bool = False
    daily_budget: int = 8
    # Evidence caps, as fields so a test (and a deployment) can shrink them
    # without editing SQL.
    max_chat: int = 40
    max_memories: int = 30
    max_corrections: int = 20
    max_finance: int = 25
    max_calendar: int = 30
    # A5 generalisation knobs. `min_support` is the spec's "≥3 rows"; anything
    # below `min_confidence` is logged and never carded.
    min_support: int = 3
    min_confidence: float = 0.7
    max_generalizations: int = 5
    # Unlike the evidence window, generalisation looks at ALL of an agent's
    # memory — a lesson repeated over three months is exactly the thing that
    # should stop living as three one-liners.
    max_generalization_pool: int = 200
    _sources: tuple[str, ...] = field(
        default=("chat", "memories", "corrections", "finance", "calendar"), repr=False
    )

    @activity.defn
    async def read_profile_context(self, agent_id: str) -> dict[str, str]:
        """The agent's four persona kinds (soul/agents/user/memory), DB-first.

        Cache-bypassing: a flow that is about to patch the profile must reason
        about what is committed now, not what a 30s TTL remembers.
        """
        from aegis.services.personalities import get_personality

        return await get_personality(self.db_pool, agent_id, use_cache=False)

    @activity.defn
    async def apply_profile_patch(self, payload: dict) -> dict:
        """Patch one persona kind and log a revision.

        Payload: ``{agent_id, kind?, new_content, source?, interaction_id?,
        allow_shrink?}`` — `kind` defaults to the user-context doc, which is
        the only one automated writers should be touching.
        """
        from aegis.services.personalities import apply_profile_patch as _apply

        result = await _apply(
            self.db_pool,
            payload["agent_id"],
            payload.get("kind") or _DEFAULT_KIND,
            payload.get("new_content") or "",
            source=payload.get("source") or "worker",
            interaction_id=payload.get("interaction_id"),
            allow_shrink=bool(payload.get("allow_shrink")),
        )
        activity.logger.info(
            "profile_patch_applied agent=%s kind=%s revision=%s source=%s %s->%s chars",
            result["agent_id"],
            result["kind"],
            result["revision_id"],
            result["source"],
            result["before_length"],
            result["after_length"],
        )
        return result

    # ------------------------------------------------------------- A2 evidence

    @activity.defn
    async def gather_profile_evidence(self, agent_id: str, lookback_days: int = 7) -> dict:
        """What happened in the owner's data since last week, per source.

        Every source is independently try/excepted (the shape
        `BriefingActivities.gather_briefing_changes` uses): a dead source costs
        its own slice and lands in `failed`, never the whole bundle. `total` is
        the count the flow gates on — zero means there is nothing to reflect on
        and no card should be sent.
        """
        days = max(1, int(lookback_days or 7))
        evidence: dict[str, Any] = {}
        failed: list[str] = []

        for name, gather in (
            ("chat", self._evidence_chat),
            ("memories", self._evidence_memories),
            ("corrections", self._evidence_corrections),
            ("finance", self._evidence_finance),
            ("calendar", self._evidence_calendar),
        ):
            try:
                evidence[name] = await gather(agent_id, days)
            except Exception as exc:  # noqa: BLE001 — one dead source is not a dead run
                activity.logger.warning(
                    "profile_evidence_source_failed source=%s err=%s", name, str(exc)[:200]
                )
                evidence[name] = []
                failed.append(name)

        counts = {k: len(v) for k, v in evidence.items()}
        total = sum(counts.values())
        activity.logger.info(
            "profile_evidence_gathered agent=%s days=%d total=%d failed=%s",
            agent_id,
            days,
            total,
            ",".join(failed) or "-",
        )
        return {
            **evidence,
            "counts": counts,
            "total": total,
            "failed": failed,
            "lookback_days": days,
        }

    async def _evidence_chat(self, agent_id: str, days: int) -> list[str]:
        rows = await self.db_pool.fetch(
            "SELECT content FROM chat_history "
            "WHERE agent_id = $1 AND role = 'user' AND content <> '' "
            "AND created_at >= now() - make_interval(days => $2) "
            "ORDER BY created_at DESC LIMIT $3",
            agent_id,
            days,
            self.max_chat,
        )
        return [str(r["content"])[:400] for r in rows if (r["content"] or "").strip()]

    async def _evidence_memories(self, agent_id: str, days: int) -> list[str]:
        rows = await self.db_pool.fetch(
            "SELECT content, importance, source FROM agent_memory "
            "WHERE agent_id = $1 AND created_at >= now() - make_interval(days => $2) "
            "ORDER BY importance DESC, created_at DESC LIMIT $3",
            agent_id,
            days,
            self.max_memories,
        )
        return [f"[{r['source']} {float(r['importance']):.2f}] {str(r['content'])[:400]}" for r in rows]

    async def _evidence_corrections(self, agent_id: str, days: int) -> list[str]:
        """Resolved interactions that carried a human REASON.

        The same keys `record_correction_from_interaction` treats as a
        correction — a bare accept teaches nothing.
        """
        rows = await self.db_pool.fetch(
            "SELECT prompt, response FROM interactions "
            "WHERE agent_id = $1 AND status = 'resolved' "
            "AND resolved_at >= now() - make_interval(days => $2) "
            "AND (response ? 'reason' OR response ? 'note' OR response ? 'feedback' "
            "     OR response ? 'comment' OR response ? 'correction') "
            "ORDER BY resolved_at DESC LIMIT $3",
            agent_id,
            days,
            self.max_corrections,
        )
        out: list[str] = []
        for r in rows:
            resp = r["response"] or {}
            if isinstance(resp, str):
                import json

                try:
                    resp = json.loads(resp)
                except ValueError:
                    resp = {}
            reason = next(
                (
                    str(resp[k]).strip()
                    for k in ("reason", "note", "feedback", "comment", "correction")
                    if isinstance(resp, dict) and resp.get(k)
                ),
                "",
            )
            if reason:
                out.append(f"asked \"{str(r['prompt'] or '')[:160]}\" → {reason[:300]}")
        return out

    async def _evidence_finance(self, agent_id: str, days: int) -> list[str]:
        charges = await self.db_pool.fetch(
            "SELECT vendor_name, category, cadence FROM finance.recurring_charge "
            "WHERE status = 'active' ORDER BY last_seen_at DESC LIMIT $1",
            self.max_finance,
        )
        receipts = await self.db_pool.fetch(
            "SELECT sender, subject FROM finance.receipt_email "
            "WHERE received_at >= now() - make_interval(days => $1) "
            "ORDER BY received_at DESC LIMIT $2",
            days,
            self.max_finance,
        )
        out = [
            f"recurring: {r['vendor_name']} ({r['category']}, {r['cadence']})"
            for r in charges
            if (r["vendor_name"] or "").strip()
        ]
        out += [
            f"receipt: {str(r['sender'])[:80]} — {str(r['subject'] or '')[:120]}" for r in receipts
        ]
        return out

    async def _evidence_calendar(self, agent_id: str, days: int) -> list[str]:
        """Events from the `calendar_events_%` settings KV rows —
        the same source `BriefingActivities.gather_calendar_events` reads."""
        import json

        rows = await self.db_pool.fetch(
            "SELECT value FROM settings WHERE key LIKE 'calendar_events_%'"
        )
        out: list[str] = []
        for row in rows:
            raw = row["value"]
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(parsed, list):
                continue
            for ev in parsed:
                if not isinstance(ev, dict):
                    continue
                title = str(ev.get("summary") or ev.get("title") or "").strip()
                when = str(ev.get("start") or ev.get("start_time") or "")[:32]
                if title:
                    out.append(f"event: {title[:120]} ({when})")
                if len(out) >= self.max_calendar:
                    return out
        return out

    # ------------------------------------------------- A5 generalisation

    async def _promoted_memory_ids(self, agent_id: str) -> set[int]:
        """Memory ids already promoted into the profile by an approved card.

        The `interactions` row IS the promotion ledger — no column and no
        second table. A row counts as a promotion only when all three are true:

          * the human answered `{"action": "approve"}` (a reject, a timeout or
            an archived card promotes nothing), AND
          * `apply_profile_reflection` actually wrote — proven by an
            `agent_profile_revisions` row carrying that interaction id. An
            approval that A1's shrink guard refused therefore leaves the
            memories in the pool, where they belong.

        This is what makes the pass idempotent across weeks: once a claim's
        supporting rows have graduated, they never reach the model again, so
        the claim cannot be re-proposed.
        """
        rows = await self.db_pool.fetch(
            "SELECT DISTINCT jsonb_array_elements_text(i.metadata -> 'promoted_memory_ids') AS mid "
            "FROM interactions i "
            "WHERE i.agent_id = $1 AND i.origin = $2 AND i.status = 'resolved' "
            "AND i.response ->> 'action' = 'approve' "
            "AND jsonb_typeof(i.metadata -> 'promoted_memory_ids') = 'array' "
            "AND EXISTS (SELECT 1 FROM agent_profile_revisions r WHERE r.interaction_id = i.id)",
            agent_id,
            _PURPOSE,
        )
        out: set[int] = set()
        for row in rows:
            memory_id = _as_memory_id(row["mid"])
            if memory_id is not None:
                out.add(memory_id)
        return out

    @activity.defn
    async def propose_generalizations(self, agent_id: str) -> dict:
        """Repeated lessons in `agent_memory` → claims worth promoting.

        Returns ``{status, candidates, below_threshold, dropped, pool,
        excluded}``. `candidates` is ALWAYS a list and is empty on every
        failure path (no client, too few rows, timeout, truncation, malformed
        JSON) — this activity never raises, because a broken generalisation
        pass must not cost the owner their weekly draft.

        Nothing here writes: the claims are handed to `propose_profile_patch`
        and ride the human-approved `draft_review` card. `agent_memory` is read
        only.

        The call is recorded in `llm_calls` under
        ``purpose='profile_generalization'`` on SUCCESS as well as failure —
        `LLMClient.think()` records only failures and raises
        `LLMTruncationError` outside its own recording try, so both rows are
        written here (the `aegis.services.capture_classify` template).
        """
        from aegis.llm import LLMTruncationError, parse_llm_json
        from aegis.observability import record_llm_call

        base: dict[str, Any] = {
            "candidates": [],
            "below_threshold": 0,
            "dropped": 0,
            "pool": 0,
            "excluded": 0,
        }

        excluded = await self._promoted_memory_ids(agent_id)
        rows = await self.db_pool.fetch(
            "SELECT id, content, importance, source FROM agent_memory "
            "WHERE agent_id = $1 AND NOT (id = ANY($2::bigint[])) "
            "ORDER BY importance DESC, created_at DESC LIMIT $3",
            agent_id,
            sorted(excluded),
            self.max_generalization_pool,
        )
        base["pool"] = len(rows)
        base["excluded"] = len(excluded)

        min_support = max(2, int(self.min_support))
        if len(rows) < min_support:
            # Fewer rows than it takes to support one claim — no call to make.
            return {**base, "status": "skipped", "reason": "too_few_memories"}
        if self.llm_client is None:
            activity.logger.warning("profile_generalize_no_llm agent=%s", agent_id)
            return {**base, "status": "skipped", "reason": "no_llm_client"}

        import json as _json

        payload = _json.dumps(
            [
                {
                    "id": int(r["id"]),
                    "content": str(r["content"])[:400],
                    "importance": round(float(r["importance"]), 2),
                    "source": str(r["source"]),
                }
                for r in rows
            ]
        )

        _t0 = time.monotonic()
        try:
            result = await self.llm_client.think(
                prompt=_GENERALIZATION_PROMPT.format(
                    memories=payload[:16000], min_support=min_support
                ),
                model=self.model,
                system_prompt=_GENERALIZATION_SYSTEM_PROMPT,
                max_tokens=_GENERALIZATION_MAX_TOKENS,
                db_pool=self.db_pool,
                purpose=_GENERALIZATION_PURPOSE,
                agent_id=agent_id,
            )
        except LLMTruncationError as exc:
            # Raised AFTER a successful HTTP call, outside think()'s own
            # failure-recording try — without this row a truncating model looks
            # like zero traffic.
            activity.logger.warning("profile_generalize_truncated err=%s", str(exc)[:200])
            await record_llm_call(
                self.db_pool,
                model=self.model,
                prompt_tokens=0,
                completion_tokens=0,
                latency_ms=int((time.monotonic() - _t0) * 1000),
                purpose=_GENERALIZATION_PURPOSE,
                agent_id=agent_id,
                status="error",
                error=f"truncated: {exc}"[:500],
            )
            return {**base, "status": "skipped", "reason": "truncated"}
        except Exception as exc:  # noqa: BLE001 — no claims beats a failed run
            activity.logger.warning(
                "profile_generalize_failed err=%s type=%s", str(exc)[:200], type(exc).__name__
            )
            return {**base, "status": "skipped", "reason": "llm_failed"}

        await record_llm_call(
            self.db_pool,
            model=result.get("model", self.model),
            prompt_tokens=result.get("prompt_tokens", 0),
            completion_tokens=result.get("completion_tokens", 0),
            latency_ms=int((time.monotonic() - _t0) * 1000),
            purpose=_GENERALIZATION_PURPOSE,
            agent_id=agent_id,
        )

        parsed = parse_llm_json(result.get("response", ""))
        candidates, below, dropped = _validate_generalizations(
            parsed,
            {int(r["id"]) for r in rows},
            min_support=min_support,
            min_confidence=float(self.min_confidence),
            limit=int(self.max_generalizations),
        )
        activity.logger.info(
            "profile_generalize agent=%s pool=%d excluded=%d candidates=%d "
            "below_threshold=%d dropped=%d",
            agent_id,
            len(rows),
            len(excluded),
            len(candidates),
            below,
            dropped,
        )
        return {
            **base,
            "status": "ok" if parsed is not None else "unparseable",
            "candidates": candidates,
            "below_threshold": below,
            "dropped": dropped,
        }

    # ------------------------------------------------------------- A2 proposal

    @activity.defn
    async def propose_profile_patch(
        self,
        agent_id: str,
        evidence: dict,
        current_doc: str,
        generalizations: list | None = None,
    ) -> dict:
        """One LLM call: evidence + generalisations + current doc → a proposed doc.

        `generalizations` (A5) are the validated claims from
        `propose_generalizations`, rendered into their own prompt block so the
        model can weight a claim three memories agree on above a single mention
        in the week's evidence. Absent/empty is the A2 behaviour, unchanged.

        Returns ``{proposed_doc, rationale, changed_lines, revision_of}`` or
        ``{}`` on ANY failure (no client, no evidence, timeout, truncation,
        unparseable JSON, empty doc). The flow treats ``{}`` as
        ``status="skipped"`` — a quiet week is always better than a failed run
        or, worse, a card proposing nonsense.

        The call is recorded in `llm_calls` under
        ``purpose='profile_reflection'`` on SUCCESS as well as failure.
        `LLMClient.think()` records only failures, and it raises
        `LLMTruncationError` outside its own recording try — so both the success
        row and the truncation row have to be written here, exactly as
        `aegis.services.capture_classify` does.
        """
        from aegis.llm import LLMTruncationError, parse_llm_json
        from aegis.observability import record_llm_call

        current = current_doc or ""
        if self.llm_client is None:
            activity.logger.warning("profile_propose_no_llm agent=%s", agent_id)
            return {}

        rendered = self._render_evidence(evidence or {})
        if not rendered:
            return {}

        _t0 = time.monotonic()
        try:
            result = await self.llm_client.think(
                prompt=_PROMPT.format(
                    current=current[:20000],
                    evidence=rendered[:20000],
                    generalizations=self._render_generalizations(generalizations),
                    days=int((evidence or {}).get("lookback_days") or 7),
                ),
                model=self.model,
                system_prompt=_SYSTEM_PROMPT,
                max_tokens=_MAX_TOKENS,
                db_pool=self.db_pool,
                purpose=_PURPOSE,
                agent_id=agent_id,
            )
        except LLMTruncationError as exc:
            # think() raises this AFTER a successful HTTP call, outside its own
            # failure-recording try — nothing lands in llm_calls unless we write
            # it here, and a truncating model would look like zero traffic.
            activity.logger.warning("profile_propose_truncated err=%s", str(exc)[:200])
            await record_llm_call(
                self.db_pool,
                model=self.model,
                prompt_tokens=0,
                completion_tokens=0,
                latency_ms=int((time.monotonic() - _t0) * 1000),
                purpose=_PURPOSE,
                agent_id=agent_id,
                status="error",
                error=f"truncated: {exc}"[:500],
            )
            return {}
        except Exception as exc:  # noqa: BLE001 — a quiet week beats a failed run
            # think() already wrote the llm_calls failure row (db_pool + purpose
            # were passed); the kill-switch path raises before any row.
            activity.logger.warning(
                "profile_propose_failed err=%s type=%s", str(exc)[:200], type(exc).__name__
            )
            return {}

        await record_llm_call(
            self.db_pool,
            model=result.get("model", self.model),
            prompt_tokens=result.get("prompt_tokens", 0),
            completion_tokens=result.get("completion_tokens", 0),
            latency_ms=int((time.monotonic() - _t0) * 1000),
            purpose=_PURPOSE,
            agent_id=agent_id,
        )

        parsed = parse_llm_json(result.get("response", ""))
        if not isinstance(parsed, dict):
            activity.logger.warning("profile_propose_unparseable agent=%s", agent_id)
            return {}
        proposed = parsed.get("proposed_doc")
        if not isinstance(proposed, str) or not proposed.strip():
            activity.logger.warning("profile_propose_empty_doc agent=%s", agent_id)
            return {}

        changed = [str(x)[:300] for x in (parsed.get("changed_lines") or []) if str(x).strip()]
        return {
            "proposed_doc": proposed,
            "rationale": str(parsed.get("rationale") or "")[:1000],
            "changed_lines": changed[:40],
            "revision_of": _doc_fingerprint(current),
            "unchanged": proposed.strip() == current.strip(),
        }

    def _render_evidence(self, evidence: dict) -> str:
        """Evidence bundle → the prompt block. Empty string when there is none."""
        blocks: list[str] = []
        for name in self._sources:
            items = evidence.get(name) or []
            if not items:
                continue
            body = "\n".join(f"- {str(i)}" for i in items)
            blocks.append(f"## {name}\n{body}")
        return "\n\n".join(blocks)

    @staticmethod
    def _render_generalizations(candidates: list | None) -> str:
        """A5 claims → the prompt block. "(none)" when there are none, so the
        model is told there is nothing to weight rather than being handed an
        empty section it might read as an instruction."""
        lines = []
        for c in candidates or []:
            if not isinstance(c, dict):
                continue
            claim = str(c.get("claim") or "").strip()
            if not claim:
                continue
            support = len(c.get("supporting_memory_ids") or [])
            lines.append(
                f"- {claim[:_MAX_CLAIM_CHARS]} "
                f"(backed by {support} memories, confidence {float(c.get('confidence') or 0):.2f})"
            )
        return "\n".join(lines) if lines else "(none)"

    # --------------------------------------------------------------- A2 budget

    @activity.defn
    async def check_profile_budget(self, agent_id: str, max_per_day: int = 1) -> dict:
        """Read-only gate the flow consults BEFORE it spawns anything.

        Three reasons to stay quiet, checked in this order:

          `budget`        — this flow already carded today. Checked first so a
                            same-day rerun reports the cap it hit rather than
                            the still-open card that cap left behind.
          `global_budget` — the shared daily notification budget is spent
                            (`aegis.services.notifications.should_send`; a no-op
                            while `notification_budget_enabled` is off, which is
                            the current deployment state — which is exactly why
                            the per-flow cap above exists as its own check).
          `pending`       — an unanswered profile draft is still open. Stacking
                            a second proposed rewrite on top of the first is how
                            a careful editor becomes a spammer, and the second
                            draft would be computed from a doc the human has
                            already been asked to change.
        """
        from aegis.services.notifications import should_send

        sent_today = int(
            await self.db_pool.fetchval(
                "SELECT count(*) FROM notification_log "
                "WHERE log_event = $1 AND sent "
                "AND created_at >= date_trunc('day', now())",
                _PURPOSE,
            )
            or 0
        )
        pending = int(
            await self.db_pool.fetchval(
                "SELECT count(*) FROM interactions "
                "WHERE origin = $1 AND status = 'pending' AND agent_id = $2",
                _PURPOSE,
                agent_id,
            )
            or 0
        )
        allow, global_today = await should_send(
            self.db_pool, enabled=self.budget_enabled, daily_budget=self.daily_budget
        )

        if sent_today >= max(0, int(max_per_day)):
            reason = "budget"
        elif not allow:
            reason = "global_budget"
        elif pending:
            reason = "pending"
        else:
            reason = "ok"

        activity.logger.info(
            "profile_budget agent=%s reason=%s sent_today=%d pending=%d global_today=%d",
            agent_id,
            reason,
            sent_today,
            pending,
            global_today,
        )
        return {
            "allow": reason == "ok",
            "reason": reason,
            "sent_today": sent_today,
            "pending": pending,
            "global_today": global_today,
        }

    @activity.defn
    async def record_profile_card(self, agent_id: str, sent: bool = True) -> dict:
        """Charge a delivered draft_review card to the daily notification budget."""
        from aegis.services.notifications import record_notification

        await record_notification(self.db_pool, agent_id, _PURPOSE, sent)
        return {"recorded": True, "sent": sent}

    # ---------------------------------------------------------------- A2 apply

    @activity.defn
    async def apply_profile_reflection(
        self, interaction_id: str, response: dict | None, metadata: dict | None
    ) -> dict:
        """InteractionFlow post-resolve hook for a `draft_review` card.

        The ONLY response that writes anything is ``{"action": "approve"}``.
        The text written is ``response["edited_doc"]`` when the human edited the
        draft in the admin panel, falling back to the ``proposed_doc`` the card
        was built from. Everything else — a reject, a missing action, a blank
        doc — is a no-op that returns a status and touches neither
        `agent_personalities` nor `agent_profile_revisions`.

        The exact payload shape is produced by the draft_review panel in
        `admin-panel/frontend/src/pages/InteractionDetail.tsx`; the two sides are
        pinned together by `tests/worker/test_profile_reflection_e2e.py`, which
        reads that file and drives the real resolve route with what it submits.

        `allow_shrink` is deliberately not passed: A1's >50%-loss guard raises,
        and a refused patch is reported here rather than crashing the hook (the
        interaction is already resolved by the time we run, so raising would only
        lose the reason).
        """
        response = response if isinstance(response, dict) else {}
        metadata = metadata if isinstance(metadata, dict) else {}
        action = str(response.get("action") or "").strip().lower()

        if action != "approve":
            activity.logger.info(
                "profile_reflection_not_applied interaction=%s action=%s",
                interaction_id,
                action or "-",
            )
            return {"applied": False, "status": action or "no_action"}

        doc = response.get("edited_doc")
        used = "edited_doc"
        if not isinstance(doc, str) or not doc.strip():
            doc = metadata.get("proposed_doc")
            used = "proposed_doc"
        if not isinstance(doc, str) or not doc.strip():
            activity.logger.warning(
                "profile_reflection_empty_doc interaction=%s", interaction_id
            )
            return {"applied": False, "status": "empty_doc"}

        agent_id = str(metadata.get("agent_id") or "").strip()
        if not agent_id:
            activity.logger.warning(
                "profile_reflection_no_agent interaction=%s", interaction_id
            )
            return {"applied": False, "status": "no_agent"}
        kind = str(metadata.get("kind") or _DEFAULT_KIND)

        from aegis.services.personalities import apply_profile_patch as _apply
        from aegis.services.personalities import get_personality

        # Did the doc move under the human while the card was open? A week is
        # long enough for a hand edit through the admin UI. We still apply —
        # the human approved the text in front of them and the revision row
        # holds what it replaced — but a silent clobber would be undiagnosable.
        base = str(metadata.get("revision_of") or "")
        if base:
            live = await get_personality(self.db_pool, agent_id, use_cache=False)
            if _doc_fingerprint(live.get(kind, "") or "") != base:
                activity.logger.warning(
                    "profile_reflection_base_drift interaction=%s agent=%s kind=%s",
                    interaction_id,
                    agent_id,
                    kind,
                )

        try:
            result = await _apply(
                self.db_pool,
                agent_id,
                kind,
                doc,
                source=_PURPOSE,
                interaction_id=interaction_id,
            )
        except ValueError as exc:
            # A1's shrink guard, or an unknown kind in the card metadata.
            activity.logger.warning(
                "profile_reflection_refused interaction=%s agent=%s err=%s",
                interaction_id,
                agent_id,
                str(exc)[:300],
            )
            return {"applied": False, "status": "refused", "reason": str(exc)[:300]}

        # A5: which memories this approval graduated. Deliberately NOT written
        # back to `agent_memory` — the card's own metadata plus the revision row
        # this apply just wrote ARE the ledger `_promoted_memory_ids` reads, so
        # the promotion is recorded by the same transaction that makes it true
        # and nothing mutates the owner's memory rows.
        promoted = sorted(
            {
                mid
                for mid in (_as_memory_id(v) for v in (metadata.get("promoted_memory_ids") or []))
                if mid is not None
            }
        )

        activity.logger.info(
            "profile_reflection_applied interaction=%s agent=%s kind=%s revision=%s "
            "source=%s %s->%s chars promoted_memories=%d",
            interaction_id,
            agent_id,
            kind,
            result["revision_id"],
            result["source"],
            result["before_length"],
            result["after_length"],
            len(promoted),
        )
        return {
            "applied": True,
            "status": "applied",
            "doc_source": used,
            "promoted_memory_ids": promoted,
            **result,
        }
