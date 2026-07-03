# tests/worker/activities/test_alert_governance.py
from __future__ import annotations

import datetime as _dt

import pytest
from aegis.db import run_migrations
from aegis_worker.activities.alert_governance import (
    AlertGovernanceActivities,
    CheckMuteInput,
    StagePendingPrInput,
    WriteMuteInput,
)
from temporalio.testing import ActivityEnvironment


async def _prep(db_pool):
    """Migrate + truncate governance tables. Called inline at the top of
    each test to avoid pytest fixture sequencing issues with the pool."""
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        await conn.execute("TRUNCATE alert_mutes, pending_prs")


@pytest.mark.asyncio
async def test_check_mute_returns_false_when_no_row(db_pool):
    await _prep(db_pool)
    act = AlertGovernanceActivities(db_pool=db_pool)
    env = ActivityEnvironment()
    muted = await env.run(
        act.check_alert_mute,
        CheckMuteInput(mute_key="github:youruser/aegis:ci-test"),
    )
    assert muted is False


@pytest.mark.asyncio
async def test_write_then_check_mute(db_pool):
    await _prep(db_pool)
    act = AlertGovernanceActivities(db_pool=db_pool)
    env = ActivityEnvironment()
    await env.run(
        act.write_alert_mute,
        WriteMuteInput(
            mute_key="github:youruser/aegis:ci-test",
            ttl_seconds=3600,
            reason="active_dev",
            created_by="interaction-abc",
        ),
    )
    muted = await env.run(
        act.check_alert_mute,
        CheckMuteInput(mute_key="github:youruser/aegis:ci-test"),
    )
    assert muted is True


@pytest.mark.asyncio
async def test_expired_mute_treated_as_not_muted(db_pool):
    await _prep(db_pool)
    act = AlertGovernanceActivities(db_pool=db_pool)
    env = ActivityEnvironment()
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO alert_mutes (mute_key, muted_until) VALUES ($1, $2)",
            "github:youruser/aegis:expired",
            _dt.datetime.now(_dt.UTC) - _dt.timedelta(seconds=1),
        )
    muted = await env.run(
        act.check_alert_mute,
        CheckMuteInput(mute_key="github:youruser/aegis:expired"),
    )
    assert muted is False


@pytest.mark.asyncio
async def test_stage_pending_pr_creates_row(db_pool):
    await _prep(db_pool)
    act = AlertGovernanceActivities(db_pool=db_pool)
    env = ActivityEnvironment()
    pr_id = await env.run(
        act.stage_pending_pr,
        StagePendingPrInput(
            alert_fingerprint="github:workflow_run:youruser/aegis:abc12345",
            repo="youruser/aegis",
            branch="fix/null-check",
            title="fix: null check in foo()",
            body="Kimi verdict summary",
            diff="--- a/foo.py\n+++ b/foo.py\n...",
            kimi_session_id="sess-123",
        ),
    )
    assert pr_id
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT repo, status FROM pending_prs WHERE id = $1::uuid", pr_id)
    assert row["repo"] == "youruser/aegis"
    assert row["status"] == "pending"


class _FakeRemote:
    _repo_base = "/home/user/Workspace"
    _host = "kimi-host"

    def __init__(self, results: list[dict] | None = None):
        self.commands: list[tuple[str, str]] = []
        self._results = list(results or [])

    async def ensure_config(self):
        pass

    async def run_on_host(self, host, cmd, timeout=30, stdin=None):
        self.commands.append((host, cmd))
        if self._results:
            return self._results.pop(0)
        return {"status": "succeeded", "exit_code": 0, "stdout": "", "stderr": ""}


@pytest.mark.asyncio
async def test_create_github_pr_push_failure_does_not_crash_on_logging(db_pool):
    """Regression: a failed git push must return status='failed', NOT raise.

    The push-failure branch logs via activity.logger.warning(...). Passing
    structlog-style kwargs (repo=, error=) to Temporal's stdlib activity
    logger raises `TypeError: Logger._log() got an unexpected keyword
    argument 'repo'` at the default-enabled WARNING level — which killed the
    real AlertInvestigationFlow after kimi pushed a branch (live 2026-05-30).
    """
    import aegis_worker.activities.alert_governance as agmod

    await _prep(db_pool)
    # First run_on_host call (the git push) fails with a remote error.
    act = AlertGovernanceActivities(
        db_pool=db_pool,
        remote_script=_FakeRemote(
            results=[
                {"status": "failed", "exit_code": 1, "stdout": "", "stderr": "remote: push denied"}
            ]
        ),
    )
    env = ActivityEnvironment()

    pr_id = await env.run(
        act.stage_pending_pr,
        StagePendingPrInput(
            alert_fingerprint="chat-investigate:acme/bcp:exec-info",
            repo="acme/bcp",
            branch="aegis-fix/chat-investigate-x",
            title="fix: remove invalid exec_info kwarg",
            body="verdict",
            diff="--- a/x\n+++ b/x\n",
            kimi_session_id="sess-x",
        ),
    )

    result = await env.run(
        act.create_github_pr,
        agmod.CreateGithubPrInput(
            pending_pr_id=str(pr_id),
            repo="acme/bcp",
            branch="aegis-fix/chat-investigate-x",
            base="master",
        ),
    )

    assert result["status"] == "failed"
    assert "git push failed" in result["error"]
