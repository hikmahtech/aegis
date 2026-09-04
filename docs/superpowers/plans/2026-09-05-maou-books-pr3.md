# Maou books — PR3 "outputs and tools" Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The books reach the user: a Sunday money brief and a monthly close from hledger, four ledger tools for Maou and the operator, curiosity that asks about unknown payees once and turns the answer into a rule, an admin ledger tab, and the v1 subscription machinery deleted.

**Architecture:** Two new scheduled flows (`MoneyBriefFlow`, `MonthCloseFlow`) drive new `MoneyActivities` methods that shell out through `books.run_hledger` and read `finance.journal_index`; rendering is pure functions in `aegis_worker/activities/money_render.py` (`<pre>` tables for Slack, Markdown for the repo's `reports/`). Tools live in `core/src/aegis/services/tools/ledger.py` behind `@aegis_tool` and the existing `books.py` writer. Curiosity's charge detector is replaced by an unknown-payee detector over the index, and its answer hook gains a books branch. `MoneyHygieneDailyFlow`, `SubscriptionAuditFlow`, the v1 extractor and the `recurring_charge` writers are deleted (tables stay, unwritten).

**Tech Stack:** as PR2 (hledger 1.52.3, git, pydantic, Temporal, asyncpg, pytest); React/TypeScript for the admin page.

**Spec:** `docs/superpowers/specs/2026-09-05-maou-books-design.md` §6, §7.2–7.4, §8, §9, §10 (rollout), §11, §12 PR3 bullet. Builds on PR1 and PR2 (`2026-09-05-maou-books-pr1.md`, `-pr2.md`): `books.py` (`BooksConfig`, `config_from_settings`, `run_hledger`, `append_prices`, `append_rule`, `rewrite_event`, `write_report`, `unpushed_commits`, `post_event`, `load_rules`), `journal_index.py`, `MoneyEvent`, `fmt_money`, `money_format.py`, `<pre>` in comms.

## Global Constraints

- Tests one package at a time, `-n 8 --dist loadfile --timeout=300`, `tee logs/…`; interpreter `/home/arshad/Workspace/hikmah/aegis/.venv/bin/python` with `PYTHONPATH=core/src:worker/src:comms/src` (the worktree has no `.venv`); every `.venv/bin/python` / `.venv/bin/ruff` below means that absolute path.
- hledger-backed tests skip without the binary (`shutil.which("hledger") is None`); on meem it is at `~/.local/bin/hledger`.
- Ruff per package; never `ruff format` `core/src/aegis/services/chat.py` or `core/src/aegis/services/tools/infra.py`. `chat.py` edits are hand-laid single-line hunks; verify a minimal diff with `git diff main -- core/src/aegis/services/chat.py | grep -c '^@@'`.
- Human-facing amounts via `fmt_money`; hledger output is quoted verbatim inside `<pre>` (Slack) or fenced code (Markdown) — never re-formatted by hand.
- Slack messages are light HTML (`<b>`, `<pre>`, `<code>`); `safe_send_message(self.delivery, agent_id=self.agent_id, message=…, log_event=…)`.
- Schedules: `money-brief-weekly` cron `0 3 * * 0` (08:30 IST Sunday, before `gtd-weekly-review` at `30 3 * * 0`), config `{"days": 7}`; `money-close-monthly` cron `0 4 1 * *`; both `agent_id: maou`, `feature_flag="money_hygiene_enabled"`. Migration `027_drop_v1_money_schedules.sql` deletes the `money-hygiene-daily` and `subscription-audit-monthly` activity rows.
- Tool whitelist (`books.run_hledger`) is the only path to hledger from a tool; write tools validate accounts against `hledger accounts --declared` before writing; `ledger_post`, `ledger_reclassify`, `ledger_add_rule` join `_UNSERVED_TOOLS` in `mcp_server.py`; the operator mount serves all four.
- Grants are seed-time only in code (`AGENT_TOOL_SETS`, `config/seed/agents.yaml`); prod grants are a DB write at rollout.
- The legacy tables `finance.recurring_charge`, `finance.renewal_alert`, `finance.subscription_digest` are NOT dropped in this PR.
- Commit messages: single line, semantic type, no trailers.

---

### Task 1: Brief and close data — `build_money_brief`, `build_month_close`, `refresh_fx_prices`

**Files:**
- Modify: `worker/src/aegis_worker/activities/money.py` (three new activities + `finance: Any = None` field)
- Modify: `worker/src/aegis_worker/__main__.py` (`MoneyActivities(... finance=connectors.get("finance"))` — check the connector dict key used for `FinanceConnector` in that file and use it)
- Test: `tests/worker/activities/test_money_brief_data.py`

**Interfaces:**
- `MoneyActivities.refresh_fx_prices() -> dict` — `self.finance.get_quotes(["USDINR=X", "GBPINR=X", "EURINR=X"])`; for each quote with a numeric `price`, one line `P <today> <symbol> ₹<price:.2f>` (`$`, `£`, `€`); `books.append_prices(lines, cfg)`; returns `{"written": n, "errors": [...]}`; no finance connector or no books ⇒ `{"written": 0, "errors": ["disabled"]}`. Never raises.
- `MoneyActivities.build_money_brief(days: int = 7) -> dict` with keys: `as_of` (ISO date, Asia/Kolkata today), `since`, `entities` (`{"personal": {"income": Decimal-as-str, "expenses": str}, "hikmah": {...}}` parsed from `bal -X ₹ -b since -e as_of+1 income expenses --depth 2 -O csv`), `by_account` (the same csv rows as `[{"account", "balance"}]`), `top_payees` (`[{"payee", "amount"}]` from `bal -X ₹ -b since -e as_of+1 expenses --pivot payee --flat --sort-amount -O csv`, first 10), `unknowns` (index rows in `*:unknown` since `since`: `[{"msgid", "payee", "amount", "currency", "occurred_on", "channel"}]`, amount desc, max 15), `dues` (index `kind IN ('due','failed') AND linked_message_id IS NULL AND due_on BETWEEN as_of-7 AND as_of+14`, `[{"msgid", "payee", "amount", "currency", "due_on", "kind", "todoist_ref"}]`), `forecast` (`reg -X ₹ --forecast=<as_of>..<as_of+14> -b as_of -e as_of+15 expenses tag:generated-transaction -O csv` → `[{"date", "description", "amount"}]`), `closed_dues` (index dues linked since `since`), `large_unexplained` (unknowns with INR amount ≥ 5000), `unpushed` (`books.unpushed_commits`), `low_confidence` (index `parser='llm' AND confidence < 0.8 AND created_at >= since` count), `bal_text` (`bal -X ₹ -b since -e as_of+1 income expenses --depth 2`, verbatim), `books_ok` (False when `BooksDisabled`/`BooksError`; then only the index-derived keys are filled and `bal_text` is "").
- `MoneyActivities.build_month_close() -> dict`: `month` (previous calendar month `YYYY-MM`), `is_text` (`is -X ₹ -M -b <month-1 first> -e <this month first> --depth 2`, verbatim), `bs_text` (`bs -X ₹ -e <this month first> --depth 2`), `is_rows` (csv of the same `is` command → `[{"account", "prev", "month"}]`), `recurring_total` (sum of `bal -X ₹ --forecast=<month first>..<month last> -b <month first> -e <this month first> expenses tag:generated-transaction --depth 1 -O csv`), `unknown_count` (index, that month), `dues_paid` / `dues_open` (index counts for `due_on` in the month by `linked_message_id` null-ness), `books_ok`.
- CSV parsing helper `parse_hledger_csv(text) -> list[list[str]]` (stdlib `csv`) and `amount_from_cell("₹ 1,234.56") -> Decimal` (regex `-?[\d,]+(?:\.\d+)?`, commas stripped, `Decimal("0")` when no number) in `money.py` module scope.

- [ ] **Step 1: Write the failing tests**

```python
# tests/worker/activities/test_money_brief_data.py
"""build_money_brief / build_month_close / refresh_fx_prices against a temp books repo."""

from __future__ import annotations

import shutil
import subprocess
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aegis.api.models.money import MoneyEvent
from aegis.services import books, journal_index as ji
from aegis_worker.activities.money import MoneyActivities, amount_from_cell, parse_hledger_csv
from temporalio.testing import ActivityEnvironment

HAS_HLEDGER = shutil.which("hledger") is not None and shutil.which("git") is not None

ACCOUNTS = """commodity ₹ 1,00,000.00
commodity $ 1000.00
account assets:bank:hdfc:1225
account assets:unknown
account liabilities:card:axis:1313
account expenses:unknown
account expenses:saas
account expenses:groceries
account expenses:hikmah:unknown
account expenses:hikmah:infra
account income:unknown
account income:hikmah:stockopedia
account income:hikmah:other
account equity:transfers
"""


def _repo(tmp_path: Path, today: date) -> books.BooksConfig:
    root = tmp_path / "books"
    (root / "personal").mkdir(parents=True)
    (root / "hikmah").mkdir()
    (root / "accounts.journal").write_text(ACCOUNTS)
    (root / "prices.journal").write_text("P 2026-09-01 $ ₹84.00\n")
    (root / "recurring.journal").write_text(
        f"~ monthly from {today.replace(day=1).isoformat()}  Apple iCloud+\n"
        "    expenses:saas                 ₹219.00\n    liabilities:card:axis:1313\n"
    )
    (root / "personal" / f"{today.year}.journal").write_text("; p\n")
    (root / "hikmah" / f"{today.year}.journal").write_text("; h\n")
    (root / "main.journal").write_text(
        f"include accounts.journal\ninclude prices.journal\ninclude personal/{today.year}.journal\n"
        f"include hikmah/{today.year}.journal\ninclude recurring.journal\n"
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"], cwd=root, check=True)
    return books.BooksConfig(path=root)


@pytest_asyncio.fixture(autouse=True)
async def _clean(db_pool):
    await db_pool.execute("DELETE FROM finance.journal_index WHERE mailbox = 'brief-t'")
    yield
    await db_pool.execute("DELETE FROM finance.journal_index WHERE mailbox = 'brief-t'")


def _ev(**kw) -> MoneyEvent:
    base = dict(kind="transaction", direction="out", amount=Decimal("10"), currency="INR", payee="Shop",
                payee_key="shop", channel="upi", instrument="hdfc-1225", occurred_on=date.today(),
                entity="personal", account="expenses:unknown", parser="hdfc_upi", source_class="bank")
    base.update(kw)
    return MoneyEvent(**base)


def test_csv_helpers():
    rows = parse_hledger_csv('"account","balance"\n"expenses:saas","₹ 1,234.56"\n"total","₹ 1,234.56"\n')
    assert rows[1] == ["expenses:saas", "₹ 1,234.56"]
    assert amount_from_cell("₹ 1,234.56") == Decimal("1234.56")
    assert amount_from_cell("₹ -140.00") == Decimal("-140.00")
    assert amount_from_cell("0") == Decimal("0") and amount_from_cell("") == Decimal("0")


@pytest.mark.skipif(not HAS_HLEDGER, reason="hledger/git not installed")
@pytest.mark.asyncio
async def test_build_money_brief_reads_books_and_index(db_pool, tmp_path):
    today = date.today()
    cfg = _repo(tmp_path, today)
    act = MoneyActivities(db_pool=db_pool, llm=None, delivery=None, fx_rates={}, books_cfg=cfg)
    await books.post_event(_ev(amount=Decimal("6000"), payee="Unknown Big", payee_key="unknown big"), "brief-t/a", cfg)
    await books.post_event(_ev(amount=Decimal("250"), payee="Grocer", payee_key="grocer", account="expenses:groceries"), "brief-t/b", cfg)
    await books.post_event(
        _ev(amount=Decimal("1000"), direction="in", payee="Stockopedia", payee_key="stockopedia", entity="hikmah",
            account="income:hikmah:stockopedia", instrument=None), "brief-t/c", cfg)
    await ji.upsert(db_pool, "brief-t/a", "brief-t", _ev(amount=Decimal("6000"), payee="Unknown Big", payee_key="unknown big"), journal_file="x")
    await ji.upsert(db_pool, "brief-t/d", "brief-t", _ev(kind="due", due_on=today, payee="Axis card", payee_key="axis card",
                                                        amount=Decimal("99"), channel="statement"), todoist_ref="t1")
    await ji.upsert(db_pool, "brief-t/e", "brief-t", _ev(parser="llm", confidence=0.4, payee="Vague", payee_key="vague"))

    brief = await ActivityEnvironment().run(act.build_money_brief, 7)

    assert brief["books_ok"] is True and brief["as_of"] == today.isoformat()
    assert Decimal(brief["entities"]["personal"]["expenses"]) == Decimal("6250.00")
    assert Decimal(brief["entities"]["hikmah"]["income"]) == Decimal("-1000.00")
    assert brief["top_payees"][0]["payee"] == "Unknown Big"
    assert [u["msgid"] for u in brief["unknowns"]] == ["brief-t/a"] and brief["large_unexplained"][0]["msgid"] == "brief-t/a"
    assert brief["dues"][0]["msgid"] == "brief-t/d"
    assert any("Apple iCloud+" in row["description"] for row in brief["forecast"])
    assert brief["low_confidence"] == 1 and brief["unpushed"] == 0
    assert "expenses:groceries" in brief["bal_text"]


@pytest.mark.asyncio
async def test_build_money_brief_without_books_still_reports_index(db_pool, tmp_path):
    act = MoneyActivities(db_pool=db_pool, llm=None, delivery=None, fx_rates={}, books_cfg=books.BooksConfig(path=tmp_path / "none"))
    await ji.upsert(db_pool, "brief-t/z", "brief-t", _ev(kind="due", due_on=date.today(), payee="X", payee_key="x"), todoist_ref="t")
    brief = await ActivityEnvironment().run(act.build_money_brief, 7)
    assert brief["books_ok"] is False and brief["bal_text"] == "" and len(brief["dues"]) == 1


@pytest.mark.skipif(not HAS_HLEDGER, reason="hledger/git not installed")
@pytest.mark.asyncio
async def test_build_month_close(db_pool, tmp_path):
    today = date.today()
    first = today.replace(day=1)
    prev_last = first.fromordinal(first.toordinal() - 1)
    cfg = _repo(tmp_path, prev_last)
    act = MoneyActivities(db_pool=db_pool, llm=None, delivery=None, fx_rates={}, books_cfg=cfg)
    await books.post_event(_ev(amount=Decimal("300"), occurred_on=prev_last, account="expenses:saas", payee="Saas", payee_key="saas"), "brief-t/m", cfg)
    close = await ActivityEnvironment().run(act.build_month_close)
    assert close["books_ok"] is True and close["month"] == prev_last.strftime("%Y-%m")
    assert "expenses:saas" in close["is_text"] and "Balance Sheet" in close["bs_text"]
    assert close["recurring_total"] == "219.00"
    assert close["unknown_count"] == 0 and close["dues_open"] == 0


@pytest.mark.skipif(not HAS_HLEDGER, reason="hledger/git not installed")
@pytest.mark.asyncio
async def test_refresh_fx_prices_appends_p_lines(db_pool, tmp_path):
    cfg = _repo(tmp_path, date.today())
    finance = AsyncMock()
    finance.get_quotes = AsyncMock(return_value=[
        {"symbol": "USDINR=X", "price": 84.1}, {"symbol": "GBPINR=X", "price": 106.25}, {"symbol": "EURINR=X", "error": "timeout"},
    ])
    act = MoneyActivities(db_pool=db_pool, llm=None, delivery=None, fx_rates={}, books_cfg=cfg, finance=finance)
    out = await ActivityEnvironment().run(act.refresh_fx_prices)
    assert out["written"] == 2 and out["errors"] == ["EURINR=X: timeout"]
    text = (cfg.path / "prices.journal").read_text()
    assert f"P {date.today().isoformat()} $ ₹84.10\n" in text and f"P {date.today().isoformat()} £ ₹106.25\n" in text
    act_none = MoneyActivities(db_pool=db_pool, llm=None, delivery=None, fx_rates={}, books_cfg=cfg, finance=None)
    assert (await ActivityEnvironment().run(act_none.refresh_fx_prices)) == {"written": 0, "errors": ["disabled"]}
```

- [ ] **Step 2: Run to verify failure** — ImportError on `amount_from_cell`/`parse_hledger_csv`, `AttributeError` on the activities.

- [ ] **Step 3: Implement**

Module-level helpers in `money.py`:

```python
import csv
import io

_NUM_RE = re.compile(r"-?[\d,]+(?:\.\d+)?")


def parse_hledger_csv(text: str) -> list[list[str]]:
    return [row for row in csv.reader(io.StringIO(text)) if row]


def amount_from_cell(cell: str) -> Decimal:
    m = _NUM_RE.search((cell or "").replace(" ", " "))
    return Decimal(m.group(0).replace(",", "")) if m else Decimal("0")
```

Field `finance: Any = None` on `MoneyActivities` (the `FinanceConnector`; wired in `__main__`).

Activities (Asia/Kolkata "today" via `datetime.now(ZoneInfo(self.home_tz)).date()`; every hledger call goes through `books.run_hledger(args, self.books_cfg, output_format=...)`; `BooksError`/`BooksDisabled` from the first hledger call sets `books_ok=False` and skips the rest of the hledger-derived keys):

```python
    _FX_SYMBOLS = {"USDINR=X": "$", "GBPINR=X": "£", "EURINR=X": "€"}

    @activity.defn
    async def refresh_fx_prices(self) -> dict:
        """Weekly P lines from the keyless quote provider (spec §7.2 step 1)."""
        if self.finance is None or self.books_cfg is None:
            return {"written": 0, "errors": ["disabled"]}
        today = datetime.now(ZoneInfo(self.home_tz)).date().isoformat()
        lines: list[str] = []
        errors: list[str] = []
        try:
            quotes = await self.finance.get_quotes(list(self._FX_SYMBOLS))
        except Exception as exc:  # noqa: BLE001
            return {"written": 0, "errors": [f"quotes: {str(exc)[:120]}"]}
        for q in quotes or []:
            sym = self._FX_SYMBOLS.get(str(q.get("symbol")))
            price = q.get("price")
            if sym and isinstance(price, (int, float)) and price > 0:
                lines.append(f"P {today} {sym} ₹{Decimal(str(price)).quantize(Decimal('0.01'))}")
            else:
                errors.append(f"{q.get('symbol')}: {q.get('error') or 'no price'}")
        if lines:
            try:
                await books.append_prices(lines, self.books_cfg)
            except books.BooksError as exc:
                return {"written": 0, "errors": errors + [f"books: {str(exc)[:120]}"]}
        return {"written": len(lines), "errors": errors}

    async def _hl(self, args: list[str], fmt: str = "text") -> str:
        return await books.run_hledger(args, self.books_cfg, output_format=fmt)

    @activity.defn
    async def build_money_brief(self, days: int = 7) -> dict:
        today = datetime.now(ZoneInfo(self.home_tz)).date()
        since = today - timedelta(days=days)
        end = (today + timedelta(days=1)).isoformat()
        brief: dict = {"as_of": today.isoformat(), "since": since.isoformat(), "books_ok": True,
                       "entities": {"personal": {"income": "0", "expenses": "0"}, "hikmah": {"income": "0", "expenses": "0"}},
                       "by_account": [], "top_payees": [], "forecast": [], "bal_text": "", "unpushed": 0}
        try:
            rows = parse_hledger_csv(await self._hl(["bal", "-X", "₹", "-b", since.isoformat(), "-e", end,
                                                     "income", "expenses", "--depth", "2"], "csv"))
            for account, balance in (r[:2] for r in rows[1:] if len(r) >= 2 and ":" in r[0]):
                ent = "hikmah" if account.startswith(("expenses:hikmah", "income:hikmah")) else "personal"
                side = "income" if account.startswith("income") else "expenses"
                brief["entities"][ent][side] = str(Decimal(brief["entities"][ent][side]) + amount_from_cell(balance))
                brief["by_account"].append({"account": account, "balance": balance})
            payees = parse_hledger_csv(await self._hl(["bal", "-X", "₹", "-b", since.isoformat(), "-e", end,
                                                       "expenses", "--pivot", "payee", "--flat", "--sort-amount"], "csv"))
            brief["top_payees"] = [{"payee": r[0], "amount": r[1]} for r in payees[1:11] if len(r) >= 2 and r[0].lower() != "total:"]
            fc = parse_hledger_csv(await self._hl(["reg", "-X", "₹", f"--forecast={today.isoformat()}..{(today + timedelta(days=14)).isoformat()}",
                                                   "-b", today.isoformat(), "-e", (today + timedelta(days=15)).isoformat(),
                                                   "expenses", "tag:generated-transaction"], "csv"))
            brief["forecast"] = [{"date": r[1], "description": r[3], "amount": r[5]} for r in fc[1:] if len(r) >= 6]
            brief["bal_text"] = await self._hl(["bal", "-X", "₹", "-b", since.isoformat(), "-e", end, "income", "expenses", "--depth", "2"])
            brief["unpushed"] = await books.unpushed_commits(self.books_cfg)
        except books.BooksError as exc:
            logger.warning("money_brief_books_unavailable", error=str(exc)[:200])
            brief["books_ok"] = False
            brief["bal_text"] = ""
        unknowns = await self.db_pool.fetch(
            "SELECT message_id, payee, amount, currency, occurred_on, channel FROM finance.journal_index "
            "WHERE kind = 'transaction' AND account LIKE '%:unknown' AND occurred_on >= $1 "
            "ORDER BY amount DESC NULLS LAST LIMIT 15", since)
        brief["unknowns"] = [{"msgid": r["message_id"], "payee": r["payee"], "amount": str(r["amount"]), "currency": r["currency"],
                              "occurred_on": r["occurred_on"].isoformat(), "channel": r["channel"]} for r in unknowns]
        brief["large_unexplained"] = [u for u in brief["unknowns"] if u["currency"] == "INR" and Decimal(u["amount"]) >= 5000]
        dues = await self.db_pool.fetch(
            "SELECT message_id, payee, amount, currency, due_on, kind, todoist_ref FROM finance.journal_index "
            "WHERE kind IN ('due','failed') AND linked_message_id IS NULL AND due_on BETWEEN $1 AND $2 ORDER BY due_on",
            today - timedelta(days=7), today + timedelta(days=14))
        brief["dues"] = [{"msgid": r["message_id"], "payee": r["payee"], "amount": str(r["amount"]), "currency": r["currency"],
                          "due_on": r["due_on"].isoformat(), "kind": r["kind"], "todoist_ref": r["todoist_ref"]} for r in dues]
        closed = await self.db_pool.fetch(
            "SELECT message_id, payee, amount, currency, due_on FROM finance.journal_index "
            "WHERE kind IN ('due','failed') AND linked_message_id IS NOT NULL AND updated_at >= $1 ORDER BY due_on", since)
        brief["closed_dues"] = [{"msgid": r["message_id"], "payee": r["payee"], "amount": str(r["amount"]), "currency": r["currency"],
                                 "due_on": r["due_on"].isoformat() if r["due_on"] else None} for r in closed]
        brief["low_confidence"] = int(await self.db_pool.fetchval(
            "SELECT count(*) FROM finance.journal_index WHERE parser = 'llm' AND confidence < 0.8 AND created_at >= $1", since))
        return brief

    @activity.defn
    async def build_month_close(self) -> dict:
        today = datetime.now(ZoneInfo(self.home_tz)).date()
        this_first = today.replace(day=1)
        last = this_first - timedelta(days=1)
        month_first = last.replace(day=1)
        prev_first = (month_first - timedelta(days=1)).replace(day=1)
        close: dict = {"month": last.strftime("%Y-%m"), "books_ok": True, "is_text": "", "bs_text": "",
                       "is_rows": [], "recurring_total": "0"}
        try:
            close["is_text"] = await self._hl(["is", "-X", "₹", "-M", "-b", prev_first.isoformat(), "-e", this_first.isoformat(), "--depth", "2"])
            close["bs_text"] = await self._hl(["bs", "-X", "₹", "-e", this_first.isoformat(), "--depth", "2"])
            rows = parse_hledger_csv(await self._hl(["is", "-X", "₹", "-M", "-b", prev_first.isoformat(), "-e", this_first.isoformat(), "--depth", "2"], "csv"))
            close["is_rows"] = [{"account": r[0], "prev": r[1], "month": r[2]} for r in rows if len(r) >= 3 and ":" in r[0]]
            fc = parse_hledger_csv(await self._hl(["bal", "-X", "₹", f"--forecast={month_first.isoformat()}..{last.isoformat()}",
                                                   "-b", month_first.isoformat(), "-e", this_first.isoformat(),
                                                   "expenses", "tag:generated-transaction", "--depth", "1"], "csv"))
            total = sum((amount_from_cell(r[1]) for r in fc[1:] if len(r) >= 2 and r[0].lower() != "total:"), Decimal("0"))
            close["recurring_total"] = str(total.quantize(Decimal("0.01")))
        except books.BooksError as exc:
            logger.warning("month_close_books_unavailable", error=str(exc)[:200])
            close["books_ok"] = False
        close["unknown_count"] = int(await self.db_pool.fetchval(
            "SELECT count(*) FROM finance.journal_index WHERE kind = 'transaction' AND account LIKE '%:unknown' "
            "AND occurred_on BETWEEN $1 AND $2", month_first, last))
        close["dues_paid"] = int(await self.db_pool.fetchval(
            "SELECT count(*) FROM finance.journal_index WHERE kind IN ('due','failed') AND linked_message_id IS NOT NULL "
            "AND due_on BETWEEN $1 AND $2", month_first, last))
        close["dues_open"] = int(await self.db_pool.fetchval(
            "SELECT count(*) FROM finance.journal_index WHERE kind IN ('due','failed') AND linked_message_id IS NULL "
            "AND due_on BETWEEN $1 AND $2", month_first, last))
        return close
```

Check the exact CSV column layout of `hledger reg -O csv` (`txnidx,date,code,description,account,amount,total`) and of `is -M -O csv` on hledger 1.52.3 with a quick local run against the test repo and adjust the column indexes in the code above if they differ; the tests assert on content, not on indexes. Also verify that `--pivot payee` with `--sort-amount` emits a final `Total:` row (skip it as written) and that `bal --forecast` with `tag:generated-transaction` selects only the periodic entries.

- [ ] **Step 4: Run, lint, commit**

Run: `PYTHONPATH=core/src:worker/src:comms/src .venv/bin/python -m pytest tests/worker/activities/test_money_brief_data.py -q -rs 2>&1 | tail -5` — all passed, none skipped on meem.
Run: `.venv/bin/ruff check worker/src/ tests/worker/` — clean.

```bash
git add worker/src/aegis_worker/activities/money.py worker/src/aegis_worker/__main__.py tests/worker/activities/test_money_brief_data.py
git commit -m "feat(money): brief and month-close data from hledger and the journal index"
```

### Task 2: Rendering, `MoneyBriefFlow`, `MonthCloseFlow`, schedules

**Files:**
- Create: `worker/src/aegis_worker/activities/money_render.py` (pure)
- Create: `worker/src/aegis_worker/flows/money_brief.py`, `worker/src/aegis_worker/flows/month_close.py`
- Modify: `worker/src/aegis_worker/activities/money.py` (activities `notify_money_message(html, log_event)`, `write_money_report(rel_path, text)`)
- Modify: `worker/src/aegis_worker/registry.py` (two `FlowSpec`s), `worker/src/aegis_worker/__main__.py` (`WORKFLOWS` is derived from the registry — nothing to add)
- Modify: `config/seed/activities.yaml` (two rows)
- Create: `migrations/027_drop_v1_money_schedules.sql`
- Test: `tests/worker/test_money_render.py`, `tests/worker/flows/test_money_brief.py`, `tests/worker/flows/test_month_close.py`, `tests/worker/test_registry.py` (adjust counts if it asserts them), `tests/worker/test_schedule_sync_mappers.py` (add the two mappings if the file enumerates flows)

**Interfaces:**
- `money_render.render_money_brief(brief: dict, home_symbol: str = "₹") -> dict` → `{"html": str, "markdown": str}`. HTML sections: `<b>Money brief · <since> → <as_of></b>`; `Personal: in <fmt> · out <fmt>` and `Hikmah: …` (from `entities`, using `fmt_money(abs(Decimal(x)), "INR")`); `<b>Where it went</b>` + `<pre>bal_text</pre>` (omitted when `books_ok` is False, replaced by the line `Books unavailable — index only.`); `<b>Top payees</b>` list of up to 10 `payee — amount`; `<b>Due in the next 14 days</b>` each `due_on · payee · fmt_money · (task)`/`(no task)` and `<b>Forecast</b>` rows; `<b>Unexplained</b>` each `occurred_on · payee · fmt_money · channel` (max 10) with a line `Reply with "<payee> is <account>" or use ledger_add_rule.`; `<b>Paid this week</b>` closed dues; `<b>Housekeeping</b>` only when `unpushed > 0` or `low_confidence > 0`. Markdown mirrors it with `#`/`##` headings and fenced code for `bal_text`.
- `money_render.render_month_close(close: dict) -> dict` → same shape: `<b>Month close · <month></b>`, `<pre>is_text</pre>`, `<pre>bs_text</pre>`, `Recurring commitments: <fmt>`, `Unexplained postings: n`, `Dues paid: a · still open: b`.
- `MoneyActivities.notify_money_message(html: str, log_event: str) -> None` → `safe_send_message(self.delivery, agent_id=self.agent_id, message=html, log_event=log_event)`.
- `MoneyActivities.write_money_report(rel_path: str, text: str) -> None` → `books.write_report(rel_path, text, self.books_cfg)`; no-op with a warning when `books_cfg` is None or `BooksError`.
- Flows: `MoneyBriefConfig(agent_id="maou", days=7, silent=False)`, `MoneyBriefFlow` (name `MoneyBriefFlow`): `refresh_fx_prices` (best effort) → `build_money_brief(days)` → `render_money_brief` → `notify_money_message(html, "money_brief_notify_failed")` unless silent → `write_money_report(f"reports/weekly/{as_of}.md", markdown)`; returns `{"as_of", "sent": bool, "dues": n, "unknowns": n, "books_ok"}`. `MonthCloseConfig(agent_id="maou", silent=False)`, `MonthCloseFlow`: `build_month_close` → `render_month_close` → notify (`month_close_notify_failed`) → `write_money_report(f"reports/monthly/{month}.md", markdown)`; returns `{"month", "sent", "books_ok"}`. Rendering runs as an activity (`render_money_brief`/`render_month_close` are `MoneyActivities` methods delegating to the pure module) so the flow stays deterministic.
- `FlowSpec(MoneyBriefFlow, lambda act: MoneyBriefConfig(agent_id=act["agent_id"], days=int(act["config"].get("days", 7)), silent=bool(act["config"].get("silent", False))), feature_flag="money_hygiene_enabled")` and the same shape for `MonthCloseFlow`.
- Seed rows: `money-brief-weekly` (`MoneyBriefFlow`, `maou`, `"0 3 * * 0"`, `config: {days: 7}`, active) and `money-close-monthly` (`MonthCloseFlow`, `maou`, `"0 4 1 * *"`, `config: {}`, active).
- Migration 027: `DELETE FROM activities WHERE slug IN ('money-hygiene-daily', 'subscription-audit-monthly');` (idempotent by nature).

- [ ] **Step 1: Write the failing tests**

```python
# tests/worker/test_money_render.py
from aegis_worker.activities.money_render import render_money_brief, render_month_close

BRIEF = {
    "as_of": "2026-09-06", "since": "2026-08-30", "books_ok": True,
    "entities": {"personal": {"income": "-1500.00", "expenses": "6250.00"}, "hikmah": {"income": "-100000.00", "expenses": "262.30"}},
    "by_account": [], "top_payees": [{"payee": "Unknown Big", "amount": "₹ 6,000.00"}],
    "unknowns": [{"msgid": "m/a", "payee": "Unknown Big", "amount": "6000.00", "currency": "INR", "occurred_on": "2026-09-02", "channel": "upi"}],
    "large_unexplained": [{"msgid": "m/a", "payee": "Unknown Big", "amount": "6000.00", "currency": "INR", "occurred_on": "2026-09-02", "channel": "upi"}],
    "dues": [{"msgid": "m/d", "payee": "Axis credit card XX13", "amount": "100308.53", "currency": "INR", "due_on": "2026-09-07", "kind": "due", "todoist_ref": "t1"},
             {"msgid": "m/f", "payee": "Medium", "amount": "199.00", "currency": "INR", "due_on": "2026-09-15", "kind": "failed", "todoist_ref": None}],
    "forecast": [{"date": "2026-09-15", "description": "MSEDCL Suncity 501", "amount": "₹ 7,170.00"}],
    "closed_dues": [{"msgid": "m/c", "payee": "Airtel", "amount": "5306.46", "currency": "INR", "due_on": "2026-09-06"}],
    "unpushed": 2, "low_confidence": 1, "bal_text": "  ₹ 6,250.00  expenses\n",
}


def test_brief_html_has_every_section_and_major_units():
    out = render_money_brief(BRIEF)
    html = out["html"]
    assert html.startswith("<b>Money brief · 2026-08-30 → 2026-09-06</b>")
    assert "Personal: in ₹1,500.00 · out ₹6,250.00" in html
    assert "Hikmah: in ₹1,00,000.00 · out ₹262.30" in html
    assert "<pre>  ₹ 6,250.00  expenses\n</pre>" in html
    assert "Unknown Big — ₹ 6,000.00" in html
    assert "2026-09-07 · Axis credit card XX13 · ₹1,00,308.53 · (task)" in html
    assert "2026-09-15 · Medium · ₹199.00 · fix payment · (no task)" in html
    assert "2026-09-15 · MSEDCL Suncity 501 · ₹ 7,170.00" in html
    assert "2026-09-02 · Unknown Big · ₹6,000.00 · upi" in html
    assert "ledger_add_rule" in html
    assert "Airtel · ₹5,306.46" in html
    assert "2 unpushed commits" in html and "1 low-confidence" in html
    assert "amount_cents" not in html


def test_brief_markdown_mirrors_html():
    md = render_money_brief(BRIEF)["markdown"]
    assert md.startswith("# Money brief · 2026-08-30 → 2026-09-06")
    assert "```\n  ₹ 6,250.00  expenses\n```" in md and "## Due in the next 14 days" in md


def test_brief_without_books():
    out = render_money_brief({**BRIEF, "books_ok": False, "bal_text": "", "top_payees": [], "forecast": []})
    assert "Books unavailable — index only." in out["html"] and "<pre>" not in out["html"]


def test_month_close_render():
    out = render_month_close({"month": "2026-08", "books_ok": True, "is_text": "IS", "bs_text": "BS", "is_rows": [],
                              "recurring_total": "15625.76", "unknown_count": 3, "dues_paid": 2, "dues_open": 1})
    assert out["html"].startswith("<b>Month close · 2026-08</b>")
    assert "<pre>IS</pre>" in out["html"] and "<pre>BS</pre>" in out["html"]
    assert "Recurring commitments: ₹15,625.76" in out["html"]
    assert "Unexplained postings: 3" in out["html"] and "Dues paid: 2 · still open: 1" in out["html"]
    assert out["markdown"].startswith("# Month close · 2026-08")
```

```python
# tests/worker/flows/test_money_brief.py
from __future__ import annotations

import pytest
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from aegis_worker.flows.money_brief import MoneyBriefConfig, MoneyBriefFlow

calls: dict[str, list] = {k: [] for k in ("fx", "build", "render", "notify", "report")}


@activity.defn(name="refresh_fx_prices")
async def stub_fx() -> dict:
    calls["fx"].append(1)
    return {"written": 3, "errors": []}


@activity.defn(name="refresh_fx_prices")
async def stub_fx_boom() -> dict:
    raise RuntimeError("quotes down")


@activity.defn(name="build_money_brief")
async def stub_build(days: int = 7) -> dict:
    calls["build"].append(days)
    return {"as_of": "2026-09-06", "books_ok": True, "dues": [1, 2], "unknowns": [1]}


@activity.defn(name="render_money_brief")
async def stub_render(brief: dict) -> dict:
    calls["render"].append(brief["as_of"])
    return {"html": "<b>x</b>", "markdown": "# x"}


@activity.defn(name="notify_money_message")
async def stub_notify(html: str, log_event: str) -> None:
    calls["notify"].append((html, log_event))


@activity.defn(name="write_money_report")
async def stub_report(rel_path: str, text: str) -> None:
    calls["report"].append((rel_path, text))


def _reset():
    for v in calls.values():
        v.clear()


async def _run(config, stubs, wid):
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(env.client, task_queue="tq", workflows=[MoneyBriefFlow], activities=stubs),
    ):
        return await env.client.execute_workflow(MoneyBriefFlow.run, config, id=wid, task_queue="tq")


@pytest.mark.asyncio
async def test_brief_flow_end_to_end():
    _reset()
    result = await _run(MoneyBriefConfig(days=14), [stub_fx, stub_build, stub_render, stub_notify, stub_report], "mb-1")
    assert result == {"as_of": "2026-09-06", "sent": True, "dues": 2, "unknowns": 1, "books_ok": True}
    assert calls["build"] == [14] and calls["notify"] == [("<b>x</b>", "money_brief_notify_failed")]
    assert calls["report"] == [("reports/weekly/2026-09-06.md", "# x")]


@pytest.mark.asyncio
async def test_brief_flow_survives_fx_failure_and_silent():
    _reset()
    result = await _run(MoneyBriefConfig(silent=True), [stub_fx_boom, stub_build, stub_render, stub_notify, stub_report], "mb-2")
    assert result["sent"] is False and calls["notify"] == [] and len(calls["report"]) == 1
```

```python
# tests/worker/flows/test_month_close.py
from __future__ import annotations

import pytest
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from aegis_worker.flows.month_close import MonthCloseConfig, MonthCloseFlow

seen: list = []


@activity.defn(name="build_month_close")
async def stub_build() -> dict:
    return {"month": "2026-08", "books_ok": True}


@activity.defn(name="render_month_close")
async def stub_render(close: dict) -> dict:
    return {"html": "<b>c</b>", "markdown": "# c"}


@activity.defn(name="notify_money_message")
async def stub_notify(html: str, log_event: str) -> None:
    seen.append(("notify", log_event))


@activity.defn(name="write_money_report")
async def stub_report(rel_path: str, text: str) -> None:
    seen.append(("report", rel_path))


@pytest.mark.asyncio
async def test_month_close_flow():
    seen.clear()
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(env.client, task_queue="tq", workflows=[MonthCloseFlow], activities=[stub_build, stub_render, stub_notify, stub_report]),
    ):
        result = await env.client.execute_workflow(MonthCloseFlow.run, MonthCloseConfig(), id="mc-1", task_queue="tq")
    assert result == {"month": "2026-08", "sent": True, "books_ok": True}
    assert seen == [("notify", "month_close_notify_failed"), ("report", "reports/monthly/2026-08.md")]
```

- [ ] **Step 2: Run to verify failure** — ImportErrors.

- [ ] **Step 3: Implement `money_render.py`**

```python
"""Pure renderers for the money brief and the month close (spec §7.2, §7.3).
Light HTML for Slack (`<b>`, `<pre>`); Markdown for the books repo. hledger
text is quoted verbatim; only index-derived amounts go through fmt_money."""

from __future__ import annotations

from decimal import Decimal
from html import escape

from aegis.services.money_format import fmt_money


def _money(value: str, currency: str = "INR") -> str:
    return fmt_money(abs(Decimal(value or "0")), currency)


def render_money_brief(brief: dict, home_symbol: str = "₹") -> dict:
    ent = brief.get("entities") or {}
    p = ent.get("personal") or {}
    h = ent.get("hikmah") or {}
    html: list[str] = [f"<b>Money brief · {brief['since']} → {brief['as_of']}</b>"]
    md: list[str] = [f"# Money brief · {brief['since']} → {brief['as_of']}", ""]

    line = f"Personal: in {_money(p.get('income', '0'))} · out {_money(p.get('expenses', '0'))}"
    line2 = f"Hikmah: in {_money(h.get('income', '0'))} · out {_money(h.get('expenses', '0'))}"
    html += [line, line2, ""]
    md += [line, line2, ""]

    if brief.get("books_ok") and brief.get("bal_text"):
        html += ["<b>Where it went</b>", f"<pre>{escape(brief['bal_text'])}</pre>"]
        md += ["## Where it went", "```", brief["bal_text"].rstrip("\n"), "```", ""]
    elif not brief.get("books_ok"):
        html += ["Books unavailable — index only.", ""]
        md += ["_Books unavailable — index only._", ""]

    if brief.get("top_payees"):
        html.append("<b>Top payees</b>")
        md.append("## Top payees")
        for r in brief["top_payees"][:10]:
            html.append(f"{escape(r['payee'])} — {escape(r['amount'])}")
            md.append(f"- {r['payee']} — {r['amount']}")
        html.append("")
        md.append("")

    html.append("<b>Due in the next 14 days</b>")
    md.append("## Due in the next 14 days")
    rows = 0
    for d in brief.get("dues") or []:
        kind = " · fix payment" if d.get("kind") == "failed" else ""
        task = "(task)" if d.get("todoist_ref") else "(no task)"
        text = f"{d['due_on']} · {escape(d['payee'])} · {_money(d['amount'], d.get('currency') or 'INR')}{kind} · {task}"
        html.append(text)
        md.append(f"- {text}")
        rows += 1
    for f in brief.get("forecast") or []:
        text = f"{f['date']} · {escape(f['description'])} · {escape(f['amount'])}"
        html.append(text)
        md.append(f"- {text}")
        rows += 1
    if rows == 0:
        html.append("Nothing due.")
        md.append("Nothing due.")
    html.append("")
    md.append("")

    if brief.get("unknowns"):
        html.append("<b>Unexplained</b>")
        md.append("## Unexplained")
        for u in brief["unknowns"][:10]:
            text = f"{u['occurred_on']} · {escape(u['payee'])} · {_money(u['amount'], u.get('currency') or 'INR')} · {u.get('channel') or '-'}"
            html.append(text)
            md.append(f"- {text}")
        hint = 'Reply with "<payee> is <account>" or use ledger_add_rule.'
        html += [escape(hint), ""]
        md += [hint, ""]

    if brief.get("closed_dues"):
        html.append("<b>Paid this week</b>")
        md.append("## Paid this week")
        for c in brief["closed_dues"]:
            text = f"{escape(c['payee'])} · {_money(c['amount'], c.get('currency') or 'INR')}"
            html.append(text)
            md.append(f"- {text}")
        html.append("")
        md.append("")

    house = []
    if brief.get("unpushed"):
        house.append(f"{brief['unpushed']} unpushed commits")
    if brief.get("low_confidence"):
        house.append(f"{brief['low_confidence']} low-confidence LLM postings")
    if house:
        html += ["<b>Housekeeping</b>", " · ".join(house)]
        md += ["## Housekeeping", " · ".join(house)]

    return {"html": "\n".join(html).rstrip("\n"), "markdown": "\n".join(md).rstrip("\n") + "\n"}


def render_month_close(close: dict) -> dict:
    html = [f"<b>Month close · {close['month']}</b>"]
    md = [f"# Month close · {close['month']}", ""]
    if close.get("books_ok"):
        html += [f"<pre>{escape(close.get('is_text') or '')}</pre>", f"<pre>{escape(close.get('bs_text') or '')}</pre>"]
        md += ["```", (close.get("is_text") or "").rstrip("\n"), "```", "", "```", (close.get("bs_text") or "").rstrip("\n"), "```", ""]
    else:
        html.append("Books unavailable — index only.")
        md.append("_Books unavailable — index only._")
    lines = [
        f"Recurring commitments: {_money(close.get('recurring_total', '0'))}",
        f"Unexplained postings: {close.get('unknown_count', 0)}",
        f"Dues paid: {close.get('dues_paid', 0)} · still open: {close.get('dues_open', 0)}",
    ]
    html += lines
    md += [f"- {ln}" for ln in lines]
    return {"html": "\n".join(html), "markdown": "\n".join(md) + "\n"}
```

- [ ] **Step 4: Activities and flows**

`money.py` additions:

```python
    @activity.defn
    async def render_money_brief(self, brief: dict) -> dict:
        from aegis_worker.activities.money_render import render_money_brief

        return render_money_brief(brief, _symbol(self.home_currency))

    @activity.defn
    async def render_month_close(self, close: dict) -> dict:
        from aegis_worker.activities.money_render import render_month_close

        return render_month_close(close)

    @activity.defn
    async def notify_money_message(self, html: str, log_event: str) -> None:
        await safe_send_message(self.delivery, agent_id=self.agent_id, message=html, log_event=log_event)

    @activity.defn
    async def write_money_report(self, rel_path: str, text: str) -> None:
        if self.books_cfg is None:
            return
        try:
            await books.write_report(rel_path, text, self.books_cfg)
        except books.BooksError as exc:
            logger.warning("money_report_write_failed", path=rel_path, error=str(exc)[:200])
```

`flows/money_brief.py`:

```python
"""MoneyBriefFlow — the Sunday money brief (spec §7.2)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis_worker.shared.retry import FAST, NO_RETRY

_FAST = timedelta(seconds=60)
_SLOW = timedelta(seconds=180)


@dataclass
class MoneyBriefConfig:
    agent_id: str = "maou"
    days: int = 7
    silent: bool = False


@workflow.defn(name="MoneyBriefFlow")
class MoneyBriefFlow:
    @workflow.run
    async def run(self, config: MoneyBriefConfig) -> dict:
        try:
            await workflow.execute_activity("refresh_fx_prices", start_to_close_timeout=_FAST, retry_policy=NO_RETRY)
        except Exception as exc:  # noqa: BLE001 — prices are a nicety
            workflow.logger.warning("money_brief_fx_failed err=%s", str(exc)[:200])
        brief = await workflow.execute_activity(
            "build_money_brief", args=[config.days], start_to_close_timeout=_SLOW, retry_policy=FAST
        )
        rendered = await workflow.execute_activity(
            "render_money_brief", args=[brief], start_to_close_timeout=_FAST, retry_policy=NO_RETRY
        )
        sent = False
        if not config.silent:
            await workflow.execute_activity(
                "notify_money_message", args=[rendered["html"], "money_brief_notify_failed"],
                start_to_close_timeout=_FAST, retry_policy=NO_RETRY,
            )
            sent = True
        await workflow.execute_activity(
            "write_money_report", args=[f"reports/weekly/{brief['as_of']}.md", rendered["markdown"]],
            start_to_close_timeout=_SLOW, retry_policy=NO_RETRY,
        )
        return {
            "as_of": brief["as_of"],
            "sent": sent,
            "dues": len(brief.get("dues") or []),
            "unknowns": len(brief.get("unknowns") or []),
            "books_ok": bool(brief.get("books_ok")),
        }
```

`flows/month_close.py` mirrors it with `MonthCloseConfig(agent_id="maou", silent=False)`, `MonthCloseFlow`, activities `build_month_close` → `render_month_close` → `notify_money_message(html, "month_close_notify_failed")` → `write_money_report(f"reports/monthly/{close['month']}.md", markdown)`, returning `{"month", "sent", "books_ok"}`.

Registry: import both flows/configs; add the two `FlowSpec`s next to `ReceiptIngestFlow`'s (leave the two v1 specs in place until Task 3 deletes them). Seed: the two rows in `config/seed/activities.yaml` under the Maou comment block. Migration 027 as above.

- [ ] **Step 5: Run, lint, commit**

Run: `PYTHONPATH=core/src:worker/src:comms/src .venv/bin/python -m pytest tests/worker/test_money_render.py tests/worker/flows/test_money_brief.py tests/worker/flows/test_month_close.py tests/worker/test_registry.py tests/worker/test_schedule_sync_mappers.py tests/worker/test_schedule_sync_feature_flags.py -n 4 --dist loadfile --timeout=300 -q 2>&1 | tail -5` — all passed (adjust the registry count tests' expected numbers for two new flows, documenting the delta in the test).
Run: `.venv/bin/ruff check worker/src/ tests/worker/` — clean.

```bash
git add worker/src/aegis_worker/activities/money_render.py worker/src/aegis_worker/activities/money.py worker/src/aegis_worker/flows/money_brief.py worker/src/aegis_worker/flows/month_close.py worker/src/aegis_worker/registry.py config/seed/activities.yaml migrations/027_drop_v1_money_schedules.sql tests/worker/test_money_render.py tests/worker/flows/test_money_brief.py tests/worker/flows/test_month_close.py tests/worker/test_registry.py tests/worker/test_schedule_sync_mappers.py
git commit -m "feat(money): weekly money brief and month close flows from the books"
```

---

### Task 3: Delete the v1 subscription machinery

**Files:**
- Delete: `worker/src/aegis_worker/flows/money_hygiene.py`, `worker/src/aegis_worker/flows/subscription_audit.py`, `core/src/aegis/services/fx.py`, `tests/worker/flows/test_money_hygiene.py`, `tests/worker/activities/test_money_bundle_e.py`, `tests/core/test_fx.py` (if it exists)
- Modify: `worker/src/aegis_worker/activities/money.py` — delete `upsert_charges`, `classify_and_extract`, `detect_cancellations`, `evaluate_renewal_alerts`, `notify_renewal_alert`, `notify_cancellation`, `build_subscription_digest`, `notify_subscription_digest`, `_previous_month_window`, `parse_bank_alert_senders`, `_is_bank_alert_sender`, the `bank_alert_senders` and `fx_rates` fields, the `to_monthly_home` import; keep `_symbol`, `_format_agent_persona`, `store_receipt_email`, `load_receipts`, `store_receipt_body`, `find_stuck_receipts`, `parse_money_email`, `post_money_event`, `store_money_result`, and the Task 1/2 activities
- Modify: `core/src/aegis/llm/__init__.py` — delete `extract_receipts_batch`, `_BATCH_RECEIPT_PROMPT`, `_format_receipts_for_prompt`; `core/src/aegis/api/models/money.py` — delete `ReceiptExtraction`
- Modify: `worker/src/aegis_worker/registry.py` (remove the two FlowSpecs + imports), `worker/src/aegis_worker/__main__.py` (constructor args), `core/src/aegis/config.py` (remove `bank_alert_senders`, `money_hygiene_fx_rates`), `core/src/aegis/services/integrations_config.py` (remove the `bank_alert_senders` key), `config/seed/activities.yaml` (remove the two v1 rows)
- Modify tests: `tests/worker/activities/test_money.py` (keep only tests of surviving code — likely none: delete the file if nothing survives), `tests/core/test_llm.py` (remove `extract_receipts_batch` tests), `tests/worker/test_schedule_sync_feature_flags.py` / `test_schedule_sync_mappers.py` / `test_registry.py` (remove the two flows from any enumeration and adjust counts), `tests/worker/test_curiosity_gaps.py` (its `_add_charge` helper stays until Task 4 replaces the detector — do not touch here), `tests/core/test_money_routes.py` (untouched until Task 6)
- Docs: `docs/how-it-works.md` schedule table (drop the two rows, add `money-brief-weekly` and `money-close-monthly` rows), `docs/architecture/overview.md:39` (Maou flows list → `MoneyProcessFlow`, `ReceiptIngestFlow`, `MoneyBriefFlow`, `MonthCloseFlow`)

**Interfaces:** nothing new. `MoneyActivities.__init__` loses `fx_rates` and `bank_alert_senders`; every remaining constructor call and test fixture is updated.

- [ ] **Step 1: Delete and fix imports**

Delete the files, then remove the listed functions and fields. Run `.venv/bin/ruff check core/src/ tests/core/ worker/src/ tests/worker/` and `grep -rn "upsert_charges\|classify_and_extract\|extract_receipts_batch\|ReceiptExtraction\|to_monthly_home\|bank_alert_senders\|money_hygiene_fx_rates\|MoneyHygiene\|SubscriptionAudit\|detect_cancellations\|evaluate_renewal_alerts\|notify_renewal_alert\|notify_cancellation\|subscription_digest" core/src worker/src comms/src tests config docs` until the only hits are the legacy-table mention in the spec/plan documents and the `finance.subscription_digest`/`recurring_charge` table names in `docs/architecture/overview.md` (annotate that line: "legacy tables, unwritten since 2026-09; dropped in a follow-up").

- [ ] **Step 2: Run every package, lint, commit**

Run the three package suites sequentially (`-n 8 --dist loadfile --timeout=300`, `tee logs/test-<pkg>-t3.log`) — all green. `ci-grep-guard.yml` patterns: check `.github/workflows/ci-grep-guard.yml` does not list any of the deleted names as must-exist (it lists must-NOT-exist n8n names; fine).

```bash
git add -A
git commit -m "refactor(money): delete the v1 subscription tracker, extractor and renewal machinery"
```

<!-- CONTINUED IN TASK 4 -->
