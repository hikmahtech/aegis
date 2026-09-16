"""A real `LLMClient` with only its HTTP layer stubbed.

`llm_calls` rows are written inside `LLMClient._record_call` — the single choke
point every `think()`/`chat()` outcome funnels through (issue #106). That makes a
hand-rolled fake with its own `think()` method actively misleading in any test
that asserts on recording: the fake replaces the code under test, so the
assertion passes whether or not the production path works.

Drive the real client instead and stub only `chat.completions.create`, which is
the one thing a test genuinely cannot run. Combine with the real-Postgres
`db_pool` fixture and read the rows back: `record_llm_call` swallows its own
errors, so a mock assertion also passes against a write that never landed.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from aegis.llm import LLMClient


class StubbedLLMClient(LLMClient):
    """Real `think()`/`chat()`; the OpenAI call is scripted.

    `content` is the assistant text (or a list, consumed one per call), or use
    `responder(create_kwargs) -> str` when the reply has to depend on the
    prompt. `finish_reason="length"` with empty content reproduces the
    reasoning-model truncation case; pass a LIST to vary it per call, which is
    what a truncate-then-succeed sequence needs now that `think()` re-rolls a
    truncated call once (#321). `raises` makes the upstream call blow up,
    which is the failure-row path.
    """

    def __init__(
        self,
        *,
        db_pool: Any = None,
        content: str | list[str] = "",
        responder: Any = None,
        finish_reason: str | list[str] = "stop",
        prompt_tokens: int = 11,
        completion_tokens: int = 22,
        raises: BaseException | None = None,
        tool_calls: list | None = None,
        embed_vector: list[float] | None = None,
    ):
        super().__init__(base_url="http://litellm.invalid/v1", db_pool=db_pool)
        self.calls: list[dict] = []
        self._scripted = list(content) if isinstance(content, list) else None
        self._content = "" if self._scripted is not None else content
        self._responder = responder
        # A list is consumed one entry per call, like `content`; once it runs
        # out every further call reports "stop".
        self._finish_reasons = list(finish_reason) if isinstance(finish_reason, list) else None
        self._finish_reason = "stop" if self._finish_reasons is not None else finish_reason
        self._prompt_tokens = prompt_tokens
        self._completion_tokens = completion_tokens
        self._raises = raises
        self._tool_calls = tool_calls
        self._embed_vector = embed_vector

        async def _create(**kwargs):
            self.calls.append(kwargs)
            if self._raises is not None:
                raise self._raises
            if self._responder is not None:
                text = self._responder(kwargs)
            elif self._scripted is not None:
                text = self._scripted.pop(0) if self._scripted else ""
            else:
                text = self._content
            if self._finish_reasons is not None:
                reason = self._finish_reasons.pop(0) if self._finish_reasons else "stop"
            else:
                reason = self._finish_reason
            message = SimpleNamespace(content=text, tool_calls=self._tool_calls)
            choice = SimpleNamespace(message=message, finish_reason=reason)
            return SimpleNamespace(
                choices=[choice],
                usage=SimpleNamespace(
                    prompt_tokens=self._prompt_tokens,
                    completion_tokens=self._completion_tokens,
                ),
            )

        self._client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=_create))
        )

    async def embed(self, texts, model="nomic-embed-text"):
        """`embed()` is not a generation call and records nothing; stubbed only
        so knowledge tests don't need a live embeddings endpoint."""
        if self._embed_vector is None:
            raise AssertionError("StubbedLLMClient.embed needs embed_vector")
        return [list(self._embed_vector) for _ in texts]

    @property
    def call_count(self) -> int:
        return len(self.calls)


class RecordingFakeLLM:
    """A fake `think()` that records the kwargs it was called with.

    Reach for this ONLY when the assertion is about what the CALLER handed
    `think()` — `purpose`, `agent_id`, the `db_pool` it passes to the ledger,
    the prompt text — or about an `llm_calls` row NOT being written. A fake
    replaces `think()`, so it records nothing: that absence is the point of the
    tests asserting the table stayed empty, and it is why `StubbedLLMClient` is
    the wrong tool for them. Anything asserting a row WAS written must use
    `StubbedLLMClient`, which drives the real recording path.

    `response` is the reply text, or a LIST consumed one per call whose last
    entry then repeats — a retry that keeps getting garbage is what the
    terminal case looks like in production. `by_purpose` answers one specific
    `purpose` and falls back to `response`; `responder(think_kwargs) -> str`
    covers a reply that has to be computed from the call; `raises` makes the
    call blow up.
    """

    def __init__(
        self,
        response: Any = None,
        *,
        raises: BaseException | None = None,
        by_purpose: dict[str, str] | None = None,
        responder: Any = None,
        model: str = "fake-model",
        prompt_tokens: int = 11,
        completion_tokens: int = 22,
    ):
        self._responses = list(response) if isinstance(response, list) else [response]
        self._raises = raises
        self._by_purpose = by_purpose or {}
        self._responder = responder
        self._model = model
        self._prompt_tokens = prompt_tokens
        self._completion_tokens = completion_tokens
        self.calls: list[dict] = []

    async def think(self, prompt: Any = None, **kwargs) -> dict[str, Any]:
        kwargs.setdefault("prompt", prompt)
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        if self._responder is not None:
            response = self._responder(kwargs)
        elif kwargs.get("purpose") in self._by_purpose:
            response = self._by_purpose[kwargs["purpose"]]
        else:
            response = self._responses[min(len(self.calls), len(self._responses)) - 1]
        return {
            "response": response,
            "model": self._model,
            "prompt_tokens": self._prompt_tokens,
            "completion_tokens": self._completion_tokens,
        }

    def prompt_for(self, purpose: str) -> str:
        """The prompt of the first call made with `purpose` (empty if none)."""
        return next(
            (str(c.get("prompt") or "") for c in self.calls if c.get("purpose") == purpose), ""
        )
