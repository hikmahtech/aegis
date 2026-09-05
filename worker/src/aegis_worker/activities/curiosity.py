"""Curiosity gap-finder — deterministic detection of what AEGIS does NOT know.

Turns data gaps into at most a handful of ranked candidate questions. Detection
itself needs no LLM: three independent SQL detectors look for a subject that is
demonstrably present in the owner's data but absent from everything the agent
has ever been told (its memories and its persona docs).

  (a) calendar  — an attendee who keeps showing up on ingested calendar events
                  and is never mentioned in chat_history / agent_memory / profile
  (b) finance   — a payee the books could not categorise (`finance.journal_index`
                  rows sitting in an `:unknown` account) and that is never
                  mentioned in agent_memory / profile
  (c) todoist   — a project carrying real OPEN task volume with no profile
                  context (a finished project is not a gap)

Each detector is independently try/excepted: a broken one costs its own
candidates, never the run. `novelty_key` (`attendee:<email>`,
`payee:<payee_key>`, `project:<name>`) is the never-ask-twice handle — ANY
`interactions` row already carrying that key removes the candidate, archived
included: a timed-out card is a question the owner declined once, and the
weekly money brief is the retry channel, not another card.

The money lane closes the loop: when the owner answers an `unknown_payee` card,
`apply_curiosity_answer` turns the answer into a permanent `rules/accounts.yaml`
entry and reclassifies that payee's backlog in the journal, so the same question
is never worth asking again (spec §6). It replaced a detector over
`finance.recurring_charge` — a table nothing writes any more, which therefore
kept raising cards about vendors that could never age out.

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
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from temporalio import activity

# `Attendees: a@x.com, b@y.com` — the line `calendar_event_to_content` writes
# (core/src/aegis/services/claims.py) into the chunk text of every ingested event.
_ATTENDEE_LINE_RE = re.compile(r"^Attendees:\s*(.+)$", re.MULTILINE)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

_DETECTORS = ("calendar_attendee", "unknown_payee", "todoist_project")

# How far back the unknown-payee detector looks. A payment from last year is
# not worth an interruption; the weekly money brief lists the older backlog.
_UNKNOWN_DAYS = 60
# The books hold one currency per journal, and the question states ONE summed
# figure — mixing currencies into it would print a number that is simply false.
_UNKNOWN_CURRENCY = "INR"

# How sure the model has to be before its answer becomes a permanent rule that
# files every future payment from this payee (spec §6).
_RULE_CONFIDENCE = 0.8
# Bounds on the generated rule regex, mirroring `services/tools/ledger.py`:
# the pattern is persisted and then run against every incoming money event in
# another process, forever. BOTH bounds refuse; neither truncates — a clipped
# pattern is a PREFIX rule, strictly broader than the payee the owner answered
# about, and this is the one place a rule is written with no human authoring it.
_MAX_RULE_WORDS = 6
_MAX_RULE_MATCH = 200
# `books.apply_rules` matches against "<sender> | <payee>", so an unanchored
# pattern also fires on the mail's FROM address. That is live: Google Pay
# mirrors MSEDCL, Airtel and the rest (hence `bank_parsers.parse_gpay_bill`),
# so an unanchored rule from a payee called "Google" would re-file — and
# rename — every Google-Pay-mirrored bill. This prefix pins the match to the
# payee half; the ledger tool's sweep passes an empty sender, whose haystack
# still starts " | ", so that call site is unaffected.
_PAYEE_HALF = r"\|[^|]*"


def rule_match_for(payee_key: str) -> str:
    """A regex matching every punctuation variant of one payee, or "".

    The detector groups by `payee_key` precisely because one biller arrives
    under several spellings ('Mahavitaran (MSEDCL)' and 'Mahavitaran -
    Maharashtra Electricity (MSEDCL)' are one bill), so a rule escaping the ONE
    spelling that happened to win the GROUP BY would leave the others in
    `expenses:unknown` for good — and the novelty key means they are never
    asked about again. Joining the key's words with `[^a-z0-9]+` matches them
    all (spec §6, "escaped payee_key words").

    Cheap to run and impossible to make invalid: `payee_key` yields only
    `[a-z0-9]` words, so the result is literal words separated by one bounded
    character class — no nesting, no alternation, and no ambiguity between
    adjacent atoms, so no backtracking blowup.

    Returns "" when there is nothing to build from or the result would breach
    either bound. The caller then writes no rule at all — but still applies the
    backlog, which it selects by `payee_key` and not by this pattern.
    """
    words = [re.escape(w) for w in (payee_key or "").split()]
    if not words or len(words) > _MAX_RULE_WORDS:
        return ""
    match = _PAYEE_HALF + "[^a-z0-9]+".join(words)
    return match if len(match) <= _MAX_RULE_MATCH else ""


@dataclass
class CuriosityActivities:
    """Detect knowledge gaps worth asking the owner about."""

    db_pool: Any
    llm_client: Any = None
    model: str = "gpt-oss:20b"
    # `books.BooksConfig` — the shared hledger checkout. Filled by the worker's
    # main(); None means the answer is banked as memory and the books are left
    # alone, which is also what a fork with no books repo gets.
    books_cfg: Any = None
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

        LIVE rows only (`superseded_at IS NULL`, migration 020's marker — the
        same predicate every A4 reader uses). A consolidation pass retires a
        memory it judged redundant, contradicted or wrong; leaving it in this
        haystack would keep suppressing the question about a belief AEGIS has
        already withdrawn.
        """
        rows = await self.db_pool.fetch(
            "SELECT content FROM agent_memory WHERE agent_id = $1 AND superseded_at IS NULL",
            agent_id,
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
        """novelty_keys carried by ANY interaction, archived included.

        A timed-out card is not an answered question, but re-sending it is
        an interruption the user already declined once. The weekly money
        brief lists unexplained charges; that is the retry channel.
        """
        rows = await self.db_pool.fetch(
            "SELECT DISTINCT metadata->>'novelty_key' AS k FROM interactions "
            "WHERE metadata ? 'novelty_key'"
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

    async def _detect_unknown_payee(self, agent_id: str, known: str) -> list[tuple[float, dict]]:
        """Money that left the account for something the books could not name.

        `expenses:unknown` (and its siblings) is the books' review queue, so
        this reads the queue itself rather than a vendor table: a posting the
        owner has since explained leaves the queue and stops being a question,
        with no separate bookkeeping to keep in sync.

        One candidate per `payee_key`, not per posting — the same shop arrives
        under several spellings and each used to be its own card.
        """
        from aegis.services.money_format import fmt_money

        rows = await self.db_pool.fetch(
            "SELECT payee_key, max(payee) AS payee, sum(amount) AS total, count(*) AS n, "
            "max(occurred_on) AS last_on, max(channel) AS channel "
            "FROM finance.journal_index "
            # `direction = 'out'` is not a nicety. `income:unknown` matches
            # `%:unknown` too, so without it an uncategorised inbound credit is
            # carded as "You paid ₹42,000.00 to Nkgsb Bank … What was it for?"
            # — false, on the one surface allowed to interrupt the owner, and
            # it steers the account-picking model toward an EXPENSE account for
            # money that came in.
            "WHERE kind = 'transaction' AND direction = 'out' AND account LIKE '%:unknown' "
            f"AND occurred_on >= now() - interval '{_UNKNOWN_DAYS} days' AND currency = $1 "
            "GROUP BY payee_key ORDER BY total DESC LIMIT 50",
            _UNKNOWN_CURRENCY,
        )
        out: list[tuple[float, dict]] = []
        for r in rows:
            key = (r["payee_key"] or "").strip()
            payee = (r["payee"] or "").strip()
            if not key or not payee or payee.lower() in known:
                continue
            total = Decimal(r["total"] or 0)
            n = int(r["n"] or 0)
            last_on = r["last_on"].isoformat() if r["last_on"] else "?"
            channel = (r["channel"] or "other").strip()
            out.append(
                (
                    # Cost is the signal — a big unexplained payment outranks a
                    # small one, and any of them outranks a bare calendar face.
                    10.0 + float(total),
                    {
                        "gap_type": "unknown_payee",
                        "subject": payee,
                        "question": (
                            f"You paid {fmt_money(total, _UNKNOWN_CURRENCY)} to {payee} "
                            f"({n} time{'' if n == 1 else 's'}, last {last_on}, {channel}). "
                            "What was it for?"
                        ),
                        # JSON-safe: this crosses a Temporal payload boundary
                        # and then lands in the card's `metadata` jsonb.
                        "evidence": {
                            "payee_key": key,
                            "total": float(total),
                            "n": n,
                            "last_on": last_on,
                        },
                        "novelty_key": f"payee:{key}",
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

        listing = "\n".join(
            f"{i}. [{c['gap_type']}] {c['subject']} — {c['question']}"
            for i, c in enumerate(candidates)
        )
        # db_pool + purpose ⇒ think() writes the llm_calls row itself, for
        # success and failure alike (LLMClient._record_call). Do not record here.
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
        """InteractionFlow post-resolve hook — bank the answer as memory, and
        for an `unknown_payee` card turn it into a books rule as well.

        An empty answer (the owner submitted nothing, or the card was resolved
        by something other than a typed reply) writes nothing: a memory row
        saying the owner said nothing is worse than no row.

        The books half is strictly an extra. It runs after the memory is
        written and every failure inside it is logged and swallowed, because
        the owner answered a question and that answer must not be lost when a
        pull, an `hledger check --strict` or the model has a bad day.
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
        out = {"recorded": True, "agent_id": agent_id, "subject": subject}

        if meta.get("gap_type") == "unknown_payee" and self.llm_client and self.books_cfg:
            try:
                out.update(await self._apply_books_answer(agent_id, meta, subject, answer))
            except Exception as exc:  # noqa: BLE001 — the memory write stands
                activity.logger.warning(
                    "curiosity_books_answer_failed id=%s subject=%s err=%s",
                    interaction_id,
                    subject,
                    str(exc)[:200],
                )
                out.update({"rule": None, "reason": "books_failed"})
        return out

    async def _apply_books_answer(
        self, agent_id: str, meta: dict, payee: str, answer: str
    ) -> dict:
        """Turn one answered unknown-payee card into a rule + a reclassification.

        The account is chosen by the model but is NOT trusted: it has to be one
        the chart already declares, or `hledger check --strict` would reject
        every block written under it and the rule would misfile the payee's
        mail forever. Below `_RULE_CONFIDENCE` nothing is written at all — the
        answer is already in memory, and a wrong rule is worse than none.
        """
        from aegis.api.models.money import payee_key as payee_key_of
        from aegis.llm import parse_llm_json
        from aegis.services import books

        cfg = self.books_cfg
        # `declared_accounts`, not `run_hledger(["accounts", "--declared"])`:
        # the latter caps its output at 12,000 characters, which would silently
        # drop the tail of a large chart and turn a declared account into a
        # refusal.
        accounts = await books.declared_accounts(cfg)
        result = await self.llm_client.think(
            prompt=(
                f"Payee: {payee}\n"
                f"The owner says: {answer}\n\n"
                "Accounts declared in the chart:\n" + "\n".join(sorted(accounts))
            ),
            model=self.model,
            system_prompt=(
                "You file a payment into one account of a double-entry chart. "
                "Pick the single account from the list that best fits what the "
                "owner said this payee is. Copy it EXACTLY as written; never "
                'invent one. Answer "NONE" if none of them fits or the owner\'s '
                "reply does not say what the payment was for. Return JSON: "
                '{"account": "<one of the list, or NONE>", "confidence": 0.0-1.0}'
            ),
            max_tokens=300,
            db_pool=self.db_pool,
            purpose="books_answer_account",
            agent_id=agent_id,
        )
        parsed = parse_llm_json(result.get("response", ""))
        parsed = parsed if isinstance(parsed, dict) else {}
        account = str(parsed.get("account") or "").strip()
        try:
            confidence = float(parsed.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0

        # Membership first, so "NONE" and a hallucinated account report the
        # reason they were actually refused for.
        if account not in accounts:
            activity.logger.info(
                "curiosity_books_answer_undeclared payee=%s account=%s", payee, account[:80]
            )
            return {"rule": None, "reason": "undeclared"}
        if confidence < _RULE_CONFIDENCE:
            activity.logger.info(
                "curiosity_books_answer_unsure payee=%s account=%s confidence=%.2f",
                payee,
                account,
                confidence,
            )
            return {"rule": None, "reason": "low_confidence"}

        # The card carries the key the detector grouped on; a card already in
        # flight when this shipped does not, so derive it the same way.
        key = str(meta.get("payee_key") or "").strip() or payee_key_of(payee)
        if not key:
            # `WHERE payee_key = ''` matches every blank-key row in the index,
            # so an unusable key stops here — it must not even reach the
            # backlog sweep below.
            return {"rule": None, "reason": "no_payee_key"}

        # A pattern this payee is too long to express safely means NO RULE —
        # never a clipped one, which would be a prefix rule broader than the
        # question the owner answered, persisted forever and applied
        # unattended. The backlog still moves: the sweep selects on `payee_key`
        # by exact match and never consults the pattern, so the money already
        # sitting in `:unknown` reaches the account the owner just explained
        # and only FUTURE events from this payee miss out. That is the cost the
        # cap is meant to buy.

        # Which books the answer names. Both ledger tools already refuse the
        # cross-entity move this would otherwise make unattended: an AWS bill
        # arrives in the personal mailbox, the owner says "that is the Hikmah
        # infra bill", and `expenses:hikmah:infra` is declared and balances —
        # so `check --strict` passes and nothing reverts, while the block sits
        # in `personal/2026.journal` and the entity-less rule repeats it for
        # every future AWS mail (`post_event` files by `event.entity`, which
        # the rule never corrected). `ledger_reclassify` then REFUSES to move
        # it back, so the repair path is narrower than the path that made it.
        # None means an entity-neutral account (assets, liabilities, equity),
        # which belongs to both sets of books — no stamp and no filter.
        entity = books.account_entity(account)

        match = rule_match_for(key)
        if match:
            # Sanitized once: this name is stored in the rule and written into
            # every future block for this payee, so the rule, the journal and
            # the index have to carry the same string.
            rule = {"match": match, "account": account, "payee": books.sanitize_payee(payee)}
            if entity:
                rule["entity"] = entity
            await books.append_rule(rule, cfg)

        rows = await self.db_pool.fetch(
            "SELECT message_id FROM finance.journal_index "
            # Same `direction = 'out'` as the detector, and for the same
            # reason: the owner was asked about money they PAID this payee, so
            # a credit from the same name (a refund, a transfer back) is not
            # what they explained and must not be filed to an expense account.
            "WHERE payee_key = $1 AND kind = 'transaction' AND direction = 'out' "
            "AND account LIKE '%:unknown' AND journal_file IS NOT NULL "
            # And only the books the account belongs to: rewriting in place
            # changes the account, never the file the block lives in.
            "AND ($2::text IS NULL OR entity = $2)",
            key,
            entity,
        )
        msgids = [r["message_id"] for r in rows]
        # ONE write for the whole backlog: one flock, one pull, one strict
        # check, one commit, one push. A loop over `rewrite_event` would hold
        # the books against every other writer for the length of the sweep and
        # leave one commit per posting.
        rewritten, failed = await books.rewrite_events(msgids, cfg, account=account)
        for msgid in rewritten:
            await self.db_pool.execute(
                "UPDATE finance.journal_index SET account = $2, updated_at = now() "
                "WHERE message_id = $1",
                msgid,
                account,
            )
        if failed:
            activity.logger.warning(
                "curiosity_books_reclassify_partial payee=%s failed=%d ids=%s",
                payee,
                len(failed),
                failed[:10],
            )
        activity.logger.info(
            "curiosity_books_answer_applied payee=%s account=%s rule=%s "
            "reclassified=%d failed=%d",
            payee,
            account,
            bool(match),
            len(rewritten),
            len(failed),
        )
        out = {"reclassified": len(rewritten), "failed": len(failed)}
        if match:
            return {"rule": account, **out}
        # Backlog moved, nothing persisted — a distinct outcome from both a
        # clean success and a refusal that did nothing.
        return {"rule": None, "reason": "rule_refused_backlog_applied", **out}
