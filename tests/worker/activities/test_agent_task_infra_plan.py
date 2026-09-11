"""`plan_infra_task` (#344): what the infra verb does, by the problem behind a task.

Before, the verb understood one thing — a swarm service — and everything else
was parked with an apology, or worse: a Dagster pipeline failure ran
`docker service ps dagster` and was offered a restart card for a service that
does not exist. In the 14 days before the fix, 85 of 110 `@pandora` runs were
parked with nothing done. Every handler here is read-only.

Real database and the real hub throughout: problems are made by
`hub.ingest_event`, groups by `hub_group.upgrade`, and the task is linked the
way the projector links it. The swarm is the real `InfraOpsActivities` over a
fake `HomelabConnector` that answers with the connector's own envelope.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx
import pytest_asyncio
import respx
from aegis.services import hub, hub_group, hub_project
from aegis_worker.activities.agent_task import AgentTaskActivities
from aegis_worker.activities.alerts import AlertActivities
from aegis_worker.activities.homelab import HomelabActivities
from aegis_worker.activities.infra_ops import InfraOpsActivities

_HEARTBEAT_KEY = HomelabActivities._HEARTBEAT_STATE_KEY


class _Homelab:
    """`HomelabConnector`'s surface, returning its standard envelope. Every
    call is recorded, so a test can prove a handler ran no Docker at all."""

    def __init__(self, services: list[dict] | None = None):
        self.services = services or []
        self.calls: list[str] = []

    async def list_services(self) -> dict:
        self.calls.append("list_services")
        return {"ok": True, "data": self.services, "error": None, "retryable": False,
                "external_ref": None}

    async def service_ps(self, service_name: str) -> dict:
        self.calls.append(f"service_ps:{service_name}")
        return {"ok": True, "data": [], "error": None, "retryable": False, "external_ref": None}

    async def list_nodes(self) -> dict:
        self.calls.append("list_nodes")
        return {"ok": True, "data": [], "error": None, "retryable": False, "external_ref": None}

    async def restart_service(self, service_name: str) -> dict:
        self.calls.append(f"restart_service:{service_name}")
        return {"ok": True, "data": {"output": ""}, "error": None, "retryable": False,
                "external_ref": None}


def _svc(name: str, actual: int, desired: int) -> dict:
    return {"name": name, "stack": name.split("_", 1)[0], "replicas_actual": actual,
            "replicas_desired": desired, "image": "img", "mode": "replicated", "id": "x"}


def _act(db_pool, homelab: _Homelab, alert_act: AlertActivities | None = None):
    return AgentTaskActivities(
        db_pool=db_pool,
        infra_ops=InfraOpsActivities(homelab_connector=homelab),
        homelab_connector=homelab,
        alert_act=alert_act,
    )


@pytest_asyncio.fixture(loop_scope="function")
async def made(db_pool):
    """Problems and tasks this test made; everything is removed afterwards."""
    state = {"problems": [], "tasks": []}
    yield state
    for pid in state["problems"]:
        await db_pool.execute("DELETE FROM problem_events WHERE problem_id = $1::uuid", pid)
        await db_pool.execute("DELETE FROM problem_links WHERE problem_id = $1::uuid", pid)
        await db_pool.execute("DELETE FROM problems WHERE id = $1::uuid", pid)
    for tid in state["tasks"]:
        await db_pool.execute("DELETE FROM todoist_tasks WHERE id = $1", tid)


async def _problem(db_pool, made, *, source: str, klass: str, subject: str, kind: str,
                   title: str = "t", payload: dict | None = None) -> str:
    result = await hub.ingest_event(
        db_pool,
        hub.Event(
            source=source,
            external_id=f"test-{uuid.uuid4().hex}",
            kind="occurrence",
            title=title,
            subject=subject,
            subject_kind=kind,
            klass=klass,
            payload=payload or {},
            occurred_at=datetime.now(UTC),
        ),
    )
    made["problems"].append(result.problem_id)
    return result.problem_id


async def _task(db_pool, made, pid: str | None, title: str = "an alert") -> str:
    tid = f"ip-{uuid.uuid4().hex[:10]}"
    await db_pool.execute(
        "INSERT INTO todoist_tasks (id, content, labels, source_tag, assignee_label, is_completed) "
        "VALUES ($1, $2, ARRAY['#alert','@pandora'], '#alert', '@pandora', false)",
        tid,
        title,
    )
    made["tasks"].append(tid)
    if pid:
        await hub_project.link_task(db_pool, pid, tid)
    return tid


async def _verdict(db_pool, pid: str, text: str) -> None:
    """The investigation's closing event, shaped as AlertInvestigationFlow's
    `record_investigation(step="final")` writes it."""
    await hub.ingest_event(
        db_pool,
        hub.Event(
            source="investigation",
            external_id=f"investigate-{pid}-1:final:{uuid.uuid4().hex[:6]}",
            kind="investigation",
            title=text[:200],
            payload={"text": text, "status": "waiting_human", "posted": True,
                     "verdict": "logged", "resource": "org/pipeline"},
            occurred_at=datetime.now(UTC),
            problem_id=pid,
        ),
    )


# --- a real swarm service: unchanged, the flow checks it --------------------------


async def test_a_service_the_swarm_runs_goes_to_the_service_check(db_pool, made):
    name = f"svc{uuid.uuid4().hex[:6]}_api"
    homelab = _Homelab([_svc(name, 0, 1)])
    pid = await _problem(db_pool, made, source="heartbeat", klass="dockerservicedown",
                         subject=name, kind="service")
    tid = await _task(db_pool, made, pid)

    plan = await _act(db_pool, homelab).plan_infra_task(tid, "Service down")

    assert plan["action"] == "service"
    assert plan["service"] == name
    assert plan["health"]["found"] is True and plan["health"]["healthy"] is False


# --- a subject the swarm does not run: no docker ps, no restart card --------------


async def test_a_subject_the_swarm_does_not_run_reports_the_investigation(db_pool, made):
    """The Dagster case from prod: subject `dagster`, kind `service`, and no
    such swarm service. Before, the verb ran `docker service ps` and offered to
    restart it."""
    homelab = _Homelab([_svc("other_thing", 1, 1)])
    subject = f"pipeline{uuid.uuid4().hex[:6]}"
    pid = await _problem(
        db_pool, made, source="alertmanager", klass="dagster-pipeline-failure",
        subject=subject, kind="service",
        payload={"labels": {"alertname": "Dagster Pipeline Failure"},
                 "description": "Asset/Job: nightly_prices\nRun ID: r-1"},
    )
    await _verdict(db_pool, pid, "logged: an asset check ran before its upstream data loaded")
    tid = await _task(db_pool, made, pid)

    plan = await _act(db_pool, homelab).plan_infra_task(tid, "Dagster Failed: nightly_prices")

    assert plan["action"] == "report"
    assert plan["handler"] == "no_swarm_service"
    assert "an asset check ran before its upstream data loaded" in plan["comment"]
    assert "nightly_prices" in plan["comment"]  # what the alert said
    assert "restart" in plan["comment"]  # ...and why none is offered
    assert not any(c.startswith(("service_ps", "restart_service")) for c in homelab.calls)


# --- a node: the heartbeat's own record, never Docker on the node --------------------


@pytest_asyncio.fixture(loop_scope="function")
async def heartbeat_row(db_pool):
    saved = await db_pool.fetchval("SELECT value FROM settings WHERE key = $1", _HEARTBEAT_KEY)
    yield
    if saved is None:
        await db_pool.execute("DELETE FROM settings WHERE key = $1", _HEARTBEAT_KEY)
    else:
        await db_pool.execute(
            "UPDATE settings SET value = $2 WHERE key = $1", _HEARTBEAT_KEY, saved
        )


async def _heartbeat(db_pool, nodes: dict, fail_count: int = 0) -> None:
    await db_pool.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ($1, $2, now()) "
        "ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = now()",
        _HEARTBEAT_KEY,
        {"nodes": nodes, "stuck": [], "confirmed": [], "fail_count": fail_count},
    )


async def test_a_down_node_reports_the_heartbeat_and_runs_no_docker(
    db_pool, made, heartbeat_row, tmp_path
):
    node = f"node-{uuid.uuid4().hex[:6]}"
    (tmp_path / "NodeDown.md").write_text("# NodeDown\n\nCheck the power before Docker.\n")
    await _heartbeat(db_pool, {node: "Down", "other": "Ready"})
    pid = await _problem(db_pool, made, source="heartbeat", klass="NodeDown", subject=node,
                         kind="node", payload={"labels": {"alertname": "NodeDown"}})
    tid = await _task(db_pool, made, pid)
    homelab = _Homelab()

    plan = await _act(db_pool, homelab, AlertActivities(runbooks_dir=str(tmp_path))).plan_infra_task(
        tid, f"Swarm node {node} down"
    )

    assert plan["action"] == "report" and plan["handler"] == "node"
    assert node in plan["comment"] and "Down" in plan["comment"]
    assert "Check the power before Docker." in plan["comment"]  # the runbook
    assert homelab.calls == []  # not one Docker command


async def test_a_node_the_heartbeat_sees_ready_again_says_so(db_pool, made, heartbeat_row):
    node = f"node-{uuid.uuid4().hex[:6]}"
    await _heartbeat(db_pool, {node: "Ready"})
    pid = await _problem(db_pool, made, source="heartbeat", klass="NodeDown", subject=node,
                         kind="node")
    tid = await _task(db_pool, made, pid)

    plan = await _act(db_pool, _Homelab()).plan_infra_task(tid, "x")

    assert plan["handler"] == "node"
    assert "Ready" in plan["comment"]
    # Back up: nothing for a person to do, and no trip to the machine.
    assert "What to do: nothing" in plan["comment"]
    assert "power" not in plan["comment"]
    assert "Ready again" in plan["reason"]


# --- a URL: probe it once ---------------------------------------------------------------


@respx.mock
async def test_an_endpoint_is_probed_once(db_pool, made):
    url = f"https://probe-{uuid.uuid4().hex[:6]}.test/health"
    route = respx.get(url).mock(return_value=httpx.Response(503))
    pid = await _problem(
        db_pool, made, source="alertmanager", klass="criticalendpointdown",
        subject=url, kind="service",
        payload={"labels": {"instance": url, "alertname": "CriticalEndpointDown"}},
    )
    tid = await _task(db_pool, made, pid)
    homelab = _Homelab()

    plan = await _act(db_pool, homelab).plan_infra_task(tid, "Critical endpoint is unreachable")

    assert route.call_count == 1
    assert plan["action"] == "report" and plan["handler"] == "endpoint"
    assert url in plan["comment"] and "503" in plan["comment"]
    assert homelab.calls == []  # a URL is not a swarm service


@respx.mock
async def test_an_endpoint_that_answers_now_says_so(db_pool, made):
    url = f"https://probe-{uuid.uuid4().hex[:6]}.test/"
    respx.get(url).mock(return_value=httpx.Response(200))
    pid = await _problem(
        db_pool, made, source="alertmanager", klass="warningendpointdown",
        subject=url, kind="service", payload={"labels": {"instance": url}},
    )
    tid = await _task(db_pool, made, pid)

    plan = await _act(db_pool, _Homelab()).plan_infra_task(tid, "x")

    assert plan["handler"] == "endpoint"
    assert "200" in plan["comment"]
    # It answers: the alert resolves itself, so a person has nothing to chase.
    assert "resolves on its own" in plan["comment"]
    assert "proxy" not in plan["comment"]


@respx.mock
async def test_an_endpoint_that_does_not_answer_says_why(db_pool, made):
    url = f"https://probe-{uuid.uuid4().hex[:6]}.test/"
    respx.get(url).mock(side_effect=httpx.ConnectError("connection refused"))
    pid = await _problem(
        db_pool, made, source="alertmanager", klass="criticalendpointdown",
        subject=url, kind="service", payload={"labels": {"instance": url}},
    )
    tid = await _task(db_pool, made, pid)

    plan = await _act(db_pool, _Homelab()).plan_infra_task(tid, "x")

    assert plan["handler"] == "endpoint"
    assert "connection refused" in plan["comment"]


# --- a group: summarise the members from the hub ------------------------------------------


async def test_a_group_summarises_its_members(db_pool, made):
    tag = uuid.uuid4().hex[:6]
    klass = f"stuck_thing_{tag}"
    members = []
    for n in range(3):
        members.append(
            await _problem(db_pool, made, source="social", klass=klass,
                           subject=f"post-{tag}-{n}", kind="post")
        )
    grouped = await hub_group.upgrade(
        db_pool, klass=klass, subject_kind="post", title=f"3 posts stuck ({tag})",
        member_ids=members, by="test",
    )
    tid = await _task(db_pool, made, grouped["problem_id"])

    plan = await _act(db_pool, _Homelab()).plan_infra_task(tid, "x")

    assert plan["action"] == "report" and plan["handler"] == "group"
    for n in range(3):
        assert f"post-{tag}-{n}" in plan["comment"]
    assert "3" in plan["comment"]


# --- an application error: the investigation's, not a restart ------------------------------


async def test_a_sentry_error_is_not_offered_a_service_check(db_pool, made):
    """An exception on a project whose name happens to match a swarm service
    would otherwise be "healthy" and have its task closed."""
    name = f"app{uuid.uuid4().hex[:6]}"
    homelab = _Homelab([_svc(f"{name}_{name}", 1, 1)])
    pid = await _problem(db_pool, made, source="sentry", klass="numericvalueoutofrange",
                         subject=name, kind="service",
                         payload={"description": "numeric field overflow"})
    await _verdict(db_pool, pid, "logged: the column was never widened in that database")
    tid = await _task(db_pool, made, pid)

    plan = await _act(db_pool, homelab).plan_infra_task(tid, "NumericValueOutOfRange")

    assert plan["action"] == "report" and plan["handler"] == "exception"
    assert "the column was never widened" in plan["comment"]
    assert homelab.calls == []


# --- AEGIS's own kinds: what a person does -----------------------------------------------------


async def test_a_flow_problem_says_what_to_do(db_pool, made):
    pid = await _problem(db_pool, made, source="flow_health", klass="llm_dead",
                         subject=f"llm-purpose-{uuid.uuid4().hex[:6]}", kind="flow")
    tid = await _task(db_pool, made, pid)

    plan = await _act(db_pool, _Homelab()).plan_infra_task(tid, "LLM purpose x keeps failing")

    assert plan["action"] == "report" and plan["handler"] == "flow"
    # Where a person looks for a failing flow, not a generic shrug.
    assert "What to do" in plan["comment"] and "Workflows page" in plan["comment"]
    assert "sorry" not in plan["comment"].lower()


async def test_a_money_problem_keeps_its_old_answer(db_pool, made):
    """Money problems are Maou's (#497) and another change routes them; this
    verb's answer to one stays exactly what it was."""
    subject = f"acct-{uuid.uuid4().hex[:6]}"
    pid = await _problem(db_pool, made, source="money", klass="unmatched_rows",
                         subject=subject, kind="instrument")
    tid = await _task(db_pool, made, pid)

    plan = await _act(db_pool, _Homelab()).plan_infra_task(tid, "x")

    assert plan["handler"] == "money"
    assert plan["comment"] == (
        f"This is a instrument problem ({subject}); there is no service to check or "
        "restart, so I have no automatic action for it."
    )
    assert plan["reason"] == "no automatic action for a instrument problem"


# --- no problem on the hub: the title is all there is -------------------------------------------


async def test_without_a_problem_the_title_still_names_a_service(db_pool, made):
    name = f"svc{uuid.uuid4().hex[:6]}_worker"
    homelab = _Homelab([_svc(name, 1, 1)])
    tid = await _task(db_pool, made, None, title=f"Service {name} down")

    plan = await _act(db_pool, homelab).plan_infra_task(tid, f"Service {name} down")

    assert plan["action"] == "service" and plan["service"] == name


async def test_without_a_problem_or_a_name_it_says_what_a_person_does(db_pool, made):
    tid = await _task(db_pool, made, None, title="Something odd is going on")

    plan = await _act(db_pool, _Homelab()).plan_infra_task(tid, "Something odd is going on")

    assert plan["action"] == "report" and plan["handler"] == "manual"
    assert "What to do" in plan["comment"]
