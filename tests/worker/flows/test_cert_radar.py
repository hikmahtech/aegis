"""CertRadarFlow on the problem hub (#475).

The hub reads a watchdog's findings as the whole truth: a problem whose
finding goes missing has recovered (`hub_watch.reconcile_findings`). The probe
used to report a cert only on the day it crossed 14, 7 or 0 days, so the day
after a crossing there was no finding, and the hub resolved `cert_expiring`
and closed its task while the cert was still expiring.

Now a cert inside 14 days is a finding every day, and the Slack card still
goes out only when a threshold is crossed. Four layers pin that:

* the flow over stub activities: what it records and when it cards;
* the probe against real Postgres: the sticky threshold and its reset when a
  cert is renewed;
* the flow end to end, with the real probe and the real `reconcile_findings`
  on the test database, run once per "day" — which is where the bug lived;
* a replay of histories the pre-#475 flow recorded, because a daily run in
  flight across a deploy has to replay through the new code.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from aegis.connectors.homelab import _envelope
from aegis.services.hub import get_problem, list_events, slug
from aegis_worker.activities.homelab import HomelabActivities
from aegis_worker.activities.hub import HubActivities
from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Replayer, Worker

with workflow.unsafe.imports_passed_through():
    from aegis_worker.flows.cert_radar import CertRadarConfig, CertRadarFlow
    from aegis_worker.shared.retry import FAST, NO_RETRY, TIMEOUT_FAST, TIMEOUT_STANDARD

PREFIX = "zzcert-"


def _domain() -> str:
    return f"{PREFIX}{uuid.uuid4().hex[:8]}.example"


def _tq() -> str:
    return f"tq-cert-{uuid.uuid4().hex[:8]}"


# --- the flow over stub activities --------------------------------------------

_reports: dict[str, Any] = {}  # domain -> what the probe returns, or an exception to raise
_cards: list[dict] = []
_hub_calls: list[dict] = []


@activity.defn(name="probe_and_upsert_cert")
async def stub_probe(domain: str) -> dict | None:
    out = _reports[domain]
    if isinstance(out, Exception):
        raise out
    return out


@activity.defn(name="notify_cert_alert")
async def stub_notify(alert: dict) -> None:
    _cards.append(alert)


@activity.defn(name="reconcile_findings")
async def stub_reconcile(inp: dict) -> dict:
    """`HubActivities.reconcile_findings` with no pool: every finding fresh."""
    _hub_calls.append(inp)
    return {
        "fresh": [{**f, "problem_id": None} for f in inp.get("findings") or []],
        "attached": 0,
        "muted": 0,
        "suppressed": 0,
        "resolved": [],
    }


STUBS = [stub_probe, stub_notify, stub_reconcile]


def _reset(reports: dict[str, Any]) -> None:
    _reports.clear()
    _reports.update(reports)
    _cards.clear()
    _hub_calls.clear()


async def _run_stubbed(domains: list[str], *, silent: bool = False) -> dict:
    tq = _tq()
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(env.client, task_queue=tq, workflows=[CertRadarFlow], activities=STUBS),
    ):
        return await env.client.execute_workflow(
            CertRadarFlow.run,
            CertRadarConfig(silent=silent, domains=domains),
            id=f"cert-radar-{uuid.uuid4().hex[:8]}",
            task_queue=tq,
        )


def _report(domain: str, days: int, threshold: int | None) -> dict:
    """What `probe_and_upsert_cert` returns for a cert it could read."""
    return {
        "domain": domain,
        "days": days,
        "threshold": threshold,
        "not_after": "2026-09-23T07:00:00+00:00",
    }


@pytest.mark.asyncio
async def test_a_cert_inside_14_days_is_a_finding_on_a_day_nothing_is_crossed():
    """The bug: this day used to produce no finding, so the hub resolved the
    problem. It is a finding, and it is not a card."""
    d = _domain()
    _reset({d: _report(d, 12, None)})
    result = await _run_stubbed([d])

    assert _hub_calls[0]["classes"] == ["cert_expiring", "cert_unreachable"]
    assert _hub_calls[0]["source"] == "expiry" and _hub_calls[0]["subject_kind"] == "domain"
    [finding] = _hub_calls[0]["findings"]
    assert (finding["klass"], finding["subject"], finding["severity"]) == (
        "cert_expiring",
        d,
        "warning",
    )
    assert finding["payload"]["days"] == 12
    assert _cards == [], "no threshold was crossed today, so no card"
    assert result == {"alerts": 0, "problems": 1}


@pytest.mark.asyncio
async def test_the_title_names_the_expiry_date_so_it_stays_true_while_the_problem_is_open():
    """The problem keeps the title it was opened with, and it now stays open
    for up to two weeks. "Expires in 13 day(s)" would be wrong by day two."""
    d = _domain()
    _reset({d: _report(d, 13, 14)})
    await _run_stubbed([d])
    assert _hub_calls[0]["findings"][0]["title"] == f"Certificate for {d} expires on 2026-09-23"


@pytest.mark.asyncio
async def test_a_crossing_is_carded_and_seven_days_or_less_is_critical():
    d = _domain()
    _reset({d: _report(d, 6, 7)})
    result = await _run_stubbed([d])
    [finding] = _hub_calls[0]["findings"]
    assert finding["severity"] == "critical"
    assert _cards == [_report(d, 6, 7)]
    assert result == {"alerts": 1, "problems": 1}


@pytest.mark.asyncio
async def test_a_renewed_cert_is_no_finding_so_the_hub_resolves_it():
    """A cert outside 14 days produces nothing, and the flow still asks the
    hub to reconcile, which is what resolves the open problem."""
    d = _domain()
    _reset({d: _report(d, 89, None)})
    result = await _run_stubbed([d])
    assert _hub_calls[0]["findings"] == []
    assert _hub_calls[0]["classes"] == ["cert_expiring", "cert_unreachable"]
    assert _cards == []
    assert result == {"alerts": 0, "problems": 0}


@pytest.mark.asyncio
async def test_silent_records_the_finding_and_never_cards():
    d = _domain()
    _reset({d: _report(d, 6, 7)})
    result = await _run_stubbed([d], silent=True)
    assert [f["klass"] for f in _hub_calls[0]["findings"]] == ["cert_expiring"]
    assert _cards == []
    assert result == {"alerts": 0, "problems": 1}


@pytest.mark.asyncio
async def test_an_unreachable_domain_is_a_finding_and_a_card_every_day():
    """Unchanged by #475: an unreachable probe was already reported, and
    carded, on every run."""
    d = _domain()
    unreachable = {"domain": d, "error": "tls timeout", "unreachable": True}
    _reset({d: unreachable})
    await _run_stubbed([d])
    [finding] = _hub_calls[0]["findings"]
    assert (finding["klass"], finding["severity"]) == ("cert_unreachable", "warning")
    assert finding["payload"] == {"error": "tls timeout"}
    assert _cards == [unreachable]


@pytest.mark.asyncio
async def test_an_unreachable_domain_keeps_its_expiring_problem_open():
    """A probe that could not connect has not seen a renewal. The last cert it
    saw is still expiring, so that stays a finding: otherwise one network
    blip would resolve the problem and the next day would reopen it."""
    d = _domain()
    report = {
        "domain": d,
        "error": "tls timeout",
        "unreachable": True,
        "days": 5,
        "not_after": "2026-09-16T07:00:00+00:00",
    }
    _reset({d: report})
    await _run_stubbed([d])
    by_class = {f["klass"]: f for f in _hub_calls[0]["findings"]}
    assert set(by_class) == {"cert_unreachable", "cert_expiring"}
    assert by_class["cert_expiring"]["severity"] == "critical"
    assert by_class["cert_expiring"]["payload"]["days"] == 5
    assert len(_cards) == 1 and _cards[0]["unreachable"] is True, "one card: the unreachable one"


@pytest.mark.asyncio
async def test_a_probe_that_raises_skips_only_that_domain():
    ok, broken = _domain(), _domain()
    _reset({broken: ApplicationError("db down", non_retryable=True), ok: _report(ok, 12, None)})
    await _run_stubbed([broken, ok])
    assert [f["subject"] for f in _hub_calls[0]["findings"]] == [ok]


# --- the probe against real Postgres ------------------------------------------


class FakeHomelab:
    """`HomelabConnector.probe_tls`'s envelope. `not_after` is naive, the way
    the connector parses `openssl x509 -enddate`."""

    def __init__(self) -> None:
        self.certs: dict[str, dict] = {}

    def cert(self, domain: str, serial: str, days: int) -> None:
        # Half a day past the whole-day mark, so `days` survives the seconds
        # between here and the probe.
        not_after = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=days, hours=12)
        self.certs[domain] = {"serial": serial, "not_after": not_after}

    def down(self, domain: str, error: str = "tls timeout") -> None:
        self.certs[domain] = {"error": error}

    async def probe_tls(self, domain: str, port: int = 443) -> dict:
        c = self.certs[domain]
        if "error" in c:
            return _envelope(False, error=c["error"], retryable=True)
        return _envelope(
            True, data={"domain": domain, "not_after": c["not_after"], "serial": c["serial"]}
        )


class FakeDelivery:
    """`DeliveryActivities.send_message`: records the card, answers ok."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_message(
        self,
        agent_id: str,
        message: str,
        chat_id: int = 0,
        thread_ref: dict | None = None,
        thread_overflow: bool = False,
    ) -> dict:
        self.sent.append(message)
        return {"ok": True}


@pytest_asyncio.fixture(loop_scope="function")
async def pool(db_pool):
    yield db_pool
    await db_pool.execute(
        "DELETE FROM pandoras_actor.cert_expiry WHERE domain LIKE $1", f"{PREFIX}%"
    )


@pytest.mark.asyncio
async def test_the_probe_reports_every_day_and_crosses_each_threshold_once(pool):
    homelab = FakeHomelab()
    act = HomelabActivities(db_pool=pool, homelab=homelab, delivery=FakeDelivery())
    env = ActivityEnvironment()
    d = _domain()

    seen = []
    for days in (13, 12, 6, 5, -1, -2):
        homelab.cert(d, "AAAA", days)
        out = await env.run(act.probe_and_upsert_cert, d)
        assert out["domain"] == d and out["days"] == days
        assert out["not_after"].startswith(homelab.certs[d]["not_after"].date().isoformat())
        seen.append(out["threshold"])

    # The quiet days are reports too — that is the fix. Only the crossings
    # carry a threshold, and each threshold is crossed once.
    assert seen == [14, None, 7, None, 0, None]
    sticky = await pool.fetchval(
        "SELECT last_alert_threshold FROM pandoras_actor.cert_expiry WHERE domain = $1", d
    )
    assert sticky == 0


@pytest.mark.asyncio
async def test_a_renewed_cert_starts_its_thresholds_again(pool):
    """A renewal is a new serial, so a new row with no sticky threshold. The
    old cert's row keeps its own."""
    homelab = FakeHomelab()
    act = HomelabActivities(db_pool=pool, homelab=homelab, delivery=FakeDelivery())
    env = ActivityEnvironment()
    d = _domain()

    homelab.cert(d, "OLD1", 13)
    assert (await env.run(act.probe_and_upsert_cert, d))["threshold"] == 14
    homelab.cert(d, "OLD1", 6)
    assert (await env.run(act.probe_and_upsert_cert, d))["threshold"] == 7

    homelab.cert(d, "NEW2", 89)
    renewed = await env.run(act.probe_and_upsert_cert, d)
    assert (renewed["days"], renewed["threshold"]) == (89, None)

    # Three months on, the new cert gets its own 14-day card.
    homelab.cert(d, "NEW2", 13)
    assert (await env.run(act.probe_and_upsert_cert, d))["threshold"] == 14

    rows = await pool.fetch(
        "SELECT cert_serial, last_alert_threshold FROM pandoras_actor.cert_expiry "
        "WHERE domain = $1 ORDER BY cert_serial",
        d,
    )
    assert [(r["cert_serial"], r["last_alert_threshold"]) for r in rows] == [
        ("NEW2", 14),
        ("OLD1", 7),
    ]


@pytest.mark.asyncio
async def test_an_unreachable_probe_reports_the_last_cert_it_saw(pool):
    homelab = FakeHomelab()
    act = HomelabActivities(db_pool=pool, homelab=homelab, delivery=FakeDelivery())
    env = ActivityEnvironment()
    d = _domain()

    homelab.down(d)
    never_seen = await env.run(act.probe_and_upsert_cert, d)
    assert never_seen == {"domain": d, "error": "tls timeout", "unreachable": True}

    homelab.cert(d, "OLD1", 40)
    await env.run(act.probe_and_upsert_cert, d)
    homelab.cert(d, "NEW2", 5)  # the most recently seen cert is the one that counts
    seen = await env.run(act.probe_and_upsert_cert, d)

    homelab.down(d)
    blip = await env.run(act.probe_and_upsert_cert, d)
    assert blip["unreachable"] is True and blip["error"] == "tls timeout"
    assert (blip["days"], blip["not_after"]) == (5, seen["not_after"])
    assert "threshold" not in blip, "nothing is crossed while the domain cannot be reached"


# --- the flow end to end: real probe, real hub --------------------------------


async def _problem(pool, klass: str, domain: str) -> dict | None:
    pid = await pool.fetchval(
        "SELECT id::text FROM problems WHERE correlation_key = $1 AND closed_at IS NULL",
        f"{klass}:domain:{slug(domain)}",
    )
    return await get_problem(pool, pid) if pid else None


class _Days:
    """One `CertRadarFlow` run per call, on one Temporal environment, with the
    REAL probe and the REAL `reconcile_findings` on the test database."""

    def __init__(self, env: WorkflowEnvironment, tq: str, domains: list[str]) -> None:
        self.env, self.tq, self.domains = env, tq, domains

    async def run(self) -> dict:
        return await self.env.client.execute_workflow(
            CertRadarFlow.run,
            CertRadarConfig(domains=self.domains),
            id=f"cert-radar-day-{uuid.uuid4().hex[:8]}",
            task_queue=self.tq,
        )


def _real_activities(pool, homelab: FakeHomelab, delivery: FakeDelivery) -> list:
    probe = HomelabActivities(db_pool=pool, homelab=homelab, delivery=delivery)
    hub = HubActivities(db_pool=pool)
    return [probe.probe_and_upsert_cert, probe.notify_cert_alert, hub.reconcile_findings]


@pytest.mark.asyncio
async def test_an_expiring_cert_stays_one_open_problem_until_it_is_renewed(pool):
    """Issue #475 end to end. Day 2 is the bug: nothing is crossed, the cert is
    still expiring, and the hub used to resolve the problem."""
    homelab, delivery = FakeHomelab(), FakeDelivery()
    d = _domain()
    tq = _tq()
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue=tq,
            workflows=[CertRadarFlow],
            activities=_real_activities(pool, homelab, delivery),
        ),
    ):
        days = _Days(env, tq, [d])

        homelab.cert(d, "AAAA", 13)
        await days.run()
        opened = await _problem(pool, "cert_expiring", d)
        assert opened is not None and opened["status"] == "open"
        assert len(delivery.sent) == 1 and "[CERT][T-14d]" in delivery.sent[0]

        homelab.cert(d, "AAAA", 12)
        await days.run()
        day2 = await get_problem(pool, opened["id"])
        assert day2["status"] == "open", "the cert is still expiring: the problem stays open"
        assert day2["occurrences"] == 2
        assert len(delivery.sent) == 1, "no threshold crossed, so no card"

        homelab.cert(d, "AAAA", 6)
        await days.run()
        day3 = await get_problem(pool, opened["id"])
        assert (day3["status"], day3["occurrences"]) == ("open", 3)
        assert len(delivery.sent) == 2 and "[CERT][T-7d]" in delivery.sent[1]
        latest = [e for e in await list_events(pool, opened["id"]) if e["kind"] == "occurrence"][0]
        assert latest["severity"] == "critical"
        assert latest["payload"]["days"] == 6

        homelab.cert(d, "BBBB", 89)
        await days.run()
        renewed = await get_problem(pool, opened["id"])
        assert renewed["status"] == "resolved", "a renewed cert is the recovery"
        assert len(delivery.sent) == 2, "a renewal is not a card"


@pytest.mark.asyncio
async def test_a_network_blip_does_not_resolve_an_expiring_cert(pool):
    homelab, delivery = FakeHomelab(), FakeDelivery()
    d = _domain()
    tq = _tq()
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue=tq,
            workflows=[CertRadarFlow],
            activities=_real_activities(pool, homelab, delivery),
        ),
    ):
        days = _Days(env, tq, [d])

        homelab.cert(d, "AAAA", 13)
        await days.run()
        expiring = await _problem(pool, "cert_expiring", d)

        homelab.down(d)
        await days.run()
        assert (await get_problem(pool, expiring["id"]))["status"] == "open"
        unreachable = await _problem(pool, "cert_unreachable", d)
        assert unreachable is not None and unreachable["status"] == "open"
        assert "[CERT][UNREACHABLE]" in delivery.sent[-1]

        homelab.cert(d, "AAAA", 11)
        await days.run()
        assert (await get_problem(pool, expiring["id"]))["status"] == "open"
        assert (await get_problem(pool, unreachable["id"]))["status"] == "resolved"


# --- replaying histories the pre-#475 flow recorded ---------------------------


@workflow.defn(name="CertRadarFlow", sandboxed=False)
class _CertRadarFlowPre475:
    """CertRadarFlow.run as it was at 5fd7a97, verbatim apart from the names.
    It only records histories for the replay test below."""

    @workflow.run
    async def run(self, config: CertRadarConfig) -> dict:
        alerts = 0
        findings: list[dict] = []
        try:
            for domain in config.domains:
                try:
                    alert = await workflow.execute_activity_method(
                        HomelabActivities.probe_and_upsert_cert,
                        args=[domain],
                        start_to_close_timeout=TIMEOUT_STANDARD,
                        retry_policy=FAST,
                    )
                except Exception:
                    continue
                if alert is None:
                    continue
                if alert.get("unreachable"):
                    findings.append(
                        {
                            "klass": "cert_unreachable",
                            "subject": domain,
                            "title": f"TLS probe of {domain} failed",
                            "severity": "warning",
                            "payload": {"error": alert.get("error", "")},
                        }
                    )
                else:
                    findings.append(
                        {
                            "klass": "cert_expiring",
                            "subject": domain,
                            "title": f"Certificate for {domain} expires in {alert['days']} day(s)",
                            "severity": "critical" if alert["days"] <= 7 else "warning",
                            "payload": {k: v for k, v in alert.items() if k != "domain"},
                        }
                    )
                if config.silent:
                    continue
                alerts += 1
                try:
                    await workflow.execute_activity_method(
                        HomelabActivities.notify_cert_alert,
                        args=[alert],
                        start_to_close_timeout=TIMEOUT_FAST,
                        retry_policy=NO_RETRY,
                    )
                except Exception:
                    pass
            if config.domains:
                try:
                    await workflow.execute_activity_method(
                        HubActivities.reconcile_findings,
                        args=[
                            {
                                "source": "expiry",
                                "subject_kind": "domain",
                                "classes": ["cert_expiring", "cert_unreachable"],
                                "findings": findings,
                            }
                        ],
                        start_to_close_timeout=TIMEOUT_STANDARD,
                        retry_policy=NO_RETRY,
                    )
                except Exception as exc:  # noqa: BLE001
                    workflow.logger.warning("cert_radar_hub_failed err=%s", str(exc)[:200])
        except Exception as exc:
            workflow.logger.error("cert_radar_failed error=%s", str(exc)[:200])
            raise
        return {"alerts": alerts, "problems": len(findings)}


@pytest.mark.asyncio
@pytest.mark.parametrize("silent", [False, True])
async def test_a_history_recorded_before_475_replays_through_the_new_flow(silent):
    """Every shape the old probe returned: a crossing, `None`, an unreachable
    domain, and a probe that raised. The new flow must issue the same
    commands for them, or a run in flight across the deploy fails replay."""
    crossing, quiet, down, broken = _domain(), _domain(), _domain(), _domain()
    _reset(
        {
            crossing: {
                "domain": crossing,
                "days": 13,
                "threshold": 14,
                "not_after": "2026-09-24T07:00:00+00:00",
            },
            quiet: None,
            down: {"domain": down, "error": "tls timeout", "unreachable": True},
            broken: ApplicationError("db down", non_retryable=True),
        }
    )
    tq = _tq()
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(env.client, task_queue=tq, workflows=[_CertRadarFlowPre475], activities=STUBS),
    ):
        handle = await env.client.start_workflow(
            "CertRadarFlow",
            CertRadarConfig(silent=silent, domains=[crossing, quiet, down, broken]),
            id=f"cert-radar-pre475-{uuid.uuid4().hex[:8]}",
            task_queue=tq,
        )
        old_result = await handle.result()
        history = await handle.fetch_history()

    assert len(_cards) == (0 if silent else 2), "the old flow carded the crossing and the outage"
    assert old_result == {"alerts": 0 if silent else 2, "problems": 2}

    # Raises on any nondeterminism between the recorded commands and the new code.
    await Replayer(workflows=[CertRadarFlow]).replay_workflow(history)
