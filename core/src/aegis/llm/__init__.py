"""LLM client for AEGIS v2.

Thin wrapper around OpenAI-compatible API (via LiteLLM gateway).
Supports tool calling for agentic chat.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import structlog
from openai import AsyncOpenAI
from opentelemetry import trace

logger = structlog.get_logger()
_tracer = trace.get_tracer(__name__)

# OpenTelemetry GenAI semantic conventions
# (https://opentelemetry.io/docs/specs/semconv/gen-ai/). Emitted alongside the
# legacy llm.* attrs so Langfuse / Tempo / any GenAI-aware backend can read token
# usage and model off the spans without app-specific parsing.
_GENAI_SYSTEM = "litellm"


def _set_genai_request(span, operation: str, model: str, max_tokens: int | None = None) -> None:
    span.set_attribute("gen_ai.system", _GENAI_SYSTEM)
    span.set_attribute("gen_ai.operation.name", operation)
    span.set_attribute("gen_ai.request.model", model)
    if max_tokens is not None:
        span.set_attribute("gen_ai.request.max_tokens", max_tokens)


def _set_genai_usage(span, input_tokens: int, output_tokens: int) -> None:
    span.set_attribute("gen_ai.usage.input_tokens", input_tokens)
    span.set_attribute("gen_ai.usage.output_tokens", output_tokens)


def parse_llm_json(raw: str) -> Any | None:
    """Tolerant parser for an LLM's JSON output.

    Strips a ```json ... ``` (or bare ```) fence, ignores surrounding prose,
    and parses the first JSON object or array. Returns None on any failure so
    callers fall back instead of crashing on a raw json.loads. Replaces the
    ad-hoc fence-strip idioms that were copy-pasted across the activities.
    """
    if not raw:
        return None
    cleaned = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.DOTALL)
    if fenced:
        cleaned = fenced.group(1).strip()
    if cleaned[:1] not in "{[":
        # Model wrapped the payload in prose — grab the first object/array span.
        span = re.search(r"\{.*\}|\[.*\]", cleaned, re.DOTALL)
        if not span:
            return None
        cleaned = span.group(0)
    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        return None


# 2048 was too tight in practice: prod ran 11/54 clarify_classification calls
# and 1/3 daylog_narrative calls straight into empty-content truncation, and
# 6/42 intel_score_significance calls came back clipped at exactly 2048 wearing
# a success label. The largest *successful* visible output observed anywhere is
# 2944 tokens, and that came from the one call site that already passes a 4000
# budget of its own — so 4096 clears every real output with headroom.
_REASONING_MIN_TOKENS = 4096

# Substring match, because the tier map resolves to bare proxy names that carry
# no "reasoning" marker of their own. Both families billed hidden
# reasoning_content against max_tokens in prod. A model outside this list falls
# back to the caller's raw budget, so add new reasoning models here when the
# tier map moves — a missing entry is silent, not loud (that is exactly how
# qwen3.5:9b came to run briefing_frame at a raw 2000 and fail 3/3).
_REASONING_MODELS = ("kimi", "qwen")

# One-shot re-roll budget for a call that came back EMPTY with
# finish_reason=length. Deliberately NOT a new floor: 30 days of prod kimi-k2.5
# is 984 successes averaging 705 visible output tokens (max 5796) against 22
# empty-truncations that burned 2048-4096. So 4096 is the right steady-state
# budget and the ~2% that die are stochastic overthink spirals, not a model
# that needs more room — the cure is another roll of the dice with enough
# headroom to swallow one spiral, not a permanently wider budget on every call.
#
# Raising the floor again would be treating a cure that already worked. Those
# 22 truncations cluster on 2026-08-03..06 and taper to ~1-2/day afterwards,
# which is exactly when #255 took the floor from 2048 to 4096: the floor moved
# the bulk, and what is left is a residual tail no floor removes, because a
# spiral can exhaust any budget. A retry is what a stochastic tail needs.
# 16384 is confirmed accepted by the proxy.
_TRUNCATION_RETRY_TOKENS = 16384


def _reasoning_floor(model: str, max_tokens: int) -> int:
    """Reasoning models bill hidden reasoning_content against max_tokens,
    so tight caller budgets (512-1000) truncate to empty visible content.

    # ponytail: floor the budget here instead of touching every call site.
    """
    if any(m in model for m in _REASONING_MODELS) and max_tokens < _REASONING_MIN_TOKENS:
        return _REASONING_MIN_TOKENS
    return max_tokens


class LLMTruncationError(RuntimeError):
    """Raised when the model returns an empty content string with finish_reason='length'.

    This happens with reasoning models (e.g. gpt-oss:20b) when the hidden
    reasoning_content consumes the entire max_tokens budget before the visible
    content is written.  Callers that parse structured JSON from think() MUST
    handle this rather than silently receiving '' and crashing on json.loads.
    """


class LLMKillSwitchError(RuntimeError):
    """Raised when LLM generation is disabled by the spend-governor kill switch.

    The switch is a `settings` row (`llm_kill_switch`) flipped either by
    `LLMSpendGuardFlow` on a rolling-24h token-budget breach, or by hand from
    the admin Settings page. It gates generation only — `embed()` is exempt so
    knowledge search keeps working while spend is frozen.

    See `aegis.services.llm_governor`. Clear it by setting
    `llm_kill_switch.active = false` (the governor auto-clears only switches it
    set itself, i.e. `set_by == "governor"`).
    """


def _classify_llm_error(exc: BaseException) -> str:
    """Map an exception to a status string for `llm_calls.status`.

    Returns "timeout" for timeout-class errors, "error" otherwise.
    """
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    if "timeout" in name or "timeout" in msg or "timed out" in msg:
        return "timeout"
    return "error"


_BATCH_RECEIPT_PROMPT = """\
You are extracting structured data from email receipts and renewal notices.

For EACH receipt below, return one JSON object with these fields:
- is_receipt: true ONLY if this confirms money actually charged or due — a
  payment receipt, invoice, renewal notice, or subscription charge
  confirmation. false for newsletters, marketing, alerts, account statements,
  order confirmations for physical goods, AND any promotional/offer email even
  when it quotes a figure: insurance coverage / sum-assured amounts, credit-card
  or loan eligibility/limit offers, reward/cashback/discount amounts, "you are
  eligible for ₹X" pitches. A number in an advertisement is NOT a charge.
- vendor_name: human-readable display name (e.g. "Namecheap", "Zerodha").
- sender_label: lowercased canonical id, prefer the sender domain
  (e.g. "namecheap.com").
- category: one of domain, saas, insurance, lease, media, infra, other.
- amount: REQUIRED when is_receipt=true. Extract the charge amount as a
  float. Look for patterns like "Rs. 1,234", "₹1234", "USD 29.99",
  "$29.99", "INR 1234.00", "Total: 500", "Amount Due: 1,499". Strip
  commas from numbers. Return null ONLY if truly no amount appears.
- currency: ISO-3 code (INR, USD, EUR). Infer from ₹/Rs./Rupees → INR,
  $→USD, €→EUR. Null if unknown.
- cadence: monthly | quarterly | yearly | unknown. Infer from "annual",
  "every month", "billed quarterly", "3 months", "1 year plan".
- next_due_at: ISO date (YYYY-MM-DD) if explicitly stated; null otherwise.
- is_recurring: true if this looks like a SUBSCRIPTION or UTILITY that will
  bill again (SaaS plan, streaming, domain/hosting renewal, insurance
  premium, electricity/water/phone bill). false if it is a ONE-OFF purchase
  that will not repeat (a single Amazon/e-commerce order, a one-time
  service, a single event ticket). null if you cannot tell.
- confidence: 0.0–1.0 self-rating.

IMPORTANT: When is_receipt=true you MUST provide the amount and currency that
was actually billed/charged. Do not leave amount null for a real receipt. But
do NOT manufacture a charge from an unrelated figure: if the only number is a
coverage limit, eligibility/credit limit, reward, or advertised price in a
marketing email, set is_receipt=false rather than recording it as a charge.

Also set is_receipt=false (no money was actually charged to a vendor) for:
- FAILED / declined / reversed / unsuccessful / refunded payments — a failure
  notice ("payment failed", "failed for", "declined", "unsuccessful",
  "reversed", "refund") means nothing was charged, not a receipt.
- Bank/card AUTOPAY REMINDERS and ACTIVATION notices ("upcoming autopay",
  "autopay reminder", "autopay … activated", "mandate") — a bank telling you a
  charge is *upcoming* is a heads-up, NOT a receipt of money charged; and the
  named merchant is the autopay target, never the email sender.
- Credit-card STATEMENTS / bills ("new bill", "credit card bill", "statement",
  "minimum due", "total due", "pay now") — a card statement total is the bill
  for the whole card, not a per-vendor subscription charge.

Return a JSON array with EXACTLY one object per receipt, in the same
order. Wrap in ```json fences.

RECEIPTS:
{receipts}
"""


def _format_receipts_for_prompt(receipts: list[dict]) -> str:
    parts = []
    for i, r in enumerate(receipts):
        parts.append(
            f"--- Receipt {i + 1} ---\n"
            f"From: {r.get('sender', '')}\n"
            f"Subject: {r.get('subject', '')}\n"
            f"Body (truncated): {(r.get('body_plain') or '')[:4000]}\n"
        )
    return "\n".join(parts)


_MONEY_EVENT_PROMPT = """\
You are the bookkeeper. For EACH email below return one JSON object describing
the money event it carries.

Fields:
- kind: "transaction" (money actually moved: a receipt, payment confirmation,
  debit or credit alert), "due" (a bill, card statement, autopay reminder or
  deadline asking for payment by a date), "failed" (a declined, failed or
  reversed payment that needs fixing), "info" (statement available, KYC,
  balance or account notice with no money moving), "ignore" (newsletter,
  marketing, offers, anything else).
- direction: "out" (you paid) or "in" (you received); null for info/ignore.
- amount: number in MAJOR units (245.50, never 24550). Strip commas. Null if none.
- currency: ISO code. ₹ / Rs / INR -> INR, $ -> USD, £ -> GBP, € -> EUR.
- payee: the other party (merchant, biller, person, sender of funds) as a
  display name, never an email address.
- category: one of saas, media, infra, internet, electricity, mobile,
  groceries, food, transport, shopping, health, insurance, fees, tax,
  professional, ads, people, salary, interest, refund, other.
- channel: one of upi, imps, neft, card, autopay, remittance, receipt, bill,
  statement, other.
- instrument: the paying or receiving account as hdfc-1225 (bank + last 4
  digits), axis-cc-1313 (credit card), card-1313 (card, bank unknown), or null.
- occurred_on: date money moved, YYYY-MM-DD, or null.
- due_on: payment-due or fix-by date, YYYY-MM-DD, or null.
- is_recurring: true for a subscription or utility that bills again, false
  for a one-off, null if unsure.
- confidence: 0.0-1.0.

Rules: a number in an advertisement, offer, insurance cover or credit limit
is NOT a charge (kind ignore). An autopay reminder or a card statement is
"due", never "transaction". A failed, declined or reversed payment is
"failed"; a refund you received is a "transaction" with direction "in". A
statement delivered as a PDF with no figures in the text is "info".

Return a JSON array with EXACTLY one object per email, in the same order,
inside ```json fences.

EMAILS:
{emails}
"""


def _format_money_emails(receipts: list[dict]) -> str:
    """One block per email for `_MONEY_EVENT_PROMPT`.

    The body is the FULL plain text (capped at 4000 chars), not the 200-char
    snippet v1 classified on: the due date, the last-4 of the card and the
    "declined" wording all live below the fold.
    """
    parts = []
    for i, r in enumerate(receipts):
        parts.append(
            f"--- Email {i + 1} ---\n"
            f"From: {r.get('sender', '')}\n"
            f"Subject: {r.get('subject', '')}\n"
            f"Body: {(r.get('body_plain') or '')[:4000]}\n"
        )
    return "\n".join(parts)


class LLMClient:
    """Async LLM client using OpenAI-compatible API."""

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        timeout: int = 300,
        concurrency_limits: dict[str, int] | None = None,
        db_pool: Any = None,
    ):
        self._client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key or "not-needed",
            timeout=timeout,
        )
        # Optional — enables the spend-governor kill switch on generation calls
        # (`think`/`chat`, and `extract_receipts_batch` via `think`). When None
        # the client is ungoverned, which is what comms and the ad-hoc
        # backend-connectivity test client want.
        self._db_pool = db_pool
        # Per-model semaphores. Used to throttle models that share a single
        # busy GPU (e.g. gemma4:e2b on node-a's GPU alongside postgres,
        # core, worker, comms and redis). Bursts of concurrent calls
        # otherwise serialize through ollama and compound latency.
        self._concurrency_limits = dict(concurrency_limits or {})
        self._semaphores: dict[str, asyncio.Semaphore] = {}

    async def _check_kill_switch(self) -> None:
        """Refuse generation while the spend-governor kill switch is active.

        No pool ⇒ ungoverned, return immediately. `get_kill_switch` never
        raises (any DB error resolves to "inactive"), so this guard fails
        OPEN by construction — it sits in front of every generation call in
        AEGIS and must never be able to take the system down itself.
        """
        if self._db_pool is None:
            return
        # Local import: aegis.services.llm_governor imports nothing from
        # aegis.llm, so there is no cycle — but keep it lazy anyway so the
        # LLM package stays importable without the services package.
        from aegis.services.llm_governor import get_kill_switch

        ks = await get_kill_switch(self._db_pool)
        if ks.get("active"):
            raise LLMKillSwitchError(
                f"llm kill switch active: {ks.get('reason') or 'unset'} "
                f"(set_by={ks.get('set_by') or 'unknown'})"
            )

    def _semaphore_for(self, model: str) -> asyncio.Semaphore | None:
        limit = self._concurrency_limits.get(model)
        if not limit:
            return None
        sem = self._semaphores.get(model)
        if sem is None:
            sem = asyncio.Semaphore(limit)
            self._semaphores[model] = sem
        return sem

    async def think(
        self,
        prompt: str,
        model: str = "gemma4:e2b",
        system_prompt: str | None = None,
        max_tokens: int = 2000,
        db_pool: Any = None,
        purpose: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Send a prompt to the LLM and return the response (no tool calling).

        Every terminal outcome — success, truncation, upstream failure — is
        recorded to `llm_calls` by `_record_call`. Supply a `purpose`; the pool
        is the client's own unless you pass a different `db_pool`. Do NOT call
        `record_llm_call` yourself afterwards: that double-counts spend.

        A response that comes back EMPTY with `finish_reason='length'` is
        re-issued ONCE at `_TRUNCATION_RETRY_TOKENS` before
        `LLMTruncationError` is raised (#321). Two upstream calls is a hard cap
        — the re-roll never re-rolls — and both are billed, so both are
        recorded. The retry keys on the truncation SYMPTOM, never on a model
        name: that is the point. The recurring failure is a reasoning model
        nobody added to `_REASONING_MODELS`, which therefore gets no floor at
        all and fails silently (#255), and a symptom-keyed retry rescues it
        without anyone having to notice first.
        """
        await self._check_kill_switch()
        max_tokens = _reasoning_floor(model, max_tokens)

        messages: list[dict[str, Any]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        # None ⇒ nothing to re-roll at, because the caller already asked for at
        # least the retry budget. That call gets one attempt and the raise.
        retry_budget = (
            _TRUNCATION_RETRY_TOKENS if max_tokens < _TRUNCATION_RETRY_TOKENS else None
        )
        try:
            return await self._think_once(
                messages,
                model,
                max_tokens,
                db_pool,
                purpose,
                agent_id,
                retry_budget=retry_budget,
            )
        except LLMTruncationError:
            if retry_budget is None:
                raise
            logger.warning(
                "llm_truncated_retrying",
                model=model,
                purpose=purpose,
                first_max_tokens=max_tokens,
                retry_max_tokens=retry_budget,
            )
        # `retry_budget=None` makes this attempt terminal — it raises instead of
        # recursing, which is what pins the hard cap at two upstream calls.
        return await self._think_once(
            messages,
            model,
            retry_budget,
            db_pool,
            purpose,
            agent_id,
            retry_budget=None,
        )

    async def _think_once(
        self,
        messages: list[dict[str, Any]],
        model: str,
        max_tokens: int,
        db_pool: Any,
        purpose: str | None,
        agent_id: str | None,
        *,
        retry_budget: int | None,
    ) -> dict[str, Any]:
        """One upstream completion for `think()`; raises on empty truncation.

        `retry_budget` changes nothing about the request. It is the budget
        `think()` will re-roll at should this attempt truncate (None when this
        attempt is terminal), and it lands in the recorded error text so an
        `llm_calls` row tells the operator whether they are looking at a
        rescued attempt or a real failure. The count of `(retrying at N)` rows
        is the meter for a stale floor: when it climbs, `_REASONING_MIN_TOKENS`
        has fallen behind whatever the tier map now resolves to.
        """
        import time

        sem = self._semaphore_for(model)
        with _tracer.start_as_current_span("llm.call") as span:
            span.set_attribute("llm.model", model)
            span.set_attribute("llm.operation", "think")
            span.set_attribute("llm.max_tokens", max_tokens)
            _set_genai_request(span, "text_completion", model, max_tokens)
            _t0 = time.monotonic()
            try:
                if sem is not None:
                    async with sem:
                        completion = await self._client.chat.completions.create(
                            model=model,
                            messages=messages,
                            max_tokens=max_tokens,
                        )
                else:
                    completion = await self._client.chat.completions.create(
                        model=model,
                        messages=messages,
                        max_tokens=max_tokens,
                    )
            except Exception as exc:
                span.set_attribute("llm.status", "error")
                await self._record_call(
                    db_pool,
                    model,
                    purpose,
                    agent_id,
                    _t0,
                    status=_classify_llm_error(exc),
                    error=str(exc)[:500],
                )
                raise

            choice = completion.choices[0]
            response = choice.message.content or ""
            finish_reason = getattr(choice, "finish_reason", None)
            usage = completion.usage
            prompt_tokens = getattr(usage, "prompt_tokens", 0)
            completion_tokens = getattr(usage, "completion_tokens", 0)
            latency_ms = int((time.monotonic() - _t0) * 1000)

            span.set_attribute("llm.input_tokens", prompt_tokens)
            span.set_attribute("llm.output_tokens", completion_tokens)
            span.set_attribute("llm.latency_ms", latency_ms)
            span.set_attribute("llm.finish_reason", finish_reason or "")
            _set_genai_usage(span, prompt_tokens, completion_tokens)
            span.set_attribute("gen_ai.response.finish_reasons", [finish_reason or "unknown"])

            # Reasoning models (e.g. gpt-oss:20b) bill hidden reasoning_content
            # against max_tokens.  When the budget is exhausted before visible
            # content is written, finish_reason='length' AND content is empty.
            # Returning '' silently causes downstream json.loads('') to raise a
            # cryptic JSONDecodeError — surface a typed error instead.
            if not response.strip() and finish_reason == "length":
                span.set_attribute("llm.status", "truncated")
                logger.warning(
                    "llm_truncated",
                    model=model,
                    max_tokens=max_tokens,
                    output_tokens=completion_tokens,
                    purpose=purpose,
                )
                detail = (
                    f"model={model} returned empty content with finish_reason=length "
                    f"(max_tokens={max_tokens}, output_tokens={completion_tokens}); "
                    "increase max_tokens or suppress reasoning"
                )
                # This branch runs AFTER a real, billed upstream call, so it
                # needs its own row: a model that truncates every call would
                # otherwise be indistinguishable from a model nobody called.
                # That stays true of a rescued attempt — the tokens were spent
                # either way — so the retry gets a row too, marked as such.
                recorded = f"truncated: {detail}"
                if retry_budget is not None:
                    recorded += f" (retrying at {retry_budget})"
                await self._record_call(
                    db_pool,
                    model,
                    purpose,
                    agent_id,
                    _t0,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    status="error",
                    error=recorded[:500],
                )
                raise LLMTruncationError(detail)

            # The OTHER truncation: budget exhausted AFTER some visible content
            # was written. `finish_reason=length` with a non-empty body is a
            # response cut mid-sentence — for a JSON caller that means a short
            # array or an unparseable tail, and prod ran 6/42
            # intel_score_significance calls into it, every one recorded as a
            # plain success. It is deliberately NOT raised: partial content is
            # often still usable, and the callers that parse it already handle
            # a short/failed parse. It gets its own status so the failure mode
            # stops hiding inside the success count.
            clipped = finish_reason == "length"
            if clipped:
                span.set_attribute("llm.status", "clipped")
                logger.warning(
                    "llm_clipped",
                    model=model,
                    max_tokens=max_tokens,
                    output_tokens=completion_tokens,
                    purpose=purpose,
                )
            else:
                span.set_attribute("llm.status", "success")
            await self._record_call(
                db_pool,
                model,
                purpose,
                agent_id,
                _t0,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                status="clipped" if clipped else "success",
                error=(
                    f"clipped: model={model} hit finish_reason=length after "
                    f"{completion_tokens} visible tokens (max_tokens={max_tokens}); "
                    "content may be cut mid-response"
                    if clipped
                    else None
                ),
            )

            logger.debug(
                "llm_complete",
                model=model,
                response_len=len(response),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )

            return {
                "response": response,
                "model": model,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            }

    async def _record_call(
        self,
        db_pool: Any,
        model: str,
        purpose: str | None,
        agent_id: str | None,
        t0: float,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        status: str = "success",
        error: str | None = None,
    ) -> None:
        """Write one `llm_calls` row for a generation call. Never raises.

        THE choke point (issue #106). Every terminal outcome of
        `think()`/`chat()` funnels through here, so "an LLM call is visible in
        `llm_calls`" is structural rather than a convention every new call site
        has to remember — several never did, and their spend was invisible.
        **Call sites must not also call `record_llm_call` themselves**: a second
        row inflates reported spend with no error anywhere.

        Pool resolution: the per-call `db_pool` when the caller passes one, else
        the client's own. A client constructed with a pool is governed — it
        already pays for the kill-switch lookup on every call — so it records
        too, which is what makes a bare `purpose=` enough at a core route.
        A client built without a pool is deliberately ungoverned
        (`routes/llm_backend.py::test_backend`) and stays silent.

        `purpose` is what makes a row attributable, so there is no row without
        one. A caller that hands over an explicit `db_pool` and no `purpose` has
        simply forgotten it — say so at WARNING rather than dropping the call on
        the floor. Passing neither is the opted-out path (`services/chat.py`'s
        tool loop, which records itself).

        The kill switch deliberately produces no row: it raises before any HTTP
        request, so nothing was spent.
        """
        pool = db_pool if db_pool is not None else self._db_pool
        if pool is None or not purpose:
            if db_pool is not None and not purpose:
                logger.warning("llm_call_unrecorded", model=model, reason="missing_purpose")
            return
        import time

        try:
            from aegis.observability import record_llm_call

            await record_llm_call(
                pool,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency_ms=int((time.monotonic() - t0) * 1000),
                purpose=purpose,
                agent_id=agent_id,
                status=status,
                error=error,
            )
        except Exception:
            logger.warning("record_llm_call_failed", model=model, purpose=purpose)

    async def chat(
        self,
        messages: list[dict[str, Any]],
        model: str = "qwen3:14b",
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 2000,
        db_pool: Any = None,
        purpose: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Full chat completion with tool calling support.

        Args:
            messages: OpenAI-format message list [{role, content}]
            model: Model to use
            tools: OpenAI-format tool definitions [{type, function: {name, description, parameters}}]
            max_tokens: Max response tokens

        Returns:
            {response, tool_calls, model, usage}
            tool_calls is a list of {id, name, arguments} if the model wants to call tools.
        """
        await self._check_kill_switch()
        max_tokens = _reasoning_floor(model, max_tokens)

        import time

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        sem = self._semaphore_for(model)
        with _tracer.start_as_current_span("llm.call") as span:
            span.set_attribute("llm.model", model)
            span.set_attribute("llm.operation", "chat")
            span.set_attribute("llm.max_tokens", max_tokens)
            span.set_attribute("llm.tools_count", len(tools) if tools else 0)
            _set_genai_request(span, "chat", model, max_tokens)
            _t0 = time.monotonic()
            try:
                if sem is not None:
                    async with sem:
                        completion = await self._client.chat.completions.create(**kwargs)
                else:
                    completion = await self._client.chat.completions.create(**kwargs)
            except Exception as exc:
                span.set_attribute("llm.status", "error")
                await self._record_call(
                    db_pool,
                    model,
                    purpose,
                    agent_id,
                    _t0,
                    status=_classify_llm_error(exc),
                    error=str(exc)[:500],
                )
                raise

            choice = completion.choices[0]
            message = choice.message
            usage = completion.usage

            # Extract tool calls if any
            tool_calls = []
            if message.tool_calls:
                for tc in message.tool_calls:
                    tool_calls.append(
                        {
                            "id": tc.id,
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,  # JSON string
                        }
                    )

            prompt_tokens = getattr(usage, "prompt_tokens", 0)
            completion_tokens = getattr(usage, "completion_tokens", 0)
            latency_ms = int((time.monotonic() - _t0) * 1000)

            span.set_attribute("llm.input_tokens", prompt_tokens)
            span.set_attribute("llm.output_tokens", completion_tokens)
            span.set_attribute("llm.latency_ms", latency_ms)
            span.set_attribute("llm.tool_calls_returned", len(tool_calls))
            span.set_attribute("llm.status", "success")
            _set_genai_usage(span, prompt_tokens, completion_tokens)
            await self._record_call(
                db_pool,
                model,
                purpose,
                agent_id,
                _t0,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )

            logger.debug(
                "llm_chat_complete",
                model=model,
                response_len=len(message.content or ""),
                tool_calls=len(tool_calls),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )

            return {
                "response": message.content or "",
                "tool_calls": tool_calls,
                "model": model,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            }

    async def extract_receipts_batch(
        self,
        receipts: list[dict],
        model: str = "gemma4:e2b",
        system_prompt: str | None = None,
        db_pool: Any = None,
        agent_id: str | None = None,
    ) -> list[dict]:
        """Classify + extract structured fields for a batch of receipts.

        Sends one prompt with all N receipts, parses the JSON-array
        response into per-receipt dicts matching the
        `aegis.api.models.money.ReceiptExtraction` schema.

        On full-batch failure (LLM error, JSON decode, wrong shape) this
        method RAISES so the caller can decide to retry or drop. The one
        exception is `LLMTruncationError`: when the model exhausts its token
        budget on hidden reasoning before writing visible content, we return
        N items each marked `_parse_failed=True` rather than crashing the
        whole MoneyProcessFlow.  Per-item parse failure inside an
        otherwise-OK batch is also signalled with `_parse_failed=True`.

        Callers that want a fire-and-forget "always return N items" path
        should wrap in try/except themselves; this used to swallow all
        failures silently and let money_process upsert garbage rows.

        `system_prompt` — optional persona context prepended to the
        extraction instruction so downstream agents (maou) can steer
        the classifier's voice/policy without changing the schema.

        `agent_id` — owning agent (e.g. "maou"), threaded through to the
        `llm_calls` row so per-agent spend/usage stays attributable (issue:
        95% of worker-side llm_calls rows had NULL agent_id).

        The spend-governor kill switch applies here transitively: this
        delegates to `think()`, whose guard raises `LLMKillSwitchError`
        before any HTTP call. That error is NOT converted to
        `_parse_failed` stubs — only `LLMTruncationError` is — so a
        spend freeze surfaces as a real failure rather than silently
        marking every receipt unparseable.
        """
        from aegis.api.models.money import ReceiptExtraction

        if not receipts:
            return []
        prompt = _BATCH_RECEIPT_PROMPT.format(receipts=_format_receipts_for_prompt(receipts))
        try:
            result = await self.think(
                prompt=prompt,
                model=model,
                system_prompt=system_prompt,
                max_tokens=4000,
                db_pool=db_pool,
                purpose="money_receipt_extraction",
                agent_id=agent_id,
            )
            parsed = parse_llm_json(result.get("response", ""))
            if not isinstance(parsed, list):
                raise ValueError("expected JSON array")
        except LLMTruncationError as exc:
            # Reasoning model consumed the token budget on hidden content.
            # Return _parse_failed stubs so MoneyProcessFlow skips these
            # receipts without crashing the whole batch or retrying endlessly.
            logger.warning(
                "extract_receipts_batch_truncated",
                error=str(exc)[:200],
                count=len(receipts),
            )
            return [
                {"is_receipt": False, "confidence": 0.0, "_parse_failed": True}
                for _ in receipts
            ]
        except Exception as exc:
            logger.warning(
                "extract_receipts_batch_failed",
                error=str(exc)[:200],
                count=len(receipts),
            )
            raise

        out: list[dict] = []
        for i in range(len(receipts)):
            if i < len(parsed) and isinstance(parsed[i], dict):
                try:
                    out.append(ReceiptExtraction(**parsed[i]).model_dump())
                except Exception:
                    out.append(
                        {
                            "is_receipt": False,
                            "confidence": 0.0,
                            "_parse_failed": True,
                        }
                    )
            else:
                out.append(
                    {
                        "is_receipt": False,
                        "confidence": 0.0,
                        "_parse_failed": True,
                    }
                )
        return out

    async def extract_money_batch(
        self,
        receipts: list[dict],
        model: str = "gemma4:e2b",
        system_prompt: str | None = None,
        db_pool: Any = None,
        agent_id: str | None = None,
    ) -> list[dict]:
        """One `MoneyEvent` dict per input email (spec §4).

        The v2 extractor. It replaces the "is this a receipt?" question with
        "what money event is this?", and reads the FULL body rather than the
        200-char snippet `extract_receipts_batch` saw — a declined payment, an
        autopay reminder and a paid invoice are only distinguishable further
        down the mail.

        Failure semantics, deliberately three-way:

        * `LLMTruncationError` — the token budget went on hidden reasoning, so
          nothing came back for anyone. One `_parse_failed` stub per input, no
          raise: a bad budget must not take the whole flow down.
        * any other batch-level failure (upstream error, non-JSON, not an
          array, `LLMKillSwitchError`) — RAISES, so an outage or a spend
          freeze surfaces instead of quietly reading as "nothing to book".
        * a per-item shape failure inside an otherwise-good batch — a stub for
          that item only; its siblings still book.

        Callers must therefore check `_parse_failed` before writing a row.
        """
        from aegis.api.models.money import MoneyEvent, payee_key

        if not receipts:
            return []
        stub = {"kind": "ignore", "parser": "llm", "_parse_failed": True}
        prompt = _MONEY_EVENT_PROMPT.format(emails=_format_money_emails(receipts))
        try:
            result = await self.think(
                prompt=prompt,
                model=model,
                system_prompt=system_prompt,
                max_tokens=4000,
                db_pool=db_pool,
                purpose="money_event_extraction",
                agent_id=agent_id,
            )
            parsed = parse_llm_json(result.get("response", ""))
            if not isinstance(parsed, list):
                raise ValueError("expected JSON array")
        except LLMTruncationError as exc:
            logger.warning(
                "extract_money_batch_truncated",
                error=str(exc)[:200],
                count=len(receipts),
            )
            return [dict(stub) for _ in receipts]
        except Exception as exc:
            logger.warning(
                "extract_money_batch_failed",
                error=str(exc)[:200],
                count=len(receipts),
            )
            raise

        allowed = set(MoneyEvent.model_fields)
        out: list[dict] = []
        for i in range(len(receipts)):
            item = parsed[i] if i < len(parsed) and isinstance(parsed[i], dict) else None
            if item is None:
                out.append(dict(stub))
                continue
            # Unknown keys are dropped rather than rejected: a model that
            # invents a field must not cost us the whole event.
            data = {k: v for k, v in item.items() if k in allowed}
            if isinstance(data.get("amount"), str):
                data["amount"] = data["amount"].replace(",", "")
            data["parser"] = "llm"
            data["source_class"] = (
                "receipt" if data.get("channel") in ("receipt", "bill") else "other"
            )
            try:
                ev = MoneyEvent(**data)
            except Exception:
                out.append(dict(stub))
                continue
            ev.payee_key = payee_key(ev.payee)
            out.append(ev.model_dump(mode="json"))
        return out

    async def embed(
        self,
        texts: list[str],
        model: str = "nomic-embed-text",
    ) -> list[list[float]]:
        """Embed a batch of texts via the OpenAI-compatible /embeddings endpoint.

        Used by the native pgvector knowledge subsystem. Returns one vector per
        input text, in order. Empty input returns []. The model default is a
        local Ollama embedder (no cloud key); the vector dim must match the
        `knowledge_chunks.embedding` column (768 for nomic-embed-text).
        """
        if not texts:
            return []
        # POST /embeddings directly instead of via the OpenAI SDK: the SDK always
        # sends `encoding_format` (base64 by default, and it can't be omitted),
        # which LiteLLM's Ollama embeddings provider rejects with a 400
        # (UnsupportedParamsError). A plain request omits it. Reuse the SDK
        # client's resolved base_url + api_key so this matches the chat path.
        import httpx

        base = str(self._client.base_url).rstrip("/")
        headers = {}
        api_key = getattr(self._client, "api_key", None)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        with _tracer.start_as_current_span("llm.call") as span:
            span.set_attribute("llm.model", model)
            span.set_attribute("llm.operation", "embed")
            span.set_attribute("llm.embed_count", len(texts))
            _set_genai_request(span, "embeddings", model)
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    f"{base}/embeddings", json={"model": model, "input": texts}, headers=headers
                )
                resp.raise_for_status()
                return [item["embedding"] for item in resp.json()["data"]]

    async def close(self):
        """Close the underlying HTTP client."""
        await self._client.close()


# Imported after LLMClient to avoid intra-package circular imports.
from aegis.llm.tier import (  # noqa: E402
    resolve_model_for_agent,
    set_model_tiers,
    tier_to_model,
)

__all__ = [
    "LLMClient",
    "LLMKillSwitchError",
    "LLMTruncationError",
    "parse_llm_json",
    "resolve_model_for_agent",
    "set_model_tiers",
    "tier_to_model",
]
