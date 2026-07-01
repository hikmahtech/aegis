"""Unit tests for aegis.telemetry.setup_telemetry().

The module is gated by OTEL_ENABLED env var. Default-off must be a true
no-op (no SDK init, no logging handler swap, no global state mutation).
"""

from __future__ import annotations

import importlib
import logging
import os
from unittest.mock import patch

import pytest


@pytest.fixture
def fresh_telemetry_module():
    """Reload telemetry to reset its module-level _INITIALISED flag.

    setup_telemetry caches initialisation in a module global. Each test
    needs a clean slate so prior tests can't leak state.
    """
    import aegis.telemetry as t

    importlib.reload(t)
    yield t
    importlib.reload(t)


def test_setup_telemetry_noop_when_disabled(fresh_telemetry_module, caplog):
    """OTEL_ENABLED unset → no SDK init, no handler changes."""
    root_logger = logging.getLogger()
    handlers_before = list(root_logger.handlers)

    env = {k: v for k, v in os.environ.items() if k != "OTEL_ENABLED"}
    with patch.dict(os.environ, env, clear=True):
        fresh_telemetry_module.setup_telemetry()

    assert fresh_telemetry_module._INITIALISED is False
    assert root_logger.handlers == handlers_before


def test_setup_telemetry_noop_when_explicitly_false(fresh_telemetry_module):
    """OTEL_ENABLED=false → still a no-op (only the literal "true" enables)."""
    with patch.dict(os.environ, {"OTEL_ENABLED": "false"}, clear=False):
        fresh_telemetry_module.setup_telemetry()

    assert fresh_telemetry_module._INITIALISED is False


def test_setup_telemetry_idempotent(fresh_telemetry_module):
    """Second call is a no-op when already initialised.

    We simulate "already initialised" by flipping the module flag, since
    actually calling the real exporter pulls in network IO. The contract
    we care about is: when _INITIALISED is True, the function returns
    early without touching anything.
    """
    fresh_telemetry_module._INITIALISED = True

    # Even with OTEL_ENABLED=true, the early return must fire.
    with patch.dict(os.environ, {"OTEL_ENABLED": "true"}, clear=False):
        fresh_telemetry_module.setup_telemetry()

    # Still True, untouched.
    assert fresh_telemetry_module._INITIALISED is True


def test_setup_telemetry_swallows_exceptions(fresh_telemetry_module):
    """If anything in the SDK init path raises, the host service stays up."""
    with (
        patch.dict(os.environ, {"OTEL_ENABLED": "true"}, clear=False),
        patch(
            "opentelemetry.sdk.trace.TracerProvider",
            side_effect=RuntimeError("boom"),
        ),
    ):
        # Must not raise.
        fresh_telemetry_module.setup_telemetry()

    # Init failed, so flag stays False — caller can retry later.
    assert fresh_telemetry_module._INITIALISED is False
