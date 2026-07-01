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


class LLMTruncationError(RuntimeError):
    """Raised when the model returns an empty content string with finish_reason='length'.

    This happens with reasoning models (e.g. gpt-oss:20b) when the hidden
    reasoning_content consumes the entire max_tokens budget before the visible
    content is written.  Callers that parse structured JSON from think() MUST
    handle this rather than silently receiving '' and crashing on json.loads.
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
            f"Body (truncated): {(r.get('body_plain') or '')[:1500]}\n"
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
    ):
        self._client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key or "not-needed",
            timeout=timeout,
        )
        # Per-model semaphores. Used to throttle models that share a single
        # busy GPU (e.g. gemma4:e2b on node-a's RTX 2070 alongside postgres,
        # core, worker, telegram and redis). Bursts of concurrent calls
        # otherwise serialize through ollama and compound latency.
        self._concurrency_limits = dict(concurrency_limits or {})
        self._semaphores: dict[str, asyncio.Semaphore] = {}

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

        When `db_pool` and `purpose` are both provided, failures are
        recorded into `llm_calls` with status="timeout"|"error" so we
        can measure the real failure rate (success rows are still
        written by the caller — that path is unchanged).
        """
        import time

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

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
                await self._record_failure(
                    db_pool, model, purpose, agent_id, _t0, exc
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
                raise LLMTruncationError(
                    f"model={model} returned empty content with finish_reason=length "
                    f"(max_tokens={max_tokens}, output_tokens={completion_tokens}); "
                    "increase max_tokens or suppress reasoning"
                )

            span.set_attribute("llm.status", "success")

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

    async def _record_failure(
        self,
        db_pool: Any,
        model: str,
        purpose: str | None,
        agent_id: str | None,
        t0: float,
        exc: BaseException,
    ) -> None:
        """Best-effort failure row for `llm_calls`. Never raises."""
        if db_pool is None or not purpose:
            return
        import time

        try:
            from aegis.observability import record_llm_call

            await record_llm_call(
                db_pool,
                model=model,
                prompt_tokens=0,
                completion_tokens=0,
                latency_ms=int((time.monotonic() - t0) * 1000),
                purpose=purpose,
                agent_id=agent_id,
                status=_classify_llm_error(exc),
                error=str(exc)[:500],
            )
        except Exception:
            logger.warning("record_llm_failure_failed", model=model, purpose=purpose)

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
                await self._record_failure(
                    db_pool, model, purpose, agent_id, _t0, exc
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
        """
        import time

        from aegis.api.models.money import ReceiptExtraction

        if not receipts:
            return []
        prompt = _BATCH_RECEIPT_PROMPT.format(receipts=_format_receipts_for_prompt(receipts))
        _t0 = time.monotonic()
        try:
            result = await self.think(
                prompt=prompt,
                model=model,
                system_prompt=system_prompt,
                max_tokens=4000,
                db_pool=db_pool,
                purpose="money_receipt_extraction",
            )
            if db_pool is not None:
                from aegis.observability import record_llm_call

                await record_llm_call(
                    db_pool,
                    model=result.get("model", model),
                    prompt_tokens=result.get("prompt_tokens", 0),
                    completion_tokens=result.get("completion_tokens", 0),
                    latency_ms=int((time.monotonic() - _t0) * 1000),
                    purpose="money_receipt_extraction",
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
    load_model_tiers,
    resolve_model_for_agent,
    set_model_tiers,
    tier_to_model,
)

__all__ = [
    "LLMClient",
    "LLMTruncationError",
    "load_model_tiers",
    "parse_llm_json",
    "resolve_model_for_agent",
    "set_model_tiers",
    "tier_to_model",
]
