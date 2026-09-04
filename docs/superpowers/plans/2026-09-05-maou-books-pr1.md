# Maou books — PR1 "stop the bleeding" Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the receipt extractor the full email body, stop the per-receipt and renewal Inbox task spam, stop curiosity re-asking the same money question, and put a single money formatter in place, so the finance lane stops hurting this week while PR2/PR3 build the books.

**Architecture:** Four small, independent changes to the existing v1 money pipeline (`MoneyProcessFlow`, `MoneyHygieneDailyFlow`, `CuriosityActivities`) plus one comms formatter tweak. No schema change: the body is stored under `receipt_email.parsed.body_text` and the existing `parsed` writes become merges so it survives extraction. Nothing here is thrown away by PR2; `fetch_message_body`, `store_receipt_body`, `fmt_money` and the `<pre>` mapping are all reused.

**Tech Stack:** Python 3.12, Temporal (`temporalio`), asyncpg, pytest + pytest-asyncio, `ActivityEnvironment` / `WorkflowEnvironment.start_time_skipping()`.

**Spec:** `docs/superpowers/specs/2026-09-05-maou-books-design.md` — sections 1 (Problem), 7.4 (What stops), 12 (PR split, PR1 bullet). This plan is PR1 only.

## Global Constraints

- Tests run one package at a time: `PYTHONPATH=core/src:worker/src:comms/src .venv/bin/python -m pytest tests/<pkg>/ -n 8 --dist loadfile --timeout=300 2>&1 | tee logs/test-<pkg>.log` from the worktree root. `-n 8`, not `-n auto` (20 workers exhaust the test Postgres). Never run the whole suite in one process.
- Lint per package: `.venv/bin/ruff check core/src/ tests/core/` (and `worker/src/ tests/worker/`, `comms/src/ tests/comms/`). Never `ruff format` `core/src/aegis/services/chat.py` or `core/src/aegis/services/tools/infra.py`.
- The repo `.venv` resolves editable installs to the MAIN checkout; always set `PYTHONPATH=core/src:worker/src:comms/src` when running tests from this worktree.
- Amounts shown to a human come from `fmt_money` only. `amount_cents` never appears in a title, message or report.
- Commit messages: single line, semantic type (`feat(money): …`, `fix(curiosity): …`), no co-author trailer.
- Do not touch `finance.recurring_charge` semantics beyond what a task says; PR2 replaces the pipeline.

---

### Task 1: `fmt_money` and its use in the Slack notifies

**Files:**
- Create: `core/src/aegis/services/money_format.py`
- Modify: `worker/src/aegis_worker/activities/money.py:566-580` (`notify_renewal_alert` body), `:608-622` (`notify_cancellation` body)
- Test: `tests/core/test_money_format.py`

**Interfaces:**
- Produces: `fmt_money(amount: Decimal | int | float | str, currency: str | None) -> str`. Indian digit grouping for INR (`₹1,00,308.53`), thousands grouping with symbol for USD/GBP/EUR (`$5.89`, `£6,285.01`, `€10.00`), `"<grouped> <ISO>"` for anything else (`12.00 SGD`), two decimals always, `ROUND_HALF_UP`, leading `-` for negatives, `""`/`None` currency renders as a bare grouped number.

- [ ] **Step 1: Write the failing tests**

```python
# tests/core/test_money_format.py
from decimal import Decimal

from aegis.services.money_format import fmt_money


def test_inr_uses_indian_grouping():
    assert fmt_money(Decimal("100308.53"), "INR") == "₹1,00,308.53"
    assert fmt_money(Decimal("1234.56"), "INR") == "₹1,234.56"
    assert fmt_money(Decimal("10"), "INR") == "₹10.00"
    assert fmt_money(Decimal("12345678.9"), "inr") == "₹1,23,45,678.90"


def test_western_currencies_use_symbol_and_thousands():
    assert fmt_money(Decimal("5.89"), "USD") == "$5.89"
    assert fmt_money(Decimal("6285.01"), "GBP") == "£6,285.01"
    assert fmt_money(10, "EUR") == "€10.00"


def test_unknown_currency_is_iso_suffix():
    assert fmt_money(Decimal("12"), "SGD") == "12.00 SGD"


def test_missing_currency_is_bare_number():
    assert fmt_money(Decimal("12.5"), None) == "12.50"
    assert fmt_money(Decimal("12.5"), "") == "12.50"


def test_negative_and_rounding():
    assert fmt_money(Decimal("-5"), "INR") == "-₹5.00"
    assert fmt_money("1.005", "USD") == "$1.01"
    assert fmt_money(0.1 + 0.2, "USD") == "$0.30"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=core/src:worker/src:comms/src .venv/bin/python -m pytest tests/core/test_money_format.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'aegis.services.money_format'`

- [ ] **Step 3: Write the implementation**

```python
# core/src/aegis/services/money_format.py
"""The one money formatter. Every human-facing amount goes through `fmt_money`.

Major units in, grouped string out. Indian grouping for INR (₹1,00,308.53),
thousands grouping for the symbol currencies, ISO suffix for the rest.
`amount_cents` never reaches a title, message or report (spec §5.1).
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

_SYMBOL = {"INR": "₹", "USD": "$", "GBP": "£", "EUR": "€"}
_CENT = Decimal("0.01")


def _indian_group(whole: str) -> str:
    """'100308' -> '1,00,308'; '1234' -> '1,234'; '999' -> '999'."""
    if len(whole) <= 3:
        return whole
    head, tail = whole[:-3], whole[-3:]
    parts: list[str] = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts) + "," + tail


def fmt_money(amount: Decimal | int | float | str, currency: str | None) -> str:
    q = Decimal(str(amount)).quantize(_CENT, rounding=ROUND_HALF_UP)
    sign = "-" if q < 0 else ""
    whole, frac = f"{abs(q):.2f}".split(".")
    code = (currency or "").upper()
    grouped = _indian_group(whole) if code == "INR" else f"{int(whole):,}"
    sym = _SYMBOL.get(code)
    if sym:
        return f"{sign}{sym}{grouped}.{frac}"
    if code:
        return f"{sign}{grouped}.{frac} {code}"
    return f"{sign}{grouped}.{frac}"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=core/src:worker/src:comms/src .venv/bin/python -m pytest tests/core/test_money_format.py -v`
Expected: 5 passed

- [ ] **Step 5: Use it in the two Slack notifies**

In `worker/src/aegis_worker/activities/money.py` add the import next to the existing `from aegis.services.fx import to_monthly_home`:

```python
from aegis.services.money_format import fmt_money
```

In `notify_renewal_alert` replace

```python
        amount = alert["amount_cents"] / 100
```
with
```python
        amount = fmt_money(Decimal(alert["amount_cents"]) / 100, alert.get("currency") or "")
```
and the body line `f"Amount: {amount:.2f} {currency}\n"` with `f"Amount: {amount}\n"`. Delete the now-unused `currency = _html.escape(...)` line in that function (ruff will flag it). Add `from decimal import Decimal` to the module imports.

In `notify_cancellation` replace

```python
        amount_cents = cancellation.get("amount_cents") or 0
        amount = amount_cents / 100
```
with
```python
        amount = fmt_money(
            Decimal(cancellation.get("amount_cents") or 0) / 100, cancellation.get("currency") or ""
        )
```
and `f"Amount: {amount:.2f} {currency} ({cadence})\n"` with `f"Amount: {amount} ({cadence})\n"`. Delete the unused `currency = _html.escape(...)` line there too.

- [ ] **Step 6: Run the worker money tests and lint**

Run: `PYTHONPATH=core/src:worker/src:comms/src .venv/bin/python -m pytest tests/worker/activities/test_money.py tests/worker/activities/test_money_bundle_e.py -n 4 --dist loadfile --timeout=300 2>&1 | tee logs/test-task1.log`
Expected: all passed (the notify tests assert routing and escaping, not the amount text).
Run: `.venv/bin/ruff check core/src/ tests/core/ worker/src/ tests/worker/`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add core/src/aegis/services/money_format.py tests/core/test_money_format.py worker/src/aegis_worker/activities/money.py
git commit -m "feat(money): fmt_money formatter, major units in renewal and cancellation notices"
```

---

### Task 2: `fetch_message_body`, `store_receipt_body`, body-first `load_receipts`, merged `parsed` writes

**Files:**
- Modify: `worker/src/aegis_worker/activities/gmail.py` (after `_extract_text_from_part`, ~line 133; new activity after `fetch_thread`, ~line 500)
- Modify: `worker/src/aegis_worker/activities/money.py:178-205` (`load_receipts`), `:291-299`, `:316-322`, `:339-345`, `:409-414` (the four `parsed` UPDATEs in `upsert_charges`); new activity `store_receipt_body` after `load_receipts`
- Modify: `core/src/aegis/llm/__init__.py:211` (`_format_receipts_for_prompt` body cap 1500 → 4000)
- Test: `tests/worker/test_fetch_message_body.py` (new), `tests/worker/activities/test_money_store_receipt.py` (add three tests)

**Interfaces:**
- Produces: `GmailActivities.fetch_message_body(account_label: str, message_id: str, max_chars: int = 6000) -> str` — text/plain part first, else text/html reduced to text; `<style>`/`<script>` blocks dropped, tags stripped, entities unescaped, `https?://…` runs replaced by `<url>`, runs of spaces/tabs/NBSP/zero-width collapsed to one space, blank-line runs collapsed to one newline; truncated to `max_chars`; **returns `""` on any failure** (logged as `fetch_message_body_failed`), never raises.
- Produces: module-level `html_to_text(html: str) -> str` and `_extract_html_from_part(part: dict) -> str` in `gmail.py`.
- Produces: `MoneyActivities.store_receipt_body(receipt_id: str, body_text: str) -> None` — merges `{"body_text": body_text}` into `receipt_email.parsed`.
- Changes: `load_receipts` returns `body_plain` = `parsed->>'body_text'` when non-empty, else `parsed->>'snippet'`; every `parsed` write in `upsert_charges` merges (`COALESCE(parsed,'{}'::jsonb) || $2`) instead of replacing.

- [ ] **Step 1: Write the failing activity tests**

```python
# tests/worker/test_fetch_message_body.py
"""fetch_message_body: the full email text for the money extractor (spec §2 step 2)."""

from __future__ import annotations

import base64

import pytest
from aegis_worker.activities.gmail import GmailActivities, html_to_text
from temporalio.testing import ActivityEnvironment

HTML = (
    "<html><head><style>.x{color:red}</style></head><body>"
    "<p>Dear Customer,</p><p>Rs.10.00 is debited from your account ending 1225 "
    "towards VPA q203028199@ybl (Jai shree nakoda) on 02-09-26.</p>"
    "<a href='https://example.com/track?id=1'>https://example.com/track?id=1</a>"
    "<script>alert(1)</script>&nbsp;&nbsp;Regards,&amp; HDFC</body></html>"
)


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


class _FakeSvc:
    def __init__(self, payload: dict, raise_exc: Exception | None = None):
        self._payload = payload
        self._raise = raise_exc

    def users(self):
        return self

    def messages(self):
        return self

    def get(self, **_kw):
        return self

    def execute(self):
        if self._raise:
            raise self._raise
        return {"payload": self._payload, "snippet": "snip"}


def _acts(monkeypatch, tmp_path, svc):
    (tmp_path / "acct.json").write_text("{}")
    monkeypatch.setattr("aegis_worker.activities.gmail._build_gmail_service", lambda *_a: svc)
    return GmailActivities(gmail_credentials_file="c.json", gmail_token_dir=str(tmp_path))


def test_html_to_text_strips_markup_scripts_and_urls():
    text = html_to_text(HTML)
    assert "Dear Customer," in text
    assert "Rs.10.00 is debited" in text
    assert "color:red" not in text
    assert "alert(1)" not in text
    assert "example.com" not in text and "<url>" in text
    assert "Regards,& HDFC" in text
    assert "  " not in text


@pytest.mark.asyncio
async def test_plain_part_wins_over_html(monkeypatch, tmp_path):
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": _b64("plain body\n\n\nend")}},
            {"mimeType": "text/html", "body": {"data": _b64(HTML)}},
        ],
    }
    acts = _acts(monkeypatch, tmp_path, _FakeSvc(payload))
    body = await ActivityEnvironment().run(acts.fetch_message_body, "acct", "m1")
    assert body == "plain body\nend"


@pytest.mark.asyncio
async def test_html_only_message_is_reduced_to_text(monkeypatch, tmp_path):
    payload = {"mimeType": "text/html", "body": {"data": _b64(HTML)}}
    acts = _acts(monkeypatch, tmp_path, _FakeSvc(payload))
    body = await ActivityEnvironment().run(acts.fetch_message_body, "acct", "m1")
    assert body.startswith("Dear Customer,")
    assert "<p>" not in body


@pytest.mark.asyncio
async def test_max_chars_truncates(monkeypatch, tmp_path):
    payload = {"mimeType": "text/plain", "body": {"data": _b64("x" * 10_000)}}
    acts = _acts(monkeypatch, tmp_path, _FakeSvc(payload))
    body = await ActivityEnvironment().run(acts.fetch_message_body, "acct", "m1", 100)
    assert len(body) == 100


@pytest.mark.asyncio
async def test_failure_returns_empty_string(monkeypatch, tmp_path):
    acts = _acts(monkeypatch, tmp_path, _FakeSvc({}, raise_exc=RuntimeError("gmail down")))
    body = await ActivityEnvironment().run(acts.fetch_message_body, "acct", "m1")
    assert body == ""
```

Append to `tests/worker/activities/test_money_store_receipt.py`. It already has a `db_pool` fixture, `_make_act(db_pool)` (line 38) and `_insert_receipt_email(conn, *, message_id, parsed, received_days_ago) -> str` (line 117, returns the id as a string). Message ids must start with `rt-` so the autouse cleanup fixture deletes them:

```python
@pytest.mark.asyncio
async def test_store_receipt_body_merges_into_parsed(db_pool):
    act = _make_act(db_pool)
    async with db_pool.acquire() as conn:
        rid = await _insert_receipt_email(
            conn, message_id="rt-body-1", parsed={"snippet": "snip"}, received_days_ago=0.1
        )
    await act.store_receipt_body(rid, "full body text")
    async with db_pool.acquire() as conn:
        parsed = await conn.fetchval(
            "SELECT parsed FROM finance.receipt_email WHERE id = $1::uuid", rid
        )
    assert parsed == {"snippet": "snip", "body_text": "full body text"}


@pytest.mark.asyncio
async def test_load_receipts_prefers_body_text_over_snippet(db_pool):
    act = _make_act(db_pool)
    async with db_pool.acquire() as conn:
        with_body = await _insert_receipt_email(
            conn, message_id="rt-body-2", parsed={"snippet": "snip", "body_text": "full"},
            received_days_ago=0.1,
        )
        without = await _insert_receipt_email(
            conn, message_id="rt-body-3", parsed={"snippet": "snip only"}, received_days_ago=0.1
        )
    rows = await act.load_receipts([with_body, without])
    by_id = {r["message_id"]: r["body_plain"] for r in rows}
    assert by_id == {"rt-body-2": "full", "rt-body-3": "snip only"}


@pytest.mark.asyncio
async def test_upsert_charges_keeps_body_text(db_pool):
    act = _make_act(db_pool)
    async with db_pool.acquire() as conn:
        rid = await _insert_receipt_email(
            conn, message_id="rt-body-4", parsed={"body_text": "full"}, received_days_ago=0.1
        )
    await act.upsert_charges(
        "_t", [{"receipt_id": rid, "is_receipt": False, "confidence": 0.9}]
    )
    async with db_pool.acquire() as conn:
        parsed = await conn.fetchval(
            "SELECT parsed FROM finance.receipt_email WHERE id = $1::uuid", rid
        )
    assert parsed["body_text"] == "full"
    assert parsed["is_receipt"] is False
```

In the first test, fix the `rid` usage the same way (`await act.store_receipt_body(rid, …)` and `$1::uuid", rid`) — `_insert_receipt_email` already returns a string.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=core/src:worker/src:comms/src .venv/bin/python -m pytest tests/worker/test_fetch_message_body.py tests/worker/activities/test_money_store_receipt.py -v 2>&1 | tail -20`
Expected: ImportError on `html_to_text`; `AttributeError: 'MoneyActivities' object has no attribute 'store_receipt_body'`.

- [ ] **Step 3: Implement the Gmail side**

In `worker/src/aegis_worker/activities/gmail.py`, directly after `_extract_text_from_part`:

```python
def _extract_html_from_part(part: dict) -> str:
    """Recursively extract the first text/html part, decoded."""
    import base64

    mime = part.get("mimeType", "")
    body_data = (part.get("body") or {}).get("data", "")
    if mime == "text/html" and body_data:
        try:
            return base64.urlsafe_b64decode(body_data + "==").decode("utf-8", errors="replace")
        except Exception:
            return ""
    for subpart in part.get("parts") or []:
        text = _extract_html_from_part(subpart)
        if text:
            return text
    return ""


_URL_RE = re.compile(r"https?://\S+")
_TAG_BLOCK_RE = re.compile(r"<(style|script)[^>]*>.*?</\1>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"[ \t\xa0​‌͏]+")
_BLANK_RE = re.compile(r"\n\s*\n+")


def html_to_text(html_src: str) -> str:
    """Reduce an HTML email body to readable text for the money extractor."""
    import html as _html

    text = _TAG_BLOCK_RE.sub(" ", html_src)
    text = _TAG_RE.sub(" ", text)
    text = _html.unescape(text)
    return _clean_text(text)


def _clean_text(text: str) -> str:
    text = _URL_RE.sub("<url>", text)
    text = _SPACE_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    text = _BLANK_RE.sub("\n", text)
    return text.strip()
```

(`re` is already imported at the top of `gmail.py`; if not, add `import re`.)

After `fetch_thread`, add the activity:

```python
    @activity.defn
    async def fetch_message_body(
        self, account_label: str, message_id: str, max_chars: int = 6000
    ) -> str:
        """Full text of one message for the money extractor (spec §2 step 2).

        text/plain part first, else text/html reduced to text. Best-effort:
        any failure returns "" so the caller falls back to the snippet.
        """
        token_path = Path(self.gmail_token_dir) / f"{account_label}.json"

        def _sync() -> str:
            svc = _build_gmail_service(self.gmail_credentials_file, token_path)
            full = svc.users().messages().get(userId="me", id=message_id, format="full").execute()
            payload = full.get("payload") or {}
            text = _clean_text(_extract_text_from_part(payload))
            if not text:
                text = html_to_text(_extract_html_from_part(payload))
            return text[:max_chars]

        try:
            return await asyncio.to_thread(_sync)
        except Exception as exc:  # noqa: BLE001 — body is an enhancement; never fail the flow
            activity.logger.warning(
                "fetch_message_body_failed account=%s msg=%s err=%s",
                account_label,
                message_id,
                str(exc)[:200],
            )
            return ""
```

- [ ] **Step 4: Implement the money side**

In `worker/src/aegis_worker/activities/money.py`:

`load_receipts` SQL: replace `"parsed->>'snippet' AS body_plain, received_at "` with
`"COALESCE(NULLIF(parsed->>'body_text', ''), parsed->>'snippet') AS body_plain, received_at "`.

Add after `load_receipts`:

```python
    @activity.defn
    async def store_receipt_body(self, receipt_id: str, body_text: str) -> None:
        """Merge the fetched full text into `parsed.body_text` (spec §2 step 2)."""
        if not self.db_pool or not receipt_id:
            return
        async with self.db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE finance.receipt_email "
                "SET parsed = COALESCE(parsed, '{}'::jsonb) || $2 "
                "WHERE id = $1::uuid",
                receipt_id,
                {"body_text": body_text},
            )
```

In `upsert_charges`, change every `"UPDATE finance.receipt_email SET parsed=$2 WHERE id=$1::uuid"` (three sites) to `"UPDATE finance.receipt_email SET parsed = COALESCE(parsed, '{}'::jsonb) || $2 WHERE id=$1::uuid"`, and the final `"UPDATE finance.receipt_email SET parsed=$2, charge_id=$3 WHERE id=$1::uuid"` to `"UPDATE finance.receipt_email SET parsed = COALESCE(parsed, '{}'::jsonb) || $2, charge_id=$3 WHERE id=$1::uuid"`.

In `core/src/aegis/llm/__init__.py` `_format_receipts_for_prompt`: `[:1500]` → `[:4000]`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `PYTHONPATH=core/src:worker/src:comms/src .venv/bin/python -m pytest tests/worker/test_fetch_message_body.py tests/worker/activities/test_money_store_receipt.py tests/worker/activities/test_money.py tests/worker/activities/test_money_bundle_e.py tests/worker/test_fetch_thread_budget.py -n 4 --dist loadfile --timeout=300 2>&1 | tee logs/test-task2.log | tail -5`
Expected: all passed.

- [ ] **Step 6: Falsifiability check, then lint**

Temporarily change `COALESCE(NULLIF(parsed->>'body_text', ''), parsed->>'snippet')` back to `parsed->>'snippet'` in `load_receipts`, rerun `test_load_receipts_prefers_body_text_over_snippet`, confirm it FAILS, revert. Then `.venv/bin/ruff check core/src/ tests/core/ worker/src/ tests/worker/` — clean.

- [ ] **Step 7: Commit**

```bash
git add worker/src/aegis_worker/activities/gmail.py worker/src/aegis_worker/activities/money.py core/src/aegis/llm/__init__.py tests/worker/test_fetch_message_body.py tests/worker/activities/test_money_store_receipt.py
git commit -m "feat(money): fetch the full email body for receipt extraction"
```

---

### Task 3: `MoneyProcessFlow` and the sweep use the body; delete the per-receipt Inbox capture

**Files:**
- Modify: `worker/src/aegis_worker/flows/money_process.py` (whole `run`)
- Modify: `worker/src/aegis_worker/flows/receipt_ingest.py:143-200` (`_sweep_stuck_receipts`)
- Modify: `docs/how-it-works.md` (the `source_tag = '#receipt'` row of the verb table, ~line 273, and the `#receipt` node in the mermaid graph ~line 290)
- Test: `tests/worker/flows/test_money_process.py`, `tests/worker/flows/test_receipt_ingest.py`

**Interfaces:**
- Consumes: `fetch_message_body(account_label, message_id)` and `store_receipt_body(receipt_id, body_text)` from Task 2 (called by activity name string, `start_to_close_timeout=_ACT_TIMEOUT`, `retry_policy=ACT_RETRY`).
- Changes: `MoneyProcessFlow.run` result for the charged path is `{"status": "charged", "receipt_id", "processed"}` (unchanged) but no Todoist capture happens. `CaptureActivities`, `TIMEOUT_FAST`, `NO_RETRY` imports leave `money_process.py`.

- [ ] **Step 1: Update the flow tests first**

In `tests/worker/flows/test_money_process.py`:

Replace the `_calls` dict with

```python
_calls: dict[str, list] = {
    "store": [],
    "body": [],
    "store_body": [],
    "load": [],
    "classify": [],
    "upsert": [],
}
```

Delete `stub_capture` and its `@activity.defn(name="capture_to_inbox")`. Add, after `stub_store`:

```python
@activity.defn(name="fetch_message_body")
async def stub_body(account_label: str, message_id: str, max_chars: int = 6000) -> str:
    _calls["body"].append((account_label, message_id))
    return "Receipt from Stripe $9.99 Paid"


@activity.defn(name="fetch_message_body")
async def stub_body_empty(account_label: str, message_id: str, max_chars: int = 6000) -> str:
    _calls["body"].append((account_label, message_id))
    return ""


@activity.defn(name="store_receipt_body")
async def stub_store_body(receipt_id: str, body_text: str) -> None:
    _calls["store_body"].append((receipt_id, body_text))
```

`_HAPPY_STUBS = [stub_store, stub_body, stub_store_body, stub_load, stub_classify_receipt, stub_upsert]`. Every other stub list in the file that contained `stub_capture` gets `stub_body, stub_store_body` instead.

In `test_charged_path`, replace the four capture assertions (from `# Capture should fire once` to `assert "Stripe" in title`) with:

```python
    assert _calls["body"] == [("user-personal", "gmail-msg-1")]
    assert _calls["store_body"] == [("uid-gmail-msg-1", "Receipt from Stripe $9.99 Paid")]
```

Add a new test:

```python
@pytest.mark.asyncio
async def test_empty_body_is_not_stored():
    _reset()
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=[MoneyProcessFlow],
            activities=[stub_store, stub_body_empty, stub_store_body, stub_load,
                        stub_classify_receipt, stub_upsert],
        ),
    ):
        result = await env.client.execute_workflow(
            MoneyProcessFlow.run,
            MoneyProcessInput(agent_id="maou", msg=_MSG, account_label="user-personal"),
            id="mp-empty-body",
            task_queue="tq",
        )
    assert result["status"] == "charged"
    assert _calls["store_body"] == []
```

In `tests/worker/flows/test_receipt_ingest.py`, in `test_receipt_flow_sweeps_stuck_receipts` (line ~231) and `test_receipt_flow_sweep_leaves_still_failing_rows_unparsed` (line ~313): add two local stubs before the `Worker(...)` and put them in the `activities=[...]` list:

```python
    body_calls: list[tuple] = []

    @activity.defn(name="fetch_message_body")
    async def stub_body(account_label: str, message_id: str, max_chars: int = 6000) -> str:
        body_calls.append((account_label, message_id))
        return "full body"

    @activity.defn(name="store_receipt_body")
    async def stub_store_body(receipt_id: str, body_text: str) -> None:
        return None
```

and in `test_receipt_flow_sweeps_stuck_receipts` assert, after the workflow returns, `assert body_calls == [("sebas", "m-stuck-1")]` (its `find_stuck` stub returns `["stuck-1"]` and its `load` stub returns account `"sebas"`, message id `"m-stuck-1"`).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=core/src:worker/src:comms/src .venv/bin/python -m pytest tests/worker/flows/test_money_process.py tests/worker/flows/test_receipt_ingest.py -n 4 --dist loadfile --timeout=300 2>&1 | tail -15`
Expected: `test_charged_path` fails on `_calls["body"] == []`; `test_empty_body_is_not_stored` fails; the sweep test fails on `body_calls`.

- [ ] **Step 3: Rewrite `MoneyProcessFlow.run`**

Replace the file body from the `with workflow.unsafe.imports_passed_through():` block to the end with:

```python
with workflow.unsafe.imports_passed_through():
    from aegis_worker.shared.retry import ACT_RETRY

_ACT_TIMEOUT = timedelta(seconds=60)
_CLASSIFY_TIMEOUT = timedelta(seconds=120)


@dataclass
class MoneyProcessInput:
    agent_id: str
    msg: dict
    account_label: str


@workflow.defn(name="MoneyProcessFlow")
class MoneyProcessFlow:
    @workflow.run
    async def run(self, input: MoneyProcessInput) -> dict:
        receipt_id = await workflow.execute_activity(
            "store_receipt_email",
            args=[input.msg, input.account_label],
            start_to_close_timeout=_ACT_TIMEOUT,
            retry_policy=ACT_RETRY,
        )
        if not receipt_id:
            return {"status": "duplicate", "message_id": input.msg.get("id", "")}

        # Full body for the extractor (spec §2 step 2). "" = fetch failed; the
        # snippet stored by store_receipt_email is the fallback.
        body = await workflow.execute_activity(
            "fetch_message_body",
            args=[input.account_label, input.msg.get("id", "")],
            start_to_close_timeout=_ACT_TIMEOUT,
            retry_policy=ACT_RETRY,
        )
        if body:
            await workflow.execute_activity(
                "store_receipt_body",
                args=[receipt_id, body],
                start_to_close_timeout=_ACT_TIMEOUT,
                retry_policy=ACT_RETRY,
            )

        receipts = await workflow.execute_activity(
            "load_receipts",
            [receipt_id],
            start_to_close_timeout=_ACT_TIMEOUT,
            retry_policy=ACT_RETRY,
        )
        if not receipts:
            return {"status": "load_failed", "receipt_id": receipt_id}

        try:
            extractions = await workflow.execute_activity(
                "classify_and_extract",
                args=[receipts, input.agent_id],
                start_to_close_timeout=_CLASSIFY_TIMEOUT,
                retry_policy=ACT_RETRY,
            )
        except Exception as exc:
            # ACT_RETRY already gave us up to 3 attempts. Treat persistent
            # failure as transient — DON'T mark parsed so the next pass
            # re-tries. receipt_email row stays in the unparsed state.
            workflow.logger.warning(
                "money_extract_failed receipt_id=%s err=%s",
                receipt_id,
                str(exc)[:200],
            )
            return {"status": "extract_failed", "receipt_id": receipt_id}

        if not extractions:
            return {"status": "extract_failed", "receipt_id": receipt_id}

        ext = extractions[0]
        if ext.get("_parse_failed"):
            workflow.logger.warning(
                "money_parse_failed receipt_id=%s — leaving unparsed",
                receipt_id,
            )
            return {"status": "parse_failed", "receipt_id": receipt_id}

        if not ext.get("is_receipt"):
            await workflow.execute_activity(
                "upsert_charges",
                args=[input.account_label, [ext]],
                start_to_close_timeout=_ACT_TIMEOUT,
                retry_policy=ACT_RETRY,
            )
            return {"status": "not_a_receipt", "receipt_id": receipt_id}

        processed = await workflow.execute_activity(
            "upsert_charges",
            args=[input.account_label, [ext]],
            start_to_close_timeout=_ACT_TIMEOUT,
            retry_policy=ACT_RETRY,
        )
        # No per-receipt Todoist capture any more (spec §7.4): a successful
        # payment is read in the weekly brief, never filed as a task.
        return {
            "status": "charged",
            "receipt_id": receipt_id,
            "processed": processed,
        }
```

Update the module docstring's pipeline list to include `fetch_message_body(account, id) → store_receipt_body` after `store_receipt_email`, and delete the sentence about capturing to the Inbox if present.

- [ ] **Step 4: Update the sweep**

In `receipt_ingest.py` `_sweep_stuck_receipts`, after `if not receipts: continue` and before `classify_and_extract`:

```python
                body = await workflow.execute_activity(
                    "fetch_message_body",
                    args=[receipts[0]["account"], receipts[0]["message_id"]],
                    start_to_close_timeout=_ACT_TIMEOUT,
                    retry_policy=ACT_RETRY,
                )
                if body:
                    await workflow.execute_activity(
                        "store_receipt_body",
                        args=[receipt_id, body],
                        start_to_close_timeout=_ACT_TIMEOUT,
                        retry_policy=ACT_RETRY,
                    )
                    receipts[0]["body_plain"] = body
```

- [ ] **Step 5: Update the docs row**

In `docs/how-it-works.md`, the verb-table row for `source_tag = '#receipt'`: change its description to "Legacy: `#receipt` tasks are no longer created by `MoneyProcessFlow` (since 2026-09-05); an existing one still gets the merchant-history decision card." Leave the graph node in place.

- [ ] **Step 6: Run the tests to verify they pass, then lint**

Run: `PYTHONPATH=core/src:worker/src:comms/src .venv/bin/python -m pytest tests/worker/flows/test_money_process.py tests/worker/flows/test_receipt_ingest.py tests/worker/flows/test_gmail_ingest.py -n 4 --dist loadfile --timeout=300 2>&1 | tee logs/test-task3.log | tail -5`
Expected: all passed.
Run: `.venv/bin/ruff check worker/src/ tests/worker/` — clean (the unused `CaptureActivities`/`TIMEOUT_FAST`/`NO_RETRY` imports must be gone).

- [ ] **Step 7: Commit**

```bash
git add worker/src/aegis_worker/flows/money_process.py worker/src/aegis_worker/flows/receipt_ingest.py tests/worker/flows/test_money_process.py tests/worker/flows/test_receipt_ingest.py docs/how-it-works.md
git commit -m "feat(money): extract from the full body and stop filing a task per receipt"
```

---

### Task 4: `MoneyHygieneDailyFlow` stops filing Inbox tasks

**Files:**
- Modify: `worker/src/aegis_worker/flows/money_hygiene.py` (both sweeps)
- Test: `tests/worker/flows/test_money_hygiene.py`

**Interfaces:**
- Changes: no `capture_to_inbox` activity call from this flow; `CaptureActivities` and `TIMEOUT_FAST`/`NO_RETRY` imports are removed if unused (`NO_RETRY` and `TIMEOUT_FAST` are still used by the notify calls — keep those). Result shape `{"cancelled": n, "renewals": n}` unchanged; Slack notifies unchanged.

- [ ] **Step 1: Update the tests first**

In `tests/worker/flows/test_money_hygiene.py`: delete `_capture_calls`, `stub_capture` and its decorator, remove `stub_capture` from `ALL_STUBS` and from the explicit list in `test_cancellation_failure_does_not_block_renewals`, remove `_capture_calls.clear()` from `_clear()` if present. In `test_runs_both_sweeps_with_config` delete the three capture lines (`assert len(_capture_calls) == 3 …`, `ext_ids = …`, `assert "cancel-c1" …`). In `test_silent_suppresses_capture_and_notify` delete `assert _capture_calls == []` and rename it `test_silent_suppresses_notify`. Add:

```python
@pytest.mark.asyncio
async def test_flow_never_calls_capture_to_inbox():
    """Spec §7.4: renewals and cancellations are Slack FYIs, never Inbox tasks."""
    _clear()
    seen: list[str] = []

    @activity.defn(name="capture_to_inbox")
    async def trap(source_tag: str, external_id: str, title: str, description=None) -> str:
        seen.append(title)
        return "task"

    await _run(MoneyHygieneConfig(), activities=[*ALL_STUBS, trap], wid="mh-4")
    assert seen == []
```

- [ ] **Step 2: Run to verify the new test fails**

Run: `PYTHONPATH=core/src:worker/src:comms/src .venv/bin/python -m pytest tests/worker/flows/test_money_hygiene.py -n 2 --dist loadfile --timeout=300 2>&1 | tail -8`
Expected: `test_flow_never_calls_capture_to_inbox` FAILS with `seen == ['Possible cancellation: Netflix', …]`.

- [ ] **Step 3: Remove the captures**

In `money_hygiene.py`:

- In `_scan_cancellations`, delete from `vendor = cancel.get("vendor_name") or "subscription"` through the `except Exception as exc: workflow.logger.warning("cancel_capture_failed" …)` block (the whole capture, including the `amount_fmt`, `currency`, `cadence`, `last_seen`, `last_date` locals that only feed it). Keep the `notify_cancellation` call.
- In `_scan_renewals`, delete from `charge_id = a.get("charge_id")` through the `except Exception as exc: workflow.logger.warning("renewal_capture_failed" …)` block. Keep the `notify_renewal_alert` call.
- Remove `from aegis_worker.activities.capture import CaptureActivities`.
- Update the module docstring: "`silent` suppresses the Slack notifies (the DB state changes still happen). Nothing here files a Todoist task (spec §7.4)."

- [ ] **Step 4: Run the tests to verify they pass, then lint**

Run: `PYTHONPATH=core/src:worker/src:comms/src .venv/bin/python -m pytest tests/worker/flows/test_money_hygiene.py -n 2 --dist loadfile --timeout=300 2>&1 | tee logs/test-task4.log | tail -5`
Expected: all passed.
Run: `.venv/bin/ruff check worker/src/ tests/worker/` — clean.

- [ ] **Step 5: Commit**

```bash
git add worker/src/aegis_worker/flows/money_hygiene.py tests/worker/flows/test_money_hygiene.py
git commit -m "fix(money): renewal and cancellation sweeps stop filing Inbox tasks"
```

---

### Task 5: Curiosity asks each money question once

**Files:**
- Modify: `worker/src/aegis_worker/activities/curiosity.py:142-151` (`_already_asked`), `:206-244` (`_detect_recurring_charge`), module docstring lines 4 and 18
- Test: `tests/worker/test_curiosity_gaps.py`

**Interfaces:**
- Changes: `_already_asked()` returns novelty keys from interactions of **any** status. `_detect_recurring_charge` keys candidates as `charge:<first word of the normalised vendor name>` and emits one candidate per key (the highest `monthly_home_equivalent` wins). Produces module-level `charge_key(vendor: str) -> str`.

- [ ] **Step 1: Update the tests first**

In `tests/worker/test_curiosity_gaps.py` replace `test_archived_interaction_does_not_suppress` with:

```python
async def test_archived_interaction_suppresses(clean_db):
    """Spec §6: an unanswered card is not re-sent as a fresh card. The weekly
    brief's unknown list is the retry channel, not another interruption."""
    await _add_charge(clean_db, "Framer")
    await _add_interaction(clean_db, "charge:framer", status="archived")

    assert await _run(clean_db) == []


async def test_vendor_name_variants_share_one_key(clean_db):
    """'Mahavitaran (MSEDCL)' and 'Mahavitaran - Maharashtra Electricity (MSEDCL)'
    were six cards in a month. One key, one card, biggest charge wins."""
    await _add_charge(clean_db, "Mahavitaran (MSEDCL)", monthly=8100.0)
    await _add_charge(clean_db, "Mahavitaran - Maharashtra Electricity (MSEDCL)", monthly=7200.0)

    out = await _run(clean_db)

    assert [c["novelty_key"] for c in out] == ["charge:mahavitaran"]
    assert out[0]["subject"] == "Mahavitaran (MSEDCL)"


def test_charge_key_is_first_normalised_word():
    from aegis_worker.activities.curiosity import charge_key

    assert charge_key("Apple iCloud") == "apple"
    assert charge_key("Mahavitaran - Maharashtra Electricity (MSEDCL)") == "mahavitaran"
    assert charge_key("  1Password ") == "1password"
    assert charge_key("") == ""
```

Also update the module docstring at line 4 of the test file if it says "non-archived" (make it "any interaction").

- [ ] **Step 2: Run to verify the new tests fail**

Run: `PYTHONPATH=core/src:worker/src:comms/src .venv/bin/python -m pytest tests/worker/test_curiosity_gaps.py -n 2 --dist loadfile --timeout=300 2>&1 | tail -10`
Expected: `test_archived_interaction_suppresses` fails (candidate returned), `test_vendor_name_variants_share_one_key` fails (two keys), `test_charge_key_is_first_normalised_word` fails on ImportError.

- [ ] **Step 3: Implement**

In `curiosity.py` add near the other module constants:

```python
_KEY_RE = re.compile(r"[^a-z0-9]+")


def charge_key(vendor: str) -> str:
    """First word of the lowercased, punctuation-free vendor name.

    'Mahavitaran (MSEDCL)' and 'Mahavitaran - Maharashtra Electricity (MSEDCL)'
    are the same bill; keying on the first word collapses every variant the
    extractor has produced for one vendor (#PR1 of the books spec, §6).
    # ponytail: first word; PR3 replaces this detector with payee_key from the
    # journal index.
    """
    words = _KEY_RE.sub(" ", (vendor or "").lower()).split()
    return words[0] if words else ""
```

(`import re` if the module lacks it.)

Replace `_already_asked`:

```python
    async def _already_asked(self) -> set[str]:
        """novelty_keys carried by ANY interaction, archived included.

        A timed-out card is not an answered question, but re-sending it is
        an interruption the user already declined once. The weekly money
        brief lists unexplained charges; that is the retry channel.
        """
        rows = await self.db_pool.fetch(
            "SELECT DISTINCT metadata->>'novelty_key' AS k FROM interactions "
            "WHERE metadata ? 'novelty_key'"
        )
        return {r["k"] for r in rows if r["k"]}
```

In `_detect_recurring_charge`, build one candidate per key:

```python
        best: dict[str, tuple[float, dict]] = {}
        for r in rows:
            vendor = (r["vendor_name"] or "").strip()
            key = charge_key(vendor)
            if not key or vendor.lower() in known or key in known.split():
                continue
            monthly = float(r["monthly_home_equivalent"] or 0)
            cand = (
                10.0 + monthly,
                {
                    "gap_type": "recurring_charge",
                    "subject": vendor,
                    "question": (
                        f"You have an active {r['cadence']} charge from {vendor}, "
                        "but nothing on record about why. What is it for?"
                    ),
                    "evidence": {
                        "category": r["category"],
                        "cadence": r["cadence"],
                        "monthly_home_equivalent": monthly,
                    },
                    "novelty_key": f"charge:{key}",
                },
            )
            if key not in best or cand[0] > best[key][0]:
                best[key] = cand
        return list(best.values())
```

(The rows are already ordered by `monthly_home_equivalent DESC`, so the first row per key wins ties.) Update the module docstring lines that describe the archived rule ("A row already carrying that key removes the candidate" is now true for every status).

- [ ] **Step 4: Run the tests to verify they pass, then lint**

Run: `PYTHONPATH=core/src:worker/src:comms/src .venv/bin/python -m pytest tests/worker/test_curiosity_gaps.py tests/worker/test_curiosity_card_flow.py -n 2 --dist loadfile --timeout=300 2>&1 | tee logs/test-task5.log | tail -5`
Expected: all passed (`test_active_charge_with_no_memory_yields_candidate` still passes: `charge:framer`).
Run: `.venv/bin/ruff check worker/src/ tests/worker/` — clean.

- [ ] **Step 5: Commit**

```bash
git add worker/src/aegis_worker/activities/curiosity.py tests/worker/test_curiosity_gaps.py
git commit -m "fix(curiosity): one money question per vendor, archived cards count as asked"
```

---

### Task 6: `<pre>` renders as a Slack code block

**Files:**
- Modify: `comms/src/aegis_comms/format.py:27` (`_WRAP`) and the module docstring mapping list
- Test: `tests/comms/test_format.py`

**Interfaces:**
- Changes: `html_to_mrkdwn("<pre>…</pre>")` → "```\n…\n```". Inner whitespace and newlines preserved (the parser already keeps them).

- [ ] **Step 1: Write the failing test**

Append to `tests/comms/test_format.py`:

```python
def test_pre_becomes_code_block():
    assert html_to_mrkdwn("<pre>a  b\n c</pre>") == "```\na  b\n c\n```"


def test_pre_inside_message_keeps_surrounding_text():
    assert html_to_mrkdwn("<b>Week</b>\n<pre>x</pre>\nend") == "*Week*\n```\nx\n```\nend"
```

- [ ] **Step 2: Run to verify it fails**

Run: `PYTHONPATH=core/src:worker/src:comms/src .venv/bin/python -m pytest tests/comms/test_format.py -v 2>&1 | tail -6`
Expected: the two new tests FAIL (`<pre>` is stripped today, output `a  b\n c`).

- [ ] **Step 3: Implement**

In `comms/src/aegis_comms/format.py` add to `_WRAP`:

```python
    "pre": ("```\n", "\n```"),
```

and add the line `  <pre>                   -> ```…``` (block)` to the docstring mapping.

- [ ] **Step 4: Run the comms tests, then lint**

Run: `PYTHONPATH=core/src:worker/src:comms/src .venv/bin/python -m pytest tests/comms/ -n 4 --dist loadfile --timeout=300 2>&1 | tee logs/test-task6.log | tail -5`
Expected: all passed.
Run: `.venv/bin/ruff check comms/src/ tests/comms/` — clean.

- [ ] **Step 5: Commit**

```bash
git add comms/src/aegis_comms/format.py tests/comms/test_format.py
git commit -m "feat(comms): render <pre> as a Slack code block"
```

---

### Task 7: Full per-package runs and PR

**Files:** none new.

- [ ] **Step 1: Run each package exactly as CI does**

```bash
PYTHONPATH=core/src:worker/src:comms/src .venv/bin/python -m pytest tests/core/ -n 8 --dist loadfile --timeout=300 2>&1 | tee logs/test-core.log | tail -3
PYTHONPATH=core/src:worker/src:comms/src .venv/bin/python -m pytest tests/worker/ -n 8 --dist loadfile --timeout=300 2>&1 | tee logs/test-worker.log | tail -3
PYTHONPATH=core/src:worker/src:comms/src .venv/bin/python -m pytest tests/comms/ -n 8 --dist loadfile --timeout=300 2>&1 | tee logs/test-comms.log | tail -3
.venv/bin/ruff check core/src/ tests/core/ && .venv/bin/ruff check worker/src/ tests/worker/ && .venv/bin/ruff check comms/src/ tests/comms/
```
Expected: three green runs (run them one after another, never concurrently: they share the test Postgres), ruff clean.

- [ ] **Step 2: Push and open the PR**

```bash
git push -u origin worktree-maou-books
gh pr create --title "feat(money): full-body extraction, no per-receipt tasks, curiosity dedupe (books PR1)" --body-file /tmp/claude-1000/-home-arshad-Workspace-hikmah-aegis/4bab0db5-7fb2-462d-91e9-41a63ca50390/scratchpad/pr1-body.md
```

The body file: what changed (the six bullets above), the spec path, "PR1 of 3 for `docs/superpowers/specs/2026-09-05-maou-books-design.md`", and the test summary lines from Step 1.
