"""Intent classification for `POST /api/admin/capture {kind:"auto"}` (B3).

Voice-first capture: a transcript arrives with no lane attached and this
module picks one, so a spoken note does not need the speaker to decide up
front whether it was a task or a fact.

Two properties matter more than accuracy:

**Degradation is one-directional.** Every failure path — no LLM client, an
unresolvable tier, the spend kill switch, a timeout, a truncated reasoning
model, unparseable JSON, an unknown label, low confidence — resolves to
`SAFE_LANE` (`task`). A life fact mis-filed as a task is one Inbox line to
re-file; a task mis-filed as a life fact is an assertion sitting in the RAG
store that later answers will be built on. Degrade towards the recoverable
side, always.

**The decision is inspectable.** Every classification writes an `audit_log`
row (`action='capture_classified'`) carrying the label the model chose, the
lane actually taken, its confidence, its stated reason and a text preview —
so a wrong route can be diagnosed after the fact rather than guessed at. The
LLM call itself lands in `llm_calls` under `purpose='capture_classify'`, on
success as well as failure.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import structlog

from aegis.llm import LLMTruncationError, parse_llm_json
from aegis.llm.tier import tier_to_model
from aegis.observability import log_audit, record_llm_call

logger = structlog.get_logger()

# What the model is allowed to answer with. Only two of these are *write*
# lanes; the other two exist so the model is not forced to mislabel input
# that has no home — being able to say "this is a question" produces a more
# honest answer than a coerced task/life_fact binary.
LABELS = ("task", "life_fact", "question", "observation")

# Label → the lane the capture route actually writes to.
#
# `question` has no answering surface on the capture route, and `observation`
# would need a metric/value/unit extraction that a transcript classifier
# cannot do reliably — a hallucinated number in life.observations is exactly
# the kind of write nobody notices is wrong. Both therefore land in the
# Todoist Inbox, which preserves the original words verbatim and is trivially
# re-filed.
LANE_FOR_LABEL: dict[str, str] = {
    "task": "task",
    "life_fact": "life_fact",
    "question": "task",
    "observation": "task",
}

# The lane every failure path resolves to.
SAFE_LANE = "task"

# Below this the model is guessing. Guessing into the knowledge store is the
# one outcome worth avoiding, so a low-confidence answer takes the safe lane
# regardless of which label it named.
MIN_CONFIDENCE = 0.6

PURPOSE = "capture_classify"

# The expected reply is ~100 tokens of JSON; structured-extraction calls need
# ≥3× the expected size as headroom or a reasoning model burns the budget on
# hidden content and returns nothing. `LLMClient._reasoning_floor` raises this
# again for kimi-class models.
_MAX_TOKENS = 600

_SYSTEM_PROMPT = (
    "You are a capture router. You classify one short note — usually a voice "
    "transcript — into exactly one lane and answer with JSON only."
)

_PROMPT = """Classify the note below into exactly one lane.

task        — something to DO, now or later: errands, reminders, follow-ups,
              anything phrased as an instruction to yourself.
life_fact   — a durable FACT about the speaker's life, true until it changes:
              preferences, relationships, documents, dates, decisions already
              made, where things are kept.
question    — a request for information the speaker wants answered.
observation — a numeric or time-series reading with a value attached (weight,
              hours slept, blood pressure, a meter reading).

Rules:
- Pick the single best lane.
- "confidence" is 0.0-1.0: your honest probability that the lane is right. If
  the note is genuinely ambiguous, say so with a low number instead of
  inventing certainty.
- "reason" is one short sentence naming the words that decided it.

Return ONLY this JSON object — no prose, no code fence:
{{"lane": "task|life_fact|question|observation", "confidence": 0.0, "reason": "..."}}

Note:
\"\"\"
{text}
\"\"\"
"""


@dataclass(frozen=True)
class Classification:
    """The routing decision. `lane` is always a real write lane."""

    lane: str
    label: str
    confidence: float
    reason: str

    @property
    def degraded(self) -> bool:
        """True when the lane taken is not the label the model named."""
        return self.lane != self.label


async def classify_capture(
    *,
    pool: Any,
    llm: Any,
    text: str,
    external_id: str,
    source: str = "chat",
) -> Classification:
    """Route `text` to a capture lane. Never raises — worst case is `task`."""
    result = await _classify(pool=pool, llm=llm, text=text)
    logger.info(
        "capture_classified",
        lane=result.lane,
        label=result.label,
        confidence=result.confidence,
        reason=result.reason,
        degraded=result.degraded,
        source=source,
        external_id=external_id[:64],
    )
    if pool is not None:
        await log_audit(
            pool,
            actor=f"capture:{source}",
            action="capture_classified",
            target_type="capture",
            target_id=external_id[:200],
            details={
                "lane": result.lane,
                "label": result.label,
                "confidence": result.confidence,
                "reason": result.reason,
                "degraded": result.degraded,
                # The words that were routed — without them a wrong lane in
                # the log is undiagnosable.
                "text": text[:300],
            },
        )
    return result


async def _classify(*, pool: Any, llm: Any, text: str) -> Classification:
    if llm is None:
        return Classification(SAFE_LANE, "unknown", 0.0, "llm_unavailable")
    try:
        model = tier_to_model("balanced")
    except KeyError:
        return Classification(SAFE_LANE, "unknown", 0.0, "tier_unresolved")

    t0 = time.monotonic()
    try:
        result = await llm.think(
            prompt=_PROMPT.format(text=text[:2000]),
            model=model,
            system_prompt=_SYSTEM_PROMPT,
            max_tokens=_MAX_TOKENS,
            db_pool=pool,
            purpose=PURPOSE,
        )
    except LLMTruncationError as exc:
        # think() raises this AFTER a successful HTTP call, outside its own
        # failure-recording try — so nothing lands in llm_calls unless we do
        # it here, and a truncating model would look like zero traffic.
        logger.warning("capture_classify_truncated", error=str(exc)[:200])
        if pool is not None:
            await record_llm_call(
                pool,
                model=model,
                prompt_tokens=0,
                completion_tokens=0,
                latency_ms=int((time.monotonic() - t0) * 1000),
                purpose=PURPOSE,
                status="error",
                error=f"truncated: {exc}"[:500],
            )
        return Classification(SAFE_LANE, "unknown", 0.0, "llm_truncated")
    except Exception as exc:  # noqa: BLE001 — capture must survive any LLM fault
        # think() already wrote the llm_calls failure row (db_pool + purpose
        # were passed); the kill-switch path raises before any row, which the
        # audit row still records.
        logger.warning(
            "capture_classify_failed",
            error=str(exc)[:200],
            exc_type=type(exc).__name__,
        )
        return Classification(
            SAFE_LANE, "unknown", 0.0, f"llm_error:{type(exc).__name__}"
        )

    if pool is not None:
        await record_llm_call(
            pool,
            model=result.get("model", model),
            prompt_tokens=result.get("prompt_tokens", 0),
            completion_tokens=result.get("completion_tokens", 0),
            latency_ms=int((time.monotonic() - t0) * 1000),
            purpose=PURPOSE,
        )

    parsed = parse_llm_json(result.get("response", ""))
    if not isinstance(parsed, dict):
        return Classification(SAFE_LANE, "unknown", 0.0, "unparseable")

    label = str(parsed.get("lane") or "").strip().lower()
    if label not in LANE_FOR_LABEL:
        return Classification(
            SAFE_LANE, "unknown", 0.0, f"unknown_label:{label[:32]}"
        )
    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    why = str(parsed.get("reason") or "")[:300]

    if confidence < MIN_CONFIDENCE:
        return Classification(SAFE_LANE, label, confidence, f"low_confidence: {why}")
    return Classification(LANE_FOR_LABEL[label], label, confidence, why)
