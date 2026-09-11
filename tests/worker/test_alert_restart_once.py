"""The restart's evidence, and the question "was this problem restarted just
now?" (#501), against the real hub on the test database.

`remediate_infra_service` now keeps what `docker service ps` said after the
restart, which is where a scheduling or resource failure shows. The flow
records it on the problem. `recent_auto_restart` reads it back: the newest
automatic restart of THIS problem inside the window, what the service looks
like now, and which tasks are new since. The problem is the identity, not the
service name, with one guard: a group problem holds many services, and a
restart of one of them says nothing about another.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from aegis.connectors.homelab import _envelope
from aegis.services.hub import Event, ingest_event
from aegis_worker.activities import alerts as alerts_mod
from aegis_worker.activities.alerts import AlertActivities
from aegis_worker.activities.hub import HubActivities
from temporalio.testing import ActivityEnvironment

_SCHEDULING = "no suitable node (insufficient resources on 3 nodes)"


class FakeHomelab:
    """`HomelabConnector`'s three swarm calls the restart path makes, with
    the envelopes the real connector returns."""

    def __init__(self, tasks: list[dict] | None = None, *, converges: bool = True) -> None:
        self.tasks = tasks if tasks is not None else []
        self.converges = converges
        self.restarted: list[str] = []

    async def restart_service(self, service_name: str) -> dict:
        self.restarted.append(service_name)
        return _envelope(True, data={"output": service_name})

    async def list_services(self) -> dict:
        names = {t["service"] for t in self.tasks} or set(self.restarted)
        return _envelope(
            True,
            data=[
                {
                    "name": name,
                    "stack": name.split("_", 1)[0],
                    "replicas_actual": 1 if self.converges else 0,
                    "replicas_desired": 1,
                    "image": "img",
                    "mode": "replicated",
                    "id": "svc-id",
                }
                for name in sorted(names)
            ],
        )

    async def service_ps(self, service_name: str) -> dict:
        return _envelope(
            True,
            data=[
                {
                    "task_id": t["task_id"],
                    "current_state": t["current_state"],
                    "desired_state": t["desired_state"],
                    "error": t["error"],
                    "image": "img",
                    "node": t["node"],
                }
                for t in self.tasks
                if t["service"] == service_name
            ],
        )


def task(service: str, task_id: str, state: str, error: str = "", node: str = "noon") -> dict:
    return {
        "service": service,
        "task_id": task_id,
        "current_state": state,
        "desired_state": "Running" if state.startswith("Running") else "Shutdown",
        "error": error,
        "node": node,
    }


def _alert(service: str) -> dict:
    return {
        "title": f"Service {service} down",
        "source": "aegis-heartbeat",
        "service": service,
        "labels": {"alertname": "DockerServiceDown", "service_name": service},
    }


@pytest.fixture(autouse=True)
def _fast_polls(monkeypatch):
    monkeypatch.setattr(alerts_mod, "_REMEDIATE_POLL_INTERVAL_S", 0)
    monkeypatch.setattr(alerts_mod, "_REMEDIATE_POLLS", 2)


@pytest_asyncio.fixture(loop_scope="function")
async def window(db_pool):
    """Set the `alert_remediation` settings row for one test."""

    async def _set(value):
        await db_pool.execute(
            "INSERT INTO settings (key, value) VALUES ('alert_remediation', $1) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            value,
        )

    yield _set
    await db_pool.execute("DELETE FROM settings WHERE key = 'alert_remediation'")


async def _problem(pool, service: str) -> str:
    out = await ingest_event(
        pool,
        Event(
            source="heartbeat",
            external_id=f"{service}@{uuid.uuid4().hex[:8]}",
            kind="occurrence",
            title=f"Service {service} down",
            klass="DockerServiceDown",
            subject=service,
            subject_kind="service",
            severity="critical",
        ),
    )
    return out.problem_id


async def _record_restart(pool, problem_id: str, service: str, *, diagnostics=None, legacy=False):
    """What the flow records after an automatic restart. `legacy` is the
    shape it recorded before #501: the same step, no `remediation` payload."""
    payload = (
        {}
        if legacy
        else {
            "remediation": {
                "service": service,
                "command": f"docker service update --force {service}",
                "recovered": True,
                "reason": "recovered",
                "diagnostics": diagnostics or [],
            }
        }
    )
    await ActivityEnvironment().run(
        HubActivities(db_pool=pool).record_investigation,
        {
            "problem_id": problem_id,
            "status": "resolved",
            "text": f"Auto-remediated: docker service update --force {service} and it recovered.",
            "external_id": f"wf-{uuid.uuid4().hex[:8]}:auto_remediated",
            "posted": True,
            "payload": payload,
        },
    )


async def _backdate(pool, problem_id: str, minutes: int) -> None:
    await pool.execute(
        "UPDATE problem_events SET occurred_at = now() - make_interval(mins => $2) "
        "WHERE problem_id = $1::uuid AND kind = 'investigation'",
        problem_id,
        minutes,
    )


def _svc() -> str:
    return f"zzrestart-{uuid.uuid4().hex[:8]}_web"


# --- the restart keeps its evidence -------------------------------------------


async def test_the_restart_keeps_what_service_ps_said():
    svc = _svc()
    hl = FakeHomelab(
        [
            task(svc, "t2", "Running 5 seconds ago"),
            task(svc, "t1", "Rejected 1 minute ago", _SCHEDULING),
        ]
    )
    res = await ActivityEnvironment().run(
        AlertActivities(homelab_connector=hl).remediate_infra_service, _alert(svc)
    )
    assert res["recovered"] is True
    assert hl.restarted == [svc]
    assert res["diagnostics"] == [
        {"task_id": "t2", "node": "noon", "current_state": "Running 5 seconds ago",
         "desired_state": "Running", "error": ""},
        {"task_id": "t1", "node": "noon", "current_state": "Rejected 1 minute ago",
         "desired_state": "Shutdown", "error": _SCHEDULING},
    ]


async def test_evidence_that_cannot_be_read_does_not_fail_the_restart():
    class Broken(FakeHomelab):
        async def service_ps(self, service_name: str) -> dict:
            raise RuntimeError("docker socket gone")

    svc = _svc()
    res = await ActivityEnvironment().run(
        AlertActivities(homelab_connector=Broken([task(svc, "t1", "Running now")])).remediate_infra_service,
        _alert(svc),
    )
    assert res["recovered"] is True and res["diagnostics"] == []


# --- recent_auto_restart ------------------------------------------------------


async def test_a_problem_never_restarted_is_not_a_repeat(db_pool):
    svc = _svc()
    pid = await _problem(db_pool, svc)
    out = await ActivityEnvironment().run(
        AlertActivities(db_pool=db_pool, homelab_connector=FakeHomelab()).recent_auto_restart,
        pid,
        _alert(svc),
    )
    assert out == {"repeat": False, "window_minutes": 60}


async def test_a_restart_inside_the_window_is_a_repeat_with_then_now_and_what_changed(db_pool):
    svc = _svc()
    pid = await _problem(db_pool, svc)
    then = [
        {"task_id": "t2", "node": "noon", "current_state": "Running 5 seconds ago",
         "desired_state": "Running", "error": ""},
        {"task_id": "t1", "node": "noon", "current_state": "Rejected 1 minute ago",
         "desired_state": "Shutdown", "error": _SCHEDULING},
    ]
    await _record_restart(db_pool, pid, svc, diagnostics=then)
    await _backdate(db_pool, pid, 10)
    hl = FakeHomelab(
        [
            task(svc, "t3", "Pending 1 minute ago", "no suitable node (scheduling constraints)", node=""),
            task(svc, "t2", "Failed 2 minutes ago", "task: non-zero exit (137)"),
            task(svc, "t1", "Rejected 12 minutes ago", _SCHEDULING),
        ]
    )

    out = await ActivityEnvironment().run(
        AlertActivities(db_pool=db_pool, homelab_connector=hl).recent_auto_restart, pid, _alert(svc)
    )

    assert out["repeat"] is True
    assert out["window_minutes"] == 60
    assert out["service"] == svc
    assert out["command"] == f"docker service update --force {svc}"
    assert out["recovered"] is True
    assert 9.0 <= out["minutes_ago"] <= 11.0
    assert datetime.fromisoformat(out["restarted_at"]) < datetime.now(UTC)
    assert out["diagnostics_then"] == then
    assert [t["task_id"] for t in out["diagnostics_now"]] == ["t3", "t2", "t1"]
    # What changed: the task the scheduler could not place is new since.
    assert [t["task_id"] for t in out["new_tasks"]] == ["t3"]
    assert hl.restarted == [], "looking must never restart anything"


async def test_a_restart_before_the_window_is_not_a_repeat(db_pool):
    svc = _svc()
    pid = await _problem(db_pool, svc)
    await _record_restart(db_pool, pid, svc)
    await _backdate(db_pool, pid, 61)
    out = await ActivityEnvironment().run(
        AlertActivities(db_pool=db_pool, homelab_connector=FakeHomelab()).recent_auto_restart,
        pid,
        _alert(svc),
    )
    assert out["repeat"] is False


async def test_a_restart_recorded_before_501_still_counts(db_pool):
    """The flow's `auto_remediated` step, without the payload it carries now:
    a problem restarted just before the deploy is not restarted again just
    after it."""
    svc = _svc()
    pid = await _problem(db_pool, svc)
    await _record_restart(db_pool, pid, svc, legacy=True)
    out = await ActivityEnvironment().run(
        AlertActivities(db_pool=db_pool, homelab_connector=FakeHomelab()).recent_auto_restart,
        pid,
        _alert(svc),
    )
    assert out["repeat"] is True
    assert out["diagnostics_then"] == []
    assert out["recovered"] is True


async def test_another_service_on_the_same_problem_is_not_a_repeat(db_pool):
    """A group problem holds many services; restarting one says nothing
    about the next."""
    svc, other = _svc(), _svc()
    pid = await _problem(db_pool, svc)
    await _record_restart(db_pool, pid, other)
    out = await ActivityEnvironment().run(
        AlertActivities(db_pool=db_pool, homelab_connector=FakeHomelab()).recent_auto_restart,
        pid,
        _alert(svc),
    )
    assert out["repeat"] is False


async def test_the_window_is_a_setting(db_pool, window):
    svc = _svc()
    pid = await _problem(db_pool, svc)
    await _record_restart(db_pool, pid, svc)
    await _backdate(db_pool, pid, 10)
    act = AlertActivities(db_pool=db_pool, homelab_connector=FakeHomelab())

    await window({"repeat_window_minutes": 5})
    assert await ActivityEnvironment().run(act.recent_auto_restart, pid, _alert(svc)) == {
        "repeat": False,
        "window_minutes": 5,
    }

    # Off: restart every time, as before. Off means off even for a restart
    # stamped a moment "from now" by a worker on a node whose clock runs
    # ahead, which a zero-minute window alone would still count.
    await _backdate(db_pool, pid, -1)
    await window({"repeat_window_minutes": 0})
    assert await ActivityEnvironment().run(act.recent_auto_restart, pid, _alert(svc)) == {
        "repeat": False,
        "window_minutes": 0,
    }
    await _backdate(db_pool, pid, 10)

    await window({"repeat_window_minutes": "soon"})  # unreadable: the default
    out = await ActivityEnvironment().run(act.recent_auto_restart, pid, _alert(svc))
    assert out["repeat"] is True and out["window_minutes"] == 60


async def test_without_a_problem_or_a_pool_nothing_is_a_repeat(db_pool):
    svc = _svc()
    env = ActivityEnvironment()
    assert (await env.run(AlertActivities(db_pool=db_pool).recent_auto_restart, "", _alert(svc)))[
        "repeat"
    ] is False
    assert (await env.run(AlertActivities().recent_auto_restart, str(uuid.uuid4()), _alert(svc)))[
        "repeat"
    ] is False
