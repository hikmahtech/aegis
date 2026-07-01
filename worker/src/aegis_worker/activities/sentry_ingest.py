"""SentryIngestActivities — fetch new Sentry issues, convert to alert shape, manage cursor."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog
from temporalio import activity

logger = structlog.get_logger()


@dataclass
class FetchNewIssuesInput:
    since_issue_id: str | None = None
    limit: int = 25


@dataclass
class FetchNewIssuesResult:
    issues: list[dict] = field(default_factory=list)
    latest_issue_id: str | None = None


@dataclass
class SentryIngestActivities:
    db_pool: Any
    sentry_url: str
    sentry_token: str
    sentry_org: str
    http_client: httpx.AsyncClient | None = None
    sentry_projects: list[int] = field(default_factory=list)

    @activity.defn
    async def fetch_new_issues(self, input: FetchNewIssuesInput) -> FetchNewIssuesResult:
        """Fetch new Sentry issues, optionally filtered to those newer than since_issue_id."""
        if not self.sentry_url:
            return FetchNewIssuesResult()

        url = f"{self.sentry_url}/api/0/organizations/{self.sentry_org}/issues/"
        # Valid Sentry sort keys: date|new|priority|freq|user|trends|inbox.
        # `new` sorts by first-seen (creation) descending — what we want for
        # incremental polling filtered by since_issue_id.
        params: dict[str, Any] = {"sort": "new", "limit": input.limit}
        if self.sentry_projects:
            params["project"] = [str(p) for p in self.sentry_projects]

        client = self.http_client or httpx.AsyncClient()
        try:
            resp = await client.get(
                url,
                headers={"Authorization": f"Bearer {self.sentry_token}"},
                params=params,
            )
            resp.raise_for_status()
        finally:
            if self.http_client is None:
                await client.aclose()

        raw: list[dict] = resp.json()

        # Sentry returns newest first by default. Filter: keep only issues
        # where id > since_issue_id (i.e. stop when we hit since_issue_id or older).
        issues: list[dict] = []
        for issue in raw:
            if input.since_issue_id and issue.get("id") == input.since_issue_id:
                break
            issues.append(issue)

        latest_issue_id = raw[0]["id"] if raw else None

        activity.logger.info(
            "sentry_fetch_done",
            fetched=len(raw),
            new=len(issues),
            latest_issue_id=latest_issue_id,
        )
        return FetchNewIssuesResult(issues=issues, latest_issue_id=latest_issue_id)

    @activity.defn
    async def issue_to_alert(self, issue: dict) -> dict:
        """Convert a Sentry issue dict to the alert shape AlertInvestigationFlow consumes."""
        project = issue.get("project") or {}
        metadata = issue.get("metadata") or {}
        culprit = issue.get("culprit") or ""
        meta_value = metadata.get("value") or ""
        description = culprit + ("\n" + meta_value if meta_value else "")

        return {
            "source": "sentry",
            "title": issue.get("title", ""),
            "fingerprint": f"sentry:{issue.get('id', '')}",
            "severity": issue.get("level", "error"),
            "service": (project if isinstance(project, dict) else {}).get("slug", ""),
            "description": description,
            "labels": {
                "environment": issue.get("environment") or "",
                "platform": issue.get("platform") or "",
            },
            "raw_payload": issue,
        }

    @activity.defn
    async def read_sentry_cursor(self) -> str | None:
        """Read the last ingested Sentry issue ID from the settings table."""
        if not self.db_pool:
            return None
        row = await self.db_pool.fetchrow(
            "SELECT value->>'last_issue_id' AS last_issue_id "
            "FROM settings WHERE key = 'sentry_last_issue_id'"
        )
        return row["last_issue_id"] if row else None

    @activity.defn
    async def write_sentry_cursor(self, last_issue_id: str) -> None:
        """Persist the latest ingested Sentry issue ID to the settings table."""
        if not self.db_pool:
            return
        await self.db_pool.execute(
            """
            INSERT INTO settings (key, value)
            VALUES ('sentry_last_issue_id', jsonb_build_object('last_issue_id', $1::text))
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
            """,
            last_issue_id,
        )
        activity.logger.info(f"sentry_cursor_written last_issue_id={last_issue_id}")

    # ingest_idempotency_claim intentionally NOT defined here —
    # ChannelActivities.ingest_idempotency_claim is the single shared
    # implementation used by every ingest flow (gmail/receipt/raindrop/rss/sentry).
    # Duplicating the @activity.defn here crashes Worker() at startup with
    # "More than one activity named ingest_idempotency_claim" (cmemory lesson).
