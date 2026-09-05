"""The four activities that carry the brief and the close to a reader:
`render_money_brief`, `render_month_close`, `notify_money_message` and
`write_money_report`. No DB, no hledger — only the seams."""

from __future__ import annotations

import pytest
from aegis.services import books
from aegis_worker.activities.money import HOME_SYMBOL, MoneyActivities
from temporalio.testing import ActivityEnvironment


class _Delivery:
    """The shape `safe_send_message` actually inspects."""

    channel = "slack"
    db_pool = None

    def __init__(self, ok: bool = True):
        self.sent: list[dict] = []
        self._ok = ok

    async def send_message(self, *, agent_id, message, chat_id=0):
        self.sent.append({"agent_id": agent_id, "message": message, "chat_id": chat_id})
        return {"ok": self._ok}


def _act(**kw) -> MoneyActivities:
    return MoneyActivities(db_pool=None, llm=None, delivery=None, fx_rates={}, **kw)


_BRIEF = {
    "as_of": "2026-09-06",
    "since": "2026-08-30",
    "books_ok": True,
    "entities": {"personal": {"income": "0", "expenses": "1200.00"}},
    "dues": [],
    "forecast": [],
    "bal_text": "",
    "fx_unconverted": ["$"],
    "fx_stale": True,
}


@pytest.mark.asyncio
async def test_render_money_brief_activity_returns_both_renderings():
    out = await ActivityEnvironment().run(_act().render_money_brief, _BRIEF)
    assert out["html"].startswith("<b>Money brief · 2026-08-30 → 2026-09-06</b>")
    assert "Personal: in ₹0.00 · out ₹1,200.00" in out["html"]
    assert out["markdown"].startswith("# Money brief · 2026-08-30 → 2026-09-06")
    # The fx caveat has to survive the activity boundary, not just the module.
    assert "$" in out["html"] and "exchange rate" in out["html"]


@pytest.mark.asyncio
async def test_render_money_brief_names_the_commodity_hledger_converted_to():
    """`build_money_brief` runs `hledger -X ₹`, so the caveat must name ₹ —
    not whatever `home_currency` happens to say."""
    out = await ActivityEnvironment().run(
        _act(home_currency="INR").render_money_brief,
        {**_BRIEF, "fx_unconverted": []},
    )
    assert f"converted to {HOME_SYMBOL}" in out["html"]


@pytest.mark.asyncio
async def test_render_month_close_activity():
    out = await ActivityEnvironment().run(
        _act().render_month_close,
        {
            "month": "2026-08",
            "books_ok": True,
            "is_text": "IS",
            "bs_text": "BS",
            "recurring_total": "100.00",
            "unknown_count": 0,
            "dues_paid": 0,
            "dues_open": 0,
        },
    )
    assert out["html"].startswith("<b>Month close · 2026-08</b>")
    assert "Recurring commitments: ₹100.00" in out["html"]


@pytest.mark.asyncio
async def test_notify_money_message_sends_the_html_as_maou():
    delivery = _Delivery()
    act = _act()
    act.delivery = delivery
    await ActivityEnvironment().run(
        act.notify_money_message, "<b>Money brief</b>", "money_brief_notify_failed"
    )
    assert delivery.sent == [
        {"agent_id": "maou", "message": "<b>Money brief</b>", "chat_id": 0}
    ]


@pytest.mark.asyncio
async def test_notify_money_message_never_raises():
    """A dead comms server must not fail the brief — the report is still filed."""

    class Boom(_Delivery):
        async def send_message(self, **kw):
            raise RuntimeError("comms down")

    act = _act()
    act.delivery = Boom()
    await ActivityEnvironment().run(act.notify_money_message, "<b>x</b>", "money_brief_notify_failed")


@pytest.mark.asyncio
async def test_write_money_report_is_a_noop_without_a_checkout(monkeypatch):
    called: list = []

    async def fake_write(rel_path, text, cfg):
        called.append(rel_path)

    monkeypatch.setattr(books, "write_report", fake_write)
    await ActivityEnvironment().run(
        _act(books_cfg=None).write_money_report, "reports/weekly/2026-09-06.md", "# x"
    )
    assert called == []


@pytest.mark.asyncio
async def test_write_money_report_writes_through_books(monkeypatch):
    called: list = []

    async def fake_write(rel_path, text, cfg):
        called.append((rel_path, text, cfg))

    monkeypatch.setattr(books, "write_report", fake_write)
    await ActivityEnvironment().run(
        _act(books_cfg="CFG").write_money_report, "reports/monthly/2026-08.md", "# c"
    )
    assert called == [("reports/monthly/2026-08.md", "# c", "CFG")]


@pytest.mark.asyncio
async def test_write_money_report_swallows_a_books_error(monkeypatch):
    """A locked or unclonable checkout loses the filed copy, never the brief."""

    async def boom(rel_path, text, cfg):
        raise books.BooksError("hledger check --strict failed")

    monkeypatch.setattr(books, "write_report", boom)
    await ActivityEnvironment().run(
        _act(books_cfg="CFG").write_money_report, "reports/weekly/x.md", "# x"
    )
