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
    reasoning-model truncation case. `raises` makes the upstream call blow up,
    which is the failure-row path.
    """

    def __init__(
        self,
        *,
        db_pool: Any = None,
        content: str | list[str] = "",
        responder: Any = None,
        finish_reason: str = "stop",
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
        self._finish_reason = finish_reason
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
            message = SimpleNamespace(content=text, tool_calls=self._tool_calls)
            choice = SimpleNamespace(message=message, finish_reason=self._finish_reason)
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
