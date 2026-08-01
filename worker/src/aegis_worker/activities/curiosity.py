"""Curiosity gap-finder — deterministic detection of what AEGIS does NOT know.

Turns data gaps into at most a handful of ranked candidate questions. Detection
itself needs no LLM: three independent SQL detectors look for a subject that is
demonstrably present in the owner's data but absent from everything the agent
has ever been told (its memories and its persona docs).

  (a) calendar  — an attendee who keeps showing up on ingested calendar events
                  and is never mentioned in chat_history / agent_memory / profile
  (b) finance   — an active `finance.recurring_charge` whose vendor is never
                  mentioned in agent_memory / profile
  (c) todoist   — a project carrying real OPEN task volume with no profile
                  context (a finished project is not a gap)

Each detector is independently try/excepted: a broken one costs its own
candidates, never the run. `novelty_key` (`attendee:<email>`, `charge:<vendor>`,
`project:<name>`) is the never-ask-twice handle — any non-archived `interactions`
row already carrying that key removes the candidate.

An optional single LLM pass rephrases the questions; the deterministic template
is always computed first and is the fallback, so an absent or failing LLM still
yields the same candidates with usable text. Zero detectors firing returns `[]`
— never a synthetic filler question.

A7 adds the three activities `CuriosityCardFlow` needs around the detector:
`check_curiosity_budget` (the gate — interaction cards bypass
`safe_send_message`, so the notification budget has to be consulted here),
`record_curiosity_card` (what makes a delivered card consume that budget), and
`apply_curiosity_answer` (the InteractionFlow post-resolve hook that banks the
owner's reply as durable memory).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

from temporalio import activity

# `Attendees: a@x.com, b@y.com` — the line `calendar_event_to_content` writes
# (core/src/aegis/services/claims.py) into the chunk text of every ingested event.
_ATTENDEE_LINE_RE = re.compile(r"^Attendees:\s*(.+)$", re.MULTILINE)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

_DETECTORS = ("calendar_attendee", "recurring_charge", "todoist_project")


@dataclass
class CuriosityActivities:
    """Detect knowledge gaps worth asking the owner about."""

    db_pool: Any
    llm_client: Any = None
    model: str = "gpt-oss:20b"
    # Thresholds are fields, not literals, so a deployment (and a test) can say
    # what "recurring" and "frequently hit" mean without editing the detector.
    min_attendee_events: int = 3
    min_project_tasks: int = 5
    # The operator's own email addresses (Settings.owner_emails — the DB-backed
    # `owner_emails` integration config). Google puts the calendar owner in
    # every event's `attendees`, so without this the owner is a "stranger you
    # keep meeting". Empty (unconfigured) = no exclusion, same as before.
    owner_emails: frozenset[str] = frozenset()
    # Notification budget (same knobs DeliveryActivities carries). A curiosity
    # card is a proactive push like any other, but it goes out via
    # `send_interaction_card`, which never reaches `safe_send_message` — so the
    # budget is consulted here instead of being inherited.
    budget_enabled: bool = False
    daily_budget: int = 8

    @activity.defn
    async def find_curiosity_gaps(self, agent_id: str = "sebas", limit: int = 5) -> list[dict]:
        """At most `limit` ranked gap candidates for `agent_id`.

        Returns `[{gap_type, subject, question, evidence, novelty_key}]`,
        highest-signal first. Empty list when nothing is missing.
        """
        known = await self._known_text(agent_id)
        scored: list[tuple[float, dict]] = []

        for name in _DETECTORS:
            try:
                scored += await getattr(self, f"_detect_{name}")(agent_id, known)
            except Exception as exc:  # noqa: BLE001 — one bad detector must not kill the run
                activity.logger.warning(
                    "curiosity_detector_failed detector=%s err=%s", name, str(exc)[:200]
                )

        if not scored:
            activity.logger.info("curiosity_gaps agent=%s candidates=0", agent_id)
            return []

        asked = await self._already_asked()
        scored = [(s, c) for s, c in scored if c["novelty_key"] not in asked]
        # Stable order: strongest signal first, novelty_key breaks ties so the
        # same data always produces the same ranking.
        scored.sort(key=lambda sc: (-sc[0], sc[1]["novelty_key"]))
        candidates = [c for _, c in scored[: max(0, limit)]]

        if candidates:
            candidates = await self._phrase(agent_id, candidates)
        activity.logger.info(
            "curiosity_gaps agent=%s candidates=%d suppressed=%d",
            agent_id,
            len(candidates),
            len(asked),
        )
        return candidates

    # ------------------------------------------------------------------ context

    async def _known_text(self, agent_id: str) -> str:
        """Everything the agent has been told, lowercased, as one haystack.

        Memories are capped at 50/agent by the pruner and persona docs are a few
        KB, so this stays small enough to substring-match against.
        """
        rows = await self.db_pool.fetch(
            "SELECT content FROM agent_memory WHERE agent_id = $1", agent_id
        )
        rows += await self.db_pool.fetch(
            "SELECT content FROM agent_personalities WHERE agent_id = $1", agent_id
        )
        return "\n".join((r["content"] or "") for r in rows).lower()

    async def _mentioned_in_chat(self, agent_id: str, needle: str) -> bool:
        row = await self.db_pool.fetchrow(
            "SELECT 1 FROM chat_history WHERE agent_id = $1 AND content ILIKE $2 LIMIT 1",
            agent_id,
            f"%{needle}%",
        )
        return row is not None

    async def _already_asked(self) -> set[str]:
        """novelty_keys carried by any non-archived interaction — never ask twice.

        Archived (timed-out) cards deliberately do NOT suppress: an unanswered
        question is not an answered one.
        """
        rows = await self.db_pool.fetch(
            "SELECT DISTINCT metadata->>'novelty_key' AS k FROM interactions "
            "WHERE status <> 'archived' AND metadata ? 'novelty_key'"
        )
        return {r["k"] for r in rows if r["k"]}

    # ---------------------------------------------------------------- detectors

    async def _detect_calendar_attendee(
        self, agent_id: str, known: str
    ) -> list[tuple[float, dict]]:
        """A face the owner keeps meeting that AEGIS has never heard of."""
        rows = await self.db_pool.fetch(
            "SELECT c.content_id, k.chunk_text FROM knowledge_content c "
            "JOIN knowledge_chunks k ON k.content_id = c.content_id "
            "WHERE c.source_type = 'calendar' "
            "ORDER BY c.ingested_at DESC LIMIT 500"
        )
        seen: dict[str, set[str]] = {}
        for r in rows:
            for line in _ATTENDEE_LINE_RE.findall(r["chunk_text"] or ""):
                for email in _EMAIL_RE.findall(line):
                    seen.setdefault(email.lower(), set()).add(r["content_id"])

        # Normalized here so the match is case-insensitive however the operator
        # typed the config, and applied here (not at ingest) so events already
        # in the knowledge store are covered too.
        owners = {o.strip().lower() for o in self.owner_emails}

        out: list[tuple[float, dict]] = []
        for email, events in seen.items():
            # The owner attends their own meetings — never a gap.
            if email in owners:
                continue
            if len(events) < self.min_attendee_events:
                continue
            local = email.split("@")[0]
            if email in known or local in known:
                continue
            if await self._mentioned_in_chat(agent_id, email):
                continue
            out.append(
                (
                    float(len(events)),
                    {
                        "gap_type": "calendar_attendee",
                        "subject": email,
                        "question": (
                            f"You've met with {email} on {len(events)} calendar events, "
                            "but I know nothing about them. Who are they to you?"
                        ),
                        "evidence": {"events": len(events)},
                        "novelty_key": f"attendee:{email}",
                    },
                )
            )
        return out

    async def _detect_recurring_charge(self, agent_id: str, known: str) -> list[tuple[float, dict]]:
        """Money leaving every month for something never explained."""
        rows = await self.db_pool.fetch(
            "SELECT vendor_name, category, cadence, monthly_home_equivalent "
            "FROM finance.recurring_charge WHERE status = 'active' "
            "ORDER BY monthly_home_equivalent DESC LIMIT 50"
        )
        out: list[tuple[float, dict]] = []
        for r in rows:
            vendor = (r["vendor_name"] or "").strip()
            if not vendor or vendor.lower() in known:
                continue
            monthly = float(r["monthly_home_equivalent"] or 0)
            out.append(
                (
                    # Cost is the signal — a big unexplained charge outranks a
                    # small one, and any charge outranks a bare calendar face.
                    10.0 + monthly,
                    {
                        "gap_type": "recurring_charge",
                        "subject": vendor,
                        "question": (
                            f"You have an active {r['cadence']} charge from {vendor}, "
                            "but nothing on record about why. What is it for?"
                        ),
                        "evidence": {
                            "category": r["category"],
                            "cadence": r["cadence"],
                            "monthly_home_equivalent": monthly,
                        },
                        "novelty_key": f"charge:{vendor.lower()}",
                    },
                )
            )
        return out

    async def _detect_todoist_project(self, agent_id: str, known: str) -> list[tuple[float, dict]]:
        """Where the work actually goes, with no context on what it is."""
        rows = await self.db_pool.fetch(
            "SELECT p.name, COUNT(t.id) AS tasks FROM todoist_projects p "
            "JOIN todoist_tasks t ON t.project_id = p.id "
            "WHERE p.is_archived = FALSE AND t.is_completed = FALSE "
            "GROUP BY p.name HAVING COUNT(t.id) >= $1 "
            "ORDER BY COUNT(t.id) DESC LIMIT 20",
            self.min_project_tasks,
        )
        out: list[tuple[float, dict]] = []
        for r in rows:
            name = (r["name"] or "").strip()
            if not name or name.lower() in known:
                continue
            out.append(
                (
                    float(r["tasks"]),
                    {
                        "gap_type": "todoist_project",
                        "subject": name,
                        "question": (
                            f"'{name}' carries {r['tasks']} of your tasks and I have no "
                            "context on it. What is it, and what does done look like?"
                        ),
                        "evidence": {"tasks": int(r["tasks"])},
                        "novelty_key": f"project:{name.lower()}",
                    },
                )
            )
        return out

    # ------------------------------------------------------------------ phrasing

    async def _phrase(self, agent_id: str, candidates: list[dict]) -> list[dict]:
        """One optional LLM pass to make the questions sound human.

        Deterministic text is already in place; any failure leaves it there.
        """
        if not self.llm_client:
            return candidates

        from aegis.llm import parse_llm_json
        from aegis.observability import record_llm_call

        listing = "\n".join(
            f"{i}. [{c['gap_type']}] {c['subject']} — {c['question']}"
            for i, c in enumerate(candidates)
        )
        _t0 = time.monotonic()
        try:
            result = await self.llm_client.think(
                prompt=listing,
                model=self.model,
                system_prompt=(
                    "You are rephrasing an assistant's questions to its owner about gaps "
                    "in what it knows. Keep each under 25 words, warm and specific, one "
                    "question only, no preamble. Return JSON: "
                    '[{"index": 0, "question": "..."}]'
                ),
                max_tokens=1200,
                db_pool=self.db_pool,
                purpose="curiosity_phrasing",
                agent_id=agent_id,
            )
        except Exception as exc:  # noqa: BLE001 — degrade to the template, never crash
            activity.logger.warning("curiosity_phrasing_failed err=%s", str(exc)[:200])
            return candidates

        await record_llm_call(
            self.db_pool,
            model=result.get("model", self.model),
            prompt_tokens=result.get("prompt_tokens", 0),
            completion_tokens=result.get("completion_tokens", 0),
            latency_ms=int((time.monotonic() - _t0) * 1000),
            purpose="curiosity_phrasing",
            agent_id=agent_id,
        )

        try:
            parsed = parse_llm_json(result.get("response", ""))
            for item in parsed or []:
                if not isinstance(item, dict):
                    continue
                idx = int(item.get("index", -1))
                text = (item.get("question") or "").strip()
                if text and 0 <= idx < len(candidates):
                    candidates[idx]["question"] = text
        except Exception as exc:  # noqa: BLE001
            activity.logger.warning("curiosity_phrasing_parse_failed err=%s", str(exc)[:200])
        return candidates

    # -------------------------------------------------------------------- A7

    @activity.defn
    async def check_curiosity_budget(self, agent_id: str = "sebas", max_per_day: int = 1) -> dict:
        """Read-only gate the flow consults BEFORE it spawns anything.

        Three reasons to stay quiet, checked in this order:

          `budget`        — this flow already sent its card(s) today. Checked
                            first so a second run on the same day reports the
                            cap it hit, not the still-open card that cap left
                            behind.
          `global_budget` — the shared daily notification budget is spent
                            (`aegis.services.notifications.should_send`; a
                            no-op while `notification_budget_enabled` is off,
                            which is the current deployment state).
          `pending`       — an unanswered curiosity card is still open. Asking
                            a second question before the first is answered is
                            how a helpful assistant becomes a nag.

        Also reports whether `owner_emails` is configured. It is EMPTY in the
        current deployment, and Google puts the calendar owner in every event's
        attendee list, so the flow uses this to refuse the calendar-attendee
        lane rather than risk asking the owner who they are.
        """
        from aegis.services.notifications import should_send

        sent_today = int(
            await self.db_pool.fetchval(
                "SELECT count(*) FROM notification_log "
                "WHERE log_event = 'curiosity_card' AND sent "
                "AND created_at >= date_trunc('day', now())"
            )
            or 0
        )
        pending = int(
            await self.db_pool.fetchval(
                "SELECT count(*) FROM interactions "
                "WHERE origin = 'curiosity' AND status = 'pending'"
            )
            or 0
        )
        allow, global_today = await should_send(
            self.db_pool, enabled=self.budget_enabled, daily_budget=self.daily_budget
        )

        if sent_today >= max(0, max_per_day):
            reason = "budget"
        elif not allow:
            reason = "global_budget"
        elif pending:
            reason = "pending"
        else:
            reason = "ok"

        activity.logger.info(
            "curiosity_budget agent=%s reason=%s sent_today=%d pending=%d global_today=%d",
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
            "owner_emails_configured": bool(self.owner_emails),
        }

    @activity.defn
    async def record_curiosity_card(self, agent_id: str, sent: bool = True) -> dict:
        """Charge a delivered curiosity card to the daily notification budget."""
        from aegis.services.notifications import record_notification

        await record_notification(self.db_pool, agent_id, "curiosity_card", sent)
        return {"recorded": True, "sent": sent}

    @activity.defn
    async def apply_curiosity_answer(
        self, interaction_id: str, response: dict | None, metadata: dict | None
    ) -> dict:
        """InteractionFlow post-resolve hook — bank the answer as memory.

        An empty answer (the owner submitted nothing, or the card was resolved
        by something other than a typed reply) writes nothing: a memory row
        saying the owner said nothing is worse than no row.
        """
        from aegis.services.memory import record_memory

        meta = metadata or {}
        answer = str((response or {}).get("value") or "").strip()
        if not answer:
            activity.logger.info("curiosity_answer_empty id=%s", interaction_id)
            return {"recorded": False, "reason": "empty"}

        row = None
        try:
            row = await self.db_pool.fetchrow(
                "SELECT agent_id, prompt FROM interactions WHERE id = $1::uuid",
                interaction_id,
            )
        except Exception as exc:  # noqa: BLE001 — a malformed id must not lose the answer
            activity.logger.warning("curiosity_answer_lookup_failed err=%s", str(exc)[:200])
        agent_id = (row["agent_id"] if row else None) or str(meta.get("agent_id") or "sebas")
        question = str(meta.get("question") or (row["prompt"] if row else "") or "").strip()
        subject = str(meta.get("subject") or "").strip()

        head = question or (f"About {subject}" if subject else "Curiosity question")
        await record_memory(
            self.db_pool,
            agent_id,
            f"{head}\nThe owner answered: {answer}",
            importance=0.8,
            source="curiosity",
        )
        activity.logger.info(
            "curiosity_answer_recorded id=%s agent=%s subject=%s",
            interaction_id,
            agent_id,
            subject,
        )
        return {"recorded": True, "agent_id": agent_id, "subject": subject}
