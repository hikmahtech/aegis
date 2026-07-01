"""Fire-and-forget helpers for recording to observability tables."""

import structlog

logger = structlog.get_logger()


async def record_llm_call(
    pool,
    *,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    latency_ms: int,
    purpose: str,
    agent_id: str | None = None,
    status: str = "success",
    error: str | None = None,
) -> None:
    """Record an LLM call to the llm_calls table. Never raises.

    `status` is "success" | "timeout" | "error". Failure rows let us
    measure the actual failure rate — success-only logging hides outages.
    """
    try:
        await pool.execute(
            "INSERT INTO llm_calls (model, input_tokens, output_tokens, "
            "latency_ms, purpose, agent_id, status, error) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
            model,
            prompt_tokens,
            completion_tokens,
            latency_ms,
            purpose,
            agent_id,
            status,
            error,
        )
    except Exception:
        logger.warning("record_llm_call_failed", model=model, purpose=purpose)


async def record_connector_call(
    pool,
    *,
    connector: str,
    action: str,
    status: str,
    latency_ms: int,
    error: str | None = None,
) -> None:
    """Record a connector call to the connector_calls table. Never raises."""
    try:
        await pool.execute(
            "INSERT INTO connector_calls (connector, action, status, latency_ms, error) "
            "VALUES ($1,$2,$3,$4,$5)",
            connector,
            action,
            status,
            latency_ms,
            error,
        )
    except Exception:
        logger.warning("record_connector_call_failed", connector=connector, action=action)


async def record_tool_call(
    pool,
    *,
    agent_id: str,
    thread_id: str | None,
    tool_name: str,
    tool_args: dict,
    tool_result: dict,
    status: str,
    latency_ms: int,
) -> None:
    """Record a chat tool execution to chat_tool_calls table. Never raises.

    `thread_id` is accepted by callers for symmetry with chat_history but the
    chat_tool_calls table itself doesn't store it — agent_id + created_at is
    enough to correlate.
    """
    try:
        await pool.execute(
            "INSERT INTO chat_tool_calls "
            "(agent_id, tool_name, args, result, status, latency_ms) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            agent_id,
            tool_name,
            tool_args,
            tool_result,
            status,
            latency_ms,
        )
    except Exception as exc:
        logger.warning("record_tool_call_failed", tool=tool_name, error=str(exc))


async def log_audit(
    pool,
    *,
    actor: str,
    action: str,
    target_type: str,
    target_id: str,
    details: dict | None = None,
) -> None:
    """Record an audit event to the audit_log table. Never raises."""
    try:
        await pool.execute(
            "INSERT INTO audit_log (actor, action, target_type, target_id, details) "
            "VALUES ($1,$2,$3,$4,$5)",
            actor,
            action,
            target_type,
            target_id,
            details or {},
        )
    except Exception:
        logger.warning("log_audit_failed", actor=actor, action=action)
