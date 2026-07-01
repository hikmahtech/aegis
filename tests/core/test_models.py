"""Tests for AEGIS v2 domain models."""

from aegis.models import AlertPayload, AlertSeverity


def test_alert_payload_defaults():
    alert = AlertPayload(source="sentry", title="Test", severity=AlertSeverity.ERROR)
    assert alert.source == "sentry"
    assert alert.resolved is False
    assert alert.fingerprint == ""


def test_alert_severity_values():
    assert AlertSeverity.CRITICAL == "critical"
    assert AlertSeverity.ERROR == "error"
