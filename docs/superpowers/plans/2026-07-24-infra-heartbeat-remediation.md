# Infra Heartbeat & Approve-to-Run Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the three infra gaps from the 2026-07-24 outage: fast internal node/service liveness detection (2-min poll), Slack escalation-until-ack for critical infra alerts, and human-approved command execution ("Run fix") from the Gate-2 card — plus a content-route bridge so hand-captured Todoist infra tasks trigger investigations.

**Architecture:** A new 2-min `InfraHeartbeatFlow` (Temporal schedule, gated by `homelab_enabled`) polls swarm node + service state via `HomelabConnector`, stores a snapshot in a `settings` row, and on transitions spawns `AlertInvestigationFlow` as ABANDONED children with synthetic alerts (`source=aegis-heartbeat`). Recovery transitions write the same `audit_log` "resolved" rows the Alertmanager webhook writes, so all existing self-resolve machinery works unchanged. `InteractionFlow` gains an optional escalation loop (re-dispatch card with Slack @-mention). Gate 2 for infra alerts gains a "Run fix" option executing kimi-proposed commands via `RemoteScriptConnector.run_on_host`. Content routes gain an `alert_overrides` field.

**Tech Stack:** Python 3.12, temporalio (workflows + `ActivityEnvironment`/`WorkflowEnvironment.start_time_skipping()` tests), asyncpg, pytest (asyncio_mode=auto), ruff.

**Spec:** `docs/superpowers/specs/2026-07-24-infra-heartbeat-remediation-design.md`

## Global Constraints

- **Worktree venv trap:** the repo `.venv` editable-installs resolve to the MAIN checkout. Every pytest run in this worktree MUST use `PYTHONPATH=core/src:worker/src:comms/src pytest …` (the root conftest guard errors otherwise).
- **Tee test output:** always run tests as `… 2>&1 | tee logs/test-<task>.log` (create `logs/` if missing; it is gitignored — verify with `git status`, never commit logs).
- **DB tests need Postgres:** `docker compose up -d postgres` (port 25432) must be running. Activity tests here use mocks/`ActivityEnvironment`; flow tests use time-skipping env (no DB).
- **Commits:** single-line semantic messages (`feat: …`, `test: …`), no co-author lines.
- **Worker registration is TWO explicit lists** in `worker/src/aegis_worker/__main__.py` (`workflows = [...]` AND `activities = [...]`) — registering a flow but not its activities (or vice versa) is a known prod-breaking mistake (PR #90).
- **JSONB:** pass Python dicts/lists directly to asyncpg — no `::jsonb` cast, no `json.dumps()`.
- **Workflow code is sandboxed:** imports used by workflow files go inside `with workflow.unsafe.imports_passed_through():`; no `datetime.now()`/`date.today()` — use `workflow.now()`.
- **Lint:** `ruff check .` must pass before every commit. Do NOT run `ruff format` on `core/src/aegis/services/chat.py` (not touched by this plan anyway).
- Retry/timeout constants come from `aegis_worker.shared.retry`: `FAST`, `NO_RETRY`, `ACT_RETRY`, `TIMEOUT_FAST` (15s), `TIMEOUT_STANDARD` (60s), `TIMEOUT_LONG` (300s).

---

### Task 1: `HomelabConnector.list_nodes()`

**Files:**
- Modify: `core/src/aegis/connectors/homelab.py` (append after `service_ps`, ~line 148)
- Test: `tests/core/connectors/test_homelab.py` (append)

**Interfaces:**
- Consumes: existing `HomelabConnector._docker()` helper and `_envelope()`.
- Produces: `async def list_nodes(self) -> dict` — envelope whose `data` is `list[{"hostname": str, "status": str, "availability": str, "manager": str}]`. Task 3 consumes this.

- [ ] **Step 1: Write the failing tests** (append to `tests/core/connectors/test_homelab.py`, mirroring the existing `test_list_services_returns_envelope` monkeypatch style — read that test first and reuse its `_docker` stubbing approach):

```python
@pytest.mark.asyncio
async def test_list_nodes_returns_envelope(monkeypatch):
    conn = HomelabConnector(docker_context="")
    lines = (
        '{"Hostname": "baa", "Status": "Ready", "Availability": "Active", "ManagerStatus": "Leader"}\n'
        '{"Hostname": "noon", "Status": "Down", "Availability": "Active", "ManagerStatus": ""}\n'
    )

    async def fake_docker(*args, timeout=30):
        assert args == ("node", "ls", "--format", "{{json .}}")
        return (0, lines, "")

    monkeypatch.setattr(conn, "_docker", fake_docker)
    env = await conn.list_nodes()
    assert env["ok"] is True
    assert env["data"] == [
        {"hostname": "baa", "status": "Ready", "availability": "Active", "manager": "Leader"},
        {"hostname": "noon", "status": "Down", "availability": "Active", "manager": ""},
    ]


@pytest.mark.asyncio
async def test_list_nodes_failure_retryable(monkeypatch):
    conn = HomelabConnector(docker_context="")

    async def fake_docker(*args, timeout=30):
        return (1, "", "cannot connect")

    monkeypatch.setattr(conn, "_docker", fake_docker)
    env = await conn.list_nodes()
    assert env["ok"] is False
    assert env["retryable"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=core/src:worker/src:comms/src pytest tests/core/connectors/test_homelab.py -x 2>&1 | tee logs/test-task1.log`
Expected: FAIL — `AttributeError: 'HomelabConnector' object has no attribute 'list_nodes'`

- [ ] **Step 3: Implement** (in `core/src/aegis/connectors/homelab.py`, after `service_ps`):

```python
    async def list_nodes(self) -> dict:
        """Return swarm nodes. Shape per item:
        {hostname, status, availability, manager}. status is Ready|Down."""
        rc, out, err = await self._docker("node", "ls", "--format", "{{json .}}")
        if rc != 0:
            return _envelope(False, error=f"docker node ls failed: {err[:200]}", retryable=True)
        nodes = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                n = json.loads(line)
            except json.JSONDecodeError:
                continue
            nodes.append(
                {
                    "hostname": n.get("Hostname", ""),
                    "status": n.get("Status", ""),
                    "availability": n.get("Availability", ""),
                    "manager": n.get("ManagerStatus", ""),
                }
            )
        return _envelope(True, data=nodes)
```

- [ ] **Step 4: Run tests to verify they pass** (same command). Expected: PASS (all tests in file).
- [ ] **Step 5: Lint + commit**

```bash
ruff check . && git add core/src/aegis/connectors/homelab.py tests/core/connectors/test_homelab.py && git commit -m "feat: add HomelabConnector.list_nodes for swarm node liveness"
```

---

### Task 2: Source-independent infra alert signatures

Heartbeat alerts (`source=aegis-heartbeat`) must collapse onto the SAME signature as Alertmanager's own NodeDown/DockerServiceDown so the two sources never spawn duplicate investigations of one outage.

**Files:**
- Modify: `worker/src/aegis_worker/activities/alerts.py` — `build_alert_signature` infra branch (~lines 158–175)
- Test: `tests/worker/test_alert_signature_activities.py` (update existing infra expectations + add cross-source test)

**Interfaces:**
- Produces: infra-alert signatures now shaped `infra-class:{cluster}:{alertname}` for sources `{alertmanager, prometheus, grafana, aegis-heartbeat}`. Non-infra signatures unchanged. Tasks 4 relies on `aegis-heartbeat` being an accepted source.

- [ ] **Step 1: Read the current infra branch.** In `build_alert_signature`, the branch `if source in {"alertmanager", "prometheus", "grafana"}:` computes (for `is_infra_alert(...)` alerts) a signature that interpolates `source` (read lines 158–185 to see the exact current return). Also read the existing tests: `grep -n "infra\|signature" tests/worker/test_alert_signature_activities.py`.

- [ ] **Step 2: Write the failing test** (append to `tests/worker/test_alert_signature_activities.py`):

```python
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
```

(Import `build_alert_signature` the same way the file already does.)

- [ ] **Step 3: Run to verify it fails**

Run: `PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/test_alert_signature_activities.py -x 2>&1 | tee logs/test-task2.log`
Expected: FAIL (heartbeat source returns `""` today, and the am prefix is source-interpolated).

- [ ] **Step 4: Implement.** In `build_alert_signature`:
  1. Change the source guard to `if source in {"alertmanager", "prometheus", "grafana", "aegis-heartbeat"}:`.
  2. Inside the `is_infra_alert(alert, infra_cluster)` sub-branch, change the returned signature's prefix from the source-interpolated form to the literal `infra-class`: `return f"infra-class:{cluster}:{subkey}"` (keep the existing `cluster`/`subkey` computation and empty-subkey fallback exactly as-is).
  3. Non-infra alertmanager/grafana signatures keep their current shape — do not touch that return.
  Update any existing test in the file asserting the old `alertmanager-class:` / source-prefixed infra shape to the new `infra-class:` literal (the test names will surface in the failure output).
  Note for review: rows already in `alert_dedup_index` under old-prefix signatures simply age out; a one-time missed recurrence-match is accepted (spec §Decisions).

- [ ] **Step 5: Run the file + the two dedup-flow suites to verify nothing else keyed on the old prefix:**

Run: `PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/test_alert_signature_activities.py tests/worker/flows/test_alert_signature_dedup_flow.py tests/worker/test_alert_infra_routing.py 2>&1 | tee logs/test-task2b.log`
Expected: PASS.

- [ ] **Step 6: Lint + commit**

```bash
ruff check . && git add worker/src/aegis_worker/activities/alerts.py tests/worker/test_alert_signature_activities.py && git commit -m "feat: source-independent infra-class alert signatures incl aegis-heartbeat"
```

---

### Task 3: Heartbeat activities on `HomelabActivities`

**Files:**
- Modify: `worker/src/aegis_worker/activities/homelab.py` (new dataclass fields + 5 activities)
- Test: `tests/worker/activities/test_heartbeat_activities.py` (create)

**Interfaces:**
- Consumes: Task 1's `HomelabConnector.list_nodes()`; existing `list_services()`; `aegis.audit.log_audit` (already imported in `alerts.py` — check `homelab.py`'s imports and add `from aegis.audit import log_audit` if absent).
- Produces (Task 4 consumes all of these via `execute_activity_method`):
  - new dataclass fields: `heartbeat_ping_url: str = ""`, `infra_cluster: str = ""`
  - `collect_infra_state() -> dict` — `{"ok": bool, "nodes": {hostname: status}, "stuck": [service_name…], "error": str}`
  - `read_heartbeat_state() -> dict` — `{"nodes": {}, "stuck": [], "confirmed": [], "fail_count": 0}` when unset
  - `write_heartbeat_state(state: dict) -> None`
  - `record_heartbeat_resolved(fingerprint: str) -> None` — writes the audit row `check_alert_resolved` looks for
  - `ping_deadman() -> dict` — `{"pinged": bool}`
  - `get_heartbeat_routing() -> dict` — `{"infra_cluster": str}` (workflows can't read settings)

- [ ] **Step 1: Write the failing tests** (create `tests/worker/activities/test_heartbeat_activities.py`; follow `test_homelab.py`'s `AsyncMock` style):

```python
from unittest.mock import AsyncMock

import pytest

from aegis_worker.activities.homelab import HomelabActivities


def _act(homelab=None, db_pool=None, **kw):
    return HomelabActivities(db_pool=db_pool, homelab=homelab, delivery=AsyncMock(), **kw)


@pytest.mark.asyncio
async def test_collect_infra_state_merges_nodes_and_stuck_services():
    homelab = AsyncMock()
    homelab.list_nodes.return_value = {
        "ok": True,
        "data": [
            {"hostname": "baa", "status": "Ready", "availability": "Active", "manager": "Leader"},
            {"hostname": "noon", "status": "Down", "availability": "Active", "manager": ""},
        ],
    }
    homelab.list_services.return_value = {
        "ok": True,
        "data": [
            {"name": "koyracloud_order-finder", "replicas_actual": 0, "replicas_desired": 1},
            {"name": "aegis_core", "replicas_actual": 1, "replicas_desired": 1},
            {"name": "batch_job", "replicas_actual": 0, "replicas_desired": 0},
        ],
    }
    state = await _act(homelab=homelab).collect_infra_state()
    assert state["ok"] is True
    assert state["nodes"] == {"baa": "Ready", "noon": "Down"}
    assert state["stuck"] == ["koyracloud_order-finder"]  # desired>0 only


@pytest.mark.asyncio
async def test_collect_infra_state_node_failure_is_not_ok():
    homelab = AsyncMock()
    homelab.list_nodes.return_value = {"ok": False, "error": "ssh timeout", "data": None}
    state = await _act(homelab=homelab).collect_infra_state()
    assert state["ok"] is False
    assert "ssh timeout" in state["error"]


@pytest.mark.asyncio
async def test_read_heartbeat_state_defaults_when_unset():
    pool = AsyncMock()
    pool.fetchrow.return_value = None
    state = await _act(db_pool=pool).read_heartbeat_state()
    assert state == {"nodes": {}, "stuck": [], "confirmed": [], "fail_count": 0}


@pytest.mark.asyncio
async def test_write_then_read_roundtrip_shape():
    pool = AsyncMock()
    act = _act(db_pool=pool)
    await act.write_heartbeat_state({"nodes": {"baa": "Ready"}, "stuck": [], "confirmed": [], "fail_count": 0})
    sql = pool.execute.await_args.args[0]
    assert "infra_heartbeat_state" in pool.execute.await_args.args
    assert "ON CONFLICT" in sql


@pytest.mark.asyncio
async def test_record_heartbeat_resolved_writes_alert_received_resolved_row():
    pool = AsyncMock()
    act = _act(db_pool=pool)
    await act.record_heartbeat_resolved("aegis-heartbeat:NodeDown:noon")
    # log_audit inserts into audit_log; assert via the pool call it makes
    assert pool.execute.await_count + pool.fetchrow.await_count + pool.fetchval.await_count >= 1


@pytest.mark.asyncio
async def test_ping_deadman_noop_without_url():
    result = await _act().ping_deadman()
    assert result == {"pinged": False}
```

- [ ] **Step 2: Run to verify they fail**

Run: `PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/activities/test_heartbeat_activities.py 2>&1 | tee logs/test-task3.log`
Expected: FAIL — missing attributes.

- [ ] **Step 3: Implement.** In `worker/src/aegis_worker/activities/homelab.py`: add to the `HomelabActivities` dataclass fields:

```python
    heartbeat_ping_url: str = ""  # healthchecks.io dead-man URL; "" = disabled
    infra_cluster: str = ""       # Prometheus cluster label for synthetic alerts
```

Add the activities (append inside the class; add `import httpx` and `from aegis.audit import log_audit` at module top if not already imported — check first):

```python
    _HEARTBEAT_STATE_KEY = "infra_heartbeat_state"
    _HEARTBEAT_STATE_DEFAULT = {"nodes": {}, "stuck": [], "confirmed": [], "fail_count": 0}

    @activity.defn
    async def collect_infra_state(self) -> dict:
        """One heartbeat sample: node statuses + services stuck below desired."""
        if not self.homelab:
            return {"ok": False, "nodes": {}, "stuck": [], "error": "no_homelab_connector"}
        nodes_env = await self.homelab.list_nodes()
        if not nodes_env.get("ok"):
            return {"ok": False, "nodes": {}, "stuck": [], "error": str(nodes_env.get("error"))[:200]}
        svc_env = await self.homelab.list_services()
        if not svc_env.get("ok"):
            return {"ok": False, "nodes": {}, "stuck": [], "error": str(svc_env.get("error"))[:200]}
        nodes = {n["hostname"]: n["status"] for n in nodes_env.get("data") or [] if n.get("hostname")}
        stuck = sorted(
            s["name"]
            for s in svc_env.get("data") or []
            if (s.get("replicas_desired") or 0) > 0
            and (s.get("replicas_actual") or 0) < (s.get("replicas_desired") or 0)
        )
        return {"ok": True, "nodes": nodes, "stuck": stuck, "error": ""}

    @activity.defn
    async def read_heartbeat_state(self) -> dict:
        if not self.db_pool:
            return dict(self._HEARTBEAT_STATE_DEFAULT)
        row = await self.db_pool.fetchrow(
            "SELECT value FROM settings WHERE key = $1", self._HEARTBEAT_STATE_KEY
        )
        if not row or not row["value"]:
            return dict(self._HEARTBEAT_STATE_DEFAULT)
        value = row["value"]
        return {**self._HEARTBEAT_STATE_DEFAULT, **value} if isinstance(value, dict) else dict(
            self._HEARTBEAT_STATE_DEFAULT
        )

    @activity.defn
    async def write_heartbeat_state(self, state: dict) -> None:
        if not self.db_pool:
            return
        await self.db_pool.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES ($1, $2, NOW()) "
            "ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()",
            self._HEARTBEAT_STATE_KEY,
            state,
        )

    @activity.defn
    async def record_heartbeat_resolved(self, fingerprint: str) -> None:
        """Mirror the webhook's resolved-alert audit row so check_alert_resolved
        (and thus the whole self-resolve machinery) works for heartbeat alerts."""
        if not self.db_pool or not fingerprint:
            return
        await log_audit(
            self.db_pool,
            actor="alert:aegis-heartbeat",
            action="alert_received",
            target_type="alert",
            target_id=fingerprint,
            details={"resolved": "true"},
        )

    @activity.defn
    async def ping_deadman(self) -> dict:
        """Fire-and-forget healthchecks.io ping. Only called on a SUCCESSFUL
        collect, so a silent heartbeat (AEGIS/node death) stops the pings."""
        if not self.heartbeat_ping_url:
            return {"pinged": False}
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                await client.get(self.heartbeat_ping_url)
            return {"pinged": True}
        except Exception as exc:  # noqa: BLE001 — dead-man ping is never fatal
            activity.logger.warning("heartbeat_deadman_ping_failed err=%s", str(exc)[:200])
            return {"pinged": False}

    @activity.defn
    async def get_heartbeat_routing(self) -> dict:
        """Settings-derived knobs for the flow (workflows can't read Settings)."""
        return {"infra_cluster": self.infra_cluster}
```

If `log_audit`'s signature differs (check `core/src/aegis/audit.py` — `resolve_infra_resource`'s neighbour `log_alert` in `alerts.py:999` shows the calling convention), match it exactly.

- [ ] **Step 4: Run to verify pass** (same command). Expected: PASS. If `test_record_heartbeat_resolved…` fails on the assert, adjust it to assert whatever pool method `log_audit` actually uses (read `aegis/audit.py`) — the point is only that a row write was attempted with the fingerprint.
- [ ] **Step 5: Also run the existing homelab activity tests:** `PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/activities/test_homelab.py 2>&1 | tee logs/test-task3b.log` — PASS (fields have defaults, nothing breaks).
- [ ] **Step 6: Lint + commit**

```bash
ruff check . && git add worker/src/aegis_worker/activities/homelab.py tests/worker/activities/test_heartbeat_activities.py && git commit -m "feat: heartbeat collect/state/resolved/deadman activities on HomelabActivities"
```

---

### Task 4: `InfraHeartbeatFlow`

**Files:**
- Create: `worker/src/aegis_worker/flows/infra_heartbeat.py`
- Test: `tests/worker/flows/test_infra_heartbeat_flow.py` (create)

**Interfaces:**
- Consumes: Task 3 activities; `AlertInvestigationFlow` (child spawn, ABANDON — same pattern as `sentry_poll.py:127`).
- Produces: `InfraHeartbeatConfig(agent_id: str = "pandoras-actor", fail_threshold: int = 3)`; `InfraHeartbeatFlow.run(config) -> dict`; pure helpers `_hb_fingerprint(alertname, subject)` and `build_heartbeat_alert(...)`. Task 5 registers them.

**Semantics (from spec):** emit only on transitions. Node `→ Down` (from Ready/absent-and-Down) → NodeDown alert (escalate=True). Node `Down → Ready` → resolved-row write. Service stuck **two consecutive ticks** (debounce) → DockerServiceDown alert (escalate=False — the existing auto-remediation path owns it); service leaving the confirmed-stuck set → resolved-row write. Collect failing `fail_threshold` consecutive ticks → one HeartbeatCollectFailed alert (escalate=True); first success after failures → its resolved-row. Dead-man ping only on successful ticks.

- [ ] **Step 1: Write the failing tests.** Time-skipping env + stub activities registered by NAME (the flow calls them via `execute_activity_method` on the real classes, so stub with matching `@activity.defn(name=...)` functions — mirror `tests/worker/flows/test_alert_investigation_gates.py`'s structure: module-level `_state`/`_calls` dicts, `_reset()`, one `Worker` with `AlertInvestigationFlow` replaced by a recording stub workflow). Create `tests/worker/flows/test_infra_heartbeat_flow.py`:

```python
"""InfraHeartbeatFlow — transition matrix.

Covers: first-sight Down fires once; steady Down fires nothing; recovery
writes resolved row; stuck service needs 2 consecutive ticks; collect
failure threshold; dead-man only on success.
"""

from __future__ import annotations

import uuid

from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from aegis_worker.flows.infra_heartbeat import (
        InfraHeartbeatConfig,
        InfraHeartbeatFlow,
        _hb_fingerprint,
    )

_calls: dict = {}
_state: dict = {}


def _reset(collect: dict, prior: dict | None = None):
    _calls.clear()
    _calls.update({"spawned": [], "resolved": [], "written": [], "pinged": 0})
    _state.clear()
    _state["collect"] = collect
    _state["prior"] = prior or {"nodes": {}, "stuck": [], "confirmed": [], "fail_count": 0}


@activity.defn(name="collect_infra_state")
async def _collect() -> dict:
    return _state["collect"]


@activity.defn(name="read_heartbeat_state")
async def _read() -> dict:
    return _state["prior"]


@activity.defn(name="write_heartbeat_state")
async def _write(state: dict) -> None:
    _calls["written"].append(state)


@activity.defn(name="record_heartbeat_resolved")
async def _resolved(fingerprint: str) -> None:
    _calls["resolved"].append(fingerprint)


@activity.defn(name="ping_deadman")
async def _ping() -> dict:
    _calls["pinged"] += 1
    return {"pinged": True}


@activity.defn(name="get_heartbeat_routing")
async def _routing() -> dict:
    return {"infra_cluster": "homelab-swarm"}


@workflow.defn(name="AlertInvestigationFlow", sandboxed=False)
class _StubAlertFlow:
    @workflow.run
    async def run(self, alert: dict) -> dict:
        _calls["spawned"].append(alert)
        return {"status": "stub"}


_ACTS = [_collect, _read, _write, _resolved, _ping, _routing]


async def _run(config: InfraHeartbeatConfig | None = None) -> dict:
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=f"hb-{uuid.uuid4()}",
            workflows=[InfraHeartbeatFlow, _StubAlertFlow],
            activities=_ACTS,
        ) as worker:
            return await env.client.execute_workflow(
                InfraHeartbeatFlow.run,
                config or InfraHeartbeatConfig(),
                id=f"hb-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )


async def test_node_down_fires_once_with_escalate():
    _reset({"ok": True, "nodes": {"baa": "Ready", "noon": "Down"}, "stuck": [], "error": ""})
    result = await _run()
    assert result["alerts_spawned"] == 1
    alert = _calls["spawned"][0]
    assert alert["labels"]["alertname"] == "NodeDown"
    assert alert["fingerprint"] == _hb_fingerprint("NodeDown", "noon")
    assert alert["source"] == "aegis-heartbeat"
    assert alert["escalate"] is True
    assert alert["labels"]["cluster"] == "homelab-swarm"
    assert _calls["pinged"] == 1
    assert _calls["written"][0]["nodes"] == {"baa": "Ready", "noon": "Down"}


async def test_steady_down_fires_nothing():
    prior = {"nodes": {"baa": "Ready", "noon": "Down"}, "stuck": [], "confirmed": [], "fail_count": 0}
    _reset({"ok": True, "nodes": {"baa": "Ready", "noon": "Down"}, "stuck": [], "error": ""}, prior)
    result = await _run()
    assert result["alerts_spawned"] == 0
    assert _calls["resolved"] == []


async def test_recovery_writes_resolved_row_and_no_alert():
    prior = {"nodes": {"noon": "Down"}, "stuck": [], "confirmed": [], "fail_count": 0}
    _reset({"ok": True, "nodes": {"noon": "Ready"}, "stuck": [], "error": ""}, prior)
    result = await _run()
    assert result["alerts_spawned"] == 0
    assert _calls["resolved"] == [_hb_fingerprint("NodeDown", "noon")]


async def test_stuck_service_needs_two_consecutive_ticks():
    _reset({"ok": True, "nodes": {}, "stuck": ["koyracloud_order-finder"], "error": ""})
    await _run()
    assert _calls["spawned"] == []  # first sight — debounce
    prior = _calls["written"][0]
    assert prior["stuck"] == ["koyracloud_order-finder"]
    _reset({"ok": True, "nodes": {}, "stuck": ["koyracloud_order-finder"], "error": ""}, prior)
    await _run()
    assert len(_calls["spawned"]) == 1
    alert = _calls["spawned"][0]
    assert alert["labels"]["alertname"] == "DockerServiceDown"
    assert alert["labels"]["service_name"] == "koyracloud_order-finder"
    assert alert["escalate"] is False


async def test_confirmed_stuck_service_recovery_writes_resolved():
    prior = {"nodes": {}, "stuck": ["svc_a"], "confirmed": ["svc_a"], "fail_count": 0}
    _reset({"ok": True, "nodes": {}, "stuck": [], "error": ""}, prior)
    await _run()
    assert _calls["resolved"] == [_hb_fingerprint("DockerServiceDown", "svc_a")]


async def test_collect_failure_threshold_fires_once_and_no_ping():
    prior = {"nodes": {}, "stuck": [], "confirmed": [], "fail_count": 2}
    _reset({"ok": False, "nodes": {}, "stuck": [], "error": "ssh dead"}, prior)
    result = await _run(InfraHeartbeatConfig(fail_threshold=3))
    assert result["collect_ok"] is False
    assert len(_calls["spawned"]) == 1
    assert _calls["spawned"][0]["labels"]["alertname"] == "HeartbeatCollectFailed"
    assert _calls["pinged"] == 0
    assert _calls["written"][0]["fail_count"] == 3
    # 4th consecutive failure: no second alert
    _reset({"ok": False, "nodes": {}, "stuck": [], "error": "ssh dead"}, _calls["written"][0])
    # careful: _reset cleared _calls; re-prime prior BEFORE _reset in real code:
    # (structure the test so prior is captured before _reset)


async def test_collect_recovery_resolves_collect_alert():
    prior = {"nodes": {}, "stuck": [], "confirmed": [], "fail_count": 5}
    _reset({"ok": True, "nodes": {"baa": "Ready"}, "stuck": [], "error": ""}, prior)
    await _run()
    assert _hb_fingerprint("HeartbeatCollectFailed", "collect") in _calls["resolved"]
    assert _calls["written"][0]["fail_count"] == 0
```

(In `test_collect_failure_threshold…`, capture `_calls["written"][0]` into a local variable before calling `_reset` again — fix the ordering noted in the comment when writing the real file, and assert the 4th failure spawns nothing.)

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/flows/test_infra_heartbeat_flow.py 2>&1 | tee logs/test-task4.log`
Expected: FAIL — `ModuleNotFoundError: aegis_worker.flows.infra_heartbeat`

- [ ] **Step 3: Implement** `worker/src/aegis_worker/flows/infra_heartbeat.py`:

```python
"""InfraHeartbeatFlow — 2-min swarm liveness/convergence poll (spec 2026-07-24).

Emits ONLY on state transitions, never steady state:
- node → Down (first sight counts)          → NodeDown alert (escalate)
- node Down → Ready                          → resolved audit row
- service stuck 2 consecutive ticks          → DockerServiceDown alert
  (routes into the existing auto-remediation in AlertInvestigationFlow)
- confirmed-stuck service converged          → resolved audit row
- collect failed `fail_threshold` in a row   → HeartbeatCollectFailed alert
- collect recovered                          → its resolved audit row

Recovery writes the same audit_log row shape the Alertmanager webhook writes
(action=alert_received, details.resolved=true), so check_alert_resolved and
the whole self-resolve machinery work unchanged for heartbeat alerts.

Children are spawned ABANDONED (sentry_poll pattern) — investigations carry
human gates and must outlive this 2-min tick. Dead-man ping fires only on a
successful collect so a dead AEGIS/node silences healthchecks.io.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.homelab import HomelabActivities
    from aegis_worker.flows.alert_investigation import AlertInvestigationFlow
    from aegis_worker.shared.retry import FAST, NO_RETRY, TIMEOUT_FAST, TIMEOUT_STANDARD


def _safe_id_segment(text: str, max_len: int = 60) -> str:
    return re.sub(r"[^a-zA-Z0-9._\-]", "-", text)[:max_len]


def _hb_fingerprint(alertname: str, subject: str) -> str:
    return f"aegis-heartbeat:{alertname}:{subject}"


def build_heartbeat_alert(
    alertname: str,
    subject: str,
    infra_cluster: str,
    title: str,
    description: str,
    *,
    escalate: bool,
    service_name: str = "",
) -> dict:
    labels: dict = {"alertname": alertname}
    if infra_cluster:
        labels["cluster"] = infra_cluster
    if service_name:
        labels["service_name"] = service_name
    alert: dict = {
        "title": title,
        "description": description,
        "source": "aegis-heartbeat",
        "severity": "critical",
        "fingerprint": _hb_fingerprint(alertname, subject),
        "labels": labels,
        "escalate": escalate,
    }
    if service_name:
        alert["service"] = service_name
    return alert


@dataclass
class InfraHeartbeatConfig:
    agent_id: str = "pandoras-actor"
    fail_threshold: int = 3


@workflow.defn
class InfraHeartbeatFlow:
    async def _spawn(self, alert: dict) -> bool:
        """ABANDONED child; an already-running same-id child is benign."""
        child_id = (
            f"aegis-heartbeat-{_safe_id_segment(alert['labels']['alertname'].lower())}-"
            f"{_safe_id_segment(alert['fingerprint'].rsplit(':', 1)[-1])}"
        )
        try:
            await workflow.start_child_workflow(
                AlertInvestigationFlow.run,
                alert,
                id=child_id,
                parent_close_policy=workflow.ParentClosePolicy.ABANDON,
            )
            return True
        except Exception as exc:  # noqa: BLE001 — already-started dedup is benign
            workflow.logger.warning(
                "heartbeat_spawn_skipped id=%s err=%s", child_id, str(exc)[:200]
            )
            return False

    @workflow.run
    async def run(self, config: InfraHeartbeatConfig) -> dict:
        prior = await workflow.execute_activity_method(
            HomelabActivities.read_heartbeat_state,
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=FAST,
        )
        current = await workflow.execute_activity_method(
            HomelabActivities.collect_infra_state,
            start_to_close_timeout=TIMEOUT_STANDARD,
            retry_policy=FAST,
        )
        routing = await workflow.execute_activity_method(
            HomelabActivities.get_heartbeat_routing,
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=NO_RETRY,
        )
        cluster = routing.get("infra_cluster") or ""
        spawned = 0

        # ── Collect failure path ──
        if not current.get("ok"):
            fail_count = int(prior.get("fail_count") or 0) + 1
            if fail_count == config.fail_threshold:
                alert = build_heartbeat_alert(
                    "HeartbeatCollectFailed",
                    "collect",
                    cluster,
                    "Infra heartbeat cannot reach the swarm",
                    f"{fail_count} consecutive collect failures. "
                    f"Last error: {current.get('error', '')}",
                    escalate=True,
                )
                if await self._spawn(alert):
                    spawned += 1
            await workflow.execute_activity_method(
                HomelabActivities.write_heartbeat_state,
                args=[{**prior, "fail_count": fail_count}],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=FAST,
            )
            return {"collect_ok": False, "alerts_spawned": spawned, "fail_count": fail_count}

        # ── Success path: diff transitions ──
        prev_nodes: dict = prior.get("nodes") or {}
        cur_nodes: dict = current.get("nodes") or {}
        prev_stuck = set(prior.get("stuck") or [])
        prev_confirmed = set(prior.get("confirmed") or [])
        cur_stuck = set(current.get("stuck") or [])

        nodes_down, nodes_recovered = [], []
        for name, status in cur_nodes.items():
            if status == "Down" and prev_nodes.get(name) != "Down":
                nodes_down.append(name)
            elif status == "Ready" and prev_nodes.get(name) == "Down":
                nodes_recovered.append(name)

        new_confirmed = (cur_stuck & prev_stuck) - prev_confirmed
        confirmed_now = (prev_confirmed | new_confirmed) & cur_stuck
        recovered_services = prev_confirmed - cur_stuck

        for node in nodes_down:
            alert = build_heartbeat_alert(
                "NodeDown",
                node,
                cluster,
                f"Swarm node {node} down",
                f"Heartbeat poll saw node {node} transition to Down.",
                escalate=True,
            )
            if await self._spawn(alert):
                spawned += 1
        for svc in sorted(new_confirmed):
            alert = build_heartbeat_alert(
                "DockerServiceDown",
                svc,
                cluster,
                f"Service {svc} down",
                f"Heartbeat poll saw {svc} below desired replicas for 2 consecutive ticks.",
                escalate=False,
                service_name=svc,
            )
            if await self._spawn(alert):
                spawned += 1

        for node in nodes_recovered:
            await workflow.execute_activity_method(
                HomelabActivities.record_heartbeat_resolved,
                args=[_hb_fingerprint("NodeDown", node)],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=NO_RETRY,
            )
        for svc in sorted(recovered_services):
            await workflow.execute_activity_method(
                HomelabActivities.record_heartbeat_resolved,
                args=[_hb_fingerprint("DockerServiceDown", svc)],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=NO_RETRY,
            )
        if int(prior.get("fail_count") or 0) >= config.fail_threshold:
            await workflow.execute_activity_method(
                HomelabActivities.record_heartbeat_resolved,
                args=[_hb_fingerprint("HeartbeatCollectFailed", "collect")],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=NO_RETRY,
            )

        await workflow.execute_activity_method(
            HomelabActivities.write_heartbeat_state,
            args=[
                {
                    "nodes": cur_nodes,
                    "stuck": sorted(cur_stuck),
                    "confirmed": sorted(confirmed_now),
                    "fail_count": 0,
                }
            ],
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=FAST,
        )
        await workflow.execute_activity_method(
            HomelabActivities.ping_deadman,
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=NO_RETRY,
        )
        return {
            "collect_ok": True,
            "alerts_spawned": spawned,
            "nodes_down": len(nodes_down),
            "nodes_recovered": len(nodes_recovered),
            "services_confirmed_stuck": len(new_confirmed),
            "services_recovered": len(recovered_services),
        }
```

- [ ] **Step 4: Run to verify pass** (same command). Expected: PASS (all 7 tests).
- [ ] **Step 5: Lint + commit**

```bash
ruff check . && git add worker/src/aegis_worker/flows/infra_heartbeat.py tests/worker/flows/test_infra_heartbeat_flow.py && git commit -m "feat: InfraHeartbeatFlow 2-min swarm liveness poll with transition alerts"
```

---

### Task 5: Registration, schedule seed, settings plumbing

**Files:**
- Modify: `worker/src/aegis_worker/__main__.py` (BOTH lists + `HomelabActivities` construction ~line 275)
- Modify: `worker/src/aegis_worker/schedule_sync.py` (`_ACTIVITY_TYPE_MAP` + import)
- Modify: `config/seed/activities.yaml` (new seed row)
- Modify: `core/src/aegis/config.py` (2 new Settings fields)
- Modify: `core/src/aegis/services/integrations_config.py` (2 `CONFIG_REGISTRY` rows)
- Test: existing `tests/worker/test_activity_registration.py` + `tests/worker/` schedule-sync tests (run, adjust counts if they assert totals)

**Interfaces:**
- Consumes: Task 3 fields/activities, Task 4 flow.
- Produces: Settings fields `infra_heartbeat_ping_url: str = ""` and `slack_owner_member_id: str = ""` (Task 7 consumes the latter via `get_alert_routing_config`). Schedule slug `infra-heartbeat-2m`.

- [ ] **Step 1: Settings fields.** In `core/src/aegis/config.py`, next to the existing `infra_cluster` field (grep for it), add following its exact style:

```python
    infra_heartbeat_ping_url: str = ""  # healthchecks.io dead-man URL ("" = off)
    slack_owner_member_id: str = ""  # Slack member id for escalation @-mentions ("" = no mention)
```

- [ ] **Step 2: CONFIG_REGISTRY.** In `core/src/aegis/services/integrations_config.py`, after the `infra_cluster` entry (~line 75), add:

```python
    ConfigKey(
        "infra_heartbeat_ping_url", "Heartbeat dead-man ping URL (healthchecks.io)",
        "System Monitoring", False,
        help="GET on every successful 2-min heartbeat tick; configure the check to alert "
        "when pings stop. Blank = disabled. Worker restart required.",
    ),
    ConfigKey(
        "slack_owner_member_id", "Slack member id for escalation mentions",
        "System Monitoring", False,
        help="Used to @-mention you on unacked critical infra cards (e.g. UXXXXXXXXX). "
        "Blank = escalate without mention. Worker restart required.",
    ),
```

- [ ] **Step 3: Worker wiring.** In `worker/src/aegis_worker/__main__.py`:
  - `HomelabActivities(...)` construction (~line 275) — add:
    ```python
            heartbeat_ping_url=getattr(settings, "infra_heartbeat_ping_url", "") or "",
            infra_cluster=getattr(settings, "infra_cluster", "") or "",
    ```
  - The `if settings.homelab_enabled and homelab_act is not None:` activities block (~line 559) — append:
    ```python
            homelab_act.collect_infra_state,
            homelab_act.read_heartbeat_state,
            homelab_act.write_heartbeat_state,
            homelab_act.record_heartbeat_resolved,
            homelab_act.ping_deadman,
            homelab_act.get_heartbeat_routing,
    ```
  - The `if settings.homelab_enabled:` workflows block (~line 615) — add `InfraHeartbeatFlow,` and the import near the other flow imports: `from aegis_worker.flows.infra_heartbeat import InfraHeartbeatFlow`.
  - Check the module-level `WORKFLOWS`/`ACTIVITIES` stub lists near the top of the file (grep `WORKFLOWS =`) — if flows are stub-listed there for import-time tests, mirror the addition (PR #108 added an AST parity test that will fail loudly if you miss it).

- [ ] **Step 4: schedule_sync.** In `worker/src/aegis_worker/schedule_sync.py`, add the import next to the other flow imports and the map entry:

```python
from aegis_worker.flows.infra_heartbeat import InfraHeartbeatConfig, InfraHeartbeatFlow
```

```python
    "InfraHeartbeatFlow": lambda act: (
        InfraHeartbeatFlow,
        InfraHeartbeatConfig(
            agent_id=act["agent_id"],
            fail_threshold=int(act["config"].get("fail_threshold", 3)),
        ),
    ),
```

- [ ] **Step 5: Seed row.** In `config/seed/activities.yaml`, after the `service-drift-4h` block:

```yaml
  # Pandora — 2-min swarm liveness heartbeat (spec 2026-07-24). Primary fast
  # internal detector for node-down and post-reboot stuck services; emits only
  # on state transitions so steady state costs nothing. Complements (does not
  # replace) alertmanager push — a power flap that silences alertmanager is
  # exactly what this catches.
  - slug: infra-heartbeat-2m
    workflow_type: InfraHeartbeatFlow
    agent_id: pandoras-actor
    schedule_cron: "*/2 * * * *"
    config: {}
    active: true
```

- [ ] **Step 6: Run registration + schedule tests**

Run: `PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/test_activity_registration.py tests/worker/ -k "schedule or registration" 2>&1 | tee logs/test-task5.log`
Expected: PASS. If a test asserts an exact flow/activity count, update the count.

- [ ] **Step 7: Lint + commit**

```bash
ruff check . && git add -A && git commit -m "feat: register InfraHeartbeatFlow, 2-min schedule seed, heartbeat settings keys"
```

---

### Task 6: `InteractionFlow` escalation loop

**Files:**
- Modify: `worker/src/aegis_worker/flows/interaction.py`
- Test: `tests/worker/flows/test_interaction_escalation.py` (create; if `tests/worker/` already has an InteractionFlow test file — check `ls tests/worker | grep -i interaction` — append there instead)

**Interfaces:**
- Consumes: existing `send_interaction_card` activity contract `args=[interaction_id, agent_id, kind, prompt, options, allow_hint]`.
- Produces: `InteractionFlowInput.metadata["escalation"] = {"interval_minutes": int, "mention_id": str, "max_repeats": int}` — when `interval_minutes > 0` and `timeout_policy != "hold"`, the flow re-dispatches the card every interval until resolved/max_repeats/timeout. Task 7 consumes this contract.

- [ ] **Step 1: Write the failing tests** (time-skipping; stub `insert_interaction`/`send_interaction_card`/`resolve_interaction`/`apply_interaction_timeout` by name, counting card dispatches; signal mid-run via `start_workflow` + `handle.signal`):

```python
"""InteractionFlow escalation — re-dispatch card with mention until ack."""

from __future__ import annotations

import uuid

from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.interactions import (
        ApplyTimeoutInput,
        InsertInteractionInput,
        InsertInteractionResult,
        ResolveInteractionInput,
    )
    from aegis_worker.flows.interaction import InteractionFlow, InteractionFlowInput

_calls: dict = {}


def _reset():
    _calls.clear()
    _calls.update({"cards": [], "timeouts": 0, "resolved": 0})


@activity.defn(name="insert_interaction")
async def _insert(input: InsertInteractionInput) -> InsertInteractionResult:
    return InsertInteractionResult(interaction_id="int-1")


@activity.defn(name="send_interaction_card")
async def _card(
    interaction_id: str, agent_id: str, kind: str, prompt: str, options, allow_hint: bool = False
) -> dict:
    _calls["cards"].append(prompt)
    return {"ok": True, "delivery_ref": {"adapter": "web"}}


@activity.defn(name="update_interaction_delivery_ref")
async def _ref(interaction_id: str, delivery_ref: dict) -> None:
    return None


@activity.defn(name="resolve_interaction")
async def _resolve(input: ResolveInteractionInput):
    _calls["resolved"] += 1
    return None


@activity.defn(name="apply_interaction_timeout")
async def _timeout(input: ApplyTimeoutInput) -> None:
    _calls["timeouts"] += 1


_ACTS = [_insert, _card, _ref, _resolve, _timeout]


def _input(**esc) -> InteractionFlowInput:
    return InteractionFlowInput(
        agent_id="pandoras-actor",
        kind="choice",
        origin="test",
        prompt="original prompt",
        options={"a": "A"},
        timeout_seconds=3600,
        timeout_policy="archive",
        metadata={"escalation": esc} if esc else None,
    )


async def _start(input: InteractionFlowInput):
    env = await WorkflowEnvironment.start_time_skipping()
    worker = Worker(
        env.client,
        task_queue=f"esc-{uuid.uuid4()}",
        workflows=[InteractionFlow],
        activities=_ACTS,
    )
    return env, worker, input


async def test_no_escalation_metadata_keeps_single_card():
    _reset()
    env, worker, input = await _start(_input())
    async with env, worker:
        handle = await env.client.start_workflow(
            InteractionFlow.run, input, id=f"i-{uuid.uuid4()}", task_queue=worker.task_queue
        )
        await handle.signal(InteractionFlow.submit_response, {"value": "a"})
        result = await handle.result()
    assert result.status == "resolved"
    assert len(_calls["cards"]) == 1


async def test_escalation_redispatches_with_mention_until_ack():
    _reset()
    env, worker, input = await _start(
        _input(interval_minutes=3, mention_id="U042", max_repeats=10)
    )
    async with env, worker:
        handle = await env.client.start_workflow(
            InteractionFlow.run, input, id=f"i-{uuid.uuid4()}", task_queue=worker.task_queue
        )
        await env.sleep(60 * 7)  # two intervals pass unacked
        await handle.signal(InteractionFlow.submit_response, {"value": "a"})
        result = await handle.result()
    assert result.status == "resolved"
    assert len(_calls["cards"]) == 3  # 1 original + 2 escalations
    assert "<@U042>" in _calls["cards"][1]
    assert "original prompt" in _calls["cards"][1]


async def test_escalation_stops_at_max_repeats_then_times_out():
    _reset()
    env, worker, input = await _start(
        _input(interval_minutes=3, mention_id="U042", max_repeats=2)
    )
    input.timeout_seconds = 1200
    async with env, worker:
        result = await env.client.execute_workflow(
            InteractionFlow.run, input, id=f"i-{uuid.uuid4()}", task_queue=worker.task_queue
        )
    assert result.status == "archived"
    assert len(_calls["cards"]) == 3  # original + exactly max_repeats
    assert _calls["timeouts"] == 1
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/flows/test_interaction_escalation.py 2>&1 | tee logs/test-task6.log`
Expected: the mention/`cards==3` asserts FAIL (single card today); the first test may already pass — fine.

- [ ] **Step 3: Implement.** In `interaction.py`, replace ONLY the non-hold wait block (`else:` branch at lines 144–161) with an escalation-aware version, keeping the hold branch and everything after untouched:

```python
        if input.timeout_policy == "hold":
            await workflow.wait_condition(lambda: self._resolved)
        else:
            esc = (input.metadata or {}).get("escalation") or {}
            esc_interval_s = int(esc.get("interval_minutes") or 0) * 60
            esc_max = int(esc.get("max_repeats") or 10)
            mention = str(esc.get("mention_id") or "").strip()
            deadline = workflow.now() + timedelta(seconds=input.timeout_seconds)
            repeats = 0
            timed_out = False
            while not self._resolved:
                remaining = (deadline - workflow.now()).total_seconds()
                if remaining <= 0:
                    timed_out = True
                    break
                chunk = (
                    min(esc_interval_s, remaining)
                    if esc_interval_s > 0 and repeats < esc_max
                    else remaining
                )
                try:
                    await workflow.wait_condition(
                        lambda: self._resolved, timeout=timedelta(seconds=chunk)
                    )
                except TimeoutError:
                    if esc_interval_s > 0 and repeats < esc_max:
                        repeats += 1
                        prefix = f"<@{mention}> " if mention else ""
                        nag = (
                            f"{prefix}⏰ Reminder {repeats}/{esc_max} — still waiting on this:"
                            f"\n\n{input.prompt}"
                        )
                        try:
                            await workflow.execute_activity(
                                "send_interaction_card",
                                args=[
                                    interaction_id,
                                    input.agent_id,
                                    input.kind,
                                    nag,
                                    input.options,
                                    input.allow_hint,
                                ],
                                retry_policy=_BEST_EFFORT_RETRY,
                                start_to_close_timeout=_ACT_TIMEOUT,
                            )
                        except Exception as exc:  # noqa: BLE001 — nag is best-effort
                            workflow.logger.warning(
                                "interaction_escalation_dispatch_failed: %s", str(exc)[:200]
                            )
            if timed_out:
                await workflow.execute_activity(
                    "apply_interaction_timeout",
                    ApplyTimeoutInput(interaction_id=interaction_id, policy=input.timeout_policy),
                    retry_policy=ACT_RETRY,
                    start_to_close_timeout=_ACT_TIMEOUT,
                )
                if input.timeout_policy == "archive":
                    return InteractionResult(
                        interaction_id=interaction_id, status="archived", response=None
                    )
                raise ApplicationError(f"unknown timeout_policy: {input.timeout_policy}") from None
```

Behavior is byte-identical for inputs without escalation metadata (chunk == remaining → one wait → same timeout path). Determinism note for review: in-flight runs replay identically because the new branching keys off input data, not time.

- [ ] **Step 4: Run to verify pass** (same command), then the whole interaction/gate suites:

Run: `PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/flows/test_interaction_escalation.py tests/worker/flows/test_alert_investigation_gates.py tests/worker/ -k interaction 2>&1 | tee logs/test-task6b.log`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
ruff check . && git add worker/src/aegis_worker/flows/interaction.py tests/worker/flows/test_interaction_escalation.py && git commit -m "feat: optional escalate-until-ack loop in InteractionFlow"
```

---

### Task 7: Escalating alerts in `AlertInvestigationFlow` — heads-up ping, Gate-2 escalation, self-resolve race

**Files:**
- Modify: `worker/src/aegis_worker/flows/alert_investigation.py`
- Modify: `worker/src/aegis_worker/activities/alerts.py` (`get_alert_routing_config` + new dataclass field)
- Modify: `worker/src/aegis_worker/__main__.py` (inject `slack_owner_member_id` into `AlertActivities`)
- Test: `tests/worker/flows/test_alert_escalation.py` (create)

**Interfaces:**
- Consumes: `alert["escalate"] is True` (Task 4); Task 6's escalation metadata contract; Task 5's `slack_owner_member_id` setting.
- Produces: escalating alerts get (a) an immediate heads-up chat ping at flow start, (b) Gate 2 spawned with escalation metadata, raced against `check_alert_resolved` every 3 min; self-resolve signals the gate closed and returns `{"status": "self_resolved_during_gate", ...}`.

- [ ] **Step 1: Routing config.** In `alerts.py`: add dataclass field `slack_owner_member_id: str = ""` next to `infra_cluster` (~line 583), and extend `get_alert_routing_config` to `return {"infra_cluster": self.infra_cluster, "slack_owner_member_id": self.slack_owner_member_id}`. In `__main__.py`'s `AlertActivities(...)` construction add `slack_owner_member_id=getattr(settings, "slack_owner_member_id", "") or "",`.

- [ ] **Step 2: Write the failing flow test.** First read `tests/worker/flows/test_alert_investigation_gates.py` IN FULL and copy its harness into the new file verbatim: the module `_state`/`_calls` dicts, `_reset()`, every name-registered activity stub the flow touches, the real `InteractionFlow` registered as child (so signals work), and its `start_local()`-style runner that starts the flow, waits for the Gate-2 card, and signals the child. Then make these harness modifications:
  - `_reset()` seeds `_state["alert"]` with the escalating heartbeat alert below (instead of the app-code alert) and `_state["routing"] = {"infra_cluster": "homelab-swarm", "slack_owner_member_id": "U042"}`; the `get_alert_routing_config` stub returns `_state["routing"]`.
  - The `insert_interaction` stub appends its `InsertInteractionInput` argument to `_calls["insert_inputs"]`.
  - The `send_message` stub appends the message text to `_calls["messages"]`.
  - The `check_alert_resolved` stub returns `_state["resolved_check_result"]` (the gates harness already has this key).
  - The escalating alert dict: `{"title": "Swarm node noon down", "fingerprint": "aegis-heartbeat:NodeDown:noon", "severity": "critical", "source": "aegis-heartbeat", "labels": {"alertname": "NodeDown", "cluster": "homelab-swarm"}, "escalate": True}`. Seed `_state["resource_result"]` with the infra-resource shape (`source: "infra"`) and stub `resolve_infra_resource`/`remediate_infra_service` (returns `{"attempted": False}`) — infra alerts take that path, not `resolve_alert_resource`.

  Test 1 (`test_escalating_alert_sends_heads_up_and_escalation_metadata`): run the flow to Gate 2 with the harness (verdict stub `actionable`), answer the gate with `{"value": "ack"}`, await the result, then assert:

```python
    assert any("noon" in m for m in _calls["messages"])  # heads-up ping fired at start
    gate_insert = _calls["insert_inputs"][-1]
    assert gate_insert.metadata["escalation"]["interval_minutes"] == 3
    assert gate_insert.metadata["escalation"]["mention_id"] == "U042"
```

  Test 2 (`test_gate2_self_resolve_race_closes_gate`): same setup with `_state["resolved_check_result"] = {"resolved": False}`; start the workflow via `env.client.start_workflow`, wait until `_calls["insert_inputs"]` has the Gate-2 entry (poll with `env.sleep(1)` in a loop), then set `_state["resolved_check_result"] = {"resolved": True}`, `await env.sleep(200)` (past one 180s race tick), and `result = await handle.result()`. Assert:

```python
    assert result["status"] == "self_resolved_during_gate"
```

  (Do NOT answer the gate in test 2 — the race must close it.)

- [ ] **Step 3: Run to verify failure**

Run: `PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/flows/test_alert_escalation.py 2>&1 | tee logs/test-task7.log`
Expected: FAIL (no heads-up, no escalation metadata, no race).

- [ ] **Step 4: Implement in `alert_investigation.py`.**
  1. `import asyncio` at top (outside the passthrough block, next to `re`).
  2. After the agent-resolution block (after line ~336), add the heads-up ping:
     ```python
        _escalate = bool(alert.get("escalate"))
        if _escalate:
            await self._safe_send_message(
                agent_id=agent_id,
                message=(
                    f"🔴 <b>{_html_escape(title)}</b>\n"
                    f"{severity} · {source} — investigating now; "
                    f"I'll escalate until you ack the decision card."
                ),
                log_event="alert_heads_up_notify_failed",
            )
     ```
  3. The routing fetch (Step 2.65, ~line 377) already runs under `workflow.patched("infra-cluster-from-settings")`; keep it, and capture `owner_mention = routing.get("slack_owner_member_id") or ""` next to `infra_cluster`. Initialize `owner_mention = ""` before the `if workflow.patched(...)` block.
  4. Gate 2 (~line 1133): build the input once, then branch on `_escalate`:
     ```python
            gate_input = InteractionFlowInput(
                agent_id=agent_id,
                kind="choice",
                origin="alert_approve_pr",
                prompt=prompt,
                options=options,
                timeout_seconds=172800,  # 48h
                timeout_policy="archive",
                metadata=(
                    {
                        "escalation": {
                            "interval_minutes": 3,
                            "mention_id": owner_mention,
                            "max_repeats": 10,
                        }
                    }
                    if _escalate
                    else None
                ),
            )
            gate_id = f"gate2-{_safe_workflow_id_segment(alert.get('fingerprint') or '')}-{workflow.info().workflow_id}"
            if not _escalate:
                g2 = await workflow.execute_child_workflow(
                    InteractionFlow.run, gate_input, id=gate_id
                )
            else:
                handle = await workflow.start_child_workflow(
                    InteractionFlow.run, gate_input, id=gate_id
                )
                gate_task = asyncio.ensure_future(handle)
                while True:
                    done, _ = await asyncio.wait({gate_task}, timeout=180)
                    if gate_task in done:
                        g2 = gate_task.result()
                        break
                    recheck = await workflow.execute_activity_method(
                        AlertActivities.check_alert_resolved,
                        args=[fingerprint, 10],
                        start_to_close_timeout=TIMEOUT_FAST,
                        retry_policy=FAST,
                    )
                    if recheck.get("resolved"):
                        await handle.signal(
                            InteractionFlow.submit_response,
                            {"value": "self_resolved", "note": "auto-closed: alert resolved"},
                        )
                        g2 = await gate_task
                        break
     ```
  5. Immediately after the archived-check (~line 1151), handle the auto-close before the other v2 branches:
     ```python
            v2 = ((g2.response or {}).get("value") or "").strip()
            if v2 == "self_resolved":
                await self._safe_post_note(
                    track_task_id or "",
                    "✅ Self-resolved while awaiting your decision — card closed automatically.",
                )
                try:
                    await workflow.execute_activity_method(
                        AlertActivities.log_alert,
                        args=[alert],
                        start_to_close_timeout=TIMEOUT_FAST,
                        retry_policy=NO_RETRY,
                    )
                except Exception:
                    pass
                return {
                    "status": "self_resolved_during_gate",
                    "task_id": None,
                    "todoist_task_id": track_task_id,
                }
     ```
     (The existing `v2 = ...` line at 1165 is replaced by this earlier assignment — delete the duplicate.)

- [ ] **Step 5: Run to verify pass**, then the full alert-flow suites:

Run: `PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/flows/test_alert_escalation.py tests/worker/flows/ tests/worker/test_alert_investigation.py tests/worker/test_alert_investigation_v2.py tests/worker/test_alert_flow_v2.py 2>&1 | tee logs/test-task7b.log`
Expected: PASS (non-escalating alerts take the byte-identical `execute_child_workflow` path).

- [ ] **Step 6: Lint + commit**

```bash
ruff check . && git add -A && git commit -m "feat: escalating infra alerts — heads-up ping, Gate-2 escalation, self-resolve race"
```

---

### Task 8: `extract_proposed_commands` + investigation prompt + `run_remediation_commands`

**Files:**
- Modify: `worker/src/aegis_worker/activities/alerts.py` (pure fn + activity)
- Modify: `worker/src/aegis_worker/flows/alert_investigation.py` (infra_hint instruction, Step 5.5 ~line 840)
- Test: `tests/worker/test_alert_remediation.py` (append)

**Interfaces:**
- Consumes: `RemoteScriptConnector.run_on_host(host, remote_cmd, ...)` → `{status, exit_code, stdout, stderr}` (already a constructor dep as `self.remote_script`).
- Produces: module-level `extract_proposed_commands(text: str) -> list[str]` (≤5 commands, each ≤500 chars); `AlertActivities.run_remediation_commands(commands: list[str], host: str = "") -> dict` returning `{"ran": [{"command", "exit_code", "stdout", "stderr"}], "refused": str | None}`. Task 9 consumes both.

- [ ] **Step 1: Write the failing tests** (append to `tests/worker/test_alert_remediation.py`; read its header first and match its `AlertActivities` construction style):

```python
def test_extract_proposed_commands_parses_footer():
    text = (
        "…investigation findings…\n\n"
        "PROPOSED_COMMANDS:\n"
        "- docker --context swarm service update --force koyracloud_order-finder\n"
        "- docker --context swarm service ps koyracloud_order-finder\n\n"
        "Some trailing prose."
    )
    cmds = extract_proposed_commands(text)
    assert cmds == [
        "docker --context swarm service update --force koyracloud_order-finder",
        "docker --context swarm service ps koyracloud_order-finder",
    ]


def test_extract_proposed_commands_caps_and_absent():
    assert extract_proposed_commands("no footer here") == []
    many = "PROPOSED_COMMANDS:\n" + "\n".join(f"- echo {i}" for i in range(9))
    assert len(extract_proposed_commands(many)) == 5
    long = "PROPOSED_COMMANDS:\n- " + "x" * 900
    assert extract_proposed_commands(long) == []  # over-long command dropped


@pytest.mark.asyncio
async def test_run_remediation_commands_executes_and_audits():
    remote = AsyncMock()
    remote.run_on_host.return_value = {"status": "ok", "exit_code": 0, "stdout": "done", "stderr": ""}
    pool = AsyncMock()
    pool.fetchval.return_value = False  # read_only = false
    act = AlertActivities(db_pool=pool, remote_script=remote)
    result = await act.run_remediation_commands(["docker service ls"], host="meem")
    assert result["refused"] is None
    assert result["ran"][0]["exit_code"] == 0
    remote.run_on_host.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_remediation_commands_refuses_read_only_host():
    remote = AsyncMock()
    pool = AsyncMock()
    pool.fetchval.return_value = True  # coding host read_only
    act = AlertActivities(db_pool=pool, remote_script=remote)
    result = await act.run_remediation_commands(["docker service ls"], host="")
    assert result["refused"] == "coding_host_read_only"
    assert result["ran"] == []
    remote.run_on_host.assert_not_awaited()
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/test_alert_remediation.py 2>&1 | tee logs/test-task8.log`
Expected: FAIL — names not defined.

- [ ] **Step 3: Implement in `alerts.py`.** Module-level, near `build_alert_signature`:

```python
_MAX_REMEDIATION_COMMANDS = 5
_MAX_REMEDIATION_CMD_CHARS = 500


def extract_proposed_commands(text: str) -> list[str]:
    """Parse the `PROPOSED_COMMANDS:` footer an infra investigation is asked to
    emit — `- <command>` lines after the marker, until a non-list line. Pure and
    deterministic (called from workflow code). Over-long commands are dropped,
    the list is capped."""
    if not text:
        return []
    lines = text.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == "PROPOSED_COMMANDS:")
    except StopIteration:
        return []
    out: list[str] = []
    for ln in lines[start + 1 :]:
        stripped = ln.strip()
        if not stripped.startswith("- "):
            if stripped == "":
                continue
            break
        cmd = stripped[2:].strip()
        if cmd and len(cmd) <= _MAX_REMEDIATION_CMD_CHARS:
            out.append(cmd)
        if len(out) >= _MAX_REMEDIATION_COMMANDS:
            break
    return out
```

Activity on `AlertActivities` (near `remediate_infra_service`):

```python
    @activity.defn
    async def run_remediation_commands(self, commands: list[str], host: str = "") -> dict:
        """Execute HUMAN-APPROVED remediation commands on the coding host via
        SSH. Only reachable from the Gate-2 'Run fix' approval — never
        autonomous. Refuses when the coding-host infra row is read_only.
        Every execution is audit-logged with commands + exit codes."""
        result: dict = {"ran": [], "refused": None}
        if not self.remote_script:
            result["refused"] = "no_remote_script_connector"
            return result
        commands = [
            c.strip()
            for c in (commands or [])
            if c.strip() and len(c.strip()) <= _MAX_REMEDIATION_CMD_CHARS
        ][:_MAX_REMEDIATION_COMMANDS]
        if not commands:
            result["refused"] = "no_commands"
            return result
        if self.db_pool:
            read_only = await self.db_pool.fetchval(
                "SELECT read_only FROM infra WHERE coding->>'enabled' = 'true' LIMIT 1"
            )
            if read_only:
                result["refused"] = "coding_host_read_only"
                return result
        for cmd in commands:
            activity.heartbeat()
            try:
                env = await self.remote_script.run_on_host(host, cmd, timeout=120)
            except Exception as exc:  # noqa: BLE001 — report, don't crash the gate
                env = {"status": "error", "exit_code": -1, "stdout": "", "stderr": repr(exc)[:300]}
            entry = {
                "command": cmd,
                "exit_code": env.get("exit_code", -1),
                "stdout": str(env.get("stdout") or "")[:1500],
                "stderr": str(env.get("stderr") or "")[:1500],
            }
            result["ran"].append(entry)
            if entry["exit_code"] != 0:
                break  # stop the sequence on first failure — report, don't cascade
        if self.db_pool:
            await log_audit(
                self.db_pool,
                actor="alert:remediation",
                action="remediation_executed",
                target_type="infra",
                target_id=host or "coding-host",
                details={"ran": result["ran"]},
            )
        return result
```

(Check `run_on_host`'s actual signature in `core/src/aegis/connectors/remote_script.py:436` — if the timeout kwarg is named differently, match it.)

- [ ] **Step 4: Instruct the investigation.** In `alert_investigation.py` Step 5.5, extend `infra_hint` (inside the existing `if _is_infra:` block) by appending to the string:

```python
                " End your report with a PROPOSED_COMMANDS: section — one `- <command>` "
                "line per safe, idempotent recovery command you recommend (max 5, "
                "e.g. `- docker --context swarm service update --force <svc>`). "
                "Propose ONLY read-safe or idempotent commands; omit the section if "
                "no command is warranted. The commands are NOT run automatically — "
                "a human approves them."
```

- [ ] **Step 5: Run to verify pass** (same command + `tests/worker/test_alert_infra_routing.py`). Expected: PASS.
- [ ] **Step 6: Lint + commit**

```bash
ruff check . && git add -A && git commit -m "feat: proposed-commands extraction and gated run_remediation_commands activity"
```

---

### Task 9: Gate-2 "Run fix" wiring

**Files:**
- Modify: `worker/src/aegis_worker/flows/alert_investigation.py` (Gate-2 region, builds on Task 7's shape)
- Modify: `worker/src/aegis_worker/__main__.py` — add `alert_act.run_remediation_commands` to the activities list (it is NOT homelab-gated; `alert_act` activities live in the main list — put it beside the other `alert_act.*` entries; grep `alert_act.` in the file)
- Test: `tests/worker/flows/test_alert_run_fix.py` (create, same harness as Task 7's test)

**Interfaces:**
- Consumes: Task 8's `extract_proposed_commands` + `run_remediation_commands`; Task 7's gate structure.
- Produces: infra Gate-2 cards with proposed commands show a `run_fix` option; approval executes, posts results, re-checks resolution after 180s, and reports.

- [ ] **Step 1: Write the failing test.** Copy the Task-7 test file's harness (same stubs, same escalating infra alert) into `test_alert_run_fix.py`, with these deltas:
  - The `run_investigation` stub returns `status: "succeeded"` with `output` ending in `"\nPROPOSED_COMMANDS:\n- docker --context swarm service update --force svc_a\n"` and `host: "meem"`.
  - Add a name-registered `run_remediation_commands` stub that appends its `(commands, host)` args to `_calls["run_remediation_args"]` and returns `{"ran": [{"command": "docker --context swarm service update --force svc_a", "exit_code": 0, "stdout": "ok", "stderr": ""}], "refused": None}`.
  - `_state["resolved_check_result"] = {"resolved": True}` (post-fix verification succeeds).

  Test 1 (`test_infra_gate2_run_fix_executes_and_reports`): drive to Gate 2, answer `{"value": "run_fix"}`, await result, assert:

```python
    assert _calls["run_remediation_args"][0][0] == [
        "docker --context swarm service update --force svc_a"
    ]
    assert _calls["run_remediation_args"][0][1] == "meem"
    assert result["status"] == "remediated"
    gate_insert = _calls["insert_inputs"][-1]
    assert "service update --force svc_a" in gate_insert.prompt
    assert "run_fix" in gate_insert.options
```

  Test 2 (`test_infra_gate2_note_overrides_commands`): identical, but answer the gate with `{"value": "run_fix", "note": "docker node ls"}` and assert:

```python
    assert _calls["run_remediation_args"][0][0] == ["docker node ls"]
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/flows/test_alert_run_fix.py 2>&1 | tee logs/test-task9.log`
Expected: FAIL.

- [ ] **Step 3: Implement in `alert_investigation.py`.**
  1. Import `extract_proposed_commands` inside the passthrough block (extend the existing `from aegis_worker.activities.alerts import` line).
  2. Before the Gate-2 prompt build (~line 1095), compute:
     ```python
            proposed_cmds: list[str] = (
                extract_proposed_commands(investigation_output) if _is_infra else []
            )
     ```
  3. In the no-branches prompt branch, after `suggested_fix` is appended, add:
     ```python
                if proposed_cmds:
                    cmd_lines = "\n".join(f"  <code>{_html_escape(c)}</code>" for c in proposed_cmds)
                    prompt += f"\n\nProposed fix commands:\n{cmd_lines}"
     ```
  4. Options: before `options["mute_24h"] = ...`, add:
     ```python
            if proposed_cmds:
                options["run_fix"] = "🔧 Run fix"
     ```
  5. After the `v2 == "self_resolved"` branch (Task 7), add the run_fix branch:
     ```python
            if v2 == "run_fix" and proposed_cmds:
                note = ((g2.response or {}).get("note") or "").strip()
                cmds = [ln.strip() for ln in note.splitlines() if ln.strip()] if note else proposed_cmds
                exec_result = await workflow.execute_activity_method(
                    AlertActivities.run_remediation_commands,
                    args=[cmds, inv_result.get("host", "")],
                    start_to_close_timeout=TIMEOUT_LONG,
                    heartbeat_timeout=TIMEOUT_STANDARD,
                    retry_policy=NO_RETRY,
                )
                if exec_result.get("refused"):
                    outcome_note = f"🚫 Remediation refused: {exec_result['refused']}"
                else:
                    ran = exec_result.get("ran") or []
                    ok = all(r.get("exit_code") == 0 for r in ran)
                    detail = "\n".join(
                        f"$ {r['command']}\n  exit={r['exit_code']} {(r['stdout'] or r['stderr'])[:300]}"
                        for r in ran
                    )
                    outcome_note = (
                        f"{'✅' if ok else '⚠️'} Ran {len(ran)} command(s):\n{detail}"
                    )
                await self._safe_post_note(track_task_id or "", outcome_note)
                await self._safe_send_message(
                    agent_id=agent_id,
                    message=f"<b>Remediation result</b> — {_html_escape(title)}\n"
                    f"<pre>{_html_escape(outcome_note[:1500])}</pre>",
                    log_event="alert_remediation_notify_failed",
                )
                if not exec_result.get("refused"):
                    await workflow.sleep(timedelta(seconds=180))
                    post_check = await workflow.execute_activity_method(
                        AlertActivities.check_alert_resolved,
                        args=[fingerprint, 5],
                        start_to_close_timeout=TIMEOUT_FAST,
                        retry_policy=FAST,
                    )
                    verdict_note = (
                        "✅ Verified: alert resolved after remediation."
                        if post_check.get("resolved")
                        else "⚠️ Alert not yet showing resolved — the next heartbeat tick "
                        "confirms recovery; investigate further if it re-fires."
                    )
                    await self._safe_post_note(track_task_id or "", verdict_note)
                try:
                    await workflow.execute_activity_method(
                        AlertActivities.log_alert,
                        args=[alert],
                        start_to_close_timeout=TIMEOUT_FAST,
                        retry_policy=NO_RETRY,
                    )
                except Exception:
                    pass
                return {
                    "status": "remediated",
                    "task_id": None,
                    "todoist_task_id": track_task_id,
                    "commands_ran": len(exec_result.get("ran") or []),
                    "refused": exec_result.get("refused"),
                }
     ```

- [ ] **Step 4: Run to verify pass**, plus the full worker flow suite:

Run: `PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/flows/ 2>&1 | tee logs/test-task9b.log`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
ruff check . && git add -A && git commit -m "feat: Gate-2 Run-fix — approve-to-run infra remediation with note override"
```

---

### Task 10: Content-route `alert_overrides` (Todoist → investigation bridge)

**Files:**
- Modify: `core/src/aegis/services/content_routes.py` (`validate_routes`)
- Modify: `worker/src/aegis_worker/activities/clarify.py` (`_pandora_alert_payload` + 2 call sites, lines ~1276 and ~1322)
- Test: `tests/core/test_content_routes.py` if it exists (check `ls tests/core | grep content_route`; else add validation tests to wherever `validate_routes` is currently tested — grep `validate_routes` under `tests/`) + `tests/worker/test_clarify_dataclass_payload.py` (append payload tests)

**Interfaces:**
- Consumes: route dicts flowing `matched_route` → `apply_outcome` → `_pandora_alert_payload`.
- Produces: routes accept optional `"alert_overrides": {"source"?: str, "alertname"?: str, "severity"?: str}`; the spawned alert dict has `source`/`labels.alertname`/`severity` overridden. A route like `{"key": "infra-incident", "match": "regex", "value": "(?i)(node|swarm|service).*(down|unreachable|stuck)", "gate": true, "alert_overrides": {"source": "todoist-infra", "alertname": "NodeDown", "severity": "critical"}}` sends a hand-captured task through the full infra pipeline (deterministic repo resolution, auto-remediation, Gate 2 with Run fix).

- [ ] **Step 1: Write the failing tests.**

Validation (in the file that tests `validate_routes`):

```python
def test_alert_overrides_validated_and_normalized():
    routes = validate_routes(
        [
            {
                "key": "infra",
                "match": "contains",
                "value": "down",
                "alert_overrides": {"source": "todoist-infra", "alertname": "NodeDown"},
            }
        ]
    )
    assert routes[0]["alert_overrides"] == {"source": "todoist-infra", "alertname": "NodeDown"}


def test_alert_overrides_rejects_unknown_keys():
    with pytest.raises(ValueError):
        validate_routes(
            [{"key": "x", "match": "contains", "value": "y", "alert_overrides": {"exec": "rm"}}]
        )


def test_alert_overrides_absent_stays_none():
    routes = validate_routes([{"key": "x", "match": "contains", "value": "y"}])
    assert routes[0]["alert_overrides"] is None
```

Payload (append to `tests/worker/test_clarify_dataclass_payload.py`, matching its existing `_pandora_alert_payload` call style):

```python
def test_pandora_alert_payload_applies_overrides():
    payload = ClarifyActivities._pandora_alert_payload(
        "noon is down again",
        "desc",
        "route-123",
        "123",
        alert_overrides={"source": "todoist-infra", "alertname": "NodeDown", "severity": "critical"},
    )
    alert = payload["alert"]
    assert alert["source"] == "todoist-infra"
    assert alert["severity"] == "critical"
    assert alert["labels"]["alertname"] == "NodeDown"
    assert alert["todoist_task_id"] == "123"


def test_pandora_alert_payload_defaults_unchanged_without_overrides():
    payload = ClarifyActivities._pandora_alert_payload("APP-9: thing", "d", "fp", "9")
    assert payload["alert"]["source"] == "todoist-jira"
    assert payload["alert"]["labels"]["alertname"] == "APP-9: thing"
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/test_clarify_dataclass_payload.py -x 2>&1 | tee logs/test-task10.log` (+ the validate_routes test file)
Expected: FAIL — unexpected keyword / missing key.

- [ ] **Step 3: Implement.**

In `validate_routes` (`content_routes.py`), inside the per-route loop before `out.append`:

```python
        overrides_raw = r.get("alert_overrides") or {}
        if not isinstance(overrides_raw, dict):
            raise ValueError(f"route {key!r}: alert_overrides must be an object")
        _allowed_overrides = {"source", "alertname", "severity"}
        bad = set(overrides_raw) - _allowed_overrides
        if bad:
            raise ValueError(f"route {key!r}: unknown alert_overrides keys: {sorted(bad)}")
        overrides = {k: str(v) for k, v in overrides_raw.items() if str(v).strip()}
```

and add to the appended dict: `"alert_overrides": overrides or None,`.

In `clarify.py` `_pandora_alert_payload` (line ~898): add keyword param `alert_overrides: dict | None = None`, and before the `return`:

```python
        for k, v in (alert_overrides or {}).items():
            if k == "alertname":
                labels["alertname"] = v
            elif k in ("source", "severity"):
                alert[k] = v
```

At both call sites (~1276 and ~1322) add `alert_overrides=route.get("alert_overrides"),` / `alert_overrides=_route.get("alert_overrides"),`.

- [ ] **Step 4: Run to verify pass**, plus the clarify suites:

Run: `PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/test_clarify_dataclass_payload.py tests/worker/test_clarify_activities.py tests/worker/ -k clarify 2>&1 | tee logs/test-task10b.log`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
ruff check . && git add -A && git commit -m "feat: content-route alert_overrides for infra-shaped Todoist tasks"
```

---

### Task 11: Docs, full suite, PR

**Files:**
- Modify: `docs/production.md` (Alert routing + Schedules sections)
- Modify: `docs/architecture/overview.md` (flows list, if it enumerates scheduled flows)

- [ ] **Step 1: Document.** In `docs/production.md` under "Alert routing", add a bullet: `**AEGIS heartbeat (2-min poll)** → \`InfraHeartbeatFlow\` → \`AlertInvestigationFlow\` on node/service transitions (source \`aegis-heartbeat\`)`. Add a short subsection after "Per-alert runbooks":

```markdown
### Infra heartbeat & escalation

`InfraHeartbeatFlow` (schedule `infra-heartbeat-2m`, gated by `homelab_enabled`) polls
`docker node ls` + `docker service ls` every 2 min and spawns investigations on state
transitions only. Recovery transitions write the same resolved audit rows the webhook
writes, so self-resolve works for heartbeat alerts. Configure on the admin Integrations
page (worker restart required):

- **Heartbeat dead-man ping URL** — healthchecks.io check pinged on every successful tick.
- **Slack member id for escalation mentions** — critical infra Gate-2 cards re-ping with
  an @-mention every 3 min (max 10) until acked or self-resolved.

Infra Gate-2 cards can carry a **Run fix** option (kimi's `PROPOSED_COMMANDS:` footer);
approval executes the commands on the coding host via SSH (refused if the infra row is
`read_only`), posts outputs to the task, and re-verifies. Approving with a note runs the
note's lines instead. To route hand-captured Todoist tasks ("noon is down") into the
same pipeline, add a content route with `alert_overrides`, e.g.
`{"source": "todoist-infra", "alertname": "NodeDown", "severity": "critical"}`.
```

- [ ] **Step 2: Full test suite**

Run: `docker compose up -d postgres && PYTHONPATH=core/src:worker/src:comms/src pytest 2>&1 | tee logs/test-full.log`
Expected: PASS (pre-existing failures unrelated to these files, if any, noted in the PR body).

- [ ] **Step 3: Lint sweep**: `ruff check .` — clean.
- [ ] **Step 4: Commit docs + push + PR**

```bash
git add docs/ && git commit -m "docs: infra heartbeat, escalation and run-fix operations"
git push -u origin worktree-infra-heartbeat
gh pr create -t "feat: infra heartbeat, escalate-until-ack, approve-to-run remediation" -b "$(cat <<'EOF'
Implements docs/superpowers/specs/2026-07-24-infra-heartbeat-remediation-design.md.

- InfraHeartbeatFlow: 2-min swarm node/service poll, transition-only synthetic alerts (source aegis-heartbeat), recovery writes webhook-shaped resolved audit rows, healthchecks dead-man ping, self-alert on 3 consecutive collect failures.
- Source-independent infra-class signatures collapse heartbeat + alertmanager storms onto one task.
- InteractionFlow escalation: re-dispatch card with Slack @-mention until ack/max/self-resolve.
- Gate-2 Run fix for infra alerts: kimi PROPOSED_COMMANDS footer → human-approved execution via RemoteScriptConnector (read_only-gated, capped, audit-logged, note override).
- Content-route alert_overrides: hand-captured infra Todoist tasks route into the full infra pipeline.

Rollout (post-deploy, admin Integrations page): set heartbeat ping URL + Slack member id; add the infra-incident content route; verify schedule infra-heartbeat-2m registers.
EOF
)"
```

---

## Post-merge rollout checklist (operator, not code)

1. Deploy worker (`make aegis-release` in homelab-gitops ansible); verify `schedule_registered_or_updated slug=infra-heartbeat-2m` in worker logs.
2. Admin Integrations page: set **infra_heartbeat_ping_url** (create the healthchecks.io check first, ~5 min grace) and **slack_owner_member_id**; restart worker.
3. Add the infra-incident content route (admin triage/routes config) with `alert_overrides`.
4. Sanity: `temporal schedule trigger --schedule-id infra-heartbeat-2m` and check the run's `result_summary` shows `collect_ok: true`.
