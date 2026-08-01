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

A6 ships the activity only. A7 is the flow that consumes it, so this is
registered but dormant: no flow, no schedule, no _ACTIVITY_TYPE_MAP entry.
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


def parse_owner_emails(raw: str) -> frozenset[str]:
    """`"label1:email1,label2:email2"` (Settings.gmail_accounts) -> the emails.

    These are the operator's own addresses. Entries with no `:email` part
    (accounts discovered from a token file on disk) contribute nothing —
    a bare label is not an email. Blank input -> empty set.
    """
    out: set[str] = set()
    for entry in (raw or "").split(","):
        _, _, email = entry.partition(":")
        email = email.strip().lower()
        if email:
            out.add(email)
    return frozenset(out)


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
    # The operator's own email addresses, from Settings.gmail_accounts via
    # parse_owner_emails. Google puts the calendar owner in every event's
    # `attendees`, so without this the owner is a "stranger you keep meeting".
    # Empty (unconfigured) = no exclusion, same as before.
    owner_emails: frozenset[str] = frozenset()

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

        out: list[tuple[float, dict]] = []
        for email, events in seen.items():
            # The owner attends their own meetings — never a gap. Filtered here
            # (not at ingest) so already-stored events are covered too.
            if email in self.owner_emails:
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
