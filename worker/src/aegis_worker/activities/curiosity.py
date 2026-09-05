"""Curiosity gap-finder — deterministic detection of what AEGIS does NOT know.

Turns data gaps into at most a handful of ranked candidate questions. Detection
itself needs no LLM: three independent SQL detectors look for a subject that is
demonstrably present in the owner's data but absent from everything the agent
has ever been told (its memories and its persona docs).

  (a) calendar  — an attendee who keeps showing up on ingested calendar events
                  and is never mentioned in chat_history / agent_memory / profile
  (b) finance   — a payee the books could not categorise (`finance.journal_index`
                  rows sitting in an `:unknown` account, money out or money in)
                  and that is never mentioned in agent_memory / profile
  (c) todoist   — a project carrying real OPEN task volume with no profile
                  context (a finished project is not a gap)

Each detector is independently try/excepted: a broken one costs its own
candidates, never the run. `novelty_key` (`attendee:<email>`,
`payee:<payee_key>` for money out, `payee-in:<payee_key>` for money in,
`project:<name>`) is the never-ask-twice handle — ANY `interactions` row
already carrying that key removes the candidate, archived included: a timed-out
card is a question the owner declined once, and the weekly money brief is the
retry channel, not another card.

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
from html import escape
from typing import Any

from temporalio import activity

from aegis_worker.activities.delivery import safe_send_message

# `Attendees: a@x.com, b@y.com` — the line `calendar_event_to_content` writes
# (core/src/aegis/services/claims.py) into the chunk text of every ingested event.
_ATTENDEE_LINE_RE = re.compile(r"^Attendees:\s*(.+)$", re.MULTILINE)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

_DETECTORS = ("calendar_attendee", "unknown_payee", "todoist_project")

# How far back the unknown-payee detector looks. A payment from last year is
# not worth an interruption; the weekly money brief lists the older backlog.
_UNKNOWN_DAYS = 60

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
# payee half, which is the answer FOR A RULE NOBODY REVIEWS: the owner named
# one payee, so the rule may only mean that payee. `ledger_add_rule` writes
# unanchored rules on purpose — five live rules carry an address literal that
# appears in no payee, and more of them match live mail only through the
# sender, with a human in that loop — and its sweep now runs them against the
# real sender, so both writers see the haystack a real event brings.
_PAYEE_HALF = r"\|[^|]*"


# Why no rule was written, in the owner's words. Keyed on the `reason` each
# refusal branch in `_apply_books_answer` sets, so a new branch that forgets to
# add an entry gets the generic sentence rather than silence.
_NO_RULE_SAID = {
    "undeclared": "I could not match that to an account in the chart, so I wrote no rule.",
    "low_confidence": "I was not sure enough which account that is, so I wrote no rule.",
    "no_payee_key": "That card carries no usable payee key, so I filed nothing.",
    "books_failed": "The books would not take it — nothing was written.",
    "rule_refused_backlog_applied": (
        "The payee name is too long to turn into a safe pattern, so there is no rule and "
        "the next payment from it will need filing again."
    ),
}
_NO_RULE_FALLBACK = "I could not turn that into a books rule."


def books_answer_report(payee: str, out: dict) -> str | None:
    """What to tell the owner about the books half of their answer, or None.

    Silence is the success signal (issue #384). A clean outcome — a rule
    written, nothing failed, nothing left behind — already shows: the payee
    drops off the brief's Unexplained list and the card reads resolved in the
    admin. An extra push per answered question would spend the notification
    budget saying what the next brief says anyway.

    Everything else was invisible before this, and unrecoverable by any
    automated path once it happened: `InteractionFlow` discards the
    post-resolve activity's return value, the card is an ABANDONED child that
    nobody awaits, the novelty key stops the question ever being asked again,
    and `ledger_reclassify` refuses the cross-entity move by design. So the
    hook has to say it itself.
    """
    account = str(out.get("rule") or "")
    skipped = int(out.get("skipped_other_entity") or 0)
    failed = int(out.get("failed") or 0)
    moved = int(out.get("reclassified") or 0)
    # The rule was written and the backlog sweep then threw. Silence would be
    # read as the clean outcome, and the generic failure sentence claims the
    # rule does not exist — so this outcome has to say both halves itself.
    backlog_failed = bool(out.get("backlog_failed"))
    if account and not skipped and not failed and not backlog_failed:
        return None
    name = escape(payee or "that payee")

    def postings(n: int) -> str:
        return f"{n} existing posting{'' if n == 1 else 's'}"

    if not account:
        why = _NO_RULE_SAID.get(str(out.get("reason") or ""), _NO_RULE_FALLBACK)
        tail = f" {postings(moved)} still moved." if moved else ""
        return f"<b>{name}</b> — {why}{tail} Your answer is saved as a memory."

    parts = [f"<b>{name}</b> → <code>{escape(account)}</code>. The rule is saved."]
    if backlog_failed:
        parts.append(
            "The postings already in the books could not be moved and are still "
            "unexplained, so they need doing by hand; the rule will file the next one."
        )
    if skipped:
        # The VERB agrees too. `postings()` pluralises the noun and an adjacent
        # literal verb does not follow it, which is how "1 existing posting sit
        # in the other set of books" got written into the same commit as the
        # fix for "1 low-confidence LLM postings".
        parts.append(
            f"{postings(skipped)} {'sits' if skipped == 1 else 'sit'} in the other set of "
            "books: changing the account cannot change the journal file the block lives "
            "in, so I left them where they are. They need a hand edit."
        )
    if failed:
        parts.append(f"{failed} posting{'' if failed == 1 else 's'} could not be rewritten.")
    return " ".join(parts)


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

    The last check is the LOADER's, and it is not belt-and-braces. `load_rules`
    skips a rule whose pattern breaches the regex bounds (issue #390), so a
    generated pattern that breached them would be committed to the books and
    then never run — strictly worse than writing nothing, because the card is
    retired either way and the file now carries a rule that does not work. The
    two caps sit flush against each other: the join puts one quantifier in
    every gap between words, plus the anchor's own `*`, so `_MAX_RULE_WORDS`
    words is exactly `books.MAX_RULE_QUANTIFIERS` quantifiers. Raising either
    number alone would breach the other, silently, and this is where that
    stops.
    """
    from aegis.services import books

    words = [re.escape(w) for w in (payee_key or "").split()]
    if not words or len(words) > _MAX_RULE_WORDS:
        return ""
    match = _PAYEE_HALF + "[^a-z0-9]+".join(words)
    if len(match) > _MAX_RULE_MATCH or books.rule_match_problem(match) is not None:
        return ""
    return match


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
    # The commodity `prices.journal` quotes every rate in. Used to RANK
    # unexplained payments across currencies (issue #387); never rendered.
    # Same default as MoneyActivities.home_currency — change them together.
    home_currency: str = "INR"
    # `DeliveryActivities` — how the books half of an answer reports back
    # (issue #384). None means it cannot, which is a fork with no comms and
    # every existing unit test; the outcome is still returned and logged.
    delivery: Any = None
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

    def _home_rates(self) -> dict[str, Decimal]:
        """Commodity symbol → its rate in the home commodity, or `{}`.

        Ranking only, never a figure the owner is shown. Failure is `{}` — a
        fork with no books, a fresh checkout, a week the quote provider was
        down — and the caller degrades to native magnitudes rather than
        skipping a row.
        """
        if self.books_cfg is None:
            return {}
        try:
            from aegis.services import books

            return books.latest_prices(self.books_cfg)
        except Exception as exc:  # noqa: BLE001 — a rank is not worth a failed run
            activity.logger.warning("curiosity_prices_unreadable err=%s", str(exc)[:200])
            return {}

    async def _detect_unknown_payee(self, agent_id: str, known: str) -> list[tuple[float, dict]]:
        """Money the books could not name — out of the account or into it.

        `expenses:unknown` and `income:unknown` are the books' review queue, so
        this reads the queue itself rather than a vendor table: a posting the
        owner has since explained leaves the queue and stops being a question,
        with no separate bookkeeping to keep in sync.

        One candidate per `(payee_key, direction)`, not per posting and not per
        currency. Per payee, because the same shop arrives under several
        spellings and the rule the answer writes is per payee; per DIRECTION,
        because the two are different questions with different answers — the
        backlog sweep files a credit and a debit to different halves of the
        chart, and one card cannot stand for both.

        Money that came IN is asked about as money that came in. Carding it as
        "You paid ₹42,000.00 to Nkgsb Bank" was false on the one surface
        allowed to interrupt the owner, and it steered the account-picking
        model toward an EXPENSE account for money that arrived — but the answer
        to that is to say the true thing, not to skip the money (issue #387
        review). Uncategorised income is worth asking about for exactly the
        reason uncategorised spend is, and on 2026-09-05 every foreign unknown
        in the live index was inbound.

        The AMOUNTS are grouped per currency and stay that way (issue #387).
        Restricting the detector to rupees was how a foreign unexplained
        payment came to be never carded, so never ruled, so never categorised;
        adding the currencies together instead would state a number that is
        simply false. So the question names each currency's own total, and
        only the RANK converts — through the rates the lane already refreshes
        into `prices.journal` every week.
        """
        from aegis.services.money_format import currency_symbol, fmt_money

        rows = await self.db_pool.fetch(
            "SELECT payee_key, max(payee) AS payee, currency, direction, sum(amount) AS total, "
            "count(*) AS n, max(occurred_on) AS last_on, max(channel) AS channel "
            "FROM finance.journal_index "
            "WHERE kind = 'transaction' AND account LIKE '%:unknown' "
            # Only the two directions the question can be phrased for. A NULL
            # or unrecognised direction has no true sentence available, and
            # picking one would put a guess in front of the owner.
            "  AND direction IN ('in', 'out') "
            f"AND occurred_on >= now() - interval '{_UNKNOWN_DAYS} days' "
            # Both columns are nullable, and both are load-bearing here. A NULL
            # amount sums to NULL and used to render as "You paid ₹0.00 to HDFC
            # Bank" — live on 2026-09-05, four rows across two payees: a
            # statement of fact that is not a fact, on the one surface allowed
            # to interrupt. A NULL currency cannot be rendered as money at all
            # without guessing which one it is, and guessing "the home one" is
            # the assumption this detector just stopped making.
            "  AND amount IS NOT NULL AND currency IS NOT NULL "
            "GROUP BY payee_key, currency, direction ORDER BY total DESC LIMIT 50"
        )
        rates = self._home_rates()

        def in_home(total: Decimal, currency: str) -> Decimal:
            """`total` valued in the home commodity, for RANKING only.

            No rate ⇒ the native magnitude, which understates a strong
            currency and is the honest degradation: it still ranks, still
            cards, and never reaches a number the owner reads.
            """
            if currency.upper() == self.home_currency.upper():
                return total
            return total * rates.get(currency_symbol(currency), Decimal(1))

        # Fold the per-(payee, currency, direction) rows into one candidate per
        # (payee, direction). Insertion order is the query's `total DESC`,
        # which only matters as a stable starting point — the amounts below are
        # re-sorted by value.
        folded: dict[tuple[str, str], dict] = {}
        for r in rows:
            key = (r["payee_key"] or "").strip()
            payee = (r["payee"] or "").strip()
            currency = (r["currency"] or "").strip().upper()
            direction = (r["direction"] or "").strip()
            total = Decimal(r["total"])
            if not key or not payee or not currency or total <= 0:
                continue
            if payee.lower() in known:
                continue
            c = folded.setdefault(
                (key, direction),
                {"payee": payee, "totals": {}, "n": 0, "last_on": None, "channel": None,
                 "rank": Decimal(0)},
            )
            c["totals"][currency] = c["totals"].get(currency, Decimal(0)) + total
            c["n"] += int(r["n"] or 0)
            c["rank"] += in_home(total, currency)
            if r["last_on"] and (c["last_on"] is None or r["last_on"] > c["last_on"]):
                c["last_on"] = r["last_on"]
            # The channel of the biggest group, which is the one the question's
            # amounts lead with.
            if c["channel"] is None:
                c["channel"] = (r["channel"] or "other").strip()

        out: list[tuple[float, dict]] = []
        for (key, direction), c in folded.items():
            payee, n = c["payee"], c["n"]
            last_on = c["last_on"].isoformat() if c["last_on"] else "?"
            # Biggest first, in home-commodity terms, so the sentence leads
            # with the amount that earned the card.
            ordered = sorted(
                c["totals"].items(), key=lambda kv: in_home(kv[1], kv[0]), reverse=True
            )
            amounts = [fmt_money(total, currency) for currency, total in ordered]
            sums = amounts[0] if len(amounts) == 1 else ", ".join(amounts[:-1]) + f" and {amounts[-1]}"
            moved = (
                f"You paid {sums} to {payee}"
                if direction == "out"
                else f"You received {sums} from {payee}"
            )
            asked = "What was it for?" if direction == "out" else "What was it?"
            out.append(
                (
                    # Cost is the signal — a big unexplained payment outranks a
                    # small one, and any of them outranks a bare calendar face.
                    # Money arriving unexplained is worth the same attention as
                    # money leaving unexplained, so both use this scale.
                    10.0 + float(c["rank"]),
                    {
                        "gap_type": "unknown_payee",
                        "subject": payee,
                        "question": (
                            f"{moved} "
                            f"({n} time{'' if n == 1 else 's'}, last {last_on}, {c['channel']}). "
                            f"{asked}"
                        ),
                        # JSON-safe: this crosses a Temporal payload boundary
                        # and then lands in the card's `metadata` jsonb. One
                        # entry per currency, never a sum across them.
                        "evidence": {
                            "payee_key": key,
                            "direction": direction,
                            "totals": {cur: float(t) for cur, t in ordered},
                            "n": n,
                            "last_on": last_on,
                        },
                        # Outbound keeps the key it has always had, so every
                        # payee already asked about (or declined) stays
                        # suppressed. Inbound is a different question about the
                        # same name and needs its own.
                        "novelty_key": f"payee:{key}" if direction == "out" else f"payee-in:{key}",
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
            # Say what happened, when what happened is not what the owner would
            # assume from having answered (issue #384). Outside the try above,
            # so a books failure is reported rather than being the one outcome
            # that stays silent. `safe_send_message` never raises: by this
            # point the memory, the rule and the reclassification are all
            # already done, so a comms outage costs the message and nothing
            # else.
            report = books_answer_report(subject, out)
            if report:
                await safe_send_message(
                    self.delivery,
                    agent_id=agent_id,
                    message=report,
                    log_event="curiosity_books_answer",
                )
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
        # Which half of the books the card asked about. A card raised before
        # the inbound lane shipped carries no direction and was outbound by
        # construction, and anything unrecognised is treated the same way —
        # never guessed into the income half.
        direction = "in" if str(meta.get("direction") or "").strip() == "in" else "out"
        moved = "a payment the owner made" if direction == "out" else "money the owner received"
        # `declared_accounts`, not `run_hledger(["accounts", "--declared"])`:
        # the latter caps its output at 12,000 characters, which would silently
        # drop the tail of a large chart and turn a declared account into a
        # refusal.
        accounts = await books.declared_accounts(cfg)
        result = await self.llm_client.think(
            prompt=(
                f"Payee: {payee}\n"
                f"Direction: {moved}\n"
                f"The owner says: {answer}\n\n"
                "Accounts declared in the chart:\n" + "\n".join(sorted(accounts))
            ),
            model=self.model,
            system_prompt=(
                f"You file {moved} into one account of a double-entry chart. "
                "Pick the single account from the list that best fits what the "
                "owner said this payee is. Copy it EXACTLY as written; never "
                'invent one. Answer "NONE" if none of them fits or the owner\'s '
                "reply does not say what it was for. Return JSON: "
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
            # ALWAYS stamped here, unlike `ledger_add_rule` where it is opt-in
            # (issue #396). The owner answered about money moving ONE way, this
            # is the one writer with no human reviewing the rule, and the
            # payees it writes rules for are person-to-person UPI names — the
            # ones most likely to move money both ways. Without the stamp, an
            # answer of `income:people` files that name's next PAYMENT into
            # `income:people`: balanced, `check --strict` clean, never
            # `%:unknown`, so neither the brief nor this detector sees it
            # again. The other direction stays uncategorised and cardable,
            # which is the outcome the owner can still act on.
            rule["direction"] = direction
            await books.append_rule(rule, cfg)

        # Everything past the `append_rule` above is inside this, because the
        # caller's single handler reports any exception from this method as
        # "The books would not take it — nothing was written." That is true of
        # a failed LLM call, an unreadable chart or a refused `append_rule`,
        # and a LIE once the rule is committed and pushed: it will file this
        # payee's mail from now on, and the owner has been told it does not
        # exist on the only surface that reports this lane at all. Catching
        # here is what lets the two outcomes be told apart; when no rule was
        # written the exception goes on up, where that sentence is correct.
        try:
            rows = await self.db_pool.fetch(
                "SELECT message_id, entity FROM finance.journal_index "
                # The card's OWN direction, which is the whole reason the
                # detector cards the two separately. The owner was asked about
                # money moving one way; money from the same name moving the
                # other way (a refund, a transfer back) is not what they
                # explained, and filing a credit to an expense account puts it
                # in the wrong half of the books.
                "WHERE payee_key = $1 AND kind = 'transaction' AND direction = $2 "
                "AND account LIKE '%:unknown' AND journal_file IS NOT NULL",
                key,
                direction,
            )
            # Only the books the account belongs to: rewriting in place changes
            # the account, never the file the block lives in. The entity split
            # happens HERE rather than in the WHERE clause so the rows left
            # behind can be COUNTED — that count is the whole of what the owner
            # is told about a cross-entity answer, and a query that never
            # returned them could not produce it (issue #384).
            msgids = [r["message_id"] for r in rows if entity is None or r["entity"] == entity]
            skipped_other_entity = len(rows) - len(msgids)
            # ONE write for the whole backlog: one flock, one pull, one strict
            # check, one commit, one push. A loop over `rewrite_event` would
            # hold the books against every other writer for the length of the
            # sweep and leave one commit per posting.
            rewritten, failed = await books.rewrite_events(msgids, cfg, account=account)
            for msgid in rewritten:
                await self.db_pool.execute(
                    "UPDATE finance.journal_index SET account = $2, updated_at = now() "
                    "WHERE message_id = $1",
                    msgid,
                    account,
                )
        except Exception as exc:  # noqa: BLE001 — the rule's fate decides the sentence
            if not match:
                raise
            activity.logger.warning(
                "curiosity_books_backlog_failed payee=%s account=%s err=%s",
                payee,
                account,
                str(exc)[:200],
            )
            # `rewrite_events` reverts its own write, so the backlog is exactly
            # where it was — which is what the owner is now told, alongside the
            # rule that really was saved.
            return {"rule": account, "backlog_failed": True}
        if failed:
            activity.logger.warning(
                "curiosity_books_reclassify_partial payee=%s failed=%d ids=%s",
                payee,
                len(failed),
                failed[:10],
            )
        activity.logger.info(
            "curiosity_books_answer_applied payee=%s direction=%s account=%s rule=%s "
            "reclassified=%d failed=%d skipped_other_entity=%d",
            payee,
            direction,
            account,
            bool(match),
            len(rewritten),
            len(failed),
            skipped_other_entity,
        )
        out = {
            "reclassified": len(rewritten),
            "failed": len(failed),
            "skipped_other_entity": skipped_other_entity,
        }
        if match:
            return {"rule": account, **out}
        # Backlog moved, nothing persisted — a distinct outcome from both a
        # clean success and a refusal that did nothing.
        return {"rule": None, "reason": "rule_refused_backlog_applied", **out}
