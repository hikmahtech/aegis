"""`correlation_key` and severity normalisation.

Every pair the spec's Problem section calls a duplicate must produce one key
here, and every pair it calls distinct must not. The alert-dict translation
(`event_from_alert`) and its alertmanager / heartbeat shapes left with the
infra lane (DevOps vertical, a2-devops).
"""

from __future__ import annotations

from datetime import UTC, datetime

from aegis.services.hub import Event, correlation_key, normalize_severity

T0 = datetime(2026, 9, 7, 8, 40, tzinfo=UTC)

def test_repeated_comms_probe_failures_share_a_key():
    e = Event(
        source="delivery",
        external_id="x",
        kind="occurrence",
        title="AEGIS inbound comms is DOWN",
        klass="comms_inbound_down",
        subject="polling",
        subject_kind="comms",
    )
    assert correlation_key(e) == "comms_inbound_down:comms:polling"
    assert correlation_key(Event(**{**e.__dict__, "external_id": "y"})) == correlation_key(e)


def test_key_is_case_and_punctuation_insensitive():
    a = Event(source="chat", external_id="1", kind="occurrence", title="t", klass="Node Down", subject="WOW")
    b = Event(source="chat", external_id="2", kind="occurrence", title="t", klass="node-down", subject="wow")
    assert correlation_key(a) == correlation_key(b) == "node-down:service:wow"


# --- pairs that must stay distinct --------------------------------------------


def test_failing_and_stale_are_different_classes():
    failing = Event(source="flow_health", external_id="1", kind="occurrence", title="t", klass="flow_failing", subject="GmailIngestFlow", subject_kind="flow")
    stale = Event(source="flow_health", external_id="2", kind="occurrence", title="t", klass="flow_stale", subject="GmailIngestFlow", subject_kind="flow")
    assert correlation_key(failing) == "flow_failing:flow:gmailingestflow"
    assert correlation_key(stale) == "flow_stale:flow:gmailingestflow"


def test_no_class_and_no_subject_is_uncorrelated():
    e = Event(source="chat", external_id="1", kind="occurrence", title="something is off")
    assert correlation_key(e) == ""


def test_subject_without_class_keys_under_manual():
    e = Event(source="chat", external_id="1", kind="occurrence", title="t", subject="koyracloud_redis")
    assert correlation_key(e) == "manual:service:koyracloud_redis"


def test_class_without_subject_keys_with_empty_subject():
    e = Event(source="flow_health", external_id="1", kind="occurrence", title="t", klass="HeartbeatCollectFailed")
    assert correlation_key(e) == "heartbeatcollectfailed::"


def test_severity_normalisation():
    assert normalize_severity("CRITICAL") == "critical"
    assert normalize_severity("warn") == "warning"
    assert normalize_severity("fatal") == "critical"
    assert normalize_severity("notice") == "info"
    assert normalize_severity("") == "warning"
    assert normalize_severity("bogus") == "warning"


def test_two_stuck_services_do_not_collapse():
    def _down(service: str) -> Event:
        return Event(
            source="flow_health", external_id=service, kind="occurrence", title="t",
            klass="DockerServiceDown", subject=service,
        )

    assert correlation_key(_down("monitoring_cadvisor")) != correlation_key(_down("chatapp_app"))


def test_segments_are_capped():
    e = Event(source="chat", external_id="1", kind="occurrence", title="t", klass="x" * 500, subject="y" * 500)
    key = correlation_key(e)
    assert key == "x" * 80 + ":service:" + "y" * 80
