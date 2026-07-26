"""Tests for AlertActivities signature-dedup helpers + build_alert_signature.

Covers the SQL-level dedup gate added in migration 017:

- find_open_task_for_signature: respects is_completed + missing-task fall-through
- record_signature_new_task: insert + upsert (resets stats)
- record_signature_recurrence: bumps counter; no-op on missing row
- build_alert_signature: Sentry-only with required fields
"""

from __future__ import annotations

import uuid

import pytest
from aegis_worker.activities.alerts import AlertActivities, build_alert_signature, is_infra_alert

# ---------------------------------------------------------------------------
# build_alert_signature — pure function, no DB
# ---------------------------------------------------------------------------


def test_build_signature_sentry_with_metadata_type_and_service():
    alert = {
        "source": "sentry",
        "service": "acme-data",
        "raw_payload": {"metadata": {"type": "IncompatiblePeer", "value": "..."}},
    }
    assert build_alert_signature(alert) == "sentry-class:acme-data:IncompatiblePeer"


def test_build_signature_non_sentry_returns_empty():
    alert = {
        "source": "github",
        "service": "repo/x",
        "raw_payload": {"metadata": {"type": "Whatever"}},
    }
    assert build_alert_signature(alert) == ""


def test_build_signature_missing_service_returns_empty():
    alert = {
        "source": "sentry",
        "service": "",
        "raw_payload": {"metadata": {"type": "IncompatiblePeer"}},
    }
    assert build_alert_signature(alert) == ""


def test_build_signature_missing_error_class_returns_empty():
    alert = {
        "source": "sentry",
        "service": "acme-data",
        "raw_payload": {"metadata": {"value": "no type field"}},
    }
    assert build_alert_signature(alert) == ""


def test_build_signature_non_dict_payload_returns_empty():
    alert = {"source": "sentry", "service": "x", "raw_payload": "not a dict"}
    assert build_alert_signature(alert) == ""


def test_infra_signature_is_source_independent():
    """A NodeDown from alertmanager and from the aegis heartbeat must share one
    signature, so cross-source dedup collapses them onto one open task."""
    base = {
        "title": "Swarm node noon down",
        "labels": {"alertname": "NodeDown", "cluster": "homelab-swarm"},
    }
    am = {**base, "source": "alertmanager"}
    hb = {**base, "source": "aegis-heartbeat"}
    sig_am = build_alert_signature(am, "homelab-swarm")
    sig_hb = build_alert_signature(hb, "homelab-swarm")
    assert sig_am == sig_hb
    assert sig_am == "infra-class:homelab-swarm:nodedown"


def test_heartbeat_collect_failed_is_infra_alert_without_cluster_label():
    """HeartbeatCollectFailed must be self-sufficient in INFRA_ALERTNAMES —
    unlike NodeDown/DockerServiceDown it has no per-node/service subject, so
    it can't rely on a `cluster` label match alone to classify as infra."""
    assert is_infra_alert({"labels": {"alertname": "HeartbeatCollectFailed"}}) is True


def test_remediable_signature_splits_by_service():
    """Two DockerServiceDown alerts for DIFFERENT services must get DIFFERENT
    signatures (so each gets its own @pandora task + auto-remediation); the SAME
    service maps to the SAME signature (dedup collapse)."""
    def _svc_alert(svc: str) -> dict:
        return {
            "source": "aegis-heartbeat",
            "labels": {
                "alertname": "DockerServiceDown",
                "cluster": "homelab-swarm",
                "service_name": svc,
            },
            "service": svc,
        }

    sig_a = build_alert_signature(_svc_alert("koyracloud_order-finder"), "homelab-swarm")
    sig_b = build_alert_signature(_svc_alert("trading_worker"), "homelab-swarm")
    sig_a2 = build_alert_signature(_svc_alert("koyracloud_order-finder"), "homelab-swarm")
    assert sig_a != sig_b
    assert sig_a == sig_a2
    assert sig_a == "infra-class:homelab-swarm:dockerservicedown:koyracloud_order-finder"


def test_nonremediable_infra_signature_ignores_service():
    """NodeDown is NOT remediable-per-service — its signature must still collapse
    across nodes/services (storm collapse), unchanged by the per-service rule."""
    base = {"labels": {"alertname": "NodeDown", "cluster": "homelab-swarm"}}
    a = {**base, "source": "aegis-heartbeat", "service": "noon"}
    b = {**base, "source": "aegis-heartbeat", "service": "baa"}
    assert build_alert_signature(a, "homelab-swarm") == build_alert_signature(b, "homelab-swarm")
    assert build_alert_signature(a, "homelab-swarm") == "infra-class:homelab-swarm:nodedown"


# ---------------------------------------------------------------------------
# check_dedup — resolved-aware two-flap regression (real DB)
# ---------------------------------------------------------------------------


async def _log_investigated(db_pool, fingerprint: str) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO audit_log (actor, action, target_type, target_id, details) "
            "VALUES ('alert:test', 'alert_investigated', 'alert', $1, '{}'::jsonb)",
            fingerprint,
        )


async def _log_resolved(db_pool, fingerprint: str) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO audit_log (actor, action, target_type, target_id, details) "
            "VALUES ('alert:test', 'alert_received', 'alert', $1, '{\"resolved\": \"true\"}'::jsonb)",
            fingerprint,
        )


async def _clear_audit(db_pool, fingerprint: str) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM audit_log WHERE target_id = $1", fingerprint)


@pytest.mark.asyncio
async def test_check_dedup_recovery_rearms_after_flap(db_pool):
    """A 03:00 flap: investigated THEN resolved. A later firing of the SAME
    fingerprint must NOT be treated as a duplicate — the recovery re-arms
    dedup, so the real 09:00 outage gets a fresh investigation."""
    acts = AlertActivities(db_pool=db_pool)
    fp = f"aegis-heartbeat:NodeDown:{uuid.uuid4().hex[:10]}"
    await _clear_audit(db_pool, fp)
    await _log_investigated(db_pool, fp)
    await _log_resolved(db_pool, fp)  # recovery AFTER the investigation

    res = await acts.check_dedup(fp, 24)
    assert res["is_duplicate"] is False
    await _clear_audit(db_pool, fp)


@pytest.mark.asyncio
async def test_check_dedup_still_dedups_without_recovery(db_pool):
    """Inverse: investigated with NO recovery after → still a duplicate within
    the window (an ongoing incident's re-fire is correctly suppressed)."""
    acts = AlertActivities(db_pool=db_pool)
    fp = f"aegis-heartbeat:NodeDown:{uuid.uuid4().hex[:10]}"
    await _clear_audit(db_pool, fp)
    await _log_investigated(db_pool, fp)

    res = await acts.check_dedup(fp, 24)
    assert res["is_duplicate"] is True
    await _clear_audit(db_pool, fp)


@pytest.mark.asyncio
async def test_check_dedup_resolved_before_investigation_still_dedups(db_pool):
    """A recovery row that predates the latest investigation does NOT re-arm —
    only a recovery AFTER the investigation counts as a new incident."""
    acts = AlertActivities(db_pool=db_pool)
    fp = f"aegis-heartbeat:NodeDown:{uuid.uuid4().hex[:10]}"
    await _clear_audit(db_pool, fp)
    await _log_resolved(db_pool, fp)  # stale recovery first
    await _log_investigated(db_pool, fp)  # investigation AFTER it

    res = await acts.check_dedup(fp, 24)
    assert res["is_duplicate"] is True
    await _clear_audit(db_pool, fp)


# ---------------------------------------------------------------------------
# DB-backed activity helpers
# ---------------------------------------------------------------------------


async def _seed_task(db_pool, task_id: str, *, is_completed: bool) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO todoist_projects (id, name, is_managed, raw) "
            "VALUES ('P_INBOX', 'Inbox', true, '{}'::jsonb) "
            "ON CONFLICT (id) DO NOTHING"
        )
        await conn.execute("DELETE FROM todoist_tasks WHERE id = $1", task_id)
        await conn.execute(
            """
            INSERT INTO todoist_tasks
                (id, project_id, content, labels, is_completed, raw)
            VALUES ($1, 'P_INBOX', 'test task', $2, $3, '{}'::jsonb)
            """,
            task_id,
            ["@pandora", "#alert"],
            is_completed,
        )


async def _clear_dedup(db_pool, signature: str) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM alert_dedup_index WHERE signature = $1", signature)


@pytest.mark.asyncio
async def test_find_open_task_returns_none_when_no_binding(db_pool):
    acts = AlertActivities(db_pool=db_pool)
    sig = f"sentry-class:svc:{uuid.uuid4().hex}"
    await _clear_dedup(db_pool, sig)
    assert await acts.find_open_task_for_signature(sig) is None


@pytest.mark.asyncio
async def test_find_open_task_returns_id_for_open_task(db_pool):
    acts = AlertActivities(db_pool=db_pool)
    sig = f"sentry-class:svc:{uuid.uuid4().hex}"
    task_id = f"task-{uuid.uuid4().hex[:12]}"
    await _seed_task(db_pool, task_id, is_completed=False)
    await _clear_dedup(db_pool, sig)
    await acts.record_signature_new_task(sig, task_id)
    assert await acts.find_open_task_for_signature(sig) == task_id


@pytest.mark.asyncio
async def test_find_open_task_returns_none_when_task_completed(db_pool):
    acts = AlertActivities(db_pool=db_pool)
    sig = f"sentry-class:svc:{uuid.uuid4().hex}"
    task_id = f"task-{uuid.uuid4().hex[:12]}"
    await _seed_task(db_pool, task_id, is_completed=True)
    await _clear_dedup(db_pool, sig)
    await acts.record_signature_new_task(sig, task_id)
    assert await acts.find_open_task_for_signature(sig) is None


@pytest.mark.asyncio
async def test_find_open_task_returns_none_when_task_missing_from_projection(db_pool):
    acts = AlertActivities(db_pool=db_pool)
    sig = f"sentry-class:svc:{uuid.uuid4().hex}"
    missing_task_id = f"missing-{uuid.uuid4().hex[:12]}"
    await _clear_dedup(db_pool, sig)
    # Binding exists, but no row in todoist_tasks → JOIN drops it.
    await acts.record_signature_new_task(sig, missing_task_id)
    assert await acts.find_open_task_for_signature(sig) is None


@pytest.mark.asyncio
async def test_record_signature_new_task_upsert_resets_stats(db_pool):
    acts = AlertActivities(db_pool=db_pool)
    sig = f"sentry-class:svc:{uuid.uuid4().hex}"
    first_id = f"task-{uuid.uuid4().hex[:12]}"
    second_id = f"task-{uuid.uuid4().hex[:12]}"
    await _seed_task(db_pool, first_id, is_completed=False)
    await _seed_task(db_pool, second_id, is_completed=False)
    await _clear_dedup(db_pool, sig)

    await acts.record_signature_new_task(sig, first_id)
    await acts.record_signature_recurrence(sig)
    await acts.record_signature_recurrence(sig)

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT task_id, occurrence_count FROM alert_dedup_index WHERE signature = $1",
            sig,
        )
    assert row["task_id"] == first_id
    assert row["occurrence_count"] == 3

    # Upsert with a new task id → counter resets, task rebinds
    await acts.record_signature_new_task(sig, second_id)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT task_id, occurrence_count FROM alert_dedup_index WHERE signature = $1",
            sig,
        )
    assert row["task_id"] == second_id
    assert row["occurrence_count"] == 1


@pytest.mark.asyncio
async def test_record_signature_recurrence_no_op_on_missing_row(db_pool):
    acts = AlertActivities(db_pool=db_pool)
    sig = f"sentry-class:svc:{uuid.uuid4().hex}"
    await _clear_dedup(db_pool, sig)
    # Should not raise.
    await acts.record_signature_recurrence(sig)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT 1 FROM alert_dedup_index WHERE signature = $1", sig)
    assert row is None


@pytest.mark.asyncio
async def test_find_open_task_returns_none_when_signature_empty(db_pool):
    acts = AlertActivities(db_pool=db_pool)
    assert await acts.find_open_task_for_signature("") is None


@pytest.mark.asyncio
async def test_record_signature_new_task_noop_when_args_empty(db_pool):
    acts = AlertActivities(db_pool=db_pool)
    # Should not raise nor write a row with empty key.
    await acts.record_signature_new_task("", "task-1")
    await acts.record_signature_new_task("sig-x", "")
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM alert_dedup_index WHERE signature IN ('', 'sig-x')"
        )
    assert row is None
