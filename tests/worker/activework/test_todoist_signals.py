import pytest_asyncio
from aegis_worker.activework import todoist


@pytest_asyncio.fixture(loop_scope="function")
async def _seed(db_pool):
    await db_pool.execute("DELETE FROM todoist_tasks WHERE id IN ('9001', '9002', '9003')")
    await db_pool.execute(
        "INSERT INTO todoist_tasks (id, content, labels, due_date, is_completed) VALUES "
        "('9001', 'fix aegis parser', ARRAY['@pandora'], '2026-06-19', false),"   # overdue
        "('9002', 'future task', ARRAY['@me'], '2099-01-01', false),"             # future
        "('9003', 'done task', ARRAY['@me'], '2026-06-19', true)"                 # completed
    )
    yield
    await db_pool.execute("DELETE FROM todoist_tasks WHERE id IN ('9001', '9002', '9003')")


async def test_due_today_or_overdue_filters(db_pool, _seed):
    rows = await todoist.due_today_or_overdue(db_pool, "2026-06-20")
    contents = {r["content"] for r in rows}
    assert "fix aegis parser" in contents      # overdue, open
    assert "future task" not in contents       # future
    assert "done task" not in contents         # completed
    row = next(r for r in rows if r["content"] == "fix aegis parser")
    assert row["labels"] == ["@pandora"]
    assert str(row["due_date"]) == "2026-06-19"


async def test_no_pool_degrades_to_empty():
    assert await todoist.due_today_or_overdue(None, "2026-06-20") == []
