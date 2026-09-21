"""Cluster outages and the `aegis_class` label (#630). Real test database.

On 20 Sep one power event became about 40 problems, each with its own card
and task, and two hours later 30 more when `ServiceDownProlonged` fired under
a different alertname. Two things fix that here:

* an `aegis_class` label names the hub class before the alertname, so the
  two-hour escalation joins the `DockerServiceDown` problem it escalates;
* an `outage` problem (alertmanager's `ClusterOutage`, or the heartbeat
  counting nodes) opens a `service_state` window that records infra problems
  without a task or an investigation, and whatever is still open when it ends
  is promoted and raised then.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from aegis.services.hub import (
    OUTAGE_CLASS,
    OUTAGE_MAX,
    OUTAGE_STATE,
    OUTAGE_TAIL,
    Event,
    correlation_key,
    event_from_alert,
    get_problem,
    ingest_event,
    promote_expired_suppressions,
    set_service_state,
    set_status,
)
from aegis.services.hub_project import project

from tests.core.test_hub_project import _cmds, inbox, todoist  # noqa: F401 — fixtures

NOW = datetime(2026, 9, 20, 11, 1, tzinfo=UTC)


async def _clear_outages(pool) -> None:
    await pool.execute("DELETE FROM service_state WHERE subject = '*' AND subject_kind = '*'")
    # There is one outage key, so each test starts with no outage problem.
    await pool.execute(
        "UPDATE problems SET status = 'closed', closed_at = now() "
        "WHERE correlation_key = $1 AND closed_at IS NULL",
        f"{OUTAGE_CLASS}::",
    )


@pytest_asyncio.fixture(autouse=True, loop_scope="function")
async def _no_window_left_behind(db_pool):
    """The outage window is the global `*`/`*` row: one left behind would hold
    back every infra problem a later test file raises."""
    await _clear_outages(db_pool)
    yield
    await _clear_outages(db_pool)


def _am(alertname: str, *, labels: dict | None = None, fingerprint: str = "", status_at=None) -> dict:
    """The alert dict the alertmanager webhook builds (`routes/webhooks.py`)."""
    labels = {"alertname": alertname, "severity": "critical", **(labels or {})}
    return {
        "source": "alertmanager",
        "title": alertname,
        "fingerprint": fingerprint or uuid.uuid4().hex[:16],
        "severity": "critical",
        "service": labels.get("instance") or labels.get("job", ""),
        "description": "",
        "labels": labels,
        "raw_payload": {"startsAt": (status_at or NOW).isoformat(), "endsAt": (status_at or NOW).isoformat()},
    }


def _heartbeat(alertname: str, subject: str, **labels) -> dict:
    """The alert dict `InfraHeartbeatFlow` builds (`build_heartbeat_alert`)."""
    return {
        "title": alertname,
        "description": "",
        "source": "aegis-heartbeat",
        "severity": "critical",
        "fingerprint": f"aegis-heartbeat:{alertname}:{subject}",
        "labels": {"alertname": alertname, "cluster": "homelab-swarm", **labels},
        "escalate": True,
    }


def _occ(source: str, klass: str, subject: str, at: datetime, kind: str = "service") -> Event:
    return Event(
        source=source,
        external_id=f"{source}:{klass}:{subject}@{at.isoformat()}",
        kind="occurrence",
        title=f"{klass}: {subject}",
        klass=klass,
        subject=subject,
        subject_kind=kind,
        severity="critical",
        occurred_at=at,
    )


async def _window(pool) -> dict | None:
    row = await pool.fetchrow(
        "SELECT state, until_at, set_by FROM service_state WHERE subject = '*' AND subject_kind = '*'"
    )
    return dict(row) if row else None


# --- the aegis_class label ----------------------------------------------------


def test_aegis_class_is_read_before_the_alertname():
    """The Prometheus escalation and the rule it escalates carry the same
    `service_name`, so with the label they derive the same class AND subject."""
    svc = "postiz_postiz"
    down = event_from_alert(
        _am("DockerServiceDown", labels={"service_name": svc, "instance": "10.0.0.5:9323"}),
        occurred_at=NOW,
    )
    prolonged = event_from_alert(
        _am(
            "ServiceDownProlonged",
            labels={
                "service_name": svc,
                "instance": "10.0.0.5:9323",
                "escalation": "prolonged",
                "aegis_class": "DockerServiceDown",
            },
        ),
        occurred_at=NOW,
    )
    assert prolonged.klass == "DockerServiceDown"
    assert (prolonged.subject, prolonged.subject_kind) == (svc, "service")
    assert correlation_key(prolonged) == correlation_key(down) == f"dockerservicedown:service:{svc}"
    # The heartbeat's DockerServiceDown for the same service is the same key.
    hb = event_from_alert(
        {**_heartbeat("DockerServiceDown", svc, service_name=svc), "service": svc},
        occurred_at=NOW,
    )
    assert correlation_key(hb) == correlation_key(down)
    # Normalised like an alertname: case and spacing do not split the key.
    shouty = event_from_alert(
        _am("X", labels={"service_name": svc, "aegis_class": " DOCKERSERVICEDOWN "}), occurred_at=NOW
    )
    assert correlation_key(shouty) == correlation_key(down)
    # No label: the alertname, as before.
    plain = event_from_alert(_am("ServiceDownProlonged", labels={"service_name": svc}), occurred_at=NOW)
    assert correlation_key(plain) == f"servicedownprolonged:service:{svc}"


async def test_the_two_hour_escalation_joins_the_open_problem(db_pool):
    svc = f"svc_{uuid.uuid4().hex[:8]}"
    first = await ingest_event(
        db_pool,
        event_from_alert(_am("DockerServiceDown", labels={"service_name": svc}), occurred_at=NOW),
        now=NOW,
    )
    later = NOW + timedelta(hours=2)
    second = await ingest_event(
        db_pool,
        event_from_alert(
            _am(
                "ServiceDownProlonged",
                labels={"service_name": svc, "aegis_class": "DockerServiceDown"},
                status_at=later,
            ),
            occurred_at=later,
        ),
        now=later,
    )
    assert first.action == "created"
    assert second.action == "attached" and second.problem_id == first.problem_id
    # One problem, one investigation, one card: the repeat asks for none.
    assert second.investigate is False


def test_both_outage_producers_land_on_one_key():
    """`ClusterOutage` is a `count()` over nodes, so it has no service or
    instance. Whatever else a rule carries, an outage is the cluster's."""
    am = event_from_alert(
        _am("ClusterOutage", labels={"aegis_class": "outage", "job": "node-exporter"}),
        occurred_at=NOW,
    )
    hb = event_from_alert(_heartbeat("ClusterOutage", "", aegis_class="outage"), occurred_at=NOW)
    assert am.klass == hb.klass == "outage"
    assert am.source == "alertmanager" and hb.source == "heartbeat"
    assert correlation_key(am) == correlation_key(hb) == f"{OUTAGE_CLASS}::"


# --- the outage window --------------------------------------------------------


async def test_a_suppressed_problem_skips_investigation_and_projection_then_is_promoted(
    db_pool, inbox, todoist  # noqa: F811 — fixtures
):
    """The whole round trip, on the real hub and the real projector."""
    outage = await ingest_event(
        db_pool,
        event_from_alert(_am("ClusterOutage", labels={"aegis_class": "outage"}), occurred_at=NOW),
        now=NOW,
    )
    # The outage itself is raised: it is the one card and task for the event.
    assert outage.action == "created" and outage.investigate is True
    assert outage.suppressed is False
    window = await _window(db_pool)
    # It ends at most OUTAGE_MAX after the outage began (#633).
    assert window["state"] == OUTAGE_STATE and window["until_at"] == NOW + OUTAGE_MAX

    t1 = NOW + timedelta(minutes=2)
    svc = f"svc_{uuid.uuid4().hex[:8]}"
    infra = await ingest_event(db_pool, _occ("heartbeat", "DockerServiceDown", svc, t1), now=t1)
    watchdog = await ingest_event(
        db_pool, _occ("flow_health", "flow_failing", f"flow_{svc}", t1, kind="flow"), now=t1
    )
    # Recorded, not raised: no investigation…
    for r in (infra, watchdog):
        assert r.action == "created" and r.suppressed is True and r.investigate is False
        assert (await get_problem(db_pool, r.problem_id))["status"] == "suppressed"
    # …and no task.
    assert (await project(db_pool, infra.problem_id, now=t1))["skipped"] == "suppressed"
    assert _cmds(todoist, "item_add") == []

    # The heartbeat seeing the same outage attaches to it; it is never
    # suppressed by its own window.
    hb = await ingest_event(
        db_pool,
        event_from_alert(_heartbeat("ClusterOutage", "", aegis_class="outage"), occurred_at=t1),
        now=t1,
    )
    assert hb.problem_id == outage.problem_id and hb.suppressed is False

    # Still in force: nothing is promoted.
    assert infra.problem_id not in await promote_expired_suppressions(db_pool, now=t1)

    # The outage resolves. The window stays up for its tail.
    t2 = NOW + timedelta(minutes=30)
    done = await ingest_event(
        db_pool,
        event_from_alert(
            _am("ClusterOutage", labels={"aegis_class": "outage"}, fingerprint="x", status_at=t2),
            occurred_at=t2,
            resolved=True,
        ),
        now=t2,
    )
    assert done.action == "resolved" and done.problem_id == outage.problem_id
    assert (await _window(db_pool))["until_at"] == t2 + OUTAGE_TAIL
    inside = t2 + OUTAGE_TAIL - timedelta(minutes=1)
    assert infra.problem_id not in await promote_expired_suppressions(db_pool, now=inside)

    # Once it passes, what is still broken is opened, and gets its task.
    after = t2 + OUTAGE_TAIL + timedelta(seconds=1)
    promoted = await promote_expired_suppressions(db_pool, now=after)
    assert {infra.problem_id, watchdog.problem_id} <= set(promoted)
    assert (await get_problem(db_pool, infra.problem_id))["status"] == "open"
    out = await project(db_pool, infra.problem_id, now=after)
    assert out["created"] is True
    assert len(_cmds(todoist, "item_add")) == 1


async def test_an_outage_never_holds_back_what_it_does_not_explain(db_pool):
    await ingest_event(
        db_pool,
        event_from_alert(_am("ClusterOutage", labels={"aegis_class": "outage"}), occurred_at=NOW),
        now=NOW,
    )
    t1 = NOW + timedelta(minutes=1)
    tag = uuid.uuid4().hex[:8]
    for source, klass in (
        ("money", "statement_mismatch"),
        ("research", "question"),
        ("sentry", "TypeError"),
        ("expiry", "cert_expiring"),
        ("manual", "report"),
    ):
        r = await ingest_event(db_pool, _occ(source, klass, f"x_{tag}", t1), now=t1)
        assert r.suppressed is False, source
        assert (await get_problem(db_pool, r.problem_id))["status"] == "open", source


async def test_a_planned_maintenance_window_is_never_replaced_or_ended(db_pool):
    await set_service_state(
        db_pool, "*", "maintenance", subject_kind="*", minutes=120, set_by="ansible", now=NOW
    )
    outage = await ingest_event(
        db_pool,
        event_from_alert(_am("ClusterOutage", labels={"aegis_class": "outage"}), occurred_at=NOW),
        now=NOW,
    )
    assert (await _window(db_pool))["state"] == "maintenance"
    await set_status(db_pool, outage.problem_id, "resolved", reason="by hand", source="admin", now=NOW)
    window = await _window(db_pool)
    assert window["state"] == "maintenance" and window["until_at"] == NOW + timedelta(minutes=120)


async def test_an_expired_maintenance_row_gives_way_to_the_outage(db_pool):
    await set_service_state(
        db_pool, "*", "maintenance", subject_kind="*", minutes=5, set_by="ansible",
        now=NOW - timedelta(hours=1),
    )
    await ingest_event(
        db_pool,
        event_from_alert(_am("ClusterOutage", labels={"aegis_class": "outage"}), occurred_at=NOW),
        now=NOW,
    )
    window = await _window(db_pool)
    assert window["state"] == OUTAGE_STATE and window["until_at"] == NOW + OUTAGE_MAX


async def test_resolving_the_outage_by_hand_ends_its_window(db_pool):
    outage = await ingest_event(
        db_pool,
        event_from_alert(_am("ClusterOutage", labels={"aegis_class": "outage"}), occurred_at=NOW),
        now=NOW,
    )
    t1 = NOW + timedelta(minutes=5)
    assert await set_status(db_pool, outage.problem_id, "resolved", reason="by hand", source="admin", now=t1)
    assert (await _window(db_pool))["until_at"] == t1 + OUTAGE_TAIL


async def test_an_outage_that_returns_inside_the_tail_reopens_unsuppressed(db_pool):
    first = await ingest_event(
        db_pool,
        event_from_alert(_am("ClusterOutage", labels={"aegis_class": "outage"}), occurred_at=NOW),
        now=NOW,
    )
    t1 = NOW + timedelta(minutes=5)
    await set_status(db_pool, first.problem_id, "resolved", reason="nodes back", source="admin", now=t1)
    t2 = t1 + timedelta(minutes=2)
    again = await ingest_event(
        db_pool,
        event_from_alert(
            _am("ClusterOutage", labels={"aegis_class": "outage"}, status_at=t2), occurred_at=t2
        ),
        now=t2,
    )
    assert again.action == "reopened" and again.suppressed is False and again.investigate is True
    # And its window is up again, for OUTAGE_MAX from the reopen: a reopen
    # keeps first_seen_at, and a second power cut the same day is a new
    # stretch of the outage, not a late occurrence of the first (#633).
    assert (await _window(db_pool))["until_at"] == t2 + OUTAGE_MAX


# --- the cap (#633) -----------------------------------------------------------


async def test_the_window_ends_outage_max_after_the_outage_began_whatever_follows(db_pool):
    """noon stays off for days. A window as long as the outage held back every
    unrelated fault for all that time; now it ends OUTAGE_MAX after the outage
    began, later occurrences cannot move it, and the sweep then raises what is
    still broken while the outage problem itself stays open."""
    outage = await ingest_event(
        db_pool,
        event_from_alert(_am("ClusterOutage", labels={"aegis_class": "outage"}), occurred_at=NOW),
        now=NOW,
    )
    # Six hours, as #633 decided; spelled out so a changed constant fails here.
    end = NOW + timedelta(hours=6)
    assert end == NOW + OUTAGE_MAX
    t1 = NOW + timedelta(minutes=1)
    svc = f"svc_{uuid.uuid4().hex[:8]}"
    held = await ingest_event(db_pool, _occ("alertmanager", "DockerServiceDown", svc, t1), now=t1)
    assert held.suppressed is True

    # Repeat occurrences, from both producers, before and after the end.
    for at in (NOW + timedelta(hours=5), end + timedelta(hours=1)):
        again = await ingest_event(
            db_pool,
            event_from_alert(_heartbeat("ClusterOutage", "", aegis_class="outage"), occurred_at=at),
            now=at,
        )
        assert again.problem_id == outage.problem_id and again.action == "attached"
        window = await _window(db_pool)
        assert window["until_at"] == end, at
        assert window["set_by"] == f"hub:{outage.problem_id}"

    assert held.problem_id not in await promote_expired_suppressions(
        db_pool, now=end - timedelta(minutes=1)
    )
    promoted = await promote_expired_suppressions(db_pool, now=end + timedelta(seconds=1))
    assert held.problem_id in promoted
    # The outage is still open: only its window ended.
    assert (await get_problem(db_pool, outage.problem_id))["status"] == "open"
    # And a new infra fault on a healthy node is raised at once.
    late = end + timedelta(hours=2)
    fresh = await ingest_event(
        db_pool, _occ("alertmanager", "DockerServiceDown", f"{svc}_b", late), now=late
    )
    assert fresh.suppressed is False and fresh.investigate is True


async def test_the_sweep_ends_a_window_nothing_will_end(db_pool):
    """A window from before the cap existed has no end: the sweep gives it
    `first_seen_at + OUTAGE_MAX`. And a hub row whose problem is no longer
    live — merged, closed, or resolved while the release failed — gets its
    tail instead of holding its services back for ever."""
    outage = await ingest_event(
        db_pool,
        event_from_alert(_am("ClusterOutage", labels={"aegis_class": "outage"}), occurred_at=NOW),
        now=NOW,
    )
    # What #630 wrote: the same row, open-ended.
    await db_pool.execute(
        "UPDATE service_state SET until_at = NULL WHERE subject = '*' AND subject_kind = '*'"
    )
    gone = str(uuid.uuid4())
    svc = f"svc_{uuid.uuid4().hex[:8]}"
    await db_pool.execute(
        "INSERT INTO service_state (subject, subject_kind, state, until_at, set_by, note) "
        "VALUES ($1, 'service', $2, NULL, $3, '')",
        svc,
        OUTAGE_STATE,
        f"hub:{gone}",
    )
    t1 = NOW + timedelta(minutes=5)
    await promote_expired_suppressions(db_pool, now=t1)
    assert (await _window(db_pool))["until_at"] == NOW + OUTAGE_MAX
    orphan = await db_pool.fetchval(
        "SELECT until_at FROM service_state WHERE subject = $1 AND subject_kind = 'service'", svc
    )
    assert orphan == t1 + OUTAGE_TAIL
    # A live problem's window is left alone by the same sweep.
    assert (await get_problem(db_pool, outage.problem_id))["status"] == "open"
    await db_pool.execute("DELETE FROM service_state WHERE subject = $1", svc)
