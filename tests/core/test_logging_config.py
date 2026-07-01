"""Unit tests for aegis.logging_config.JsonFormatter.

The formatter must:
- emit valid JSON with ts/level/logger/msg always present;
- inject trace_id + span_id when an active span context exists
  (Loki's derivedFields regex pivots on `"trace_id":"<hex>"`);
- omit trace_id when no span is active (default state).
"""

from __future__ import annotations

import json
import logging

from aegis.logging_config import JsonFormatter
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider


def _make_record(msg: str = "hello") -> logging.LogRecord:
    return logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg=msg,
        args=(),
        exc_info=None,
    )


def test_format_emits_valid_json_baseline():
    """Default fields ts/level/logger/msg always present."""
    out = JsonFormatter().format(_make_record("hello"))
    payload = json.loads(out)
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test.logger"
    assert payload["msg"] == "hello"
    assert "ts" in payload


def test_format_omits_trace_id_when_no_active_span():
    """No span context → no trace_id / span_id keys (NoOp span path)."""
    out = JsonFormatter().format(_make_record())
    payload = json.loads(out)
    assert "trace_id" not in payload
    assert "span_id" not in payload


def test_format_includes_trace_and_span_ids_when_span_active():
    """Active span → trace_id (32 hex) + span_id (16 hex) injected."""
    # Install a real TracerProvider so spans have valid contexts.
    # We cannot rely on global state across tests, so we set a fresh one
    # for this test only.
    provider = TracerProvider()
    trace.set_tracer_provider(provider)
    tracer = trace.get_tracer(__name__)

    with tracer.start_as_current_span("test-span") as span:
        ctx = span.get_span_context()
        out = JsonFormatter().format(_make_record())

    payload = json.loads(out)
    assert "trace_id" in payload
    assert "span_id" in payload
    assert payload["trace_id"] == format(ctx.trace_id, "032x")
    assert payload["span_id"] == format(ctx.span_id, "016x")
    # 32 hex chars = 128-bit trace id; 16 hex chars = 64-bit span id.
    assert len(payload["trace_id"]) == 32
    assert len(payload["span_id"]) == 16


def test_format_includes_exception_text_when_present():
    """exc_info on the record → exception field in payload."""
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.LogRecord(
            name="t",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="failed",
            args=(),
            exc_info=sys.exc_info(),
        )

    out = JsonFormatter().format(record)
    payload = json.loads(out)
    assert "exception" in payload
    assert "ValueError" in payload["exception"]


def test_format_passes_extra_fields_through():
    """Custom keys on the LogRecord (logger.info(..., extra={...})) survive."""
    record = _make_record()
    record.custom_field = "value-x"  # type: ignore[attr-defined]
    record.numeric = 42  # type: ignore[attr-defined]
    out = JsonFormatter().format(record)
    payload = json.loads(out)
    assert payload["custom_field"] == "value-x"
    assert payload["numeric"] == 42


def test_format_repr_fallback_for_unjsonable_value():
    """A non-JSON value on the record gets stringified instead of crashing."""

    class NotJsonable:
        def __repr__(self) -> str:
            return "<NotJsonable instance>"

    record = _make_record()
    record.weird = NotJsonable()  # type: ignore[attr-defined]
    out = JsonFormatter().format(record)
    payload = json.loads(out)
    assert payload["weird"] == "<NotJsonable instance>"
