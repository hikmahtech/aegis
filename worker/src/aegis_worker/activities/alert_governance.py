"""Alert governance activities — mute check/write for AlertInvestigationFlow.

A non-expired row in alert_mutes short-circuits the investigation pipeline
before Gate 1 fires. Mute keys follow "<source>:<service>:<subkey>".
"""

from __future__ import annotations

import datetime as _dt
import shlex
from dataclasses import dataclass
from typing import Any

import structlog
from temporalio import activity

logger = structlog.get_logger()


@dataclass
class CheckMuteInput:
    mute_key: str


@dataclass
class WriteMuteInput:
    mute_key: str
    ttl_seconds: int
    reason: str = ""
    created_by: str = ""


@dataclass
class StagePendingPrInput:
    alert_fingerprint: str
    repo: str
    branch: str
    title: str
    body: str
    interaction_id: str | None = None
    diff: str = ""
    kimi_session_id: str = ""


@dataclass
class CreateGithubPrInput:
    pending_pr_id: str
    repo: str       # "owner/name"
    branch: str
    base: str = "main"
    host: str = ""  # host where kimi made the branch; "" ⇒ connector default
    # Workspace-relative checkout path (resource metadata.path, e.g.
    # "acme/bcp"). "" falls back to the basename of `repo` for
    # backward compatibility with flat layouts.
    repo_path: str = ""


@dataclass
class AlertGovernanceActivities:
    db_pool: Any
    remote_script: Any = None  # RemoteScriptConnector — optional; needed for create_github_pr

    @activity.defn
    async def check_alert_mute(self, input: CheckMuteInput) -> bool:
        if not self.db_pool or not input.mute_key:
            return False
        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT muted_until FROM alert_mutes WHERE mute_key = $1",
                input.mute_key,
            )
        if not row:
            return False
        return row["muted_until"] > _dt.datetime.now(_dt.UTC)

    @activity.defn
    async def write_alert_mute(self, input: WriteMuteInput) -> None:
        if not self.db_pool or not input.mute_key:
            return
        until = _dt.datetime.now(_dt.UTC) + _dt.timedelta(seconds=input.ttl_seconds)
        async with self.db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO alert_mutes (mute_key, muted_until, reason, created_by)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (mute_key) DO UPDATE SET
                    muted_until = EXCLUDED.muted_until,
                    reason = EXCLUDED.reason,
                    created_by = EXCLUDED.created_by
                """,
                input.mute_key,
                until,
                input.reason,
                input.created_by,
            )
        activity.logger.info(
            "alert_mute_written",
            mute_key=input.mute_key,
            muted_until=until.isoformat(),
        )

    @activity.defn
    async def stage_pending_pr(self, input: StagePendingPrInput) -> str:
        if not self.db_pool:
            raise RuntimeError("db_pool required for stage_pending_pr")
        async with self.db_pool.acquire() as conn:
            pr_id = await conn.fetchval(
                """
                INSERT INTO pending_prs
                    (interaction_id, alert_fingerprint, repo, branch,
                     title, body, diff, kimi_session_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                RETURNING id
                """,
                input.interaction_id,
                input.alert_fingerprint,
                input.repo,
                input.branch,
                input.title,
                input.body,
                input.diff,
                input.kimi_session_id,
            )
        activity.logger.info(f"pending_pr_staged pr_id={pr_id} repo={input.repo}")
        return str(pr_id)

    @activity.defn
    async def create_github_pr(self, input: CreateGithubPrInput) -> dict:
        """Run `gh pr create` on the remote host and update pending_prs."""
        if not self.remote_script:
            return {"pr_url": "", "status": "failed", "error": "remote_script not configured"}
        if not self.db_pool:
            return {"pr_url": "", "status": "failed", "error": "db_pool not available"}

        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT title, body, branch FROM pending_prs WHERE id = $1",
                input.pending_pr_id,
            )
        if not row:
            return {"pr_url": "", "status": "failed", "error": f"pending_pr {input.pending_pr_id} not found"}

        title = row["title"] or "AEGIS-proposed fix"
        body = row["body"] or ""
        branch = input.branch or row["branch"] or "main"
        rel_path = input.repo_path or (
            input.repo.rsplit("/", 1)[-1] if "/" in input.repo else input.repo
        )
        # DB-first connector config may have changed since bootstrap.
        await self.remote_script.ensure_config()
        repo_path = (
            f"{self.remote_script._repo_base}/{rel_path}"
            if self.remote_script._repo_base
            else rel_path
        )

        # Push the branch kimi created locally so GitHub knows about it.
        push = await self.remote_script.run_on_host(
            input.host,
            f"cd {shlex.quote(repo_path)} && git push -u origin {shlex.quote(branch)}",
            timeout=60,
        )
        if push["exit_code"] == -1:  # ssh error/timeout
            activity.logger.error(f"create_github_pr_push_error error={push['stderr']}")
            return {"pr_url": "", "status": "failed", "error": push["stderr"][:300]}
        if push["exit_code"] != 0:
            err_msg = push["stderr"][:500]
            activity.logger.warning(
                f"create_github_pr_push_failed repo={input.repo} error={err_msg}"
            )
            async with self.db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE pending_prs SET status = 'failed' WHERE id = $1",
                    input.pending_pr_id,
                )
            return {
                "pr_url": "",
                "status": "failed",
                "error": f"git push failed: {err_msg}",
            }

        pr_cmd = (
            f"cd {shlex.quote(repo_path)} && "
            f"gh pr create "
            f"--repo {shlex.quote(input.repo)} "
            f"--head {shlex.quote(branch)} "
            f"--base {shlex.quote(input.base)} "
            f"--title {shlex.quote(title)} "
            f"--body {shlex.quote(body[:4000])} "
            f"--draft"
        )
        pr = await self.remote_script.run_on_host(input.host, pr_cmd, timeout=60)
        if pr["exit_code"] == -1:  # ssh error/timeout
            activity.logger.error(f"create_github_pr_error error={pr['stderr']}")
            return {"pr_url": "", "status": "failed", "error": pr["stderr"][:300]}

        if pr["exit_code"] != 0:
            err_msg = pr["stderr"][:500]
            activity.logger.warning(f"create_github_pr_failed repo={input.repo} error={err_msg}")
            async with self.db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE pending_prs SET status = 'failed' WHERE id = $1",
                    input.pending_pr_id,
                )
            return {"pr_url": "", "status": "failed", "error": err_msg}

        pr_url = pr["stdout"].strip().splitlines()[-1].strip() if pr["stdout"] else ""
        async with self.db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE pending_prs SET status = 'opened', pr_url = $2 WHERE id = $1",
                input.pending_pr_id,
                pr_url,
            )
        activity.logger.info(f"create_github_pr_opened repo={input.repo} pr_url={pr_url}")
        return {"pr_url": pr_url, "status": "opened", "error": ""}
