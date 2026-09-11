"""`correlation_key` and `event_from_alert` against the alert shapes the
producers build today.

Every pair the spec's Problem section calls a duplicate must produce one key
here, and every pair it calls distinct must not. These payloads are copied
from the real builders: `routes/webhooks.py::alert_webhook`,
`flows/infra_heartbeat.py::build_heartbeat_alert`,
`activities/sentry_ingest.py::issue_to_alert`.
"""

from __future__ import annotations

from datetime import UTC, datetime

from aegis.services.hub import Event, correlation_key, event_from_alert, normalize_severity

T0 = datetime(2026, 9, 7, 8, 40, tzinfo=UTC)


def _alertmanager(alertname: str, **labels: str) -> dict:
    labels = {"alertname": alertname, "severity": "critical", **labels}
    return {
        "source": "alertmanager",
        "title": f"{alertname} on {labels.get('service_name') or labels.get('instance', '')}",
        "fingerprint": "a1b2c3",
        "severity": "critical",
        "service": labels.get("instance") or labels.get("job", ""),
        "description": "",
        "labels": labels,
        "raw_payload": {"labels": labels, "startsAt": "2026-09-07T08:40:00Z"},
    }


def _heartbeat(alertname: str, subject: str, service_name: str = "") -> dict:
    labels = {"alertname": alertname, "cluster": "swarm-baa"}
    if service_name:
        labels["service_name"] = service_name
    return {
        "title": f"{alertname}: {subject}",
        "description": "…",
        "source": "aegis-heartbeat",
        "severity": "critical",
        "fingerprint": f"aegis-heartbeat:{alertname}:{subject}",
        "labels": labels,
        "escalate": True,
    }


def _sentry(issue_id: str, error_type: str, slug: str = "koyracloud-api") -> dict:
    return {
        "source": "sentry",
        "title": f"{error_type}: boom",
        "fingerprint": f"sentry:{issue_id}",
        "severity": "error",
        "service": slug,
        "description": "",
        "labels": {"environment": "prod", "platform": "python"},
        "raw_payload": {"id": issue_id, "metadata": {"type": error_type}},
    }


def _key(alert: dict) -> str:
    return correlation_key(event_from_alert(alert, occurred_at=T0))


# --- pairs that must collapse -------------------------------------------------


def test_alertmanager_and_heartbeat_service_down_share_a_key():
    am = _alertmanager("DockerServiceDown", service_name="monitoring_cadvisor", instance="baa")
    hb = _heartbeat("DockerServiceDown", "monitoring_cadvisor", service_name="monitoring_cadvisor")
    assert _key(am) == _key(hb) == "dockerservicedown:service:monitoring_cadvisor"


def test_alertmanager_and_heartbeat_node_down_share_a_key():
    am = _alertmanager("NodeDown", node="wow", instance="10.20.0.30:9100")
    hb = _heartbeat("NodeDown", "wow")
    assert _key(am) == _key(hb) == "nodedown:node:wow"


def test_sentry_webhook_and_poll_share_a_key():
    # Both paths build the same alert dict; the class is the error type, so
    # stack-frame variations (fresh issue ids) still meet.
    a = _sentry("4711", "IncompatiblePeer")
    b = _sentry("4712", "IncompatiblePeer")
    assert _key(a) == _key(b) == "incompatiblepeer:service:koyracloud-api"


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


def test_two_stuck_services_do_not_collapse():
    a = _heartbeat("DockerServiceDown", "monitoring_cadvisor", service_name="monitoring_cadvisor")
    b = _heartbeat("DockerServiceDown", "chatapp_app", service_name="chatapp_app")
    assert _key(a) != _key(b)


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
    e = Event(source="heartbeat", external_id="1", kind="occurrence", title="t", klass="HeartbeatCollectFailed")
    assert correlation_key(e) == "heartbeatcollectfailed::"


def test_segments_are_capped():
    e = Event(source="chat", external_id="1", kind="occurrence", title="t", klass="x" * 500, subject="y" * 500)
    key = correlation_key(e)
    assert key == "x" * 80 + ":service:" + "y" * 80


# --- event_from_alert details -------------------------------------------------


def test_occurrence_id_carries_the_start_time_not_just_the_fingerprint():
    a = _alertmanager("DockerServiceDown", service_name="s")
    b = {**a, "raw_payload": {**a["raw_payload"], "startsAt": "2026-09-08T08:40:00Z"}}
    ea, eb = event_from_alert(a, occurred_at=T0), event_from_alert(b, occurred_at=T0)
    assert ea.external_id == "a1b2c3@2026-09-07T08:40:00Z"
    assert eb.external_id == "a1b2c3@2026-09-08T08:40:00Z"
    assert ea.kind == "occurrence" and ea.source == "alertmanager"


def test_resolved_alert_becomes_a_resolved_event_with_its_own_id():
    a = _alertmanager("DockerServiceDown", service_name="s")
    a["raw_payload"]["endsAt"] = "2026-09-07T09:00:00Z"
    e = event_from_alert(a, occurred_at=T0, resolved=True)
    assert e.kind == "resolved"
    assert e.external_id == "a1b2c3@2026-09-07T09:00:00Z@resolved"
    assert correlation_key(e) == "dockerservicedown:service:s"


def test_heartbeat_source_is_normalised_and_subject_read_from_fingerprint():
    e = event_from_alert(_heartbeat("NodeDown", "noon"), occurred_at=T0)
    assert e.source == "heartbeat"
    assert (e.klass, e.subject, e.subject_kind) == ("NodeDown", "noon", "node")
    assert e.external_id == f"aegis-heartbeat:NodeDown:noon@{T0.isoformat()}"


def test_sentry_without_error_type_falls_back_to_issue_id():
    a = _sentry("4711", "")
    e = event_from_alert(a, occurred_at=T0)
    assert e.klass == "sentry-4711"
    assert e.subject == "koyracloud-api"


def test_alert_with_no_fingerprint_gets_a_title_slug_id():
    a = {"source": "chat", "title": "Koyracloud redis seems down", "labels": {}}
    e = event_from_alert(a, occurred_at=T0)
    assert e.external_id.startswith("koyracloud-redis-seems-down@")
    assert correlation_key(e) == ""


# --- a synthetic alert raised about one Todoist task (#472) --------------------


def _route_alert(task_id: str, **over) -> dict:
    """What clarify's `_pandora_alert_payload` builds for a task matching the
    prod `infra-incident` content route: the class comes from the route's
    `alert_overrides`, there is no service, and the alert names its task."""
    alert = {
        "title": "Swarm node wow down",
        "description": "",
        "source": "todoist-infra",
        "severity": "normal",
        "fingerprint": f"route-{task_id}",
        "labels": {"alertname": "NodeDown"},
        "requires_approval": False,
        "todoist_task_id": task_id,
    }
    alert.update(over)
    return alert


def test_a_subject_less_alert_about_a_task_is_keyed_on_that_task():
    e = event_from_alert(_route_alert("6hVCprQ748qX4CHv"), occurred_at=T0)
    assert (e.subject, e.subject_kind) == ("6hVCprQ748qX4CHv", "task")
    assert correlation_key(e) == "nodedown:task:6hvcprq748qx4chv"


def test_two_tasks_about_different_incidents_do_not_share_a_key():
    """The prod collision: both of these were keyed `nodedown::`, so the wow
    node-down attached to the aegis_core problem and nobody saw it."""
    core = _key(_route_alert("6hRh2fhJQjpFFgCv", title="Service aegis_core down"))
    wow = _key(_route_alert("6hVCprQ748qX4CHv"))
    assert core != wow
    assert "nodedown::" not in (core, wow)


def test_a_task_never_replaces_a_real_subject():
    # A route that names a service, and `investigate_resource`, which names a
    # repo, keep keying on it: the task is only the subject of last resort.
    alert = _route_alert(
        "6hRh2fhJQjpFFgCv", service="acme", labels={"alertname": "NodeDown", "service": "acme"}
    )
    e = event_from_alert(alert, occurred_at=T0)
    assert (e.subject, e.subject_kind) == ("acme", "service")


def test_a_subject_less_alert_with_no_task_keeps_one_key_per_class():
    """A class with genuinely no subject — an aggregate alertmanager rule, a
    Sentry issue with no project — must keep sharing one key. Giving it the
    empty key would open a new problem, and a new task, on every firing."""
    assert _key(_alertmanager("WatchdogAggregate")) == "watchdogaggregate::"
    assert _key(_sentry("4711", "IncompatiblePeer", slug="")) == "incompatiblepeer::"


def test_alert_payload_is_bounded():
    a = _alertmanager("X", service_name="s")
    a["description"] = "d" * 5000
    e = event_from_alert(a, occurred_at=T0)
    assert len(e.payload["description"]) == 2000
    assert e.payload["fingerprint"] == "a1b2c3"


def test_severity_normalisation():
    assert normalize_severity("CRITICAL") == "critical"
    assert normalize_severity("warn") == "warning"
    assert normalize_severity("fatal") == "critical"
    assert normalize_severity("notice") == "info"
    assert normalize_severity("") == "warning"
    assert normalize_severity("bogus") == "warning"
