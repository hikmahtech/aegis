"""One node down holds back only its own services (#633). Real test database.

lam alone carries about 30 pinned services. `ClusterOutage` needs two nodes,
so when lam went down alone every one of them became its own problem, card
and task. The heartbeat's NodeDown now names the services that had a task on
the node, and while that problem is live the hub keeps one `service_state`
row per service (state `outage`, set by the problem). Their problems are
recorded as `suppressed`; a service on another node is not held. When the
node's problem resolves, its rows get `OUTAGE_TAIL`, and the sweep promotes
whatever is still broken.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from aegis.services.hub import (
    OUTAGE_MAX,
    OUTAGE_STATE,
    OUTAGE_TAIL,
    correlation_key,
    event_from_alert,
    get_problem,
    ingest_event,
    promote_expired_suppressions,
    set_service_state,
    set_status,
)

from tests.core.test_hub_outage import _am, _clear_outages, _heartbeat, _occ

NOW = datetime(2026, 9, 20, 11, 1, tzinfo=UTC)


@pytest_asyncio.fixture(autouse=True, loop_scope="function")
async def _no_window_left_behind(db_pool):
    """A `*`/`*` row left behind would hold back every infra problem a later
    test file raises."""
    await _clear_outages(db_pool)
    yield
    await _clear_outages(db_pool)


def _tag() -> str:
    return uuid.uuid4().hex[:8]


def _node_down(node: str, services: list[str]) -> dict:
    """The NodeDown `InfraHeartbeatFlow` raises (`build_heartbeat_alert`)."""
    return {**_heartbeat("NodeDown", node), "services": services}


async def _down(pool, node: str, services: list[str], at: datetime):
    return await ingest_event(pool, event_from_alert(_node_down(node, services), occurred_at=at), now=at)


async def _up(pool, node: str, at: datetime):
    return await ingest_event(
        pool, event_from_alert(_heartbeat("NodeDown", node), occurred_at=at, resolved=True), now=at
    )


async def _row(pool, subject: str) -> dict | None:
    row = await pool.fetchrow(
        "SELECT state, until_at, set_by FROM service_state "
        "WHERE subject = $1 AND subject_kind = 'service'",
        subject,
    )
    return dict(row) if row else None


def _am_service_down(svc: str, at: datetime) -> dict:
    """Prometheus' `DockerServiceDown`: `service_name` is the swarm service
    name, from the swarm exporter's `service.name`."""
    return _am("DockerServiceDown", labels={"service_name": svc, "instance": "10.0.0.5:9323"}, status_at=at)


def _hb_service_down(svc: str) -> dict:
    """The heartbeat's `DockerServiceDown`: its subject is the `docker service
    ls` name, the same swarm service name."""
    return {**_heartbeat("DockerServiceDown", svc, service_name=svc), "service": svc}


async def test_a_node_down_holds_back_only_its_own_services(db_pool):
    tag = _tag()
    node = f"lam{tag}"
    on_node = [f"pg{tag}_postgres", f"ch{tag}_clickhouse", f"loki{tag}_loki"]
    elsewhere = f"web{tag}_web"

    down = await _down(db_pool, node, on_node, NOW)
    # The node itself is raised: it is the one card and task for all of them.
    assert down.action == "created" and down.investigate is True and down.suppressed is False
    for svc in on_node:
        row = await _row(db_pool, svc)
        assert row == {"state": OUTAGE_STATE, "until_at": None, "set_by": f"hub:{down.problem_id}"}
    assert await _row(db_pool, elsewhere) is None

    t1 = NOW + timedelta(minutes=5)
    # The same swarm service name from alertmanager and from the heartbeat,
    # and a crash loop keyed on cadvisor's `service` label.
    am = await ingest_event(db_pool, event_from_alert(_am_service_down(on_node[0], t1), occurred_at=t1), now=t1)
    hb = await ingest_event(db_pool, event_from_alert(_hb_service_down(on_node[1]), occurred_at=t1), now=t1)
    crash = await ingest_event(
        db_pool,
        event_from_alert(_am("ServiceCrashLooping", labels={"service": on_node[2]}), occurred_at=t1),
        now=t1,
    )
    for r in (am, hb, crash):
        assert r.action == "created" and r.suppressed is True and r.investigate is False
        assert (await get_problem(db_pool, r.problem_id))["status"] == "suppressed"
    assert (await get_problem(db_pool, crash.problem_id))["class"] == "servicecrashlooping"

    # The two-hour escalation joins its problem and stays quiet too.
    t2 = NOW + timedelta(hours=2)
    prolonged = await ingest_event(
        db_pool,
        event_from_alert(
            _am(
                "ServiceDownProlonged",
                labels={"service_name": on_node[0], "aegis_class": "DockerServiceDown"},
                status_at=t2,
            ),
            occurred_at=t2,
        ),
        now=t2,
    )
    assert prolonged.problem_id == am.problem_id and prolonged.investigate is False

    # A service on a healthy node is raised as usual.
    other = await ingest_event(db_pool, event_from_alert(_am_service_down(elsewhere, t1), occurred_at=t1), now=t1)
    assert other.suppressed is False and other.investigate is True
    # And so is a judgement the node being down does not explain.
    money = await ingest_event(db_pool, _occ("money", "statement_mismatch", on_node[0], t1), now=t1)
    assert money.suppressed is False


async def test_the_node_coming_back_starts_the_tail_then_promotion_raises_what_is_still_broken(db_pool):
    tag = _tag()
    node = f"lam{tag}"
    stays, clears = f"a{tag}_a", f"b{tag}_b"
    down = await _down(db_pool, node, [stays, clears], NOW)
    t1 = NOW + timedelta(minutes=5)
    broken = await ingest_event(db_pool, event_from_alert(_hb_service_down(stays), occurred_at=t1), now=t1)
    healed = await ingest_event(db_pool, event_from_alert(_hb_service_down(clears), occurred_at=t1), now=t1)
    assert broken.suppressed and healed.suppressed

    # While the node is down, nothing is promoted.
    t2 = NOW + timedelta(hours=9)
    assert broken.problem_id not in await promote_expired_suppressions(db_pool, now=t2)

    t3 = NOW + timedelta(hours=10)
    up = await _up(db_pool, node, t3)
    assert up.action == "resolved" and up.problem_id == down.problem_id
    for svc in (stays, clears):
        assert (await _row(db_pool, svc))["until_at"] == t3 + OUTAGE_TAIL
    # One service converges inside the tail.
    t4 = t3 + timedelta(minutes=3)
    await ingest_event(
        db_pool, event_from_alert(_hb_service_down(clears), occurred_at=t4, resolved=True), now=t4
    )
    assert broken.problem_id not in await promote_expired_suppressions(
        db_pool, now=t3 + OUTAGE_TAIL - timedelta(seconds=1)
    )

    promoted = await promote_expired_suppressions(db_pool, now=t3 + OUTAGE_TAIL + timedelta(seconds=1))
    assert broken.problem_id in promoted
    assert healed.problem_id not in promoted
    assert (await get_problem(db_pool, broken.problem_id))["status"] == "open"
    assert (await get_problem(db_pool, healed.problem_id))["status"] == "resolved"


async def test_resolving_the_node_by_hand_starts_the_tail_and_a_reopen_holds_again(db_pool):
    tag = _tag()
    svc = f"s{tag}_s"
    down = await _down(db_pool, f"lam{tag}", [svc], NOW)
    t1 = NOW + timedelta(minutes=30)
    assert await set_status(db_pool, down.problem_id, "resolved", reason="by hand", source="admin", now=t1)
    assert (await _row(db_pool, svc))["until_at"] == t1 + OUTAGE_TAIL
    t2 = t1 + timedelta(minutes=5)
    assert await set_status(db_pool, down.problem_id, "open", reason="still down", source="admin", now=t2)
    assert (await _row(db_pool, svc))["until_at"] is None


async def test_an_operator_window_is_never_overwritten_or_shortened(db_pool):
    tag = _tag()
    finite, open_ended, own_outage, expired = (f"{x}{tag}_svc" for x in ("fin", "open", "out", "exp"))
    await set_service_state(db_pool, finite, "maintenance", minutes=600, set_by="ansible", now=NOW)
    await set_service_state(db_pool, open_ended, "maintenance", set_by="ansible", now=NOW)
    # An operator may set `outage` too; it is still theirs.
    await set_service_state(db_pool, own_outage, "outage", minutes=600, set_by="chat:pandora", now=NOW)
    await set_service_state(
        db_pool, expired, "deploying", minutes=5, set_by="ansible", now=NOW - timedelta(hours=1)
    )
    before = {s: await _row(db_pool, s) for s in (finite, open_ended, own_outage)}

    down = await _down(db_pool, f"lam{tag}", [finite, open_ended, own_outage, expired], NOW)
    for svc, row in before.items():
        assert await _row(db_pool, svc) == row, svc
    # An expired row is anyone's.
    assert (await _row(db_pool, expired))["set_by"] == f"hub:{down.problem_id}"

    t1 = NOW + timedelta(minutes=30)
    await _up(db_pool, f"lam{tag}", t1)
    await promote_expired_suppressions(db_pool, now=t1)
    for svc, row in before.items():
        assert await _row(db_pool, svc) == row, svc
    assert (await _row(db_pool, expired))["until_at"] == t1 + OUTAGE_TAIL


async def test_a_service_another_live_node_holds_stays_with_it(db_pool):
    tag = _tag()
    svc = f"s{tag}_s"
    first = await _down(db_pool, f"lam{tag}", [svc], NOW)
    t1 = NOW + timedelta(minutes=10)
    await _down(db_pool, f"wow{tag}", [svc], t1)
    assert (await _row(db_pool, svc))["set_by"] == f"hub:{first.problem_id}"


async def test_node_holds_are_not_capped_with_the_cluster_window(db_pool):
    """Two nodes down is a cluster outage; after OUTAGE_MAX its window ends and
    the sweep raises what is still broken — but a service on a node that is
    still down stays held by that node."""
    tag = _tag()
    outage = await ingest_event(
        db_pool,
        event_from_alert(_am("ClusterOutage", labels={"aegis_class": "outage"}), occurred_at=NOW),
        now=NOW,
    )
    t1 = NOW + timedelta(minutes=2)
    pinned, loose = f"p{tag}_p", f"l{tag}_l"
    # The NodeDown itself lands inside the outage window, and still holds.
    down = await _down(db_pool, f"lam{tag}", [pinned], t1)
    assert down.suppressed is True
    assert (await _row(db_pool, pinned))["set_by"] == f"hub:{down.problem_id}"
    t2 = NOW + timedelta(minutes=5)
    on_node = await ingest_event(db_pool, event_from_alert(_hb_service_down(pinned), occurred_at=t2), now=t2)
    unrelated = await ingest_event(db_pool, event_from_alert(_hb_service_down(loose), occurred_at=t2), now=t2)
    assert on_node.suppressed and unrelated.suppressed

    after = NOW + OUTAGE_MAX + timedelta(seconds=1)
    promoted = await promote_expired_suppressions(db_pool, now=after)
    assert unrelated.problem_id in promoted
    # The node is raised now, as one problem, and its service stays held.
    assert down.problem_id in promoted
    assert on_node.problem_id not in promoted
    assert (await get_problem(db_pool, on_node.problem_id))["status"] == "suppressed"
    assert (await get_problem(db_pool, outage.problem_id))["status"] == "open"


async def test_a_nodedown_with_no_services_holds_nothing(db_pool):
    """Alertmanager's NodeDown and a heartbeat whose `docker node ps` failed
    carry no services: the node is raised and nothing is held."""
    tag = _tag()
    before = await db_pool.fetchval("SELECT count(*) FROM service_state")
    down = await _down(db_pool, f"lam{tag}", [], NOW)
    assert down.action == "created"
    assert await db_pool.fetchval("SELECT count(*) FROM service_state") == before


# --- Prometheus' node alerts meet the heartbeat's -------------------------------


def _swarm_not_ready(host: str, at: datetime = NOW) -> dict:
    """`SwarmNodeNotReady` from the swarm exporter's `swarm_node_info`: the
    node's `hostname`, `aegis_class: NodeDown`, and no `service_name`. Its
    instance is the exporter's, which says nothing about the node."""
    return _am(
        "SwarmNodeNotReady",
        labels={
            "aegis_class": "NodeDown",
            "hostname": host,
            "state": "down",
            "instance": "swarm-exporter:9101",
            "job": "swarm-exporter",
        },
        status_at=at,
    )


def test_prometheus_node_alerts_land_on_the_heartbeats_node_problem():
    hb = event_from_alert(_node_down("lam", ["postgres_postgres"]), occurred_at=NOW)
    am = event_from_alert(_swarm_not_ready("lam"), occurred_at=NOW)
    assert (hb.klass, hb.subject, hb.subject_kind) == ("NodeDown", "lam", "node")
    assert (am.subject, am.subject_kind) == (hb.subject, hb.subject_kind)
    assert correlation_key(am) == correlation_key(hb) == "nodedown:node:lam"
    # node-exporter's own NodeDown carries the same hostname.
    up = event_from_alert(
        _am("NodeDown", labels={"hostname": "lam", "instance": "10.20.0.5:9100", "job": "node-exporter"}),
        occurred_at=NOW,
    )
    assert correlation_key(up) == "nodedown:node:lam"
    # A CPU alert on the same node keeps the key it had.
    cpu = event_from_alert(
        _am("NodeHighCPU", labels={"hostname": "lam", "instance": "10.20.0.5:9100"}), occurred_at=NOW
    )
    assert correlation_key(cpu) == "nodehighcpu:service:10.20.0.5-9100"
    # An outage keeps its empty subject, whatever labels it carries.
    outage = event_from_alert(
        _am("ClusterOutage", labels={"aegis_class": "outage", "hostname": "lam"}), occurred_at=NOW
    )
    assert correlation_key(outage) == "outage::"


async def test_an_alertmanager_node_alert_never_clears_or_replaces_the_holds(db_pool):
    """It carries no services. Arriving after the heartbeat it attaches and
    leaves the rows alone; arriving first it holds nothing, and the
    heartbeat's NodeDown then attaches to it and holds its services."""
    tag = _tag()
    node, svc = f"lam{tag}", f"pg{tag}_postgres"
    hb = await _down(db_pool, node, [svc], NOW)
    before = await _row(db_pool, svc)
    t1 = NOW + timedelta(minutes=2)
    am = await ingest_event(db_pool, event_from_alert(_swarm_not_ready(node, t1), occurred_at=t1), now=t1)
    assert am.problem_id == hb.problem_id and am.action == "attached"
    assert await _row(db_pool, svc) == before
    # And a service problem is still held.
    t2 = NOW + timedelta(minutes=6)
    down = await ingest_event(db_pool, event_from_alert(_am_service_down(svc, t2), occurred_at=t2), now=t2)
    assert down.suppressed is True

    # The other way round.
    node2, svc2 = f"wow{tag}", f"ch{tag}_clickhouse"
    first = await ingest_event(db_pool, event_from_alert(_swarm_not_ready(node2), occurred_at=NOW), now=NOW)
    assert first.action == "created" and await _row(db_pool, svc2) is None
    later = await _down(db_pool, node2, [svc2], t1)
    assert later.problem_id == first.problem_id and later.action == "attached"
    assert (await _row(db_pool, svc2))["set_by"] == f"hub:{first.problem_id}"
