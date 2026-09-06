"""`MoneyActivities.books_write` — the worker end of a chat-tool books write.

The write itself (`ledger_write.perform_write`) is driven end-to-end against a
real hledger checkout by `tests/core/test_ledger_tools.py`. What is only
testable here is the wiring: the activity has to hand the writer THIS worker's
pool and books config, relay its answer untouched, and be registered under the
name `BooksWriteFlow` calls it by.
"""

from __future__ import annotations

import pytest
from aegis.services import books, ledger_write
from aegis_worker.activities.money import MoneyActivities
from temporalio import activity
from temporalio.testing import ActivityEnvironment


def _act(**kw) -> MoneyActivities:
    return MoneyActivities(db_pool="the-pool", llm=None, delivery=None, **kw)


def test_the_flow_calls_this_activity_by_the_name_it_is_registered_under():
    """`BooksWriteFlow` names the activity as a string, so a rename here is a
    workflow that fails at call time with 'activity type not registered'."""
    defn = activity._Definition.must_from_callable(MoneyActivities.books_write)
    assert defn.name == "books_write"


@pytest.mark.asyncio
async def test_the_write_gets_this_workers_pool_and_books_config(monkeypatch, tmp_path):
    seen: dict = {}

    async def fake_perform(op, payload, pool, cfg):
        seen.update(op=op, payload=payload, pool=pool, cfg=cfg)
        return {"ok": True, "message": "posted manual/deadbeef to personal/2026.journal"}

    monkeypatch.setattr(ledger_write, "perform_write", fake_perform)
    cfg = books.BooksConfig(path=tmp_path / "books")
    out = await ActivityEnvironment().run(
        _act(books_cfg=cfg).books_write, "post", {"entity": "personal"}
    )
    assert out == {"ok": True, "message": "posted manual/deadbeef to personal/2026.journal"}
    assert seen["op"] == "post"
    assert seen["payload"] == {"entity": "personal"}
    assert seen["pool"] == "the-pool"
    assert seen["cfg"] is cfg


@pytest.mark.asyncio
async def test_books_unconfigured_on_the_worker_is_said_out_loud(monkeypatch):
    """Core validated the call against its own checkout and dispatched it, so
    this is a split configuration — not an idle lane. Returning `ok: True`, or
    a message the model reads as "nothing to do", would let the user believe a
    transaction was recorded."""

    async def never(*a, **kw):  # pragma: no cover — must not be reached
        raise AssertionError("the writer must not run without a checkout")

    monkeypatch.setattr(ledger_write, "perform_write", never)
    out = await ActivityEnvironment().run(_act(books_cfg=None).books_write, "post", {})
    assert out["ok"] is False
    assert "not configured on the worker" in out["message"]
    assert "Nothing was written" in out["message"]
