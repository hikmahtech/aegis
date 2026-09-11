"""services/runbooks.py — per-alert runbooks stored as data (#499).

Real test database (no DB mocks). Every row this file writes has a name that
normalises to a `zzrbsvc` prefix, so the wipe cannot touch another file's rows
on a shared per-xdist-worker database.
"""

from __future__ import annotations

import asyncpg
import pytest
import pytest_asyncio
from aegis.db import run_migrations
from aegis.services import runbooks as rb

PREFIX = "zzrbsvc"


@pytest_asyncio.fixture(loop_scope="function")
async def pool(db_pool):
    await run_migrations(db_pool)
    await db_pool.execute("DELETE FROM runbooks WHERE name_key LIKE $1", f"{PREFIX}%")
    yield db_pool
    await db_pool.execute("DELETE FROM runbooks WHERE name_key LIKE $1", f"{PREFIX}%")


# ── normalisation ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw",
    [
        "Dagster Pipeline Failure",
        "dagster-pipeline-failure",
        "DagsterPipelineFailure",
        "dagster_pipeline_failure",
        "  DAGSTER pipeline.failure  ",
    ],
)
def test_every_spelling_of_one_alert_name_is_one_key(raw):
    # Alert names arrive as a Prometheus alertname ("NodeDown"), a Grafana rule
    # title ("Dagster Pipeline Failure") or the hub's slug of either
    # ("dagster-pipeline-failure"). One runbook must answer all of them.
    assert rb.normalise_name(raw) == "dagsterpipelinefailure"


def test_normalise_keeps_digits_and_folds_case():
    assert rb.normalise_name("MongoDB42Down") == "mongodb42down"
    assert rb.normalise_name("NodeDown") == rb.normalise_name("nodedown")


@pytest.mark.parametrize("raw", ["", "   ", "---", None])
def test_a_name_with_no_letters_or_digits_has_no_key(raw):
    assert rb.normalise_name(raw) == ""


# ── strict validation on write ───────────────────────────────────────────────


def test_validate_returns_key_name_and_stripped_body():
    key, name, body = rb.validate(" Node Down ", "\n# NodeDown\n\nCheck power first.\n\n")
    assert key == "nodedown"
    assert name == "Node Down"
    assert body == "# NodeDown\n\nCheck power first."


@pytest.mark.parametrize(
    ("name", "body", "needle"),
    [
        ("", "text", "name"),
        ("---", "text", "letter or digit"),
        ("x" * (rb.MAX_NAME_CHARS + 1), "text", "name"),
        ("NodeDown", "", "body"),
        ("NodeDown", "   \n\t", "body"),
        ("NodeDown", "x" * (rb.MAX_BODY_CHARS + 1), str(rb.MAX_BODY_CHARS)),
        ("NodeDown", "# NodeDown\n\nTODO: fill in", "stub"),
        ("NodeDown", None, "body"),
        (None, "text", "name"),
    ],
)
def test_validate_rejects_what_would_save_and_then_do_nothing(name, body, needle):
    with pytest.raises(ValueError, match=needle):
        rb.validate(name, body)


def test_a_body_at_the_cap_is_accepted():
    _, _, body = rb.validate("NodeDown", "x" * rb.MAX_BODY_CHARS)
    assert len(body) == rb.MAX_BODY_CHARS


# ── the store ────────────────────────────────────────────────────────────────


async def test_put_then_get_by_any_spelling(pool):
    saved = await rb.put_runbook(
        pool, f"{PREFIX} Pipeline Failure", "# Runbook\n\nstep one", updated_by="tester"
    )
    assert saved["name_key"] == f"{PREFIX}pipelinefailure"
    assert saved["name"] == f"{PREFIX} Pipeline Failure"
    assert saved["body"] == "# Runbook\n\nstep one"
    assert saved["updated_by"] == "tester"
    assert saved["created"] is True

    for spelling in (f"{PREFIX}-pipeline-failure", f"{PREFIX.upper()}PipelineFailure"):
        got = await rb.get_runbook(pool, spelling)
        assert got is not None, spelling
        assert got["body"] == "# Runbook\n\nstep one"


async def test_a_second_put_under_another_spelling_replaces_the_same_row(pool):
    await rb.put_runbook(pool, f"{PREFIX}NodeDown", "old", updated_by="a")
    again = await rb.put_runbook(pool, f"{PREFIX}-node-down", "new", updated_by="b")
    assert again["created"] is False
    # The spelling the operator last saved is the one shown.
    assert again["name"] == f"{PREFIX}-node-down"

    count = await pool.fetchval(
        "SELECT count(*) FROM runbooks WHERE name_key = $1", f"{PREFIX}nodedown"
    )
    assert count == 1
    got = await rb.get_runbook(pool, f"{PREFIX}NodeDown")
    assert got["body"] == "new"
    assert got["updated_by"] == "b"


async def test_put_validates_before_touching_the_table(pool):
    with pytest.raises(ValueError):
        await rb.put_runbook(pool, f"{PREFIX}Empty", "  ", updated_by="t")
    assert await rb.get_runbook(pool, f"{PREFIX}Empty") is None


async def test_get_is_lenient(pool):
    # No row, and a name with no key, both read as "no runbook" — never raise.
    assert await rb.get_runbook(pool, f"{PREFIX}Missing") is None
    assert await rb.get_runbook(pool, "") is None
    assert await rb.get_runbook(pool, "---") is None


async def test_list_is_sorted_and_carries_no_body(pool):
    await rb.put_runbook(pool, f"{PREFIX}Beta", "b" * 10, updated_by="t")
    await rb.put_runbook(pool, f"{PREFIX}Alpha", "a" * 3, updated_by="t")
    mine = [r for r in await rb.list_runbooks(pool) if r["name_key"].startswith(PREFIX)]
    assert [r["name"] for r in mine] == [f"{PREFIX}Alpha", f"{PREFIX}Beta"]
    assert [r["chars"] for r in mine] == [3, 10]
    assert all("body" not in r for r in mine)


async def test_delete_by_any_spelling(pool):
    await rb.put_runbook(pool, f"{PREFIX}Service Down", "body", updated_by="t")
    assert await rb.delete_runbook(pool, f"{PREFIX}-service-down") is True
    assert await rb.get_runbook(pool, f"{PREFIX}ServiceDown") is None
    assert await rb.delete_runbook(pool, f"{PREFIX}ServiceDown") is False
    assert await rb.delete_runbook(pool, "") is False


async def test_the_table_refuses_a_key_the_service_would_never_write(pool):
    # A hand-written INSERT that skips normalise_name would be a row no lookup
    # can ever reach. The CHECK makes that a loud failure instead.
    with pytest.raises(asyncpg.CheckViolationError):
        await pool.execute(
            "INSERT INTO runbooks (name_key, name, body) VALUES ($1, 'x', 'body')",
            f"{PREFIX}Node-Down",
        )
    with pytest.raises(asyncpg.CheckViolationError):
        await pool.execute(
            "INSERT INTO runbooks (name_key, name, body) VALUES ($1, 'x', '   ')",
            f"{PREFIX}blank",
        )
