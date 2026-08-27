"""JiraSyncFlow / JiraActivities.

Why this exists at all: Jira sends NO notification for a transition you make
yourself, so `email_task_links` can only ever close tickets somebody else
resolved. Measured on real data, 7 of 15 open APP- tasks had no Gmail message
about them ever. Asking Jira is the only signal that covers your own.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from aegis_worker.activities.jira import DEFAULT_KEY_PATTERN, JiraActivities, _parse_issues
from temporalio.testing import ActivityEnvironment

BASE = "https://stocky.atlassian.net"
NEW_PATH = f"{BASE}/rest/api/3/search/jql"
OLD_PATH = f"{BASE}/rest/api/3/search"


def _issue(key: str, status: str, resolution: str | None) -> dict:
    return {
        "key": key,
        "fields": {
            "status": {"name": status},
            "resolution": {"name": resolution} if resolution else None,
        },
    }


class _RecordingConnector:
    def __init__(self):
        self.submitted: list[dict] = []

    async def commands(self, cmds: list[dict]) -> dict:
        self.submitted.extend(cmds)
        return {
            "ok": True,
            "data": {"sync_status": {c["uuid"]: "ok" for c in cmds}, "temp_id_mapping": {}},
            "error": None,
            "retryable": False,
        }


def _acts(db_pool=None, connector=None, **kw):
    return JiraActivities(
        db_pool=db_pool,
        connector=connector,
        base_url=kw.get("base_url", BASE),
        email=kw.get("email", "me@example.com"),
        api_token=kw.get("api_token", "tok"),
    )


# --------------------------------------------------------------------- parsing


def test_resolution_not_status_decides_resolved():
    """Status names are per-workflow; `resolution` is the portable signal.

    This board calls done "Deployed" and has an in-flight state called
    "Committed" — a status allow-list would either miss the first or close the
    second.
    """
    parsed = _parse_issues(
        {
            "issues": [
                _issue("APP-1", "Deployed", "Done"),
                _issue("APP-2", "Committed", None),
                _issue("APP-3", "Waiting to Validate", None),
                _issue("APP-4", "Closed", "Won't Do"),
            ]
        }
    )
    assert [k for k, v in parsed.items() if v["resolution"]] == ["APP-1", "APP-4"]
    assert parsed["APP-2"] == {"resolution": None, "status": "Committed"}


def test_key_pattern_matches_a_todoist_title():
    import re

    assert re.match(DEFAULT_KEY_PATTERN, "APP-11399: Upgrade prod bcp helm chart").group(0) == (
        "APP-11399"
    )
    assert re.match(DEFAULT_KEY_PATTERN, "Buy milk") is None


# ------------------------------------------------------------------ configured


@pytest.mark.asyncio
async def test_unconfigured_makes_no_request_and_does_not_raise():
    """Ships active but inert — a blank token must be a no-op, not a failure."""
    for missing in ({"base_url": ""}, {"email": ""}, {"api_token": ""}):
        acts = _acts(**missing)
        out = await ActivityEnvironment().run(acts.fetch_jira_task_states)
        assert out["reason"] == "not_configured"
        assert out["resolved"] == []


# ------------------------------------------------------------------------ read


async def _seed(db_pool, rows: list[tuple[str, str, bool]]):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM todoist_tasks WHERE content LIKE 'APP-%'")
        for tid, content, completed in rows:
            await conn.execute(
                "INSERT INTO todoist_tasks (id, content, labels, is_completed) "
                "VALUES ($1,$2,'{}',$3)",
                tid,
                content,
                completed,
            )


@pytest.mark.asyncio
@respx.mock
async def test_fetch_returns_only_resolved_open_tasks(db_pool):
    await _seed(
        db_pool,
        [
            ("j-1", "APP-1: resolved upstream", False),
            ("j-2", "APP-2: still in flight", False),
            ("j-3", "APP-9: already done in todoist", True),  # completed -> not a candidate
        ],
    )
    route = respx.post(NEW_PATH).mock(
        return_value=httpx.Response(
            200, json={"issues": [_issue("APP-1", "Deployed", "Done"), _issue("APP-2", "Dev", None)]}
        )
    )
    out = await ActivityEnvironment().run(_acts(db_pool=db_pool).fetch_jira_task_states)

    assert out["reason"] == "ok"
    assert out["checked"] == 2 and out["unresolved"] == 1
    assert [r["key"] for r in out["resolved"]] == ["APP-1"]
    assert out["resolved"][0]["task_id"] == "j-1"
    # One request for every key, not one per issue.
    assert route.call_count == 1
    sent = route.calls[0].request
    assert b"APP-1" in sent.content and b"APP-2" in sent.content
    assert b"APP-9" not in sent.content


@pytest.mark.asyncio
@respx.mock
async def test_falls_back_to_the_legacy_search_endpoint(db_pool):
    """Jira Cloud replaced /search with /search/jql; both must work."""
    await _seed(db_pool, [("j-1", "APP-1: thing", False)])
    respx.post(NEW_PATH).mock(return_value=httpx.Response(410))
    old = respx.post(OLD_PATH).mock(
        return_value=httpx.Response(200, json={"issues": [_issue("APP-1", "Done", "Done")]})
    )
    out = await ActivityEnvironment().run(_acts(db_pool=db_pool).fetch_jira_task_states)
    assert [r["key"] for r in out["resolved"]] == ["APP-1"]
    assert old.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_auth_failure_degrades_and_never_closes_anything(db_pool):
    """A bad token must not look like 'nothing is resolved'."""
    await _seed(db_pool, [("j-1", "APP-1: thing", False)])
    respx.post(NEW_PATH).mock(return_value=httpx.Response(401))
    out = await ActivityEnvironment().run(_acts(db_pool=db_pool).fetch_jira_task_states)
    assert out["resolved"] == []
    assert out["reason"].startswith("jira_error")
    assert "401" in out["reason"]


@pytest.mark.asyncio
@respx.mock
async def test_key_jira_does_not_know_is_left_alone(db_pool):
    """A moved or mistyped key returns no issue — never treat that as resolved."""
    await _seed(db_pool, [("j-1", "APP-404: never existed", False)])
    respx.post(NEW_PATH).mock(return_value=httpx.Response(200, json={"issues": []}))
    out = await ActivityEnvironment().run(_acts(db_pool=db_pool).fetch_jira_task_states)
    assert out["checked"] == 1 and out["resolved"] == [] and out["unresolved"] == 0


# ----------------------------------------------------------------------- write


@pytest.mark.asyncio
async def test_close_leaves_a_note_then_completes(db_pool):
    connector = _RecordingConnector()
    acts = _acts(db_pool=db_pool, connector=connector)
    out = await ActivityEnvironment().run(
        acts.close_resolved_jira_tasks,
        [{"task_id": "j-1", "key": "APP-1", "resolution": "Done", "status": "Deployed"}],
    )
    assert out == {"closed": 1, "queued": 0, "failed": 0}
    assert [c["type"] for c in connector.submitted] == ["note_add", "item_complete"]
    note = connector.submitted[0]["args"]["content"]
    assert "APP-1" in note and "Done" in note
    assert f"{BASE}/browse/APP-1" in note


@pytest.mark.asyncio
async def test_close_is_a_noop_without_items_or_connector(db_pool):
    assert await ActivityEnvironment().run(
        _acts(db_pool=db_pool, connector=_RecordingConnector()).close_resolved_jira_tasks, []
    ) == {"closed": 0, "queued": 0, "failed": 0}
