from unittest.mock import AsyncMock

import pytest
from aegis.observability import log_audit, record_connector_call, record_llm_call


@pytest.fixture
def mock_pool():
    pool = AsyncMock()
    pool.execute = AsyncMock()
    return pool


async def test_record_llm_call(mock_pool):
    await record_llm_call(
        mock_pool,
        model="gemma4:e2b",
        prompt_tokens=100,
        completion_tokens=50,
        latency_ms=200,
        purpose="triage",
        agent_id="sebas",
    )
    mock_pool.execute.assert_called_once()
    args = mock_pool.execute.call_args[0]
    assert "INSERT INTO llm_calls" in args[0]
    assert args[1] == "gemma4:e2b"
    assert args[5] == "triage"
    assert args[6] == "sebas"
    # Default status is "success", default error is None.
    assert args[7] == "success"
    assert args[8] is None


async def test_record_llm_call_with_failure_status(mock_pool):
    """Failure rows record status + error so we can chart failure rate."""
    await record_llm_call(
        mock_pool,
        model="qwen3:14b",
        prompt_tokens=0,
        completion_tokens=0,
        latency_ms=180000,
        purpose="gmail_classification",
        status="timeout",
        error="ReadTimeout: 180s",
    )
    args = mock_pool.execute.call_args[0]
    assert args[7] == "timeout"
    assert args[8] == "ReadTimeout: 180s"


async def test_record_llm_call_no_agent(mock_pool):
    await record_llm_call(
        mock_pool,
        model="gemma4:e2b",
        prompt_tokens=10,
        completion_tokens=5,
        latency_ms=50,
        purpose="chat",
    )
    args = mock_pool.execute.call_args[0]
    assert args[6] is None


async def test_record_llm_call_fire_and_forget(mock_pool):
    mock_pool.execute.side_effect = Exception("db down")
    await record_llm_call(
        mock_pool,
        model="x",
        prompt_tokens=0,
        completion_tokens=0,
        latency_ms=0,
        purpose="test",
    )


async def test_record_connector_call(mock_pool):
    await record_connector_call(
        mock_pool,
        connector="gmail",
        action="get_untriaged",
        status="ok",
        latency_ms=300,
    )
    args = mock_pool.execute.call_args[0]
    assert "INSERT INTO connector_calls" in args[0]
    assert args[1] == "gmail"
    assert args[2] == "get_untriaged"
    assert args[3] == "ok"


async def test_record_connector_call_with_error(mock_pool):
    await record_connector_call(
        mock_pool,
        connector="github",
        action="get_issues",
        status="error",
        latency_ms=1000,
        error="timeout",
    )
    args = mock_pool.execute.call_args[0]
    assert args[3] == "error"
    assert args[5] == "timeout"


async def test_record_connector_call_fire_and_forget(mock_pool):
    mock_pool.execute.side_effect = Exception("db down")
    await record_connector_call(
        mock_pool,
        connector="x",
        action="y",
        status="ok",
        latency_ms=0,
    )


async def test_log_audit(mock_pool):
    await log_audit(
        mock_pool,
        actor="api:work",
        action="project_created",
        target_type="project",
        target_id="proj-1",
        details={"name": "My Project"},
    )
    args = mock_pool.execute.call_args[0]
    assert "INSERT INTO audit_log" in args[0]
    assert args[1] == "api:work"
    assert args[2] == "project_created"
    assert args[5] == {"name": "My Project"}


async def test_log_audit_no_details(mock_pool):
    await log_audit(
        mock_pool,
        actor="api:settings",
        action="setting_updated",
        target_type="setting",
        target_id="key1",
    )
    args = mock_pool.execute.call_args[0]
    assert args[5] == {}


async def test_log_audit_fire_and_forget(mock_pool):
    mock_pool.execute.side_effect = Exception("db down")
    await log_audit(
        mock_pool,
        actor="x",
        action="y",
        target_type="z",
        target_id="1",
    )
