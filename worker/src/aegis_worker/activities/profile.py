"""Profile activities — the auditable persona write path, for flows.

Thin wrappers over aegis.services.personalities so a flow never imports the
FastAPI layer. A1 shipped the write substrate (`read_profile_context`,
`apply_profile_patch`); A2 adds everything `ProfileReflectionFlow` needs to
propose a weekly edit to the agent's own `user` doc and land it only after a
human said yes:

  gather_profile_evidence   what changed in the owner's data this week
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

Propose a revised version of the document.

Rules:
- Return the COMPLETE revised document, not a diff and not a summary.
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

    # ------------------------------------------------------------- A2 proposal

    @activity.defn
    async def propose_profile_patch(
        self, agent_id: str, evidence: dict, current_doc: str
    ) -> dict:
        """One LLM call: evidence + current doc → a whole proposed doc.

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

        activity.logger.info(
            "profile_reflection_applied interaction=%s agent=%s kind=%s revision=%s "
            "source=%s %s->%s chars",
            interaction_id,
            agent_id,
            kind,
            result["revision_id"],
            result["source"],
            result["before_length"],
            result["after_length"],
        )
        return {"applied": True, "status": "applied", "doc_source": used, **result}
