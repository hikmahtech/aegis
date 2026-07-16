"""LLMGovernorActivities.check_llm_budget + LLMSpendGuardFlow registration.

The governor's defining safety property: it only ever auto-clears a switch
*it* set (`set_by == "governor"`). A human kill stays killed until a human
clears it — otherwise the next under-budget tick would silently undo a
deliberate freeze.
"""

from __future__ import annotations

from temporalio.testing import ActivityEnvironment

_MODEL_PREFIX = "govflowtest-"


async def _cleanup(pool) -> None:
    await pool.execute("DELETE FROM llm_calls WHERE model LIKE $1", f"{_MODEL_PREFIX}%")
    await pool.execute(
        "DELETE FROM settings WHERE key = ANY($1::text[])",
        ["llm_governor", "llm_kill_switch"],
    )


async def _set_budget(pool, budget: int, model_filter: str = "") -> None:
    await pool.execute(
        "INSERT INTO settings (key, value) VALUES ('llm_governor', $1) "
        "ON CONFLICT (key) DO UPDATE SET value = $1",
        {"daily_token_budget": budget, "model_filter": model_filter},
    )


async def _burn_tokens(pool, total: int) -> None:
    await pool.execute(
        "INSERT INTO llm_calls (model, input_tokens, output_tokens, latency_ms, "
        "purpose, status) VALUES ($1, $2, 0, 1, 't', 'success')",
        f"{_MODEL_PREFIX}m",
        total,
    )


async def test_check_llm_budget_breach_sets_switch(db_pool):
    from aegis.services import llm_governor as g
    from aegis_worker.activities.llm_governor import LLMGovernorActivities

    try:
        await _cleanup(db_pool)
        await _set_budget(db_pool, 100, model_filter=_MODEL_PREFIX)
        await _burn_tokens(db_pool, 1800)

        env = ActivityEnvironment()
        out = await env.run(LLMGovernorActivities(db_pool=db_pool).check_llm_budget)

        assert out["breached"] is True
        assert out["tokens"] == 1800
        assert out["budget"] == 100
        assert out["message"]

        g.invalidate_kill_cache()
        ks = await g.get_kill_switch(db_pool, use_cache=False)
        assert ks["active"] is True
        assert ks["set_by"] == "governor"
    finally:
        await _cleanup(db_pool)
        g.invalidate_kill_cache()


async def test_check_llm_budget_zero_budget_is_noop(db_pool):
    """The default/unconfigured deployment must do nothing at all."""
    from aegis.services import llm_governor as g
    from aegis_worker.activities.llm_governor import LLMGovernorActivities

    try:
        await _cleanup(db_pool)
        await _set_budget(db_pool, 0)
        await _burn_tokens(db_pool, 999_999)

        env = ActivityEnvironment()
        out = await env.run(LLMGovernorActivities(db_pool=db_pool).check_llm_budget)

        assert out["breached"] is False
        assert out["cleared"] is False
        # No settings row written at all.
        assert (
            await db_pool.fetchrow("SELECT 1 FROM settings WHERE key='llm_kill_switch'")
        ) is None
        g.invalidate_kill_cache()
        assert (await g.get_kill_switch(db_pool, use_cache=False))["active"] is False
    finally:
        await _cleanup(db_pool)
        g.invalidate_kill_cache()


async def test_governor_clears_only_its_own_switch(db_pool):
    from aegis.services import llm_governor as g
    from aegis_worker.activities.llm_governor import LLMGovernorActivities

    try:
        await _cleanup(db_pool)
        await _set_budget(db_pool, 10_000, model_filter=_MODEL_PREFIX)
        await _burn_tokens(db_pool, 5)  # comfortably under budget

        # A MANUAL kill must survive an under-budget tick.
        await g.set_kill_switch(db_pool, active=True, reason="human says no", set_by="manual")
        env = ActivityEnvironment()
        out = await env.run(LLMGovernorActivities(db_pool=db_pool).check_llm_budget)
        assert out["breached"] is False
        assert out["cleared"] is False
        g.invalidate_kill_cache()
        ks = await g.get_kill_switch(db_pool, use_cache=False)
        assert ks["active"] is True, "a manual kill must NOT be auto-cleared"
        assert ks["set_by"] == "manual"

        # A GOVERNOR kill is auto-cleared once back under budget.
        await g.set_kill_switch(db_pool, active=True, reason="budget", set_by="governor")
        out = await env.run(LLMGovernorActivities(db_pool=db_pool).check_llm_budget)
        assert out["cleared"] is True
        g.invalidate_kill_cache()
        assert (await g.get_kill_switch(db_pool, use_cache=False))["active"] is False
    finally:
        await _cleanup(db_pool)
        g.invalidate_kill_cache()


async def test_check_llm_budget_no_repeat_alert_while_already_tripped(db_pool):
    """A sustained breach must not re-alert on every 15-min tick."""
    from aegis.services import llm_governor as g
    from aegis_worker.activities.llm_governor import LLMGovernorActivities

    try:
        await _cleanup(db_pool)
        await _set_budget(db_pool, 100, model_filter=_MODEL_PREFIX)
        await _burn_tokens(db_pool, 1800)

        env = ActivityEnvironment()
        act = LLMGovernorActivities(db_pool=db_pool)
        first = await env.run(act.check_llm_budget)
        assert first["breached"] is True

        second = await env.run(act.check_llm_budget)
        assert second["breached"] is False, "already-tripped switch should not re-alert"
        assert second["already_active"] is True
        g.invalidate_kill_cache()
        assert (await g.get_kill_switch(db_pool, use_cache=False))["active"] is True
    finally:
        await _cleanup(db_pool)
        g.invalidate_kill_cache()


async def test_check_llm_budget_under_budget_with_no_switch_is_quiet(db_pool):
    from aegis.services import llm_governor as g
    from aegis_worker.activities.llm_governor import LLMGovernorActivities

    try:
        await _cleanup(db_pool)
        await _set_budget(db_pool, 10_000, model_filter=_MODEL_PREFIX)
        await _burn_tokens(db_pool, 5)

        env = ActivityEnvironment()
        out = await env.run(LLMGovernorActivities(db_pool=db_pool).check_llm_budget)
        assert out["breached"] is False
        assert out["cleared"] is False
    finally:
        await _cleanup(db_pool)
        g.invalidate_kill_cache()


# --- flow (edge-triggered alerting) -------------------------------------------


async def _run_flow(budget_result: dict) -> list[str]:
    """Run LLMSpendGuardFlow against a stubbed activity; return alerts sent."""
    import uuid

    from aegis_worker.flows.llm_spend_guard import LLMSpendGuardConfig, LLMSpendGuardFlow
    from temporalio import activity
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    sent: list[str] = []

    @activity.defn(name="check_llm_budget")
    async def check_llm_budget() -> dict:
        return budget_result

    @activity.defn(name="send_system_event")
    async def send_system_event(message: str, chat_id: int = 0) -> dict:
        sent.append(message)
        return {"ok": True}

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"tq-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[LLMSpendGuardFlow],
            activities=[check_llm_budget, send_system_event],
        ):
            await env.client.execute_workflow(
                LLMSpendGuardFlow.run,
                LLMSpendGuardConfig(agent_id="pandoras-actor"),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
    return sent


async def test_flow_alerts_on_breach():
    sent = await _run_flow(
        {"breached": True, "cleared": False, "tokens": 9, "budget": 1, "message": "over budget"}
    )
    assert sent == ["over budget"]


async def test_flow_alerts_on_clear():
    sent = await _run_flow(
        {"breached": False, "cleared": True, "tokens": 1, "budget": 9, "message": "recovered"}
    )
    assert sent == ["recovered"]


async def test_flow_is_silent_when_nothing_changed():
    """The 15-min tick must not post anything on a normal, quiet run."""
    sent = await _run_flow(
        {"breached": False, "cleared": False, "tokens": 1, "budget": 9, "message": ""}
    )
    assert sent == []


# --- registration (the four-place ritual) -------------------------------------


def test_llm_spend_guard_flow_registered():
    import aegis_worker.__main__ as worker_main
    from aegis_worker.flows.llm_spend_guard import LLMSpendGuardFlow

    assert LLMSpendGuardFlow in worker_main.WORKFLOWS, (
        "LLMSpendGuardFlow must be registered in worker/__main__.py WORKFLOWS list"
    )


def test_check_llm_budget_activity_registered():
    import aegis_worker.__main__ as worker_main
    from temporalio import activity

    names = [activity._Definition.must_from_callable(a).name for a in worker_main.ACTIVITIES]
    assert "check_llm_budget" in names, (
        "LLMGovernorActivities.check_llm_budget must be in __main__.ACTIVITIES list"
    )


def test_llm_spend_guard_flow_in_schedule_map():
    from aegis_worker.schedule_sync import _ACTIVITY_TYPE_MAP

    assert "LLMSpendGuardFlow" in _ACTIVITY_TYPE_MAP
    _cls, config = _ACTIVITY_TYPE_MAP["LLMSpendGuardFlow"](
        {"agent_id": "pandoras-actor", "config": {}}
    )
    assert config.agent_id == "pandoras-actor"


def test_llm_spend_guard_seeded():
    """The seed row's agent_id must be a real agent (activities.agent_id is an FK)."""
    from pathlib import Path

    import yaml

    repo = Path(__file__).resolve().parents[2]
    activities = yaml.safe_load((repo / "config" / "seed" / "activities.yaml").read_text())
    agents = yaml.safe_load((repo / "config" / "seed" / "agents.yaml").read_text())

    rows = [a for a in activities["activities"] if a["workflow_type"] == "LLMSpendGuardFlow"]
    assert len(rows) == 1, "expected exactly one LLMSpendGuardFlow seed row"
    agent_ids = {a["id"] for a in agents["agents"]}
    assert rows[0]["agent_id"] in agent_ids
