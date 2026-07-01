"""JSON log formatter that injects OTel trace context.

Loki's Grafana datasource has a derivedFields regex on `"trace_id":"<hex>"`
that pivots logs → Tempo. This formatter emits exactly that shape when an
active span context is present.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from opentelemetry import trace

_STD_LOG_ATTRS = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "message",
    "taskName",
}


def _safe(v):
    try:
        json.dumps(v)
        return v
    except (TypeError, ValueError):
        return repr(v)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        span = trace.get_current_span()
        ctx = span.get_span_context() if span is not None else None
        if ctx is not None and ctx.is_valid:
            payload["trace_id"] = format(ctx.trace_id, "032x")
            payload["span_id"] = format(ctx.span_id, "016x")

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        for key, value in record.__dict__.items():
            if key not in _STD_LOG_ATTRS and not key.startswith("_"):
                payload[key] = _safe(value)

        return json.dumps(payload, default=str)
