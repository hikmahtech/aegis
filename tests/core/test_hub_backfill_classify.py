"""`scripts/hub_backfill.py::classify` — the key a legacy alert task migrates to.

The script is one-time and lives outside the packages, so it is loaded by path.
It is tested at all because getting a key wrong here is not a cosmetic
mistake: the key decides which problem a task becomes part of, and a shared
fallback key silently folds unrelated alerts into one problem. That happened in
production on 2026-09-08, which is why the last case below exists.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from aegis.services.hub import Event, correlation_key

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "hub_backfill.py"
_spec = importlib.util.spec_from_file_location("hub_backfill", _PATH)
backfill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backfill)


def _key(title: str, fingerprint: str = "", service: str = "") -> str:
    klass, subject, kind = backfill.classify(title, fingerprint, service)
    return correlation_key(
        Event(
            source="manual",
            external_id="x",
            kind="occurrence",
            title=title,
            klass=klass,
            subject=subject,
            subject_kind=kind,
        )
    )


def test_a_heartbeat_fingerprint_keys_the_way_the_producer_will():
    """The whole point of the backfill: the next occurrence of this alert must
    attach to the task, so the key has to be the one `event_from_alert`
    computes for the same alert."""
    assert _key("Swarm node baa down", "aegis-heartbeat:NodeDown:baa") == "nodedown:node:baa"
    assert (
        _key("x", "aegis-heartbeat:DockerServiceDown:monitoring_cadvisor")
        == "dockerservicedown:service:monitoring_cadvisor"
    )
    # Prolonged and down are one problem per service, so they share a key.
    assert (
        _key("x", "aegis-heartbeat:ServiceDownProlonged:redis_redis")
        == "dockerservicedown:service:redis_redis"
    )


def test_a_title_that_names_a_node_or_service_is_read():
    assert _key("Swarm node lam is Down") == "nodedown:node:lam"
    assert (
        _key("redis_redis has fewer tasks than desired", service="redis_redis")
        == "dockerservicedown:service:redis_redis"
    )
    # A service named but nothing that reads as an outage: still keyed on the
    # service, so two hand-captured reports about it meet.
    assert _key("look at redis_redis sometime", service="redis_redis") == "manual:service:redis_redis"


def test_an_unreadable_task_gets_an_empty_key_and_therefore_its_own_problem():
    """Two of these used to collide on `manual::` — a key no producer computes
    and every unreadable task shares — so a run merged unrelated alerts into
    one problem and completed the other's task.

    Falsifiable: return `("manual", "", "")` again and both keys are equal and
    non-empty.
    """
    a = _key("Overlay monitoring on pop-think-os cannot reach 1 endpoint(s)")
    b = _key("Overlay monitoring on daal cannot reach 1 endpoint(s)")
    assert a == "" and b == ""
    assert backfill.classify("something nobody can parse", "", "") == ("", "", "")


@pytest.mark.parametrize(
    "title",
    ["", "   ", "Overlay monitoring on daal cannot reach 1 endpoint(s)", "a note to self"],
)
def test_an_empty_key_is_never_shared(title: str):
    assert _key(title) == "", "an empty key creates; it must never attach"
