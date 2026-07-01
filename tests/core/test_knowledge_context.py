"""Tests for proactive knowledge context injection."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aegis.config import Settings
from aegis.services.chat import _gather_knowledge_context, send_message


def test_knowledge_context_settings_defaults():
    """Knowledge context config has correct defaults."""
    s = Settings(
        litellm_api_key="k",
        api_key="k",
        database_url="postgresql://x:x@localhost/x",
        litellm_url="https://litellm.test/v1",
        temporal_ui_url="https://temporal.test",
        n8n_ui_url="https://n8n.test",
        admin_username="admin",
        admin_password="admin",
        n8n_webhook_secret="test-secret",
    )
    assert s.knowledge_context_enabled is True
    assert s.knowledge_context_score_threshold == 0.3
    assert s.knowledge_context_max_results == 5
    assert s.knowledge_context_max_chars == 2000
    assert s.knowledge_context_timeout_seconds == 5.0


async def test_gather_returns_context_for_relevant_results():
    """Returns formatted text when search results exceed threshold."""
    kc = AsyncMock()
    kc.query_kg.return_value = []
    kc.search.return_value = [
        {
            "content_id": "1",
            "title": "AEGIS Architecture",
            "source_type": "article",
            "similarity": 0.85,
            "summary": "AEGIS uses Temporal for workflow orchestration.",
        },
        {
            "content_id": "2",
            "title": "Auth Design",
            "source_type": "email",
            "similarity": 0.65,
            "summary": "API keys rotate monthly via automated process.",
        },
    ]
    context, injected = await _gather_knowledge_context(kc, "how does aegis work?")
    assert context is not None
    assert "[article] AEGIS Architecture" in context
    assert "[email] Auth Design" in context
    assert "Temporal" in context
    assert "knowledge base may be relevant" in context
    assert len(injected) == 2
    assert injected[0]["content_hash"]
    assert injected[0]["keywords"]


async def test_gather_filters_below_threshold():
    """Results below score threshold are excluded."""
    kc = AsyncMock()
    kc.query_kg.return_value = []
    kc.search.return_value = [
        {"content_id": "1", "title": "Irrelevant", "source_type": "article", "similarity": 0.1},
        {"content_id": "2", "title": "Also Low", "source_type": "email", "similarity": 0.2},
    ]
    context, injected = await _gather_knowledge_context(kc, "hello", score_threshold=0.3)
    assert context is None
    assert injected == []


async def test_gather_returns_none_when_no_connector():
    """Returns None when knowledge_connector is None."""
    context, injected = await _gather_knowledge_context(None, "test query")
    assert context is None
    assert injected == []


async def test_gather_swallows_errors():
    """Returns None on connector failure, never raises."""
    kc = AsyncMock()
    kc.search.side_effect = RuntimeError("Knowledge service down")
    context, injected = await _gather_knowledge_context(kc, "test query")
    assert context is None
    assert injected == []


async def test_gather_respects_max_chars():
    """Output is capped at max_chars."""
    kc = AsyncMock()
    kc.query_kg.return_value = []
    kc.search.return_value = [
        {
            "content_id": str(i),
            "title": f"Article {i}",
            "source_type": "article",
            "similarity": 0.9,
            "summary": "x" * 200,
        }
        for i in range(10)
    ]
    context, injected = await _gather_knowledge_context(kc, "test", max_results=10, max_chars=500)
    assert context is not None
    # Lines section (between header and footer) should be under max_chars
    lines_section = context.split("\n\n")[0]  # header + lines
    assert len(lines_section) < 700  # max_chars + header overhead
    assert len(injected) > 0


async def test_gather_handles_timeout():
    """Returns None on search timeout."""
    kc = AsyncMock()
    kc.search.side_effect = TimeoutError("Search timed out")
    context, injected = await _gather_knowledge_context(kc, "test")
    assert context is None
    assert injected == []


@pytest.fixture
def settings():
    return Settings(
        litellm_api_key="k",
        api_key="k",
        database_url="postgresql://x:x@localhost/x",
        litellm_url="https://litellm.test/v1",
        temporal_ui_url="https://temporal.test",
        n8n_ui_url="https://n8n.test",
        admin_username="admin",
        admin_password="admin",
        n8n_webhook_secret="test-secret",
        model_balanced="test-model",
        tool_calling_enabled=True,
        tool_max_iterations=5,
        tool_result_max_bytes=4096,
        tool_timeout_seconds=30,
        knowledge_context_enabled=True,
    )


@pytest.fixture
def mock_llm():
    llm = AsyncMock()
    llm.chat = AsyncMock(
        return_value={
            "response": "Here is the answer.",
            "tool_calls": [],
            "model": "test-model",
            "prompt_tokens": 10,
            "completion_tokens": 5,
        }
    )
    return llm


@pytest.fixture
def mock_pool():
    pool = AsyncMock()
    pool.fetchrow.return_value = {
        "id": "sebas",
        "name": "Sebas",
        "system_prompt_path": "personalities/sebas/SOUL.md",
    }
    pool.fetch.return_value = []  # no history
    # Support `async with pool.acquire() as conn:` used by resolve_model_for_agent.
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=None)  # → falls back to 'balanced' tier
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=ctx)
    return pool


async def test_send_message_injects_knowledge_context(mock_pool, mock_llm, settings):
    """send_message appends knowledge context to system prompt."""
    kc = AsyncMock()
    kc.query_kg.return_value = []
    kc.search.return_value = [
        {
            "content_id": "1",
            "title": "Relevant Doc",
            "source_type": "article",
            "similarity": 0.8,
            "summary": "Important knowledge about the topic.",
        },
    ]
    await send_message(
        mock_pool,
        mock_llm,
        "sebas",
        "tell me about the topic",
        knowledge_connector=kc,
        settings=settings,
    )
    # Check the system prompt in the messages passed to LLM
    call_args = mock_llm.chat.call_args
    messages = call_args[1]["messages"]
    system_msg = messages[0]
    assert system_msg["role"] == "system"
    assert "## Relevant Knowledge" in system_msg["content"]
    assert "Relevant Doc" in system_msg["content"]
    assert "Important knowledge" in system_msg["content"]


async def test_send_message_skips_context_when_disabled(mock_pool, mock_llm, settings):
    """knowledge_context_enabled=False skips knowledge search entirely."""
    settings.knowledge_context_enabled = False
    kc = AsyncMock()
    await send_message(
        mock_pool,
        mock_llm,
        "sebas",
        "hello",
        knowledge_connector=kc,
        settings=settings,
    )
    # Knowledge connector should NOT be called for context
    kc.search.assert_not_called()
    # System prompt unchanged
    call_args = mock_llm.chat.call_args
    messages = call_args[1]["messages"]
    assert "## Relevant Knowledge" not in messages[0]["content"]


async def test_send_message_no_context_without_connector(mock_pool, mock_llm, settings):
    """send_message works fine without knowledge_connector."""
    await send_message(
        mock_pool,
        mock_llm,
        "sebas",
        "hello",
        knowledge_connector=None,
        settings=settings,
    )
    call_args = mock_llm.chat.call_args
    messages = call_args[1]["messages"]
    assert "## Relevant Knowledge" not in messages[0]["content"]


async def test_send_message_no_context_for_low_relevance(mock_pool, mock_llm, settings):
    """No injection when all search results are below threshold."""
    kc = AsyncMock()
    kc.query_kg.return_value = []
    kc.search.return_value = [
        {"content_id": "1", "title": "Meh", "source_type": "email", "similarity": 0.1},
    ]
    await send_message(
        mock_pool,
        mock_llm,
        "sebas",
        "hello",
        knowledge_connector=kc,
        settings=settings,
    )
    call_args = mock_llm.chat.call_args
    messages = call_args[1]["messages"]
    assert "## Relevant Knowledge" not in messages[0]["content"]
