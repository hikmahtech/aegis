"""Tests for /api/admin/money/* routes.

Two halves, with different fixtures on purpose:

* `/state` and `/digest` read the books index and the books checkout, so they
  run against a REAL Postgres (`db_pool`) and a REAL temp directory. An
  AsyncMock pool would have let `money_state` return whatever shape the mock
  was told to return, which is exactly the assertion-through-a-fake this repo
  has been bitten by.
* the `POST /{flow}/run` dispatch tests keep the mock pool — they never touch
  the database, and the flow map is the thing under test.
"""

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
import yaml
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.api.routes import money as money_routes
from aegis.config import Settings
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Every seeded row this file writes carries this prefix in its message_id so
# the cleanup can scope on it and a parallel xdist worker on the same database
# is not affected.
PREFIX = "zzt6-money/"


def _settings(**over) -> Settings:
    base = {
        "database_url": "postgresql://test:test@localhost/test",
        "litellm_url": "https://litellm.test/v1",
        "temporal_ui_url": "https://temporal.test",
        "n8n_ui_url": "https://n8n.test",
        "admin_username": "admin",
        "admin_password": "admin",
        "n8n_webhook_secret": "test-secret",
        "api_key": "test-key",
        "money_hygiene_enabled": True,
    }
    base.update(over)
    return Settings(**base)


@pytest.fixture
def settings():
    return _settings()


def _make_pool(conn):
    """Build a db_pool mock whose acquire() works as an async context manager."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    return pool


@pytest.fixture
def app(settings):
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=None)
    pool = _make_pool(conn)
    application = create_app(run_lifespan=False)
    application.dependency_overrides[get_settings] = lambda: settings
    application.state.db_pool = pool
    application.state.settings = settings
    application.state.temporal_client = AsyncMock()
    return application


@pytest.fixture
def client(app):
    return TestClient(app, headers={"X-API-Key": "test-key"})


# --------------------------------------------------------------- /state, /digest

async def _cleanup(pool) -> None:
    await pool.execute(
        "DELETE FROM finance.journal_index WHERE message_id LIKE $1", f"{PREFIX}%"
    )


async def _seed(pool, **over) -> str:
    """Insert one journal_index row; returns its message_id."""
    row: dict = {
        "message_id": f"{PREFIX}{over.pop('suffix', 'a')}",
        "mailbox": "personal",
        "entity": "personal",
        "kind": "transaction",
        "direction": "out",
        "amount": "1234.50",
        "currency": "INR",
        "payee": "Namecheap",
        "payee_key": "namecheap",
        "account": "expenses:saas",
        "channel": "card",
        "instrument": "axis-cc-1313",
        "occurred_on": "2026-09-01",
        "due_on": None,
        "parser": "hdfc_card",
        "confidence": 0.9,
        "source_class": "bank",
        "journal_file": "personal/2026.journal",
        "linked_message_id": None,
        "todoist_ref": None,
    }
    row.update(over)
    # asyncpg binds by Python type, so the ISO strings above have to become
    # real Decimal/date objects — a `::date` cast in the SQL is applied after
    # the bind and does not help.
    row["amount"] = None if row["amount"] is None else Decimal(row["amount"])
    for key in ("occurred_on", "due_on"):
        if isinstance(row[key], str):
            row[key] = date.fromisoformat(row[key])
    await pool.execute(
        """
        INSERT INTO finance.journal_index
          (message_id, mailbox, entity, kind, direction, amount, currency, payee, payee_key,
           account, channel, instrument, occurred_on, due_on, parser, confidence, source_class,
           journal_file, linked_message_id, todoist_ref)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20)
        """,
        *[row[k] for k in (
            "message_id", "mailbox", "entity", "kind", "direction", "amount", "currency",
            "payee", "payee_key", "account", "channel", "instrument", "occurred_on", "due_on",
            "parser", "confidence", "source_class", "journal_file", "linked_message_id",
            "todoist_ref",
        )],
    )
    return row["message_id"]


@pytest.fixture
def books_dir(tmp_path) -> Path:
    """The path the books checkout would live at. Deliberately NOT created:
    a fresh deployment has no checkout, and that is the state the counters
    and the digest have to survive."""
    return tmp_path / "books"


@pytest_asyncio.fixture(loop_scope="function")
async def real_client(db_pool, books_dir):
    """httpx client on a real pool — TestClient's own event loop would
    InterfaceError on the shared asyncpg pool."""
    await _cleanup(db_pool)
    app = create_app(run_lifespan=False)
    app.state.db_pool = db_pool
    app.state.temporal_client = AsyncMock()
    app.dependency_overrides[get_settings] = lambda: _settings(books_path=str(books_dir))
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-API-Key": "test-key"},
    ) as c:
        yield c
    await _cleanup(db_pool)


async def test_money_state_returns_index_events_newest_first(real_client, db_pool):
    """GET /state serialises journal_index rows, newest effective date first.

    The date a row sorts on is `coalesce(occurred_on, due_on)`: a due has no
    `occurred_on` at all, so ordering on `occurred_on` alone would bury every
    unpaid bill at the bottom of the page under a NULL.
    """
    await _seed(db_pool, suffix="old", occurred_on="2026-08-01")
    await _seed(
        db_pool,
        suffix="due",
        kind="due",
        occurred_on=None,
        due_on="2026-09-20",
        payee="Axis Bank",
        amount="95301.29",
        account="liabilities:card:axis:1313",
        journal_file=None,
        todoist_ref="8123456789",
    )

    resp = await real_client.get("/api/admin/money/state")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    mine = [e for e in body["events"] if e["message_id"].startswith(PREFIX)]
    assert [e["message_id"] for e in mine] == [f"{PREFIX}due", f"{PREFIX}old"]

    due, old = mine
    # Amount crosses the wire as the STRING the ledger wrote. A float would
    # round-trip 95301.29 through binary and the page would then disagree with
    # the journal on the last paisa.
    assert due["amount"] == "95301.29"
    assert isinstance(due["amount"], str)
    assert due["kind"] == "due"
    assert due["occurred_on"] is None
    assert due["due_on"] == "2026-09-20"
    assert due["todoist_ref"] == "8123456789"
    assert due["journal_file"] is None
    assert old["amount"] == "1234.50"
    assert old["occurred_on"] == "2026-08-01"
    assert old["journal_file"] == "personal/2026.journal"
    assert old["currency"] == "INR"
    assert old["payee"] == "Namecheap"
    assert old["account"] == "expenses:saas"
    assert old["channel"] == "card"
    assert old["instrument"] == "axis-cc-1313"
    assert old["parser"] == "hdfc_card"
    assert old["source_class"] == "bank"
    assert old["entity"] == "personal"
    assert old["mailbox"] == "personal"
    assert old["direction"] == "out"
    assert old["linked_message_id"] is None
    assert old["confidence"] == pytest.approx(0.9, abs=1e-6)


async def test_money_state_amount_null_stays_null(real_client, db_pool):
    """A row the writer refused (no amount) must not serialise as the
    string "None" — `Decimal(str(...))` downstream raises on that."""
    await _seed(db_pool, suffix="noamt", amount=None)

    body = (await real_client.get("/api/admin/money/state")).json()
    row = next(e for e in body["events"] if e["message_id"] == f"{PREFIX}noamt")

    assert row["amount"] is None


async def test_money_state_counts_unknowns_and_open_dues(real_client, db_pool):
    """`unknown_count` counts recent unknown-account transactions; `dues_open`
    counts dues nothing has been linked to."""
    base = (await real_client.get("/api/admin/money/state")).json()
    # Relative to today: the window is 60 days, so a hard-coded date would
    # turn this into a test that quietly stops asserting anything in 2027.
    recent = (date.today() - timedelta(days=3)).isoformat()
    stale = (date.today() - timedelta(days=400)).isoformat()

    await _seed(db_pool, suffix="unk", account="expenses:unknown", occurred_on=recent)
    # Same unknown account but far outside the 60-day window.
    await _seed(db_pool, suffix="stale", account="expenses:unknown", occurred_on=stale)
    # An unknown account on a due, not a transaction: not an unexplained posting.
    await _seed(
        db_pool, suffix="d1", kind="due", account="expenses:unknown",
        occurred_on=None, due_on="2026-09-20",
    )
    # A due already matched to its payment is closed, not open.
    await _seed(
        db_pool, suffix="d2", kind="due", occurred_on=None, due_on="2026-09-10",
        linked_message_id=f"{PREFIX}unk",
    )

    body = (await real_client.get("/api/admin/money/state")).json()

    assert body["unknown_count"] == base["unknown_count"] + 1
    assert body["dues_open"] == base["dues_open"] + 1


async def test_money_state_reports_books_config(real_client, books_dir):
    """`books_configured` is False with no repo url and no checkout, and
    `unpushed_commits` is 0 rather than a 500 when the path does not exist."""
    assert not books_dir.exists()

    body = (await real_client.get("/api/admin/money/state")).json()

    assert body["books_configured"] is False
    assert body["unpushed_commits"] == 0
    assert body["home_currency"] == "INR"


async def test_money_state_books_configured_when_checkout_exists(real_client, books_dir):
    """A checkout on disk counts as configured even with an empty repo url."""
    (books_dir / ".git").mkdir(parents=True)

    body = (await real_client.get("/api/admin/money/state")).json()

    assert body["books_configured"] is True


async def test_money_digest_none_when_reports_dir_missing(real_client, books_dir):
    """No books checkout at all ⇒ {digest: None}."""
    assert not books_dir.exists()

    resp = await real_client.get("/api/admin/money/digest")

    assert resp.status_code == 200
    assert resp.json() == {"digest": None}


async def test_money_digest_none_when_reports_dir_empty(real_client, books_dir):
    """The directory existing but holding no close is also None.

    Separate from the missing-directory case deliberately: that one exits on
    the OSError branch, so on its own it would still pass if the listing code
    were deleted entirely.
    """
    (books_dir / "reports" / "monthly").mkdir(parents=True)

    assert (await real_client.get("/api/admin/money/digest")).json() == {"digest": None}


async def test_money_digest_returns_newest_close(real_client, books_dir):
    """Two closes on disk ⇒ the newest month, with its Markdown read verbatim."""
    monthly = books_dir / "reports" / "monthly"
    monthly.mkdir(parents=True)
    (monthly / "2026-07.md").write_text("# July\n\nold close\n", encoding="utf-8")
    (monthly / "2026-08.md").write_text("# August\n\nnewest close ₹1,00,308.53\n", encoding="utf-8")
    # A stray non-report file must not win the sort.
    (monthly / "notes.txt").write_text("not a close", encoding="utf-8")

    resp = await real_client.get("/api/admin/money/digest")

    assert resp.status_code == 200
    digest = resp.json()["digest"]
    assert digest["path"] == "reports/monthly/2026-08.md"
    assert digest["markdown"] == "# August\n\nnewest close ₹1,00,308.53\n"


# ---------------------------------------------------------------- flow dispatch

def test_money_flow_names_still_offers_receipt_scan():
    """`receipt_scan` must stay in the map, and only a real read proves it.

    The dispatch test below monkeypatches `_start_workflow`, so the handler
    never reads `_FLOW_NAMES` there — deleting an entry keeps that test green.
    The 400 test only proves retired names are ABSENT. Neither notices a live
    name going missing, and `docs/infrastructure.md` documents
    `POST /api/admin/money/receipt_scan/run` as the backfill command, so
    dropping it would 400 the one manual trigger the money lane still has.
    """
    assert "receipt_scan" in money_routes._FLOW_NAMES


def test_money_flow_names_dispatch_seeded_workflow_types():
    """Every workflow name the route can start must be one the deployment
    actually registers.

    Core deliberately never imports `aegis_worker`, so the registry is out of
    reach — but `config/seed/activities.yaml` is the same list by another
    route, and it is in this job's CI `paths:` filter. A typo or a renamed
    flow class here would otherwise 200 and then sit in Temporal until the
    workflow-not-found timeout, with nothing on the page saying so.
    """
    seeded = {
        a["workflow_type"]
        for a in yaml.safe_load(
            (_REPO_ROOT / "config" / "seed" / "activities.yaml").read_text()
        )["activities"]
    }

    assert set(money_routes._FLOW_NAMES) == {"money_brief", "month_close", "receipt_scan"}
    assert set(money_routes._FLOW_NAMES.values()) <= seeded
    assert money_routes._FLOW_NAMES["money_brief"] == "MoneyBriefFlow"
    assert money_routes._FLOW_NAMES["month_close"] == "MonthCloseFlow"


@pytest.mark.parametrize("flow", ["receipt_scan", "money_brief", "month_close"])
def test_money_trigger_run_dispatches_workflow(app, client, monkeypatch, flow):
    """POST /{flow}/run delegates to _start_workflow for each known flow."""
    calls = []

    async def fake_start(flow_name, cfg, temporal_client):
        calls.append((flow_name, cfg))
        handle = MagicMock()
        handle.id = f"wf-{flow_name}-1"
        return handle

    monkeypatch.setattr("aegis.api.routes.money._start_workflow", fake_start)

    resp = client.post(f"/api/admin/money/{flow}/run")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["workflow_id"] == f"wf-{flow}-1"
    assert calls[0][0] == flow


@pytest.mark.parametrize("flow", ["receipt_scan", "money_brief", "month_close"])
def test_money_trigger_run_starts_the_mapped_workflow(app, client, flow):
    """The real dispatch path resolves the slug through `_FLOW_NAMES`.

    Unlike the test above this one does NOT patch `_start_workflow`, so the
    map is actually read: it is what catches an entry that has gone missing
    or points at the wrong workflow class.
    """
    started = AsyncMock()
    started.return_value = MagicMock(id="wf-1")
    app.state.temporal_client.start_workflow = started

    resp = client.post(f"/api/admin/money/{flow}/run")

    assert resp.status_code == 200, resp.text
    assert started.await_args.args[0] == money_routes._FLOW_NAMES[flow]
    assert started.await_args.kwargs["task_queue"] == "aegis-main"


def test_money_trigger_run_409_when_disabled(app, client, settings):
    """POST /{flow}/run returns 409 when money_hygiene_enabled is False."""
    settings.money_hygiene_enabled = False
    app.dependency_overrides[get_settings] = lambda: settings

    resp = client.post("/api/admin/money/receipt_scan/run")

    assert resp.status_code == 409
    assert "disabled" in resp.json()["detail"].lower()


def test_money_trigger_run_503_when_no_temporal(app, client):
    """POST /{flow}/run returns 503 when temporal_client is None."""
    app.state.temporal_client = None

    resp = client.post("/api/admin/money/receipt_scan/run")

    assert resp.status_code == 503


@pytest.mark.parametrize(
    "flow",
    [
        "bogus_flow",
        # The v1 subscription flows, deleted 2026-09. The route must not
        # advertise a workflow type the worker no longer registers: a 200 here
        # would start a workflow nothing is listening for, and it would sit in
        # Temporal until it timed out. The dispatch test above cannot catch
        # this — it monkeypatches _start_workflow, so it never reads the map.
        "money_hygiene",
        "subscription_audit",
    ],
)
def test_money_trigger_run_400_for_unknown_flow(app, client, flow):
    """POST /{unknown}/run returns 400 for unrecognized flow names."""
    resp = client.post(f"/api/admin/money/{flow}/run")

    assert resp.status_code == 400
