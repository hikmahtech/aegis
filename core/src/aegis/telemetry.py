"""OpenTelemetry SDK setup. Call setup_telemetry() once at process startup.

When OTEL_ENABLED env var is not "true", this is a no-op (zero overhead).
Safe to call multiple times. Errors are logged and swallowed so telemetry
can never crash the host service.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_INITIALISED = False


def setup_telemetry(service_name: str | None = None) -> None:
    """Initialise OTel SDK, auto-instrumentation, and JSON logging.

    No-op unless OTEL_ENABLED env var is "true". Safe to call multiple times.
    """
    global _INITIALISED
    if _INITIALISED:
        return
    if os.environ.get("OTEL_ENABLED", "").lower() != "true":
        return

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create(
            {
                "service.name": service_name or os.environ.get("OTEL_SERVICE_NAME", "unknown"),
                "service.version": _read_version(),
            }
        )

        provider = TracerProvider(resource=resource)
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)

        for instr_module, instr_class in _instrumentors():
            try:
                mod = __import__(instr_module, fromlist=[instr_class])
                getattr(mod, instr_class)().instrument()
            except ModuleNotFoundError as e:
                logger.debug("OTel instrumentor %s skipped (target lib not installed): %s", instr_class, e)
            except Exception as e:
                logger.warning("OTel instrumentor %s failed: %s", instr_class, e)

        _setup_json_logging()
        _INITIALISED = True
        logger.info(
            "OTel telemetry initialised: service=%s",
            resource.attributes.get("service.name"),
        )

    except Exception as e:
        logger.warning("setup_telemetry() failed, continuing without telemetry: %s", e)


def _instrumentors():
    """Return list of (module_path, class_name) — each is independent."""
    base = [
        ("opentelemetry.instrumentation.asyncpg", "AsyncPGInstrumentor"),
        ("opentelemetry.instrumentation.redis", "RedisInstrumentor"),
        ("opentelemetry.instrumentation.httpx", "HTTPXClientInstrumentor"),
        ("opentelemetry.instrumentation.requests", "RequestsInstrumentor"),
        # FastAPI is present in core (and comms); absent in worker. Import is
        # attempted; failure is logged-and-swallowed by the caller above.
        ("opentelemetry.instrumentation.fastapi", "FastAPIInstrumentor"),
    ]
    return base


def _read_version() -> str:
    try:
        from importlib.metadata import version

        pkg = os.environ.get("OTEL_SERVICE_NAME", "").replace("-", "_") or "aegis"
        return version(pkg)
    except Exception:
        return "unknown"


def _setup_json_logging() -> None:
    """Replace root logger handler with JSON formatter."""
    try:
        from .logging_config import JsonFormatter
    except ImportError:
        return  # logging_config absent — leave logging alone
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
