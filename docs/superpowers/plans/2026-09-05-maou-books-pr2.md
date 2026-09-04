# Maou books — PR2 "the books" Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every money email becomes a transaction in the hledger books (`hikmahtech/books`), a dated Todoist task when it is a bill, or an indexed no-op; bank alerts are parsed deterministically, everything else by the LLM from the full body.

**Architecture:** A pure `books.py` service (render/parse journal blocks, rules, git sync, hledger runner) shared by core and worker over one working copy under the `aegis_config` volume, serialised by a file lock. A `journal_index` Postgres table gives idempotency, receipt↔bank matching and dues dedupe; the journal stays the record. `MoneyProcessFlow` v2 is: store → fetch body → parse (regex parsers, else LLM) → route by kind → index. The v1 `recurring_charge` machinery is left in place, unwritten, until PR3 deletes it.

**Tech Stack:** Python 3.12, hledger 1.52.3 (static binary in both images and at `~/.local/bin/hledger` on meem), git, PyYAML (already a core dependency), pydantic v2, Temporal, asyncpg, pytest.

**Spec:** `docs/superpowers/specs/2026-09-05-maou-books-design.md` — sections 1–5, 7.1, 10, 11, and the PR2 bullet of 12. PR1 (`2026-09-05-maou-books-pr1.md`) shipped `fmt_money` (`core/src/aegis/services/money_format.py`), `GmailActivities.fetch_message_body`, `MoneyActivities.store_receipt_body`, body-first `load_receipts` and merged `parsed` writes; this plan builds on them.

## Global Constraints

- Tests run one package at a time: `PYTHONPATH=core/src:worker/src:comms/src .venv/bin/python -m pytest tests/<pkg>/ -n 8 --dist loadfile --timeout=300 2>&1 | tee logs/test-<pkg>.log` from the worktree root; `-n 8`, never `-n auto`; never the whole suite in one process. Focused runs during a task may use `-n 4`.
- Lint per package: `.venv/bin/ruff check core/src/ tests/core/` (and `worker/src/ tests/worker/`, `comms/src/ tests/comms/`). Never `ruff format` `core/src/aegis/services/chat.py` or `core/src/aegis/services/tools/infra.py`.
- The repo `.venv` resolves editable installs to the MAIN checkout; always set `PYTHONPATH=core/src:worker/src:comms/src`.
- Tests that need the hledger binary are marked `pytest.mark.skipif(shutil.which("hledger") is None, reason="hledger not installed")`; on meem it is at `~/.local/bin/hledger` (on PATH). Never download it inside a test.
- Amounts are `Decimal` with 2 places, major units. Human-facing strings use `fmt_money`; journal amounts use `render_amount` (no digit grouping): `₹1234.56`, `$5.89`, `£6285.01`, `€10.00`, `12.00 SGD`. `amount_cents` never appears in new code.
- Journal block grammar (spec §1): line 1 `YYYY-MM-DD * <payee>`; line 2 `    ; msgid: <mailbox>/<gmail id>`; line 3 `    ; channel: <c>[, ref: <r>][, instrument: <i>][, receipt: <m>][, bank: <m>]`; then posting 1 `    <category account padded to 40 chars><signed amount>` and posting 2 `    <instrument account>` with no amount. Blocks are separated by exactly one blank line. `hledger check --strict` must pass after every write.
- Ruling (spec §5.1 amended here): posting 1 is ALWAYS the category account with the signed amount (`+` for `direction=out`, `-` for `direction=in`), posting 2 is always the instrument account without an amount. One grammar for both directions makes `rewrite_block` trivial.
- Account tails are the last four digits (three for NKGSB) the bank prints; nothing longer is ever written anywhere.
- Config: `Settings.books_path` (default `/app/config/books`), `books_repo_url` (default `""` = books disabled), `books_deploy_key` (secret, default `""`), `books_ignored_mailboxes` (default `["arshad-stpd"]`), `books_todoist_projects` (default `{}`). Integration registry group `Books`: `books_repo_url` (non-secret), `books_deploy_key` (secret).
- Git identity for books commits: `Maou <maou@aegis.local>`. `GIT_SSH_COMMAND="ssh -i <gmail_token_dir>/books_deploy_key -o StrictHostKeyChecking=accept-new -o IdentitiesOnly=yes"`.
- Migration file is `migrations/026_journal_index.sql`, idempotent DDL (`IF NOT EXISTS`).
- Commit messages: single line, semantic type, no trailers.

---

### Task 1: hledger and git in both images

**Files:**
- Modify: `core/Dockerfile:30-35` (runtime apt line), `worker/Dockerfile:39-47` (runtime apt line)
- Modify: `docs/development.md` (add a "hledger" line to the setup section)
- Test: none (image build is verified by `make aegis-release`; the plan's Task 9 checks `docker run --rm <image> hledger --version` after the release)

**Interfaces:**
- Produces: `/usr/local/bin/hledger` (1.52.3, static) and `git` in both runtime images.

- [ ] **Step 1: worker/Dockerfile**

Add `git` to the apt list (`openssh-client curl ffmpeg openssl tini git`) and, after the docker-cli `RUN` block and before `WORKDIR /app`, add:

```dockerfile
# hledger — the books (spec 2026-09-05-maou-books-design.md §10). Static
# release binary, pinned by version and checksum.
ARG HLEDGER_VERSION=1.52.3
ARG HLEDGER_SHA256=d14a4fc2ac804b556f481b64e8c54efa380db1ac85b3723c9df7b1eeade74b3a
RUN curl -fsSL -o /tmp/hledger.tgz \
      "https://github.com/simonmichael/hledger/releases/download/${HLEDGER_VERSION}/hledger-linux-x64.tar.gz" \
    && echo "${HLEDGER_SHA256}  /tmp/hledger.tgz" | sha256sum -c - \
    && tar -xzf /tmp/hledger.tgz -C /tmp hledger \
    && install -m 0755 /tmp/hledger /usr/local/bin/hledger \
    && rm -f /tmp/hledger.tgz /tmp/hledger
```

- [ ] **Step 2: core/Dockerfile**

Same: add `git` to the runtime apt list at line 30 and the identical `ARG`/`RUN` block after the kubectl `RUN` (before the `EXTRA_CLOUD_CLIS` block).

- [ ] **Step 3: docs/development.md**

In the setup section add:

```
- **hledger 1.52.3** for the books tests (`tests/core/test_books.py`,
  `tests/worker/activities/test_money_v2.py` skip without it):
  `curl -fsSL https://github.com/simonmichael/hledger/releases/download/1.52.3/hledger-linux-x64.tar.gz | tar -xz hledger && install -m 0755 hledger ~/.local/bin/`.
  The images install the same pinned binary.
```

- [ ] **Step 4: Verify the Dockerfile still parses and commit**

Run: `docker build --target builder -q -f worker/Dockerfile . >/dev/null && echo builder-ok` (the builder stage is unchanged; this just proves the file parses). If Docker is unavailable, run `grep -c HLEDGER_SHA256 core/Dockerfile worker/Dockerfile` and expect `1` for each.

```bash
git add core/Dockerfile worker/Dockerfile docs/development.md
git commit -m "build(money): hledger 1.52.3 and git in the core and worker images"
```

---

### Task 2: `MoneyEvent`, `payee_key`, `account_for`, `instrument_account`, `render_amount`

**Files:**
- Modify: `core/src/aegis/api/models/money.py` (append `MoneyEvent`)
- Create: `core/src/aegis/services/books.py` (pure helpers only in this task; the writer arrives in Task 4)
- Test: `tests/core/test_books_helpers.py`

**Interfaces:**
- Produces (`aegis.api.models.money`): `class MoneyEvent(BaseModel)` exactly as spec §2, plus `def payee_key(payee: str) -> str`.
- Produces (`aegis.services.books`): `render_amount(amount: Decimal, currency: str, *, negative: bool = False) -> str`; `account_for(category: str | None, direction: str | None, entity: str) -> str`; `instrument_account(instrument: str | None, declared: set[str] | frozenset[str] = frozenset()) -> str`; `UNKNOWN = {"personal": {"out": "expenses:unknown", "in": "income:unknown"}, "hikmah": {"out": "expenses:hikmah:unknown", "in": "income:hikmah:other"}}`; re-export `fmt_money`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/core/test_books_helpers.py
from decimal import Decimal

import pytest
from aegis.api.models.money import MoneyEvent, payee_key
from aegis.services.books import account_for, fmt_money, instrument_account, render_amount


def test_payee_key_normalises():
    assert payee_key("Jai shree nakoda") == "jai shree nakoda"
    assert payee_key("  AMAZON  WEB-SERVICES (India) ") == "amazon web services india"
    assert payee_key("q203028199@ybl") == "q203028199 ybl"
    assert payee_key("") == ""


def test_money_event_defaults_and_validation():
    e = MoneyEvent(kind="transaction", direction="out", amount=Decimal("10"), currency="INR")
    assert e.entity == "personal" and e.channel == "other" and e.parser == "llm"
    assert e.source_class == "other" and e.confidence == 1.0
    with pytest.raises(ValueError):
        MoneyEvent(kind="bogus")


def test_render_amount_no_grouping():
    assert render_amount(Decimal("100308.53"), "INR") == "₹100308.53"
    assert render_amount(Decimal("5.89"), "USD") == "$5.89"
    assert render_amount(Decimal("6285.01"), "GBP") == "£6285.01"
    assert render_amount(Decimal("10"), "EUR") == "€10.00"
    assert render_amount(Decimal("12"), "SGD") == "12.00 SGD"
    assert render_amount(Decimal("150"), "INR", negative=True) == "-₹150.00"


def test_fmt_money_is_reexported():
    assert fmt_money(Decimal("1234.5"), "INR") == "₹1,234.50"


@pytest.mark.parametrize(
    "category,direction,entity,expected",
    [
        ("saas", "out", "personal", "expenses:saas"),
        ("saas", "out", "hikmah", "expenses:hikmah:saas"),
        ("infra", "out", "hikmah", "expenses:hikmah:infra"),
        ("infra", "out", "personal", "expenses:saas"),
        ("electricity", "out", "personal", "expenses:utilities:electricity"),
        ("electricity", "out", "hikmah", "expenses:hikmah:unknown"),
        ("groceries", "out", "personal", "expenses:groceries"),
        ("fees", "out", "hikmah", "expenses:hikmah:fees:bank"),
        ("professional", "out", "hikmah", "expenses:hikmah:professional"),
        ("professional", "out", "personal", "expenses:unknown"),
        ("salary", "in", "personal", "income:salary"),
        ("salary", "in", "hikmah", "income:hikmah:other"),
        ("other", "out", "personal", "expenses:unknown"),
        (None, "in", "hikmah", "income:hikmah:other"),
        (None, None, "personal", "expenses:unknown"),
    ],
)
def test_account_for(category, direction, entity, expected):
    assert account_for(category, direction, entity) == expected


def test_instrument_account():
    declared = {"liabilities:card:axis:1313", "liabilities:card:axis:1747", "assets:bank:hdfc:1225"}
    assert instrument_account("hdfc-1225") == "assets:bank:hdfc:1225"
    assert instrument_account("axis-cc-1313") == "liabilities:card:axis:1313"
    assert instrument_account("nkgsb-843") == "assets:bank:nkgsb:843"
    assert instrument_account("axis-9640") == "assets:bank:axis:9640"
    assert instrument_account("card-1313", declared) == "liabilities:card:axis:1313"
    assert instrument_account("card-9999", declared) == "assets:unknown"
    # With a declared set, a computed account that is not declared is unknown.
    assert instrument_account("hdfc-1225", declared) == "assets:bank:hdfc:1225"
    assert instrument_account("hdfc-0000", declared) == "assets:unknown"
    assert instrument_account("axis-upi", declared) == "assets:unknown"
    assert instrument_account(None) == "assets:unknown"
    assert instrument_account("") == "assets:unknown"
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=core/src:worker/src:comms/src .venv/bin/python -m pytest tests/core/test_books_helpers.py -q 2>&1 | tail -3`
Expected: ImportError (`MoneyEvent`, `aegis.services.books`).

- [ ] **Step 3: Append `MoneyEvent` to `core/src/aegis/api/models/money.py`**

```python
import re
from datetime import date
from decimal import Decimal

_KEY_RE = re.compile(r"[^a-z0-9]+")


def payee_key(payee: str) -> str:
    """Lowercase, punctuation to single spaces, trimmed. The matching key for
    rules, curiosity and dues dedupe (spec §2)."""
    return _KEY_RE.sub(" ", (payee or "").lower()).strip()


class MoneyEvent(BaseModel):
    """One money email, parsed (spec §2). The journal is written from this."""

    kind: Literal["transaction", "due", "failed", "info", "ignore"]
    direction: Literal["in", "out"] | None = None
    amount: Decimal | None = None
    currency: str | None = None
    payee: str = ""
    payee_key: str = ""
    channel: Literal[
        "upi", "imps", "neft", "card", "autopay", "remittance",
        "receipt", "bill", "statement", "manual", "other",
    ] = "other"
    instrument: str | None = None
    occurred_on: date | None = None
    due_on: date | None = None
    entity: Literal["personal", "hikmah", "none"] = "personal"
    account: str | None = None
    category: str | None = None
    ref: str | None = None
    is_recurring: bool | None = None
    parser: str = "llm"
    confidence: float = 1.0
    source_class: Literal["bank", "receipt", "other"] = "other"
```

(`Literal` and `BaseModel` are already imported in that module; add the new imports at the top, keeping `from __future__ import annotations`.) Add `"manual"` to the channel list as shown — `ledger_post` in PR3 needs it.

- [ ] **Step 4: Create `core/src/aegis/services/books.py` with the pure helpers**

```python
"""The books: journal helpers, rules, git sync and the hledger runner.

Spec: docs/superpowers/specs/2026-09-05-maou-books-design.md §1, §4, §5.
This module is shared by core (tools) and worker (flows). Amounts are
`Decimal` major units; `render_amount` is the journal form (no grouping),
`fmt_money` the human form.
"""

from __future__ import annotations

from decimal import Decimal

from aegis.services.money_format import fmt_money  # noqa: F401 — re-export (spec §5.1)

_SYMBOL = {"INR": "₹", "USD": "$", "GBP": "£", "EUR": "€"}
_CENT = Decimal("0.01")

UNKNOWN = {
    "personal": {"out": "expenses:unknown", "in": "income:unknown"},
    "hikmah": {"out": "expenses:hikmah:unknown", "in": "income:hikmah:other"},
}

# spec §4 — category → account, per entity. Missing key ⇒ the unknown account.
_ACCOUNT_MAP: dict[str, dict[str, str]] = {
    "personal": {
        "saas": "expenses:saas",
        "media": "expenses:media",
        "infra": "expenses:saas",
        "internet": "expenses:utilities:internet",
        "electricity": "expenses:utilities:electricity",
        "mobile": "expenses:utilities:mobile",
        "groceries": "expenses:groceries",
        "food": "expenses:food",
        "transport": "expenses:transport",
        "shopping": "expenses:shopping",
        "health": "expenses:health",
        "insurance": "expenses:insurance",
        "fees": "expenses:fees:bank",
        "tax": "expenses:tax",
        "people": "expenses:people",
        "salary": "income:salary",
        "interest": "income:interest",
        "refund": "income:refunds",
    },
    "hikmah": {
        "saas": "expenses:hikmah:saas",
        "media": "expenses:hikmah:saas",
        "infra": "expenses:hikmah:infra",
        "internet": "expenses:hikmah:internet",
        "fees": "expenses:hikmah:fees:bank",
        "tax": "expenses:hikmah:tax",
        "professional": "expenses:hikmah:professional",
        "ads": "expenses:hikmah:ads",
    },
}

_INCOME_CATEGORIES = frozenset({"salary", "interest", "refund"})


def render_amount(amount: Decimal, currency: str, *, negative: bool = False) -> str:
    """Journal amount: symbol-prefixed, no digit grouping, ISO suffix otherwise."""
    q = Decimal(amount).quantize(_CENT)
    code = (currency or "").upper()
    sign = "-" if negative else ""
    sym = _SYMBOL.get(code)
    return f"{sign}{sym}{q}" if sym else f"{sign}{q} {code}".strip()


def account_for(category: str | None, direction: str | None, entity: str) -> str:
    """Counter account for an event (spec §4). Unknown ⇒ the entity's unknown account."""
    ent = "hikmah" if entity == "hikmah" else "personal"
    side = "in" if direction == "in" or (category or "") in _INCOME_CATEGORIES else "out"
    if side == "in" and ent == "hikmah":
        return "income:hikmah:other"
    mapped = _ACCOUNT_MAP[ent].get((category or "").lower())
    if mapped and (mapped.startswith("income:") == (side == "in")):
        return mapped
    return UNKNOWN[ent][side]


def instrument_account(instrument: str | None, declared: set[str] | frozenset[str] = frozenset()) -> str:
    """`hdfc-1225` → `assets:bank:hdfc:1225`, `axis-cc-1313` → `liabilities:card:axis:1313`,
    `card-1313` → the declared `liabilities:card:*:1313`, else `assets:unknown`."""
    if not instrument:
        return "assets:unknown"
    parts = instrument.lower().split("-")
    if len(parts) == 2 and parts[0] == "card":
        tail = parts[1]
        for acct in sorted(declared):
            if acct.startswith("liabilities:card:") and acct.endswith(f":{tail}"):
                return acct
        return "assets:unknown"
    if len(parts) == 3 and parts[1] == "cc":
        computed = f"liabilities:card:{parts[0]}:{parts[2]}"
    elif len(parts) == 2:
        computed = f"assets:bank:{parts[0]}:{parts[1]}"
    else:
        return "assets:unknown"
    # An empty `declared` means "no chart to check against" (unit tests);
    # with a chart, an undeclared instrument must not break `check --strict`.
    if declared and computed not in declared:
        return "assets:unknown"
    return computed
```

- [ ] **Step 5: Run, lint, commit**

Run: `PYTHONPATH=core/src:worker/src:comms/src .venv/bin/python -m pytest tests/core/test_books_helpers.py -q 2>&1 | tail -3` — all passed.
Run: `.venv/bin/ruff check core/src/ tests/core/` — clean.

```bash
git add core/src/aegis/api/models/money.py core/src/aegis/services/books.py tests/core/test_books_helpers.py
git commit -m "feat(money): MoneyEvent model and journal account helpers"
```

---

### Task 3: Deterministic bank and vendor parsers

**Files:**
- Create: `core/src/aegis/services/bank_parsers.py`
- Test: `tests/core/test_bank_parsers.py`

**Interfaces:**
- Produces: `parse_any(sender: str, subject: str, body: str) -> MoneyEvent | None` trying `PARSERS` in order; each `parse_<name>(sender, subject, body) -> MoneyEvent | None`; helpers `amount_from(s) -> Decimal`, `currency_from(token) -> str | None`. Every returned event has `parser=<name>`, `payee_key` filled, `source_class` `bank` for the bank ones and `receipt` for the vendor ones, `entity="personal"` except `axis_remittance` (`hikmah`). A parser that matches its sender/subject anchors but cannot read an amount returns `None`.

- [ ] **Step 1: Write the failing tests (fixtures inline; names and numbers altered from the real mail)**

```python
# tests/core/test_bank_parsers.py
"""Table-driven tests for the deterministic parsers (spec §3). Fixture text is
the real 2026-09 mail with names, tails, refs and amounts altered."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from aegis.services import bank_parsers as bp

HDFC = "HDFC Bank InstaAlerts <alerts@hdfcbank.bank.in>"
AXIS = "Axis Bank Alerts <alerts@axis.bank.in>"

FIX = {
    "hdfc_upi_debit": (
        HDFC,
        "❗  You have done a UPI txn. Check details!",
        "Dear Customer, Greetings from HDFC Bank! Rs.245.50 is debited from your account "
        "ending 4321 towards VPA shop77@ybl (Corner Store) on 02-09-26. UPI transaction "
        "reference no.: 100000000001. If you did not authorize this transaction, please "
        "report it immediately.",
    ),
    "hdfc_upi_credit": (
        HDFC,
        "❗  You have done a UPI txn. Check details!",
        "Dear Customer, Rs.1,500.00 is credited to your account ending 4321 from VPA "
        "friend@okaxis (A Friend) on 03-09-26. UPI transaction reference no.: 100000000002.",
    ),
    "hdfc_imps": (
        HDFC,
        "View: Account update for your HDFC Bank A/c",
        "Dear Customer, Greetings from HDFC Bank! INR 10,000.00 has been debited from your "
        "account ending xxxxxxxxxx4321 on 21-08-26 and credited to the account ending "
        "xxxxxxxxxx8765 via IMPS. IMPS Reference No: 600000000003 Available Balance: INR 90,957.97",
    ),
    "nkgsb_credit": (
        "alerts@nkgsb-bank.com",
        "NKGSB Bank - Transaction Alert",
        "Dear Sir/ Madam, Received Rs.150.00 in NKGSB Bank A/C X999 on 31-08-26 "
        "UPI/CREDIT/660000000004/someone-1@okicici/N.Clr bal Rs.5923.21.",
    ),
    "axis_card_spend": (
        '"alerts@axis.bank.in" <alerts@axis.bank.in>',
        "USD 5.89 spent on credit card no. XX9876",
        "18-08-2026 Dear Customer, Here's the summary of your Axis Bank Credit Card "
        "Transaction: Transaction Amount: USD 5.89 Merchant Name: 1PASSWORD Axis Bank Credit "
        "Card No. XX9876 Date & Time: 18-08-2026, 16:37:46 IST Available Limit*: INR 157592.24",
    ),
    "axis_autopay_done": (
        AXIS,
        "AutoPay for GitHubInc: ACTIVATED",
        "21-08-2026 Dear Customer, Here's the summary of your successful AutoPay transaction: "
        "Transaction Amount: USD 4.00 Merchant Name: GitHubInc AutoPay ID: Yb0000 Axis Bank "
        "Card No. XX9876 Max Limit: USD 15000.00",
    ),
    "axis_autopay_reminder": (
        AXIS,
        "Upcoming AutoPay txn. reminder",
        "02-09-2026 Dear Customer, Here's the summary of your upcoming AutoPay transaction: "
        "Transaction Amount: INR 262.30 Merchant Name: AMAZON WEB SERVICES To be debited by: "
        "04-09-2026 Max Limit: INR 262.30 Axis Bank card No. XX5555 AutoPay ID: Xf0000",
    ),
    "axis_cc_statement": (
        "cc.statements@axis.bank.in",
        "Your Axis Bank My Zone Credit Card Statement ending XX76 - August 2026",
        "Axis Bank Dear Customer, Please find enclosed your credit card statement for AUGUST "
        "2026. Total Amount Due INR Minimum Amount Due (INR) Payment Due Date (DD-MM-YYYY) "
        "100308.53 Dr 25113 Dr 07/09/2026 How do I access my statement?",
    ),
    "axis_remittance": (
        "eforexservices@axis.bank.in",
        "Inward Remittance Notification",
        "Dear Sir/Madam, This email is to inform you that we have received foreign currency "
        "funds of GBP 6285.01 from overseas bearing reference numbers SWIFT Reference "
        "Number:GBC00000000000. ARC Reference Number:030926ARC00000. Following are the details "
        "of the transaction: Amount and Currency (F32A) : GBP 6285.01 Value Date (F32A) : "
        "02-SEP-26 Ordering Customer - Account and Name and Address (F50F) : 1/Stockopedia Ltd "
        "3/GB/OXFORD Beneficiary Customer - Account and name and address (F59): 000000000 "
        "Hikmah Technologies",
    ),
    "gpay_bill": (
        "Google Pay <google-pay-noreply@google.com>",
        "New bill from Mahavitaran - Maharashtra Electricity (MSEDCL). Pay now on Google Pay",
        "New bill from Mahavitaran - Maharashtra Electricity (MSEDCL)\nBill Amount:\nRs. 7170.00\n"
        "Bill Category:\nElectricity\nAccount Name:\nSuncity 501\nDue Date:\nSep 15, 2026\nPay Now",
    ),
    "stripe_receipt": (
        '"Eleven Labs Inc." <invoice+statements+acct_1M0000@stripe.com>',
        "Your receipt from Eleven Labs Inc. #2537-8261-6429",
        "Eleven Labs Inc.\nReceipt from Eleven Labs Inc. ₹1,936.00 Paid August 25, 2026 "
        "Download invoice Download receipt Receipt number 2537-8261-6429 Invoice number "
        "WHFZ3G4O-0008 Payment method - 9876\nReceipt #2537-8261-6429 Jul 24–Aug 24, 2026 "
        "Creator (per subscription) Qty 1 ₹1,936.00 Total ₹1,936.00 Amount paid ₹1,936.00",
    ),
    "apple_receipt": (
        "Apple <no_reply@email.apple.com>",
        "Your receipt from Apple.",
        "Tax Invoice 3 September 2026 Sequence: 3-000 Order ID: MN0000 Document: 000 Apple "
        "Account: someone@example.com Apple Music Individual (Monthly) SAC: 998432 Renews 4 "
        "October 2026 ₹119.00 Billing and Payment Someone Subtotal ₹100.85 IGST charged at 18% "
        "₹18.15 someone-1@okaxis ₹119.00 You can turn off renewal receipts",
    ),
    "airtel_bill": (
        "ebill@airtel.com",
        "Bill for your Airtel Xstream Fiber  02200000000_wifi - Aug'26 is generated",
        "airtel Airtel number 9100000000 This Month's Charges (Rs.) 5306.46 Relationship number "
        "2004XXXXX Due amount* (Rs.) 5306.46 Bill period 17-Jul-2026 to 16-Aug-2026 Due date* "
        "06-Sep-2026 View bill Pay bill",
    ),
    "airtel_receipt": (
        "update@airtel.com",
        "Here's your Airtel payment receipt!",
        "Payment Reciept Dear SOMEONE . Thank you for choosing Airtel. We have received a payment "
        "of Rs 5306.46 for your Bill Payment. Please find the payment receipt attached.",
    ),
}


def _ev(name):
    sender, subject, body = FIX[name]
    ev = bp.parse_any(sender, subject, body)
    assert ev is not None, name
    assert ev.parser == name
    assert ev.payee_key, name
    return ev


def test_hdfc_upi_debit():
    ev = _ev("hdfc_upi_debit")
    assert (ev.kind, ev.direction, ev.channel) == ("transaction", "out", "upi")
    assert ev.amount == Decimal("245.50") and ev.currency == "INR"
    assert ev.payee == "Corner Store" and ev.instrument == "hdfc-4321"
    assert ev.occurred_on == date(2026, 9, 2) and ev.ref == "100000000001"
    assert ev.source_class == "bank" and ev.entity == "personal"


def test_hdfc_upi_credit():
    ev = _ev("hdfc_upi_credit")
    assert ev.direction == "in" and ev.amount == Decimal("1500.00")
    assert ev.payee == "A Friend" and ev.occurred_on == date(2026, 9, 3)


def test_hdfc_imps():
    ev = _ev("hdfc_imps")
    assert (ev.kind, ev.direction, ev.channel) == ("transaction", "out", "imps")
    assert ev.amount == Decimal("10000.00") and ev.instrument == "hdfc-4321"
    assert ev.payee == "a/c ••8765" and ev.account == "equity:transfers"
    assert ev.occurred_on == date(2026, 8, 21) and ev.ref == "600000000003"


def test_nkgsb_credit():
    ev = _ev("nkgsb_credit")
    assert ev.direction == "in" and ev.amount == Decimal("150.00")
    assert ev.instrument == "nkgsb-999" and ev.payee == "someone-1@okicici"
    assert ev.ref == "660000000004" and ev.occurred_on == date(2026, 8, 31)


def test_axis_card_spend():
    ev = _ev("axis_card_spend")
    assert (ev.channel, ev.direction) == ("card", "out")
    assert ev.amount == Decimal("5.89") and ev.currency == "USD"
    assert ev.payee == "1PASSWORD" and ev.instrument == "axis-cc-9876"
    assert ev.occurred_on == date(2026, 8, 18)


def test_axis_autopay_done():
    ev = _ev("axis_autopay_done")
    assert (ev.kind, ev.channel, ev.direction) == ("transaction", "autopay", "out")
    assert ev.amount == Decimal("4.00") and ev.currency == "USD"
    assert ev.payee == "GitHubInc" and ev.instrument == "axis-cc-9876"
    assert ev.occurred_on == date(2026, 8, 21) and ev.is_recurring is True


def test_axis_autopay_reminder_is_a_due():
    ev = _ev("axis_autopay_reminder")
    assert ev.kind == "due" and ev.channel == "autopay"
    assert ev.amount == Decimal("262.30") and ev.currency == "INR"
    assert ev.payee == "AMAZON WEB SERVICES" and ev.due_on == date(2026, 9, 4)
    assert ev.instrument == "axis-cc-5555"


def test_axis_cc_statement_is_a_due():
    ev = _ev("axis_cc_statement")
    assert ev.kind == "due" and ev.channel == "statement"
    assert ev.amount == Decimal("100308.53") and ev.currency == "INR"
    assert ev.due_on == date(2026, 9, 7)
    assert ev.payee == "Axis credit card XX76" and ev.instrument == "axis-cc-76"


def test_axis_remittance():
    ev = _ev("axis_remittance")
    assert (ev.kind, ev.direction, ev.channel) == ("transaction", "in", "remittance")
    assert ev.amount == Decimal("6285.01") and ev.currency == "GBP"
    assert ev.payee == "Stockopedia Ltd" and ev.entity == "hikmah"
    assert ev.account == "income:hikmah:stockopedia" and ev.instrument == "axis-9640"
    assert ev.occurred_on == date(2026, 9, 2) and ev.ref == "GBC00000000000"


def test_gpay_bill_is_a_due():
    ev = _ev("gpay_bill")
    assert ev.kind == "due" and ev.channel == "bill"
    assert ev.amount == Decimal("7170.00") and ev.currency == "INR"
    assert ev.payee == "Mahavitaran - Maharashtra Electricity (MSEDCL) Suncity 501"
    assert ev.due_on == date(2026, 9, 15)


def test_stripe_receipt():
    ev = _ev("stripe_receipt")
    assert (ev.kind, ev.channel, ev.direction) == ("transaction", "receipt", "out")
    assert ev.amount == Decimal("1936.00") and ev.currency == "INR"
    assert ev.payee == "Eleven Labs Inc." and ev.instrument == "card-9876"
    assert ev.occurred_on == date(2026, 8, 25) and ev.source_class == "receipt"


def test_apple_receipt():
    ev = _ev("apple_receipt")
    assert ev.kind == "transaction" and ev.channel == "receipt"
    assert ev.amount == Decimal("119.00") and ev.currency == "INR"
    assert ev.payee == "Apple Music Individual" and ev.is_recurring is True
    assert ev.occurred_on == date(2026, 9, 3)


def test_airtel_bill_is_a_due():
    ev = _ev("airtel_bill")
    assert ev.kind == "due" and ev.amount == Decimal("5306.46")
    assert ev.due_on == date(2026, 9, 6) and ev.payee == "Airtel Xstream Fiber"


def test_airtel_receipt():
    ev = _ev("airtel_receipt")
    assert ev.kind == "transaction" and ev.channel == "receipt"
    assert ev.amount == Decimal("5306.46") and ev.payee == "Airtel"


@pytest.mark.parametrize(
    "sender,subject,body",
    [
        (HDFC, "⚠️ Non-maintenance charges may apply on A/c XX0236", "Keep AMB of Rs.5000 to avoid charges."),
        (AXIS, "Reminder to update your KYC information", "Dear Customer, please update your KYC."),
        (AXIS, "Pay your GST with Axis Bank on the go!", "Pay GST of Rs 10,000 easily."),
        ("Axis Bank Cards <info@digital.axisbankmail.bank.in>", "Manage your expenses smartly with FLEXI EMI!", "Convert Rs 50,000 to EMI"),
        ("Apple <no_reply@email.apple.com>", "Your Apple ID was used to sign in", "A new sign in."),
    ],
)
def test_marketing_and_notices_are_not_parsed(sender, subject, body):
    assert bp.parse_any(sender, subject, body) is None


def test_amount_and_currency_helpers():
    assert bp.amount_from("1,00,308.53") == Decimal("100308.53")
    assert bp.amount_from("245.5") == Decimal("245.50")
    assert bp.currency_from("Rs.") == "INR" and bp.currency_from("₹") == "INR"
    assert bp.currency_from("USD") == "USD" and bp.currency_from("$") == "USD"
    assert bp.currency_from("GBP") == "GBP" and bp.currency_from("£") == "GBP"
    assert bp.currency_from("€") == "EUR" and bp.currency_from("xyz") is None
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=core/src:worker/src:comms/src .venv/bin/python -m pytest tests/core/test_bank_parsers.py -q 2>&1 | tail -3`
Expected: `ModuleNotFoundError: aegis.services.bank_parsers`.

- [ ] **Step 3: Implement `core/src/aegis/services/bank_parsers.py`**

```python
"""Deterministic parsers for the bank and vendor mail formats we see (spec §3).

Each parser takes (sender, subject, body) and returns a MoneyEvent or None.
Anchors are the literal phrases the real emails use; a parser that matches
its anchors but cannot read an amount returns None so the LLM gets a try.
`parse_any` tries them in PARSERS order.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from aegis.api.models.money import MoneyEvent, payee_key

_CENT = Decimal("0.01")
_CURRENCY = {
    "rs": "INR", "rs.": "INR", "inr": "INR", "₹": "INR", "rupees": "INR",
    "usd": "USD", "$": "USD", "gbp": "GBP", "£": "GBP", "eur": "EUR", "€": "EUR",
}
_AMT = r"([\d,]+(?:\.\d{1,2})?)"


def amount_from(text: str) -> Decimal:
    return Decimal(text.replace(",", "")).quantize(_CENT)


def currency_from(token: str) -> str | None:
    return _CURRENCY.get((token or "").strip().lower())


def _date_dmy2(s: str) -> date:  # 02-09-26
    return datetime.strptime(s, "%d-%m-%y").date()


def _date_dmy4(s: str) -> date:  # 18-08-2026 or 07/09/2026
    return datetime.strptime(s.replace("/", "-"), "%d-%m-%Y").date()


def _date_dmon2(s: str) -> date:  # 02-SEP-26
    return datetime.strptime(s.title(), "%d-%b-%y").date()


def _date_dmon4(s: str) -> date:  # 06-Sep-2026
    return datetime.strptime(s.title(), "%d-%b-%Y").date()


def _date_mdy_text(s: str) -> date:  # Sep 15, 2026 / August 25, 2026
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    raise ValueError(s)


def _date_dmy_text(s: str) -> date:  # 3 September 2026
    return datetime.strptime(s.strip(), "%d %B %Y").date()


def _from(sender: str, needle: str) -> bool:
    return needle.lower() in (sender or "").lower()


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _event(name: str, **kw) -> MoneyEvent:
    ev = MoneyEvent(parser=name, **kw)
    ev.payee_key = payee_key(ev.payee)
    return ev


# ---------------------------------------------------------------- banks

_HDFC_UPI = re.compile(
    rf"Rs\.?\s*{_AMT} is (debited from|credited to) your account ending (\d{{4}})\s+"
    r"(?:towards|from) VPA (\S+?)(?: \(([^)]*)\))? on (\d{2}-\d{2}-\d{2})\."
    r".*?UPI transaction reference no\.?:\s*(\d+)",
    re.S,
)


def parse_hdfc_upi(sender: str, subject: str, body: str) -> MoneyEvent | None:
    if not _from(sender, "alerts@hdfcbank.bank.in"):
        return None
    m = _HDFC_UPI.search(_clean(body))
    if not m:
        return None
    amt, verb, tail, vpa, name, d, ref = m.groups()
    return _event(
        "hdfc_upi",
        kind="transaction",
        direction="out" if verb.startswith("debited") else "in",
        amount=amount_from(amt),
        currency="INR",
        payee=_clean(name) or vpa,
        channel="upi",
        instrument=f"hdfc-{tail}",
        occurred_on=_date_dmy2(d),
        ref=ref,
        source_class="bank",
    )


_HDFC_IMPS = re.compile(
    rf"INR\s*{_AMT} has been debited from your account ending x*(\d{{4}}) on (\d{{2}}-\d{{2}}-\d{{2}})"
    r" and credited to the account ending x*(\d{4}) via IMPS\. IMPS Reference No:\s*(\d+)"
)


def parse_hdfc_imps(sender: str, subject: str, body: str) -> MoneyEvent | None:
    if not _from(sender, "alerts@hdfcbank.bank.in"):
        return None
    m = _HDFC_IMPS.search(_clean(body))
    if not m:
        return None
    amt, tail, d, to_tail, ref = m.groups()
    return _event(
        "hdfc_imps",
        kind="transaction",
        direction="out",
        amount=amount_from(amt),
        currency="INR",
        payee=f"a/c ••{to_tail}",
        channel="imps",
        instrument=f"hdfc-{tail}",
        occurred_on=_date_dmy2(d),
        ref=ref,
        account="equity:transfers",
        source_class="bank",
    )


_NKGSB = re.compile(
    rf"(Received|Paid|Debited)\s+Rs\.?\s*{_AMT} (?:in|from) NKGSB Bank A/C X(\d+) on (\d{{2}}-\d{{2}}-\d{{2}})"
    r"\s+UPI/(?:CREDIT|DEBIT)/(\d+)/([^/\s]+)/"
)


def parse_nkgsb(sender: str, subject: str, body: str) -> MoneyEvent | None:
    if not _from(sender, "alerts@nkgsb-bank.com"):
        return None
    m = _NKGSB.search(_clean(body))
    if not m:
        return None
    verb, amt, tail, d, ref, vpa = m.groups()
    return _event(
        "nkgsb",
        kind="transaction",
        direction="in" if verb == "Received" else "out",
        amount=amount_from(amt),
        currency="INR",
        payee=vpa,
        channel="upi",
        instrument=f"nkgsb-{tail}",
        occurred_on=_date_dmy2(d),
        ref=ref,
        source_class="bank",
    )


_AXIS_SPEND_SUBJ = re.compile(rf"^(\w+) {_AMT} spent on credit card no\. XX(\d+)", re.I)
_AXIS_MERCHANT = re.compile(r"Merchant Name:\s*(.+?)\s+(?:Axis Bank|AutoPay ID|To be debited)")
_AXIS_DATETIME = re.compile(r"Date & Time:\s*(\d{2}-\d{2}-\d{4})")


def parse_axis_card_spend(sender: str, subject: str, body: str) -> MoneyEvent | None:
    if not _from(sender, "alerts@axis.bank.in"):
        return None
    m = _AXIS_SPEND_SUBJ.search(_clean(subject))
    if not m:
        return None
    cur, amt, tail = m.groups()
    text = _clean(body)
    merchant = _AXIS_MERCHANT.search(text)
    when = _AXIS_DATETIME.search(text)
    if not merchant or not when:
        return None
    return _event(
        "axis_card_spend",
        kind="transaction",
        direction="out",
        amount=amount_from(amt),
        currency=currency_from(cur) or cur.upper(),
        payee=merchant.group(1).strip(),
        channel="card",
        instrument=f"axis-cc-{tail}",
        occurred_on=_date_dmy4(when.group(1)),
        source_class="bank",
    )


_AXIS_AMOUNT = re.compile(rf"Transaction Amount:\s*(\w+)\s*{_AMT}")
_AXIS_CARD = re.compile(r"[Cc]ard No\.?\s*XX(\d+)")
_AXIS_DATE_HEAD = re.compile(r"^(\d{2}-\d{2}-\d{4})")
_AXIS_DEBIT_BY = re.compile(r"To be debited by:\s*(\d{2}-\d{2}-\d{4})")


def parse_axis_autopay_done(sender: str, subject: str, body: str) -> MoneyEvent | None:
    text = _clean(body)
    if not _from(sender, "alerts@axis.bank.in") or "successful AutoPay transaction" not in text:
        return None
    amt = _AXIS_AMOUNT.search(text)
    merchant = _AXIS_MERCHANT.search(text)
    card = _AXIS_CARD.search(text)
    head = _AXIS_DATE_HEAD.search(text)
    if not (amt and merchant and card and head):
        return None
    return _event(
        "axis_autopay_done",
        kind="transaction",
        direction="out",
        amount=amount_from(amt.group(2)),
        currency=currency_from(amt.group(1)) or amt.group(1).upper(),
        payee=merchant.group(1).strip(),
        channel="autopay",
        instrument=f"axis-cc-{card.group(1)}",
        occurred_on=_date_dmy4(head.group(1)),
        is_recurring=True,
        source_class="bank",
    )


def parse_axis_autopay_reminder(sender: str, subject: str, body: str) -> MoneyEvent | None:
    if not _from(sender, "alerts@axis.bank.in") or "Upcoming AutoPay" not in subject:
        return None
    text = _clean(body)
    amt = _AXIS_AMOUNT.search(text)
    merchant = _AXIS_MERCHANT.search(text)
    due = _AXIS_DEBIT_BY.search(text)
    card = _AXIS_CARD.search(text)
    if not (amt and merchant and due):
        return None
    return _event(
        "axis_autopay_reminder",
        kind="due",
        direction="out",
        amount=amount_from(amt.group(2)),
        currency=currency_from(amt.group(1)) or amt.group(1).upper(),
        payee=merchant.group(1).strip(),
        channel="autopay",
        instrument=f"axis-cc-{card.group(1)}" if card else None,
        due_on=_date_dmy4(due.group(1)),
        is_recurring=True,
        source_class="bank",
    )


_AXIS_STMT = re.compile(
    rf"Total Amount Due.*?Payment Due Date.*?{_AMT}\s*Dr\s*{_AMT}\s*Dr\s*(\d{{2}}/\d{{2}}/\d{{4}})"
)
_AXIS_STMT_TAIL = re.compile(r"ending XX(\d+)")


def parse_axis_cc_statement(sender: str, subject: str, body: str) -> MoneyEvent | None:
    if not _from(sender, "cc.statements@axis.bank.in"):
        return None
    m = _AXIS_STMT.search(_clean(body))
    tail = _AXIS_STMT_TAIL.search(subject)
    if not m:
        return None
    total, _minimum, due = m.groups()
    t = tail.group(1) if tail else "????"
    return _event(
        "axis_cc_statement",
        kind="due",
        direction="out",
        amount=amount_from(total),
        currency="INR",
        payee=f"Axis credit card XX{t}",
        channel="statement",
        instrument=f"axis-cc-{t}",
        due_on=_date_dmy4(due),
        source_class="bank",
    )


_REMIT_AMT = re.compile(r"received foreign currency funds of (\w{3}) " + _AMT)
_REMIT_DATE = re.compile(r"Value Date \(F32A\)\s*:\s*(\d{2}-[A-Za-z]{3}-\d{2})")
_REMIT_ORDER = re.compile(r"\(F50F\)\s*:\s*1/([^/]+?)\s+(?:3/|2/|\(F59\))")
_REMIT_SWIFT = re.compile(r"SWIFT Reference Number:\s*([A-Z0-9]+)")


def parse_axis_remittance(sender: str, subject: str, body: str) -> MoneyEvent | None:
    if not _from(sender, "eforexservices@axis.bank.in") or "Inward Remittance Notification" not in subject:
        return None
    text = _clean(body)
    amt = _REMIT_AMT.search(text)
    when = _REMIT_DATE.search(text)
    who = _REMIT_ORDER.search(text)
    swift = _REMIT_SWIFT.search(text)
    if not (amt and when):
        return None
    payee = who.group(1).strip() if who else "Inward remittance"
    account = "income:hikmah:stockopedia" if "stockopedia" in payee.lower() else "income:hikmah:other"
    return _event(
        "axis_remittance",
        kind="transaction",
        direction="in",
        amount=amount_from(amt.group(2)),
        currency=amt.group(1).upper(),
        payee=payee,
        channel="remittance",
        instrument="axis-9640",
        occurred_on=_date_dmon2(when.group(1)),
        ref=swift.group(1) if swift else None,
        entity="hikmah",
        account=account,
        source_class="bank",
    )


# ---------------------------------------------------------------- bills & receipts

_GPAY_SUBJ = re.compile(r"^New bill from (.+?)\. Pay now", re.I)
_GPAY_AMT = re.compile(rf"Bill Amount:\s*Rs\.?\s*{_AMT}")
_GPAY_ACCT = re.compile(r"Account Name:\s*(.+?)\s*(?:Due Date:|$)")
_GPAY_DUE = re.compile(r"Due Date:\s*([A-Za-z]{3,9} \d{1,2}, \d{4})")


def parse_gpay_bill(sender: str, subject: str, body: str) -> MoneyEvent | None:
    if not _from(sender, "google-pay-noreply@google.com"):
        return None
    s = _GPAY_SUBJ.search(_clean(subject))
    text = _clean(body)
    amt = _GPAY_AMT.search(text)
    due = _GPAY_DUE.search(text)
    if not (s and amt and due):
        return None
    acct = _GPAY_ACCT.search(text)
    biller = s.group(1).strip()
    payee = f"{biller} {acct.group(1).strip()}" if acct else biller
    return _event(
        "gpay_bill",
        kind="due",
        direction="out",
        amount=amount_from(amt.group(1)),
        currency="INR",
        payee=payee,
        channel="bill",
        due_on=_date_mdy_text(due.group(1)),
        is_recurring=True,
        source_class="receipt",
    )


_STRIPE_HEAD = re.compile(rf"Receipt from (.+?) ([₹$£€]|Rs\.?|USD|INR|GBP|EUR)\s?{_AMT} Paid ([A-Za-z]+ \d{{1,2}}, \d{{4}})")
_STRIPE_PM = re.compile(r"Payment method - (\d{4})")


def parse_stripe_receipt(sender: str, subject: str, body: str) -> MoneyEvent | None:
    if "invoice+statements" not in (sender or "").lower():
        return None
    text = _clean(body)
    m = _STRIPE_HEAD.search(text)
    if not m:
        return None
    vendor, cur, amt, paid = m.groups()
    pm = _STRIPE_PM.search(text)
    return _event(
        "stripe_receipt",
        kind="transaction",
        direction="out",
        amount=amount_from(amt),
        currency=currency_from(cur) or "INR",
        payee=vendor.strip(),
        channel="receipt",
        instrument=f"card-{pm.group(1)}" if pm else None,
        occurred_on=_date_mdy_text(paid),
        is_recurring=True,
        source_class="receipt",
    )


_APPLE_DATE = re.compile(r"Tax Invoice (\d{1,2} [A-Za-z]+ \d{4})")
_APPLE_PRODUCT_LINE = re.compile(r"Apple Account: \S+ (.+?) \((Monthly|Yearly|Weekly)\)")
_APPLE_AMT = re.compile(r"₹" + _AMT)


def parse_apple_receipt(sender: str, subject: str, body: str) -> MoneyEvent | None:
    if not _from(sender, "no_reply@email.apple.com") or "receipt from Apple" not in subject:
        return None
    text = _clean(body)
    when = _APPLE_DATE.search(text)
    product = _APPLE_PRODUCT_LINE.search(text)
    amounts = _APPLE_AMT.findall(text)
    if not (when and product and amounts):
        return None
    return _event(
        "apple_receipt",
        kind="transaction",
        direction="out",
        amount=amount_from(amounts[-1]),
        currency="INR",
        payee=f"Apple {product.group(1).strip()}",
        channel="receipt",
        occurred_on=_date_dmy_text(when.group(1)),
        is_recurring=True,
        category="media",
        source_class="receipt",
    )


_AIRTEL_DUE_AMT = re.compile(rf"Due amount\*? \(Rs\.\)\s*{_AMT}")
_AIRTEL_DUE_DATE = re.compile(r"Due date\*?\s*(\d{2}-[A-Za-z]{3}-\d{4})")


def parse_airtel_bill(sender: str, subject: str, body: str) -> MoneyEvent | None:
    if not _from(sender, "ebill@airtel.com"):
        return None
    text = _clean(body)
    amt = _AIRTEL_DUE_AMT.search(text)
    due = _AIRTEL_DUE_DATE.search(text)
    if not (amt and due):
        return None
    return _event(
        "airtel_bill",
        kind="due",
        direction="out",
        amount=amount_from(amt.group(1)),
        currency="INR",
        payee="Airtel Xstream Fiber" if "xstream" in subject.lower() else "Airtel",
        channel="bill",
        due_on=_date_dmon4(due.group(1)),
        is_recurring=True,
        source_class="receipt",
    )


_AIRTEL_PAID = re.compile(rf"received a payment of Rs\.?\s*{_AMT} for your Bill Payment")


def parse_airtel_receipt(sender: str, subject: str, body: str) -> MoneyEvent | None:
    if not _from(sender, "update@airtel.com"):
        return None
    m = _AIRTEL_PAID.search(_clean(body))
    if not m:
        return None
    return _event(
        "airtel_receipt",
        kind="transaction",
        direction="out",
        amount=amount_from(m.group(1)),
        currency="INR",
        payee="Airtel",
        channel="receipt",
        is_recurring=True,
        source_class="receipt",
    )


Parser = Callable[[str, str, str], MoneyEvent | None]

PARSERS: tuple[Parser, ...] = (
    parse_hdfc_upi,
    parse_hdfc_imps,
    parse_nkgsb,
    parse_axis_card_spend,
    parse_axis_autopay_done,
    parse_axis_autopay_reminder,
    parse_axis_cc_statement,
    parse_axis_remittance,
    parse_gpay_bill,
    parse_stripe_receipt,
    parse_apple_receipt,
    parse_airtel_bill,
    parse_airtel_receipt,
)


def parse_any(sender: str, subject: str, body: str) -> MoneyEvent | None:
    for parser in PARSERS:
        try:
            ev = parser(sender or "", subject or "", body or "")
        except (ValueError, InvalidOperation):
            ev = None
        if ev is not None:
            return ev
    return None
```

Regex notes for the implementer: the Apple product regex must yield `Apple Music Individual` from `Apple Account: someone@example.com Apple Music Individual (Monthly)`; the gpay account regex must stop before `Due Date:`; the remittance ordering-customer regex must stop before ` 3/`. Adjust the patterns until every fixture test passes — the fixtures are the contract, the patterns are yours.

- [ ] **Step 4: Run the tests until green, lint**

Run: `PYTHONPATH=core/src:worker/src:comms/src .venv/bin/python -m pytest tests/core/test_bank_parsers.py -q 2>&1 | tail -5` — all passed.
Falsifiability: change `_HDFC_UPI`'s `(\d{{4}})` to `(\d{{3}})`, rerun `test_hdfc_upi_debit`, confirm it FAILS, revert.
Run: `.venv/bin/ruff check core/src/ tests/core/` — clean.

- [ ] **Step 5: Commit**

```bash
git add core/src/aegis/services/bank_parsers.py tests/core/test_bank_parsers.py
git commit -m "feat(money): deterministic parsers for bank alerts, bills and vendor receipts"
```

### Task 4: The journal writer — blocks, rules, git sync, hledger runner

**Files:**
- Modify: `core/src/aegis/services/books.py` (append the writer to the helpers from Task 2)
- Test: `tests/core/test_books.py`

**Interfaces (all in `aegis.services.books`):**
- `class BooksError(Exception)`, `class BooksDisabled(BooksError)`, `class BooksCheckError(BooksError)`
- `@dataclass(frozen=True) class BooksConfig: path: Path; repo_url: str = ""; deploy_key: Path | None = None; main: str = "main.journal"`
- `config_from_settings(settings) -> BooksConfig`; `install_deploy_key(settings) -> Path | None`
- `sanitize_payee(payee: str) -> str`; `render_transaction(event: MoneyEvent, counter_account: str, instrument_acct: str, msgid: str) -> str`; `iter_blocks(text) -> list[tuple[int, int]]`; `find_block(text, msgid) -> tuple[int, int] | None`; `append_block(text, block) -> str`; `rewrite_block(text, msgid, *, payee=None, account=None, instrument_account=None, add_tags: dict[str, str] | None = None) -> str`; `journal_rel(entity: str, d: date) -> str`; `journal_files(cfg) -> list[Path]`
- `load_rules(path: Path) -> list[dict]`; `apply_rules(rules, sender, payee) -> dict | None`
- async: `post_event(event, msgid, cfg) -> str`, `rewrite_event(msgid, cfg, *, payee=None, account=None, instrument_account=None, add_tags=None) -> str`, `remove_event(msgid, cfg) -> None`, `append_prices(lines: list[str], cfg) -> None`, `append_rule(rule: dict, cfg) -> None`, `write_report(rel_path: str, text: str, cfg) -> None`, `run_hledger(args: list[str], cfg, *, output_format: str = "text") -> str`, `unpushed_commits(cfg) -> int`
- Every write: flock + asyncio lock → pull → mutate → `hledger check --strict` (revert on failure, raise `BooksCheckError`) → commit as `Maou <maou@aegis.local>` → push (logged, never raised). Idempotent: `post_event` for a `msgid` already in the file is a no-op that returns the file path.

- [ ] **Step 1: Write the failing tests**

```python
# tests/core/test_books.py
"""The journal writer (spec §5). Pure block tests always run; the round-trip
tests need hledger + git and skip without them."""

from __future__ import annotations

import base64
import shutil
import subprocess
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from aegis.api.models.money import MoneyEvent
from aegis.services import books

HAS_HLEDGER = shutil.which("hledger") is not None and shutil.which("git") is not None

EV_OUT = MoneyEvent(
    kind="transaction", direction="out", amount=Decimal("10"), currency="INR",
    payee="Jai shree nakoda", channel="upi", instrument="hdfc-1225", ref="128932002048",
    occurred_on=date(2026, 9, 2), entity="personal", source_class="bank",
)
EV_IN = MoneyEvent(
    kind="transaction", direction="in", amount=Decimal("6285.01"), currency="GBP",
    payee="Stockopedia Ltd", channel="remittance", instrument="axis-9640", ref="GBC02096KFMGNXTS",
    occurred_on=date(2026, 9, 2), entity="hikmah", account="income:hikmah:stockopedia",
    source_class="bank",
)

BLOCK_OUT = (
    "2026-09-02 * Jai shree nakoda\n"
    "    ; msgid: arshad-personal/1a06cf5a\n"
    "    ; channel: upi, ref: 128932002048, instrument: hdfc-1225\n"
    "    expenses:unknown                        ₹10.00\n"
    "    assets:bank:hdfc:1225\n"
)
BLOCK_IN = (
    "2026-09-02 * Stockopedia Ltd\n"
    "    ; msgid: arshad-personal/1a0659e3\n"
    "    ; channel: remittance, ref: GBC02096KFMGNXTS, instrument: axis-9640\n"
    "    income:hikmah:stockopedia               -£6285.01\n"
    "    assets:bank:axis:9640\n"
)
HEADER = "; Personal transactions, 2026.\n"


def test_render_transaction_exact_text():
    assert books.render_transaction(EV_OUT, "expenses:unknown", "assets:bank:hdfc:1225",
                                    "arshad-personal/1a06cf5a") == BLOCK_OUT
    assert books.render_transaction(EV_IN, "income:hikmah:stockopedia", "assets:bank:axis:9640",
                                    "arshad-personal/1a0659e3") == BLOCK_IN


def test_render_transaction_pads_long_accounts_with_two_spaces():
    ev = EV_OUT.model_copy(update={"payee": "x"})
    block = books.render_transaction(ev, "expenses:hikmah:professional:something:long", "assets:unknown", "m/1")
    assert "    expenses:hikmah:professional:something:long  ₹10.00\n" in block


def test_sanitize_payee_strips_journal_syntax():
    assert books.sanitize_payee("A ; B | C\nD") == "A B C D"
    assert books.sanitize_payee("") == "unknown"
    assert len(books.sanitize_payee("x" * 200)) == 80


def test_append_and_find_block():
    text = books.append_block(HEADER, BLOCK_OUT)
    text = books.append_block(text, BLOCK_IN)
    assert text == HEADER + "\n" + BLOCK_OUT + "\n" + BLOCK_IN
    assert books.find_block(text, "arshad-personal/1a06cf5a") == (
        len(HEADER) + 1, len(HEADER) + 1 + len(BLOCK_OUT))
    assert books.find_block(text, "arshad-personal/1a0659e3") == (len(text) - len(BLOCK_IN), len(text))
    assert books.find_block(text, "nope") is None
    assert books.find_block("", "x") is None


def test_rewrite_block_payee_account_instrument_and_tags():
    text = books.append_block(HEADER, BLOCK_OUT)
    out = books.rewrite_block(
        text, "arshad-personal/1a06cf5a", payee="Corner Store", account="expenses:groceries",
        instrument_account="assets:bank:hdfc:0236", add_tags={"receipt": "arshad-personal/zz"},
    )
    assert out == HEADER + "\n" + (
        "2026-09-02 * Corner Store\n"
        "    ; msgid: arshad-personal/1a06cf5a\n"
        "    ; channel: upi, ref: 128932002048, instrument: hdfc-1225, receipt: arshad-personal/zz\n"
        "    expenses:groceries                      ₹10.00\n"
        "    assets:bank:hdfc:0236\n"
    )


def test_rewrite_block_unknown_msgid_raises():
    with pytest.raises(books.BooksError):
        books.rewrite_block(BLOCK_OUT, "missing", payee="x")


def test_journal_rel():
    assert books.journal_rel("personal", date(2026, 9, 2)) == "personal/2026.journal"
    assert books.journal_rel("hikmah", date(2027, 1, 1)) == "hikmah/2027.journal"


def test_rules_first_match_wins(tmp_path):
    p = tmp_path / "accounts.yaml"
    p.write_text(
        "- match: 'lseg billing'\n  ignore: true\n"
        "- match: 'amazon web services|invoicing@aws\\.com'\n  entity: hikmah\n"
        "  account: expenses:hikmah:infra\n  payee: Amazon Web Services\n"
        "- match: 'amazon'\n  account: expenses:shopping\n"
    )
    rules = books.load_rules(p)
    assert books.apply_rules(rules, "invoicing@aws.com", "AMAZON WEB SERVICES")["payee"] == "Amazon Web Services"
    assert books.apply_rules(rules, "x@amazon.in", "Amazon")["account"] == "expenses:shopping"
    assert books.apply_rules(rules, "data@stockopedia.com", "LSEG Billing")["ignore"] is True
    assert books.apply_rules(rules, "a@b.com", "Nobody") is None
    assert books.load_rules(tmp_path / "missing.yaml") == []


def test_install_deploy_key_raw_and_base64(tmp_path):
    pem = "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----\n"
    s = SimpleNamespace(books_deploy_key=pem, gmail_token_dir=str(tmp_path / "creds"))
    path = books.install_deploy_key(s)
    assert path == tmp_path / "creds" / "books_deploy_key"
    assert path.read_text() == pem and (path.stat().st_mode & 0o777) == 0o600
    s2 = SimpleNamespace(books_deploy_key=base64.b64encode(pem.encode()).decode(), gmail_token_dir=str(tmp_path / "c2"))
    assert books.install_deploy_key(s2).read_text() == pem
    assert books.install_deploy_key(SimpleNamespace(books_deploy_key="", gmail_token_dir=str(tmp_path))) is None


@pytest.mark.asyncio
async def test_run_hledger_refuses_writes_and_file_overrides(tmp_path):
    cfg = books.BooksConfig(path=tmp_path)
    for args in (["import", "x.csv"], ["add"], ["bal", "-f", "/etc/passwd"], ["reg", "--output-file=x"], ["print", "--rules", "r"]):
        with pytest.raises(books.BooksError):
            await books.run_hledger(args, cfg)


# ------------------------------------------------------------- hledger round trip

ACCOUNTS = """commodity ₹ 1,00,000.00
commodity £ 1000.00
account assets:bank:hdfc:1225
account assets:bank:hdfc:0236
account assets:bank:axis:9640
account assets:unknown
account expenses:unknown
account expenses:groceries
account expenses:hikmah:unknown
account income:unknown
account income:hikmah:stockopedia
account income:hikmah:other
account equity:transfers
"""


def _repo(tmp_path: Path) -> books.BooksConfig:
    root = tmp_path / "books"
    root.mkdir()
    (root / "accounts.journal").write_text(ACCOUNTS)
    (root / "prices.journal").write_text("P 2026-09-01 £ ₹106.20\n")
    (root / "recurring.journal").write_text("")
    (root / "personal").mkdir()
    (root / "hikmah").mkdir()
    (root / "personal" / "2026.journal").write_text("; Personal 2026\n")
    (root / "hikmah" / "2026.journal").write_text("; Hikmah 2026\n")
    (root / "main.journal").write_text(
        "include accounts.journal\ninclude prices.journal\n"
        "include personal/2026.journal\ninclude hikmah/2026.journal\ninclude recurring.journal\n"
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"], cwd=root, check=True)
    return books.BooksConfig(path=root)


def _commits(cfg) -> int:
    return int(subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=cfg.path, capture_output=True, text=True).stdout)


@pytest.mark.skipif(not HAS_HLEDGER, reason="hledger/git not installed")
@pytest.mark.asyncio
async def test_post_rewrite_and_query_round_trip(tmp_path):
    cfg = _repo(tmp_path)
    rel = await books.post_event(EV_OUT, "arshad-personal/1a06cf5a", cfg)
    assert rel == "personal/2026.journal"
    assert (cfg.path / rel).read_text() == "; Personal 2026\n\n" + BLOCK_OUT
    rel2 = await books.post_event(EV_IN, "arshad-personal/1a0659e3", cfg)
    assert rel2 == "hikmah/2026.journal"
    assert _commits(cfg) == 3

    # idempotent: same msgid → no change, no commit
    assert await books.post_event(EV_OUT, "arshad-personal/1a06cf5a", cfg) == rel
    assert _commits(cfg) == 3

    out = await books.run_hledger(["print", "tag:msgid=arshad-personal/1a06cf5a"], cfg)
    assert "Jai shree nakoda" in out and "expenses:unknown" in out

    await books.rewrite_event("arshad-personal/1a06cf5a", cfg, account="expenses:groceries", payee="Corner Store")
    out = await books.run_hledger(["print", "tag:msgid=arshad-personal/1a06cf5a"], cfg)
    assert "Corner Store" in out and "expenses:groceries" in out
    assert _commits(cfg) == 4

    bal = await books.run_hledger(["bal", "-X", "₹", "income", "expenses", "--depth", "1"], cfg)
    assert "₹" in bal
    assert await books.unpushed_commits(cfg) == 0  # no upstream → 0


@pytest.mark.skipif(not HAS_HLEDGER, reason="hledger/git not installed")
@pytest.mark.asyncio
async def test_undeclared_account_is_reverted(tmp_path):
    cfg = _repo(tmp_path)
    await books.post_event(EV_OUT, "arshad-personal/1a06cf5a", cfg)
    before = (cfg.path / "personal" / "2026.journal").read_text()
    with pytest.raises(books.BooksCheckError):
        await books.rewrite_event("arshad-personal/1a06cf5a", cfg, account="expenses:nope")
    assert (cfg.path / "personal" / "2026.journal").read_text() == before
    assert _commits(cfg) == 2


@pytest.mark.skipif(not HAS_HLEDGER, reason="hledger/git not installed")
@pytest.mark.asyncio
async def test_post_event_uses_unknown_for_undeclared_rule_account_and_instrument(tmp_path):
    cfg = _repo(tmp_path)
    ev = EV_OUT.model_copy(update={"account": "expenses:not:declared", "instrument": "sbi-1111"})
    await books.post_event(ev, "m/undeclared", cfg)
    text = (cfg.path / "personal" / "2026.journal").read_text()
    assert "    expenses:unknown                        ₹10.00\n    assets:unknown\n" in text


@pytest.mark.skipif(not HAS_HLEDGER, reason="hledger/git not installed")
@pytest.mark.asyncio
async def test_new_year_file_is_created_and_included(tmp_path):
    cfg = _repo(tmp_path)
    ev = EV_OUT.model_copy(update={"occurred_on": date(2027, 1, 3)})
    assert await books.post_event(ev, "m/2027", cfg) == "personal/2027.journal"
    main = (cfg.path / "main.journal").read_text()
    assert main.index("include personal/2027.journal") < main.index("include recurring.journal")
    assert (cfg.path / "personal" / "2027.journal").read_text().startswith("; Personal transactions, 2027.")


@pytest.mark.skipif(not HAS_HLEDGER, reason="hledger/git not installed")
@pytest.mark.asyncio
async def test_prices_rule_and_report_appends(tmp_path):
    cfg = _repo(tmp_path)
    (cfg.path / "rules").mkdir()
    (cfg.path / "rules" / "accounts.yaml").write_text("- match: 'a'\n  account: expenses:groceries\n")
    await books.append_prices(["P 2026-09-05 £ ₹108.00"], cfg)
    await books.append_rule({"match": "corner store", "account": "expenses:groceries", "payee": "Corner Store"}, cfg)
    await books.write_report("reports/weekly/2026-09-06.md", "# brief\n", cfg)
    assert (cfg.path / "prices.journal").read_text().endswith("P 2026-09-05 £ ₹108.00\n")
    rules = books.load_rules(cfg.path / "rules" / "accounts.yaml")
    assert rules[-1]["match"] == "corner store" and len(rules) == 2
    assert (cfg.path / "reports" / "weekly" / "2026-09-06.md").read_text() == "# brief\n"
    assert _commits(cfg) == 4


def test_missing_checkout_without_repo_url_is_disabled(tmp_path):
    cfg = books.BooksConfig(path=tmp_path / "nowhere")
    with pytest.raises(books.BooksDisabled):
        books.ensure_checkout_sync(cfg)
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=core/src:worker/src:comms/src .venv/bin/python -m pytest tests/core/test_books.py -q 2>&1 | tail -3`
Expected: AttributeError on `books.render_transaction` etc.

- [ ] **Step 3: Append the writer to `books.py`**

Add these imports at the top of `books.py` (keep the existing ones):

```python
import asyncio
import base64
import fcntl
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import structlog
import yaml

from aegis.api.models.money import MoneyEvent

logger = structlog.get_logger()
```

Then append:

```python
# ----------------------------------------------------------------- errors/config

class BooksError(Exception):
    """A books operation failed; the working copy is left clean."""


class BooksDisabled(BooksError):
    """No `books_repo_url` and no checkout: the books are not configured."""


class BooksCheckError(BooksError):
    """`hledger check --strict` rejected a write; it was reverted."""


@dataclass(frozen=True)
class BooksConfig:
    path: Path
    repo_url: str = ""
    deploy_key: Path | None = None
    main: str = "main.journal"


def config_from_settings(settings) -> BooksConfig:
    key = Path(getattr(settings, "gmail_token_dir", "config/")) / "books_deploy_key"
    return BooksConfig(
        path=Path(getattr(settings, "books_path", "/app/config/books")),
        repo_url=getattr(settings, "books_repo_url", "") or "",
        deploy_key=key if key.exists() else None,
    )


def install_deploy_key(settings) -> Path | None:
    """Write `settings.books_deploy_key` (PEM, or base64 of PEM) to
    `<gmail_token_dir>/books_deploy_key` with mode 0600. Never logs the value."""
    raw = (getattr(settings, "books_deploy_key", "") or "").strip()
    if not raw:
        return None
    if "\n" not in raw:
        try:
            raw = base64.b64decode(raw, validate=True).decode("utf-8").strip()
        except Exception as exc:  # noqa: BLE001
            raise BooksError("books_deploy_key is neither PEM text nor base64 PEM") from exc
    path = Path(getattr(settings, "gmail_token_dir", "config/")) / "books_deploy_key"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw + "\n")
    path.chmod(0o600)
    return path


# ----------------------------------------------------------------- block grammar

_INDENT = "    "
_ACCOUNT_WIDTH = 40
_PAYEE_MAX = 80
_POSTING_RE = re.compile(r"^    (\S+)(?:\s{2,}(\S.*))?$")


def sanitize_payee(payee: str) -> str:
    text = re.sub(r"[;|\r\n]+", " ", payee or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:_PAYEE_MAX] or "unknown"


def _posting(account: str, amount: str = "") -> str:
    if not amount:
        return f"{_INDENT}{account}"
    if len(account) >= _ACCOUNT_WIDTH:
        return f"{_INDENT}{account}  {amount}"
    return f"{_INDENT}{account:<{_ACCOUNT_WIDTH}}{amount}"


def render_transaction(event: MoneyEvent, counter_account: str, instrument_acct: str, msgid: str) -> str:
    """One journal block (spec §1 grammar). Posting 1 = category with signed
    amount (+ out, − in), posting 2 = instrument, no amount."""
    if event.amount is None or not event.currency or event.occurred_on is None:
        raise BooksError("render_transaction needs amount, currency and occurred_on")
    tags = [f"channel: {event.channel}"]
    if event.ref:
        tags.append(f"ref: {event.ref}")
    if event.instrument:
        tags.append(f"instrument: {event.instrument}")
    amount = render_amount(event.amount, event.currency, negative=(event.direction == "in"))
    lines = [
        f"{event.occurred_on.isoformat()} * {sanitize_payee(event.payee)}",
        f"{_INDENT}; msgid: {msgid}",
        f"{_INDENT}; {', '.join(tags)}",
        _posting(counter_account, amount),
        _posting(instrument_acct),
    ]
    return "\n".join(lines) + "\n"


def iter_blocks(text: str) -> list[tuple[int, int]]:
    """(start, end) offsets of every maximal run of non-blank lines."""
    blocks: list[tuple[int, int]] = []
    pos = 0
    start: int | None = None
    for line in text.splitlines(keepends=True):
        blank = not line.strip()
        if blank and start is not None:
            blocks.append((start, pos))
            start = None
        elif not blank and start is None:
            start = pos
        pos += len(line)
    if start is not None:
        blocks.append((start, pos))
    return blocks


def find_block(text: str, msgid: str) -> tuple[int, int] | None:
    needle = f"{_INDENT}; msgid: {msgid}"
    for start, end in iter_blocks(text):
        if any(line == needle for line in text[start:end].splitlines()):
            return start, end
    return None


def append_block(text: str, block: str) -> str:
    body = text.rstrip("\n")
    return (body + "\n\n" if body else "") + block.rstrip("\n") + "\n"


def rewrite_block(
    text: str,
    msgid: str,
    *,
    payee: str | None = None,
    account: str | None = None,
    instrument_account: str | None = None,
    add_tags: dict[str, str] | None = None,
) -> str:
    span = find_block(text, msgid)
    if span is None:
        raise BooksError(f"no journal block carries msgid {msgid}")
    start, end = span
    lines = text[start:end].rstrip("\n").split("\n")
    if payee:
        date_part = lines[0].split(" * ", 1)[0]
        lines[0] = f"{date_part} * {sanitize_payee(payee)}"
    if add_tags:
        lines[2] = lines[2] + "".join(f", {k}: {v}" for k, v in add_tags.items())
    postings = [i for i, line in enumerate(lines) if line.startswith(_INDENT) and not line.startswith(f"{_INDENT};")]
    if len(postings) < 2:
        raise BooksError(f"block {msgid} has fewer than two postings")
    if account:
        m = _POSTING_RE.match(lines[postings[0]])
        lines[postings[0]] = _posting(account, m.group(2) if m and m.group(2) else "")
    if instrument_account:
        lines[postings[1]] = _posting(instrument_account)
    return text[:start] + "\n".join(lines) + "\n" + text[end:]


def journal_rel(entity: str, d: date) -> str:
    return f"{entity}/{d.year}.journal"


def journal_files(cfg: BooksConfig) -> list[Path]:
    return sorted(p for p in cfg.path.glob("*/[0-9][0-9][0-9][0-9].journal"))


# ----------------------------------------------------------------- rules

def load_rules(path: Path) -> list[dict]:
    if not Path(path).exists():
        return []
    data = yaml.safe_load(Path(path).read_text()) or []
    return [r for r in data if isinstance(r, dict) and r.get("match")]


def apply_rules(rules: list[dict], sender: str, payee: str) -> dict | None:
    haystack = f"{sender or ''} | {payee or ''}"
    for rule in rules:
        try:
            if re.search(str(rule["match"]), haystack, re.I):
                return rule
        except re.error:
            continue
    return None


# ----------------------------------------------------------------- git + hledger

_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Maou",
    "GIT_AUTHOR_EMAIL": "maou@aegis.local",
    "GIT_COMMITTER_NAME": "Maou",
    "GIT_COMMITTER_EMAIL": "maou@aegis.local",
}


def _env(cfg: BooksConfig) -> dict[str, str]:
    env = {**os.environ, **_GIT_IDENTITY}
    if cfg.deploy_key:
        env["GIT_SSH_COMMAND"] = (
            f"ssh -i {cfg.deploy_key} -o StrictHostKeyChecking=accept-new -o IdentitiesOnly=yes"
        )
    return env


def _run(args: list[str], cfg: BooksConfig, *, timeout: int = 60, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        args, cwd=str(cfg.path), env=_env(cfg), capture_output=True, text=True, timeout=timeout
    )
    if check and proc.returncode != 0:
        raise BooksError(f"{' '.join(args[:2])} failed: {proc.stderr.strip()[:500]}")
    return proc


def _has_remote(cfg: BooksConfig) -> bool:
    return bool(_run(["git", "remote"], cfg, check=False).stdout.strip())


def ensure_checkout_sync(cfg: BooksConfig) -> None:
    """Clone if the working copy is missing. Raises BooksDisabled with no
    repo url and no checkout."""
    if (cfg.path / ".git").exists():
        return
    if not cfg.repo_url:
        raise BooksDisabled("books_repo_url is not configured and no checkout exists")
    cfg.path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["git", "clone", "-q", cfg.repo_url, str(cfg.path)],
        env=_env(cfg), capture_output=True, text=True, timeout=180,
    )
    if proc.returncode != 0:
        raise BooksError(f"git clone failed: {proc.stderr.strip()[:500]}")


def _pull_sync(cfg: BooksConfig) -> None:
    if _has_remote(cfg):
        _run(["git", "pull", "-q", "--rebase", "--autostash"], cfg, timeout=120)


def _check_sync(cfg: BooksConfig) -> None:
    proc = subprocess.run(
        ["hledger", "-f", cfg.main, "check", "--strict"],
        cwd=str(cfg.path), capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise BooksCheckError(proc.stderr.strip()[:1000] or proc.stdout.strip()[:1000])


def _revert_sync(cfg: BooksConfig) -> None:
    _run(["git", "checkout", "-q", "--", "."], cfg, check=False)
    _run(["git", "clean", "-qfd"], cfg, check=False)


def _commit_push_sync(cfg: BooksConfig, summary: str) -> bool:
    _run(["git", "add", "-A"], cfg)
    if _run(["git", "diff", "--cached", "--quiet"], cfg, check=False).returncode == 0:
        return False
    _run(["git", "commit", "-q", "-m", summary], cfg)
    if _has_remote(cfg):
        push = _run(["git", "push", "-q"], cfg, check=False, timeout=120)
        if push.returncode != 0:
            logger.warning("books_push_failed", error=push.stderr.strip()[:300])
    return True


def unpushed_commits_sync(cfg: BooksConfig) -> int:
    proc = _run(["git", "rev-list", "--count", "@{u}..HEAD"], cfg, check=False)
    return int(proc.stdout.strip()) if proc.returncode == 0 and proc.stdout.strip().isdigit() else 0


_ASYNC_LOCK = asyncio.Lock()


class _FileLock:
    """flock on <books>/.aegis.lock — core and worker share the directory."""

    def __init__(self, cfg: BooksConfig) -> None:
        self._path = cfg.path / ".aegis.lock"

    def __enter__(self):
        self._fd = open(self._path, "w")  # noqa: SIM115 — held for the with-block
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self._fd, fcntl.LOCK_UN)
        self._fd.close()


def _write_sync(cfg: BooksConfig, summary: str, mutate: Callable[[], None]) -> None:
    ensure_checkout_sync(cfg)
    with _FileLock(cfg):
        _pull_sync(cfg)
        try:
            mutate()
            _check_sync(cfg)
        except Exception:
            _revert_sync(cfg)
            raise
        _commit_push_sync(cfg, summary)


async def _write(cfg: BooksConfig, summary: str, mutate: Callable[[], None]) -> None:
    async with _ASYNC_LOCK:
        await asyncio.to_thread(_write_sync, cfg, summary, mutate)


def _declared_accounts_sync(cfg: BooksConfig) -> set[str]:
    proc = subprocess.run(
        ["hledger", "-f", cfg.main, "accounts", "--declared"],
        cwd=str(cfg.path), capture_output=True, text=True, timeout=30,
    )
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


def _ensure_journal_file(cfg: BooksConfig, rel: str) -> Path:
    path = cfg.path / rel
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    entity, year = rel.split("/")[0], rel.split("/")[1][:4]
    path.write_text(f"; {entity.capitalize()} transactions, {year}. Written by AEGIS (Maou).\n")
    main = cfg.path / cfg.main
    text = main.read_text()
    line = f"include {rel}\n"
    if line not in text:
        marker = "include recurring.journal\n"
        text = text.replace(marker, line + marker) if marker in text else text + line
        main.write_text(text)
    return path


async def post_event(event: MoneyEvent, msgid: str, cfg: BooksConfig) -> str:
    """Append one transaction block. Idempotent on msgid. Returns the
    journal file's relative path."""
    if event.kind != "transaction" or event.entity == "none":
        raise BooksError(f"post_event needs a transaction with an entity, got {event.kind}/{event.entity}")
    if event.amount is None or not event.currency or event.occurred_on is None:
        raise BooksError("post_event needs amount, currency and occurred_on")
    rel = journal_rel(event.entity, event.occurred_on)

    def mutate() -> None:
        declared = _declared_accounts_sync(cfg)
        counter = event.account or account_for(event.category, event.direction, event.entity)
        if declared and counter not in declared:
            counter = UNKNOWN["hikmah" if event.entity == "hikmah" else "personal"][
                "in" if event.direction == "in" else "out"
            ]
        instrument = instrument_account(event.instrument, declared)
        path = _ensure_journal_file(cfg, rel)
        text = path.read_text()
        if find_block(text, msgid):
            return
        path.write_text(append_block(text, render_transaction(event, counter, instrument, msgid)))

    summary = (
        f"post {event.entity} {event.occurred_on} {sanitize_payee(event.payee)} "
        f"{render_amount(event.amount, event.currency)}"
    )
    await _write(cfg, summary, mutate)
    return rel


async def rewrite_event(
    msgid: str,
    cfg: BooksConfig,
    *,
    payee: str | None = None,
    account: str | None = None,
    instrument_account: str | None = None,
    add_tags: dict[str, str] | None = None,
) -> str:
    found: list[str] = []

    def mutate() -> None:
        for path in journal_files(cfg):
            text = path.read_text()
            if find_block(text, msgid) is None:
                continue
            path.write_text(
                rewrite_block(text, msgid, payee=payee, account=account,
                              instrument_account=instrument_account, add_tags=add_tags)
            )
            found.append(str(path.relative_to(cfg.path)))
            return
        raise BooksError(f"no journal block carries msgid {msgid}")

    await _write(cfg, f"reclassify {msgid}" + (f" -> {account}" if account else ""), mutate)
    return found[0]


async def remove_event(msgid: str, cfg: BooksConfig) -> None:
    def mutate() -> None:
        for path in journal_files(cfg):
            text = path.read_text()
            span = find_block(text, msgid)
            if span is None:
                continue
            start, end = span
            new = (text[:start].rstrip("\n") + "\n\n" + text[end:].lstrip("\n")).rstrip("\n") + "\n"
            path.write_text(new)
            return
        raise BooksError(f"no journal block carries msgid {msgid}")

    await _write(cfg, f"remove {msgid}", mutate)


async def append_prices(lines: list[str], cfg: BooksConfig) -> None:
    def mutate() -> None:
        path = cfg.path / "prices.journal"
        text = path.read_text() if path.exists() else ""
        path.write_text(text.rstrip("\n") + "\n" + "\n".join(lines) + "\n" if text else "\n".join(lines) + "\n")

    await _write(cfg, f"prices {lines[0].split()[1] if lines else ''}", mutate)


async def append_rule(rule: dict, cfg: BooksConfig) -> None:
    def mutate() -> None:
        path = cfg.path / "rules" / "accounts.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        text = path.read_text() if path.exists() else ""
        entry = yaml.safe_dump([rule], sort_keys=False, allow_unicode=True)
        path.write_text((text.rstrip("\n") + "\n" if text else "") + entry)

    await _write(cfg, f"rule: {rule.get('match')} -> {rule.get('account', 'payee only')}", mutate)


async def write_report(rel_path: str, text: str, cfg: BooksConfig) -> None:
    def mutate() -> None:
        path = cfg.path / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    await _write(cfg, f"report {rel_path}", mutate)


_READ_COMMANDS = frozenset({
    "bal", "balance", "reg", "register", "print", "is", "incomestatement", "bs",
    "balancesheet", "cf", "cashflow", "accounts", "payees", "tags", "stats",
    "activity", "aregister", "check",
})
_FORBIDDEN_ARG_PREFIXES = ("-f", "--file", "--rules", "-o", "--output-file", "--config")
_OUTPUT_CAP = 12_000


async def run_hledger(args: list[str], cfg: BooksConfig, *, output_format: str = "text") -> str:
    """Read-only hledger over main.journal. Whitelisted subcommands; no file,
    rules or output-file overrides; output capped at 12,000 chars."""
    if not args or args[0] not in _READ_COMMANDS:
        raise BooksError(f"hledger command not allowed: {args[:1]}")
    for a in args[1:]:
        if a.startswith(_FORBIDDEN_ARG_PREFIXES):
            raise BooksError(f"hledger argument not allowed: {a}")
    if output_format not in ("text", "json", "csv"):
        raise BooksError(f"unknown output format {output_format}")
    cmd = ["hledger", "-f", cfg.main, *args]
    if output_format != "text":
        cmd += ["-O", output_format]

    def _go() -> str:
        proc = subprocess.run(cmd, cwd=str(cfg.path), capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raise BooksError(proc.stderr.strip()[:800])
        out = proc.stdout
        return out if len(out) <= _OUTPUT_CAP else out[:_OUTPUT_CAP] + "\n… (truncated)"

    return await asyncio.to_thread(_go)


async def unpushed_commits(cfg: BooksConfig) -> int:
    return await asyncio.to_thread(unpushed_commits_sync, cfg)
```

- [ ] **Step 4: Run the tests, then lint**

Run: `PYTHONPATH=core/src:worker/src:comms/src .venv/bin/python -m pytest tests/core/test_books.py -q 2>&1 | tail -5` — all passed (the round-trip tests run on meem; confirm they are not skipped: `-rs` shows no skips).
Falsifiability: in `append_block` change `"\n\n"` to `"\n"`, run `test_append_and_find_block`, confirm FAIL, revert.
Run: `.venv/bin/ruff check core/src/ tests/core/` — clean (the `# noqa: SIM115` on the lock `open` is deliberate).

- [ ] **Step 5: Commit**

```bash
git add core/src/aegis/services/books.py tests/core/test_books.py
git commit -m "feat(money): journal writer with git sync and a read-only hledger runner"
```

### Task 5: `finance.journal_index` — migration and service

**Files:**
- Create: `migrations/026_journal_index.sql`
- Create: `core/src/aegis/services/journal_index.py`
- Test: `tests/core/test_journal_index.py`

**Interfaces (`aegis.services.journal_index`):**
- `msgid_for(mailbox: str, message_id: str) -> str` = `f"{mailbox}/{message_id}"`
- `async upsert(pool, msgid: str, mailbox: str, event: MoneyEvent, *, journal_file: str | None = None, linked: str | None = None, todoist_ref: str | None = None) -> None` — insert or replace every column except `created_at`; a `None` `journal_file`/`linked`/`todoist_ref` on update keeps the existing value (`COALESCE(EXCLUDED.x, journal_index.x)`).
- `async get(pool, msgid) -> dict | None`
- `async find_match(pool, event: MoneyEvent, exclude_msgid: str) -> dict | None` — spec §5.4: opposite `source_class` (bank↔receipt), same `currency` and `amount`, `kind='transaction'`, `occurred_on` within ±3 days, `linked_message_id IS NULL`, nearest date first.
- `async link(pool, a: str, b: str) -> None` — sets `linked_message_id` both ways.
- `async find_open_due(pool, payee_key: str, amount: Decimal, currency: str, around: date) -> dict | None` — `kind IN ('due','failed')`, `todoist_ref IS NOT NULL`, `linked_message_id IS NULL`, same `payee_key` and `currency`, amount within 1%, `due_on` within ±45 days of `around`.
- Ruling: the spec's `receipt_email.journal_msgid` column is not added — the msgid is derivable (`mailbox/message_id`) and nothing reads it.

- [ ] **Step 1: Write the migration**

```sql
-- migrations/026_journal_index.sql
-- The books index (spec 2026-09-05-maou-books-design.md §5.3). The hledger
-- journal is the record; this table is idempotency, receipt<->bank matching,
-- dues dedupe and the admin page. Idempotent DDL: the migration runner keys
-- on filename and re-runs a renamed file.
CREATE TABLE IF NOT EXISTS finance.journal_index (
    message_id        text PRIMARY KEY,
    mailbox           text NOT NULL,
    entity            text NOT NULL,
    kind              text NOT NULL,
    direction         text,
    amount            numeric(14,2),
    currency          text,
    payee             text,
    payee_key         text,
    account           text,
    channel           text,
    instrument        text,
    occurred_on       date,
    due_on            date,
    parser            text NOT NULL,
    confidence        real,
    source_class      text NOT NULL DEFAULT 'other',
    journal_file      text,
    linked_message_id text,
    todoist_ref       text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS journal_index_payee_day
    ON finance.journal_index (payee_key, occurred_on);
CREATE INDEX IF NOT EXISTS journal_index_match
    ON finance.journal_index (currency, amount, occurred_on)
    WHERE kind = 'transaction' AND linked_message_id IS NULL;
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/core/test_journal_index.py
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from aegis.api.models.money import MoneyEvent
from aegis.services import journal_index as ji


@pytest_asyncio.fixture(autouse=True)
async def _clean(db_pool):
    await db_pool.execute("DELETE FROM finance.journal_index WHERE message_id LIKE 'ji-%'")
    yield
    await db_pool.execute("DELETE FROM finance.journal_index WHERE message_id LIKE 'ji-%'")


def _bank(amount="10.00", day=2, **kw) -> MoneyEvent:
    base = dict(kind="transaction", direction="out", amount=Decimal(amount), currency="INR",
                payee="Corner Store", payee_key="corner store", channel="upi",
                instrument="hdfc-1225", occurred_on=date(2026, 9, day), entity="personal",
                account="expenses:unknown", parser="hdfc_upi", source_class="bank")
    base.update(kw)
    return MoneyEvent(**base)


def test_msgid_for():
    assert ji.msgid_for("arshad-personal", "1a06") == "arshad-personal/1a06"


@pytest.mark.asyncio
async def test_upsert_get_and_coalescing_update(db_pool):
    ev = _bank()
    await ji.upsert(db_pool, "ji-mail/1", "arshad-personal", ev, journal_file="personal/2026.journal")
    row = await ji.get(db_pool, "ji-mail/1")
    assert row["amount"] == Decimal("10.00") and row["journal_file"] == "personal/2026.journal"
    assert row["source_class"] == "bank" and row["occurred_on"] == date(2026, 9, 2)
    # re-upsert without journal_file keeps it; payee change lands
    await ji.upsert(db_pool, "ji-mail/1", "arshad-personal", ev.model_copy(update={"payee": "Shop"}))
    row = await ji.get(db_pool, "ji-mail/1")
    assert row["journal_file"] == "personal/2026.journal" and row["payee"] == "Shop"
    assert await ji.get(db_pool, "ji-nope") is None


@pytest.mark.asyncio
async def test_find_match_opposite_class_same_amount_within_3_days(db_pool):
    await ji.upsert(db_pool, "ji-bank/1", "arshad-personal", _bank(day=2), journal_file="personal/2026.journal")
    receipt = _bank(day=4, source_class="receipt", channel="receipt", parser="stripe_receipt", instrument=None)
    m = await ji.find_match(db_pool, receipt, "ji-rcpt/1")
    assert m is not None and m["message_id"] == "ji-bank/1"
    # same class never matches
    assert await ji.find_match(db_pool, _bank(day=2), "ji-bank/2") is None
    # 4 days apart does not match
    assert await ji.find_match(db_pool, receipt.model_copy(update={"occurred_on": date(2026, 9, 6)}), "ji-rcpt/2") is None
    # different amount does not match
    assert await ji.find_match(db_pool, receipt.model_copy(update={"amount": Decimal("11.00")}), "ji-rcpt/3") is None
    # a linked row is no longer a candidate
    await ji.link(db_pool, "ji-bank/1", "ji-rcpt/1")
    assert (await ji.get(db_pool, "ji-bank/1"))["linked_message_id"] == "ji-rcpt/1"
    assert await ji.find_match(db_pool, receipt, "ji-rcpt/9") is None


@pytest.mark.asyncio
async def test_find_match_prefers_nearest_date(db_pool):
    await ji.upsert(db_pool, "ji-bank/far", "arshad-personal", _bank(day=1))
    await ji.upsert(db_pool, "ji-bank/near", "arshad-personal", _bank(day=3))
    receipt = _bank(day=4, source_class="receipt")
    assert (await ji.find_match(db_pool, receipt, "ji-r"))["message_id"] == "ji-bank/near"


@pytest.mark.asyncio
async def test_find_open_due_tolerance_and_window(db_pool):
    due = MoneyEvent(kind="due", direction="out", amount=Decimal("100308.53"), currency="INR",
                     payee="Axis credit card XX13", payee_key="axis credit card xx13",
                     channel="statement", due_on=date(2026, 9, 7), entity="personal",
                     parser="axis_cc_statement", source_class="bank")
    await ji.upsert(db_pool, "ji-due/1", "arshad-personal", due, todoist_ref="task-1")
    hit = await ji.find_open_due(db_pool, "axis credit card xx13", Decimal("100300.00"), "INR", date(2026, 9, 6))
    assert hit is not None and hit["todoist_ref"] == "task-1"
    assert await ji.find_open_due(db_pool, "axis credit card xx13", Decimal("90000.00"), "INR", date(2026, 9, 6)) is None
    assert await ji.find_open_due(db_pool, "axis credit card xx13", Decimal("100308.53"), "INR", date(2026, 12, 1)) is None
    await ji.upsert(db_pool, "ji-due/2", "arshad-personal", due)  # no todoist_ref → not open
    assert await ji.find_open_due(db_pool, "other", Decimal("100308.53"), "INR", date(2026, 9, 6)) is None
```

- [ ] **Step 3: Run to verify failure**

Run: `PYTHONPATH=core/src:worker/src:comms/src .venv/bin/python -m pytest tests/core/test_journal_index.py -q 2>&1 | tail -3`
Expected: ModuleNotFoundError.

- [ ] **Step 4: Implement `core/src/aegis/services/journal_index.py`**

```python
"""finance.journal_index — the books' index (spec §5.3).

The hledger journal is the record. This table gives idempotency on the
Gmail message id, receipt<->bank matching (§5.4), dues dedupe and the
admin page. Never treat `amount` here as authoritative; run hledger.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from aegis.api.models.money import MoneyEvent

_MATCH_DAYS = 3
_DUE_DAYS = 45
_DUE_TOLERANCE = Decimal("0.01")


def msgid_for(mailbox: str, message_id: str) -> str:
    return f"{mailbox}/{message_id}"


async def upsert(
    pool: Any,
    msgid: str,
    mailbox: str,
    event: MoneyEvent,
    *,
    journal_file: str | None = None,
    linked: str | None = None,
    todoist_ref: str | None = None,
) -> None:
    await pool.execute(
        """
        INSERT INTO finance.journal_index
          (message_id, mailbox, entity, kind, direction, amount, currency, payee, payee_key,
           account, channel, instrument, occurred_on, due_on, parser, confidence, source_class,
           journal_file, linked_message_id, todoist_ref)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20)
        ON CONFLICT (message_id) DO UPDATE SET
          mailbox = EXCLUDED.mailbox, entity = EXCLUDED.entity, kind = EXCLUDED.kind,
          direction = EXCLUDED.direction, amount = EXCLUDED.amount, currency = EXCLUDED.currency,
          payee = EXCLUDED.payee, payee_key = EXCLUDED.payee_key, account = EXCLUDED.account,
          channel = EXCLUDED.channel, instrument = EXCLUDED.instrument,
          occurred_on = EXCLUDED.occurred_on, due_on = EXCLUDED.due_on, parser = EXCLUDED.parser,
          confidence = EXCLUDED.confidence, source_class = EXCLUDED.source_class,
          journal_file = COALESCE(EXCLUDED.journal_file, finance.journal_index.journal_file),
          linked_message_id = COALESCE(EXCLUDED.linked_message_id, finance.journal_index.linked_message_id),
          todoist_ref = COALESCE(EXCLUDED.todoist_ref, finance.journal_index.todoist_ref),
          updated_at = now()
        """,
        msgid, mailbox, event.entity, event.kind, event.direction, event.amount, event.currency,
        event.payee, event.payee_key, event.account, event.channel, event.instrument,
        event.occurred_on, event.due_on, event.parser, float(event.confidence), event.source_class,
        journal_file, linked, todoist_ref,
    )


async def get(pool: Any, msgid: str) -> dict | None:
    row = await pool.fetchrow("SELECT * FROM finance.journal_index WHERE message_id = $1", msgid)
    return dict(row) if row else None


async def find_match(pool: Any, event: MoneyEvent, exclude_msgid: str) -> dict | None:
    opposite = {"bank": "receipt", "receipt": "bank"}.get(event.source_class)
    if opposite is None or event.amount is None or event.occurred_on is None or not event.currency:
        return None
    row = await pool.fetchrow(
        """
        SELECT * FROM finance.journal_index
        WHERE kind = 'transaction' AND source_class = $1 AND currency = $2 AND amount = $3
          AND occurred_on BETWEEN $4 AND $5 AND linked_message_id IS NULL AND message_id <> $6
        ORDER BY abs(occurred_on - $7::date) ASC, created_at ASC
        LIMIT 1
        """,
        opposite, event.currency, event.amount,
        event.occurred_on - timedelta(days=_MATCH_DAYS), event.occurred_on + timedelta(days=_MATCH_DAYS),
        exclude_msgid, event.occurred_on,
    )
    return dict(row) if row else None


async def link(pool: Any, a: str, b: str) -> None:
    await pool.execute(
        "UPDATE finance.journal_index SET linked_message_id = CASE message_id WHEN $1 THEN $2 ELSE $1 END, "
        "updated_at = now() WHERE message_id IN ($1, $2)",
        a, b,
    )


async def find_open_due(pool: Any, payee_key: str, amount: Decimal, currency: str, around: date) -> dict | None:
    row = await pool.fetchrow(
        """
        SELECT * FROM finance.journal_index
        WHERE kind IN ('due', 'failed') AND todoist_ref IS NOT NULL AND linked_message_id IS NULL
          AND payee_key = $1 AND currency = $2
          AND abs(amount - $3) <= $3 * $4
          AND due_on BETWEEN $5 AND $6
        ORDER BY abs(due_on - $7::date) ASC LIMIT 1
        """,
        payee_key, currency, amount, _DUE_TOLERANCE,
        around - timedelta(days=_DUE_DAYS), around + timedelta(days=_DUE_DAYS), around,
    )
    return dict(row) if row else None
```

- [ ] **Step 5: Run the tests, lint, commit**

Run: `PYTHONPATH=core/src:worker/src:comms/src .venv/bin/python -m pytest tests/core/test_journal_index.py tests/core/test_migrations.py -q 2>&1 | tail -3` — all passed (if `tests/core/test_migrations.py` does not exist, run just the index test; the conftest applies `migrations/` to the test DB so 026 is exercised by the index tests).
Run: `.venv/bin/ruff check core/src/ tests/core/` — clean.

```bash
git add migrations/026_journal_index.sql core/src/aegis/services/journal_index.py tests/core/test_journal_index.py
git commit -m "feat(money): journal_index table and service for idempotency, matching and dues"
```

---

### Task 6: LLM extractor v2 — `extract_money_batch`

**Files:**
- Modify: `core/src/aegis/llm/__init__.py` (add `_MONEY_EVENT_PROMPT`, `_format_money_emails`, `LLMClient.extract_money_batch`; leave `extract_receipts_batch` in place for PR3 to delete)
- Test: `tests/core/test_llm_money.py`

**Interfaces:**
- Produces: `async LLMClient.extract_money_batch(self, receipts: list[dict], model: str, system_prompt: str | None = None, db_pool=None, agent_id: str | None = None) -> list[dict]` — one dict per input, each a `MoneyEvent.model_dump(mode="json")` with `parser="llm"`, `payee_key` filled, `source_class` = `"receipt"` when `channel` is `receipt` or `bill`, else `"other"`; a per-item failure or a truncation returns `{"kind": "ignore", "parser": "llm", "_parse_failed": True}` for that item. Batch failure raises (as `extract_receipts_batch` does). `purpose="money_event_extraction"`, `max_tokens=4000`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/core/test_llm_money.py
from __future__ import annotations

import json

import pytest
from aegis.llm import LLMClient, LLMTruncationError

RECEIPT = {
    "id": "r1", "account": "arshad-personal", "message_id": "m1",
    "sender": "Google Play <googleplay-noreply@google.com>",
    "subject": "Payment declined for Medium subscription",
    "body_plain": "Your subscription will be cancelled. Amount Due ₹199.00 Fix by 15 Sept 2026",
    "received_at": "2026-09-03T10:00:00+00:00",
}


class _Client(LLMClient):
    def __init__(self, response):
        super().__init__(base_url="http://x", api_key="k")
        self._response = response

    async def think(self, **kw):
        if isinstance(self._response, Exception):
            raise self._response
        assert kw["purpose"] == "money_event_extraction" and kw["max_tokens"] == 4000
        assert "Medium subscription" in kw["prompt"] and "Fix by" in kw["prompt"]
        return {"response": self._response}


@pytest.mark.asyncio
async def test_parses_a_failed_payment_into_a_money_event():
    payload = [{
        "kind": "failed", "direction": "out", "amount": 199, "currency": "INR",
        "payee": "Medium", "category": "media", "channel": "other", "instrument": None,
        "occurred_on": None, "due_on": "2026-09-15", "is_recurring": True, "confidence": 0.9,
    }]
    out = await _Client("```json\n" + json.dumps(payload) + "\n```").extract_money_batch([RECEIPT], model="m")
    assert len(out) == 1
    ev = out[0]
    assert ev["kind"] == "failed" and ev["amount"] == "199.00" and ev["due_on"] == "2026-09-15"
    assert ev["payee_key"] == "medium" and ev["parser"] == "llm" and ev["source_class"] == "other"
    assert "_parse_failed" not in ev


@pytest.mark.asyncio
async def test_receipt_channel_gets_receipt_source_class_and_unknown_keys_are_dropped():
    payload = [{"kind": "transaction", "direction": "out", "amount": "1,936.00", "currency": "INR",
                "payee": "Eleven Labs", "channel": "receipt", "occurred_on": "2026-08-25",
                "confidence": 0.95, "bogus": 1}]
    out = await _Client(json.dumps(payload)).extract_money_batch([RECEIPT], model="m")
    assert out[0]["source_class"] == "receipt" and out[0]["amount"] == "1936.00"


@pytest.mark.asyncio
async def test_bad_item_is_flagged_not_raised():
    payload = [{"kind": "nonsense"}]
    out = await _Client(json.dumps(payload)).extract_money_batch([RECEIPT], model="m")
    assert out[0]["_parse_failed"] is True and out[0]["kind"] == "ignore"


@pytest.mark.asyncio
async def test_truncation_returns_stubs_and_other_errors_raise():
    out = await _Client(LLMTruncationError("cut")).extract_money_batch([RECEIPT], model="m")
    assert out[0]["_parse_failed"] is True
    with pytest.raises(RuntimeError):
        await _Client(RuntimeError("down")).extract_money_batch([RECEIPT], model="m")


@pytest.mark.asyncio
async def test_empty_input_is_empty_output():
    assert await _Client("[]").extract_money_batch([], model="m") == []
```

If `LLMTruncationError.__init__` needs more arguments, construct it the way `tests/core/test_llm_truncation_retry.py` does.

- [ ] **Step 2: Run to verify failure** — `AttributeError: extract_money_batch`.

- [ ] **Step 3: Implement in `core/src/aegis/llm/__init__.py`**

Add next to `_BATCH_RECEIPT_PROMPT`:

```python
_MONEY_EVENT_PROMPT = """\
You are the bookkeeper. For EACH email below return one JSON object describing
the money event it carries.

Fields:
- kind: "transaction" (money actually moved: a receipt, payment confirmation,
  debit or credit alert), "due" (a bill, card statement, autopay reminder or
  deadline asking for payment by a date), "failed" (a declined, failed or
  reversed payment that needs fixing), "info" (statement available, KYC,
  balance or account notice with no money moving), "ignore" (newsletter,
  marketing, offers, anything else).
- direction: "out" (you paid) or "in" (you received); null for info/ignore.
- amount: number in MAJOR units (245.50, never 24550). Strip commas. Null if none.
- currency: ISO code. ₹ / Rs / INR -> INR, $ -> USD, £ -> GBP, € -> EUR.
- payee: the other party (merchant, biller, person, sender of funds) as a
  display name, never an email address.
- category: one of saas, media, infra, internet, electricity, mobile,
  groceries, food, transport, shopping, health, insurance, fees, tax,
  professional, ads, people, salary, interest, refund, other.
- channel: one of upi, imps, neft, card, autopay, remittance, receipt, bill,
  statement, other.
- instrument: the paying or receiving account as hdfc-1225 (bank + last 4
  digits), axis-cc-1313 (credit card), card-1313 (card, bank unknown), or null.
- occurred_on: date money moved, YYYY-MM-DD, or null.
- due_on: payment-due or fix-by date, YYYY-MM-DD, or null.
- is_recurring: true for a subscription or utility that bills again, false
  for a one-off, null if unsure.
- confidence: 0.0-1.0.

Rules: a number in an advertisement, offer, insurance cover or credit limit
is NOT a charge (kind ignore). An autopay reminder or a card statement is
"due", never "transaction". A failed, declined or reversed payment is
"failed"; a refund you received is a "transaction" with direction "in". A
statement delivered as a PDF with no figures in the text is "info".

Return a JSON array with EXACTLY one object per email, in the same order,
inside ```json fences.

EMAILS:
{emails}
"""


def _format_money_emails(receipts: list[dict]) -> str:
    parts = []
    for i, r in enumerate(receipts):
        parts.append(
            f"--- Email {i + 1} ---\n"
            f"From: {r.get('sender', '')}\n"
            f"Subject: {r.get('subject', '')}\n"
            f"Body: {(r.get('body_plain') or '')[:4000]}\n"
        )
    return "\n".join(parts)
```

Add the method to `LLMClient` after `extract_receipts_batch`:

```python
    async def extract_money_batch(
        self,
        receipts: list[dict],
        model: str = "gemma4:e2b",
        system_prompt: str | None = None,
        db_pool: Any = None,
        agent_id: str | None = None,
    ) -> list[dict]:
        """One `MoneyEvent` dict per input email (spec §4). Truncation and
        per-item garbage become `_parse_failed` stubs; a batch failure raises."""
        from aegis.api.models.money import MoneyEvent, payee_key

        if not receipts:
            return []
        stub = {"kind": "ignore", "parser": "llm", "_parse_failed": True}
        prompt = _MONEY_EVENT_PROMPT.format(emails=_format_money_emails(receipts))
        try:
            result = await self.think(
                prompt=prompt,
                model=model,
                system_prompt=system_prompt,
                max_tokens=4000,
                db_pool=db_pool,
                purpose="money_event_extraction",
                agent_id=agent_id,
            )
            parsed = parse_llm_json(result.get("response", ""))
            if not isinstance(parsed, list):
                raise ValueError("expected JSON array")
        except LLMTruncationError as exc:
            logger.warning("extract_money_batch_truncated", error=str(exc)[:200], count=len(receipts))
            return [dict(stub) for _ in receipts]
        except Exception as exc:
            logger.warning("extract_money_batch_failed", error=str(exc)[:200], count=len(receipts))
            raise

        allowed = set(MoneyEvent.model_fields)
        out: list[dict] = []
        for i in range(len(receipts)):
            item = parsed[i] if i < len(parsed) and isinstance(parsed[i], dict) else None
            if item is None:
                out.append(dict(stub))
                continue
            data = {k: v for k, v in item.items() if k in allowed}
            if isinstance(data.get("amount"), str):
                data["amount"] = data["amount"].replace(",", "")
            data["parser"] = "llm"
            data["source_class"] = "receipt" if data.get("channel") in ("receipt", "bill") else "other"
            try:
                ev = MoneyEvent(**data)
            except Exception:
                out.append(dict(stub))
                continue
            ev.payee_key = payee_key(ev.payee)
            out.append(ev.model_dump(mode="json"))
        return out
```

`MoneyEvent` needs `amount` coercion from int/float/str: pydantic v2 `Decimal` fields accept numbers and numeric strings; to force two places add to `MoneyEvent` (Task 2's class) a validator:

```python
    @field_validator("amount", mode="after")
    @classmethod
    def _two_places(cls, v: Decimal | None) -> Decimal | None:
        return v.quantize(Decimal("0.01")) if v is not None else None
```

(import `field_validator` from pydantic; `model_dump(mode="json")` then renders `"199.00"`.) Add that validator now if Task 2 did not.

- [ ] **Step 4: Run, lint, commit**

Run: `PYTHONPATH=core/src:worker/src:comms/src .venv/bin/python -m pytest tests/core/test_llm_money.py tests/core/test_books_helpers.py -q 2>&1 | tail -3` — all passed.
Run: `.venv/bin/ruff check core/src/ tests/core/` — clean.

```bash
git add core/src/aegis/llm/__init__.py core/src/aegis/api/models/money.py tests/core/test_llm_money.py
git commit -m "feat(money): LLM money-event extraction from the full email body"
```

### Task 7: Settings, integration registry, deploy key at boot, worker wiring

**Files:**
- Modify: `core/src/aegis/config.py:349-356` (append the books block after the money block)
- Modify: `core/src/aegis/services/integrations_config.py:39-70` (append a `Books` group to `CONFIG_REGISTRY`)
- Modify: `core/src/aegis/services/books.py` (add `parse_csv_set`, `parse_kv`)
- Modify: `core/src/aegis/api/app.py:65-68` (after `apply_config_overrides`), `worker/src/aegis_worker/bootstrap.py:146-149` (same)
- Modify: `worker/src/aegis_worker/__main__.py:251-262` (`MoneyActivities(...)`), `:438-441` (`CaptureActivities(...)`)
- Modify: `worker/src/aegis_worker/activities/money.py` (new dataclass fields only), `worker/src/aegis_worker/activities/capture.py` (new dataclass field only)
- Test: `tests/core/test_books_config.py`

**Interfaces:**
- Ruling (spec §10 amended): the three list/dict settings are plain strings so they can be DB-configured from the admin Integrations page without an Ansible change, and their defaults are EMPTY (the repo is open source; the operator's mailbox names belong in the DB): `books_ignored_mailboxes: str = ""` (comma-separated mailbox labels), `books_mailbox_entities: str = ""` (`label=entity,...`), `books_todoist_projects: str = ""` (`personal=<id>,hikmah=<id>`). Plus `books_path: str = "/app/config/books"`, `books_repo_url: str = ""`, `books_deploy_key: str = ""`.
- Produces (`aegis.services.books`): `parse_csv_set(raw: str) -> frozenset[str]`, `parse_kv(raw: str) -> dict[str, str]`.
- `MoneyActivities` new fields (defaults keep every existing test constructing it valid): `books_cfg: Any = None`, `ignored_mailboxes: frozenset[str] = frozenset()`, `mailbox_entities: dict[str, str] = field(default_factory=dict)`, `capture: Any = None`, `home_tz: str = "Asia/Kolkata"`.
- `CaptureActivities` new field: `todoist_projects: dict[str, str] = field(default_factory=dict)`.
- Boot: `books.install_deploy_key(settings)` is called right after `apply_config_overrides` in both processes, wrapped so a bad key logs `books_deploy_key_install_failed` and never blocks boot.

- [ ] **Step 1: Write the failing tests**

```python
# tests/core/test_books_config.py
from types import SimpleNamespace

from aegis.config import Settings
from aegis.services import books
from aegis.services.integrations_config import CONFIG_REGISTRY


def test_settings_defaults_are_open_source_clean():
    s = Settings(auth_disabled=True)
    assert s.books_path == "/app/config/books"
    assert s.books_repo_url == "" and s.books_deploy_key == ""
    assert s.books_ignored_mailboxes == "" and s.books_mailbox_entities == ""
    assert s.books_todoist_projects == ""


def test_registry_has_the_books_group():
    by_key = {c.key: c for c in CONFIG_REGISTRY}
    assert by_key["books_repo_url"].group == "Books" and by_key["books_repo_url"].secret is False
    assert by_key["books_deploy_key"].secret is True
    for key in ("books_ignored_mailboxes", "books_mailbox_entities", "books_todoist_projects"):
        assert by_key[key].group == "Books" and by_key[key].secret is False


def test_parse_helpers():
    assert books.parse_csv_set(" a, b ,,c") == frozenset({"a", "b", "c"})
    assert books.parse_csv_set("") == frozenset()
    assert books.parse_kv("personal=6h2f, hikmah = 6h2g") == {"personal": "6h2f", "hikmah": "6h2g"}
    assert books.parse_kv("garbage") == {} and books.parse_kv("") == {}


def test_config_from_settings(tmp_path):
    s = SimpleNamespace(books_path=str(tmp_path), books_repo_url="git@x:y.git", gmail_token_dir=str(tmp_path))
    cfg = books.config_from_settings(s)
    assert cfg.path == tmp_path and cfg.repo_url == "git@x:y.git" and cfg.deploy_key is None
    (tmp_path / "books_deploy_key").write_text("k")
    assert books.config_from_settings(s).deploy_key == tmp_path / "books_deploy_key"
```

If `Settings(auth_disabled=True)` is not how other tests build a Settings object, copy the construction from `tests/core/test_llm_backend.py` or `tests/conftest.py`.

- [ ] **Step 2: Run to verify failure** — `AttributeError: 'Settings' object has no attribute 'books_path'`.

- [ ] **Step 3: Implement**

`core/src/aegis/config.py`, after `money_hygiene_fx_rates`:

```python
    # The books — Maou's hledger journal (spec 2026-09-05-maou-books-design.md §10).
    # books_repo_url empty ⇒ books disabled: money events are still indexed,
    # never posted. The three list-ish knobs are strings so the admin
    # Integrations page can set them (DB-configured, no redeploy).
    books_path: str = "/app/config/books"
    books_repo_url: str = ""
    books_deploy_key: str = ""  # private ed25519 deploy key, PEM or base64 PEM; never logged
    books_ignored_mailboxes: str = ""  # comma-separated mailbox labels whose money is not ours
    books_mailbox_entities: str = ""  # "label=entity,..." — mailbox → personal|hikmah (default personal)
    books_todoist_projects: str = ""  # "personal=<todoist project id>,hikmah=<id>" for dues
```

`core/src/aegis/services/integrations_config.py`, appended to `CONFIG_REGISTRY`:

```python
    ConfigKey(
        "books_repo_url", "Repo URL (git@github.com:org/books.git)", "Books", False,
        help="The hledger books repo Maou writes to. Empty = books disabled (money mail is "
        "indexed, never posted). SSH form; the deploy key below must have write access. "
        "Core + worker restart required.",
    ),
    ConfigKey(
        "books_deploy_key", "Deploy key (private, ed25519)", "Books", True,
        help="Paste the PEM (multi-line) or its base64. Written to the credentials dir "
        "with mode 0600 at boot; never logged.",
    ),
    ConfigKey(
        "books_ignored_mailboxes", "Ignored mailboxes (comma-separated labels)", "Books", False,
        help="Money mail in these mailboxes is not yours (e.g. an employer's account).",
    ),
    ConfigKey(
        "books_mailbox_entities", "Mailbox → entity (label=personal|hikmah, comma-separated)", "Books", False,
        help="Which set of books a mailbox's money belongs to. Unlisted = personal.",
    ),
    ConfigKey(
        "books_todoist_projects", "Todoist projects for dues (personal=<id>,hikmah=<id>)", "Books", False,
        help="Bills and failed payments become dated tasks here. Unset = the Inbox.",
    ),
```

`core/src/aegis/services/books.py`, near the config section:

```python
def parse_csv_set(raw: str) -> frozenset[str]:
    return frozenset(s.strip() for s in (raw or "").split(",") if s.strip())


def parse_kv(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (raw or "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip() and v.strip():
                out[k.strip()] = v.strip()
    return out
```

Boot, `core/src/aegis/api/app.py` right after `await apply_config_overrides(settings, pool)`:

```python
    from aegis.services.books import install_deploy_key

    try:
        install_deploy_key(settings)
    except Exception as exc:  # noqa: BLE001 — a bad key must not block boot
        logger.warning("books_deploy_key_install_failed", error=str(exc)[:200])
```

Same four lines in `worker/src/aegis_worker/bootstrap.py` after its `apply_config_overrides` call (use that module's logger).

`worker/src/aegis_worker/activities/money.py` — add to the `MoneyActivities` dataclass after `bank_alert_senders`:

```python
    # The books (spec §5/§10). `books_cfg` is a BooksConfig; None = disabled.
    books_cfg: Any = None
    ignored_mailboxes: frozenset[str] = frozenset()
    mailbox_entities: dict[str, str] = field(default_factory=dict)
    capture: Any = None  # CaptureActivities, for dues (set after construction in __main__)
    home_tz: str = "Asia/Kolkata"
```

(`from dataclasses import dataclass, field`.) `worker/src/aegis_worker/activities/capture.py` — add after `connector`: `todoist_projects: dict[str, str] = field(default_factory=dict)` with the same import.

`worker/src/aegis_worker/__main__.py`: import `from aegis.services.books import config_from_settings, parse_csv_set, parse_kv`; in `MoneyActivities(...)` add

```python
            books_cfg=config_from_settings(settings),
            ignored_mailboxes=parse_csv_set(getattr(settings, "books_ignored_mailboxes", "")),
            mailbox_entities=parse_kv(getattr(settings, "books_mailbox_entities", "")),
```

in `CaptureActivities(...)` add `todoist_projects=parse_kv(getattr(settings, "books_todoist_projects", ""))`, and immediately after the `capture_act = CaptureActivities(...)` statement add `money_act.capture = capture_act`.

- [ ] **Step 4: Run, lint, commit**

Run: `PYTHONPATH=core/src:worker/src:comms/src .venv/bin/python -m pytest tests/core/test_books_config.py tests/worker/activities/test_money.py tests/worker/test_registry.py -n 4 --dist loadfile --timeout=300 -q 2>&1 | tail -3` — all passed.
Run: `.venv/bin/ruff check core/src/ tests/core/ worker/src/ tests/worker/` — clean.

```bash
git add core/src/aegis/config.py core/src/aegis/services/integrations_config.py core/src/aegis/services/books.py core/src/aegis/api/app.py worker/src/aegis_worker/bootstrap.py worker/src/aegis_worker/__main__.py worker/src/aegis_worker/activities/money.py worker/src/aegis_worker/activities/capture.py tests/core/test_books_config.py
git commit -m "feat(money): books settings, integration keys and deploy-key install at boot"
```

---

### Task 8: Money activities v2 — parse, post, index, dues

**Files:**
- Modify: `worker/src/aegis_worker/activities/money.py` (new activities `parse_money_email`, `post_money_event`, `store_money_result`; v2 `store_receipt_email` and `find_stuck_receipts`)
- Modify: `worker/src/aegis_worker/activities/capture.py` (`_capture` refactor, `capture_task`, `capture_due`, `complete_captured_task`)
- Test: `tests/worker/activities/test_money_v2.py`, `tests/worker/activities/test_capture_due.py`, existing `tests/worker/activities/test_money_store_receipt.py` (two tests updated)

**Interfaces:**
- `MoneyActivities.store_receipt_email(msg, account) -> str`: as before, but when the row already exists and its `parsed` lacks `"version": 2`, returns the existing id (re-process); `""` only for an already-v2 row.
- `MoneyActivities.find_stuck_receipts(limit, older_than_days) -> list[str]`: rows where `COALESCE((parsed->>'version')::int, 0) < 2`.
- `MoneyActivities.parse_money_email(receipt: dict) -> dict` — `receipt` is a `load_receipts` row; returns `MoneyEvent.model_dump(mode="json")` (plus `"_parse_failed": True` when the LLM item was unusable). Order: ignored mailbox → `kind=ignore, entity=none, parser=mailbox`; `bank_parsers.parse_any`; else `llm.extract_money_batch` with Maou's persona (`_format_agent_persona` as `classify_and_extract` does); entity from `mailbox_entities` (default `personal`) unless the parser set `hikmah`; rules file (`<books_path>/rules/accounts.yaml`, `[]` when absent): `ignore` → `kind=ignore, entity=none`; `entity`/`account`/`payee` overrides; `account` fallback `account_for(...)` — for `parser == "llm"` with `confidence < 0.8` the unknown account instead; `occurred_on` fallback = `received_at` in `home_tz`; `payee_key` recomputed last.
- `MoneyActivities.post_money_event(receipt_id, mailbox, message_id, event: dict, todoist_ref: str | None = None) -> dict` — returns `{"msgid", "status": posted|linked|indexed|books_disabled, "journal_file", "linked", "closed_due"}`; spec §5.4 matching in both directions; `close_due_if_paid` (spec §7.1) via `capture.complete_captured_task`; `BooksDisabled` → `books_disabled` (indexed, not posted).
- `MoneyActivities.store_money_result(receipt_id, event, journal_file) -> None` — merges `{"version": 2, "event": event, "journal_file": journal_file}` into `parsed`.
- `CaptureActivities.capture_task(source_tag, external_id, title, description=None, labels=None, project_id=None, due_date=None) -> str | None`; `capture_to_inbox` keeps its signature and delegates; `capture_due(event, mailbox, message_id) -> str | None` (spec §7.1: `#bill`, `f"{payee_key}:{due_on}"`, title `Pay <payee> <fmt_money>` / `Fix payment: …`, due = `due_on - 1 day` or today, project from `todoist_projects[entity]`, else Inbox, no GTD label); `complete_captured_task(task_ref) -> bool` (`item_complete` through `connector.commands`, outbox on retryable failure, `False` for an unresolved `item-…` temp ref).

- [ ] **Step 1: Write the failing tests**

```python
# tests/worker/activities/test_capture_due.py
from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import AsyncMock

import pytest
from aegis_worker.activities.capture import CaptureActivities
from temporalio.testing import ActivityEnvironment

DUE = {
    "kind": "due", "direction": "out", "amount": "100308.53", "currency": "INR",
    "payee": "Axis credit card XX13", "payee_key": "axis credit card xx13", "channel": "statement",
    "instrument": "axis-cc-13", "due_on": "2099-09-07", "entity": "personal", "parser": "axis_cc_statement",
    "confidence": 1.0, "source_class": "bank",
}


def _acts(db_pool, projects=None):
    connector = AsyncMock()
    connector.commands = AsyncMock(return_value={"sync_status": {}, "data": {"temp_id_mapping": {}}})
    return CaptureActivities(db_pool=db_pool, connector=connector, todoist_projects=projects or {}), connector


@pytest.mark.asyncio
async def test_capture_due_builds_a_dated_task_in_the_entity_project(db_pool, monkeypatch):
    await db_pool.execute("DELETE FROM todoist_capture_idempotency WHERE source_tag = '#bill'")
    acts, connector = _acts(db_pool, {"personal": "proj-personal", "hikmah": "proj-hikmah"})
    monkeypatch.setattr(
        "aegis.connectors.todoist.TodoistConnector.check_sync_status",
        staticmethod(lambda result, uuids: {"ok": True, "retryable": False, "rejected_retryable": False,
                                            "rejected": {}, "envelope_error": None}),
    )
    connector.commands = AsyncMock(return_value={"data": {"temp_id_mapping": {}}})
    ref = await ActivityEnvironment().run(acts.capture_due, DUE, "arshad-personal", "1a06")
    cmd = connector.commands.await_args.args[0][0]
    assert cmd["type"] == "item_add"
    assert cmd["args"]["project_id"] == "proj-personal"
    assert cmd["args"]["content"] == "Pay Axis credit card XX13 ₹1,00,308.53"
    assert cmd["args"]["due"] == {"date": "2099-09-06"}
    assert cmd["args"]["labels"] == ["#bill"]
    assert "Due 2099-09-07" in cmd["args"]["description"] and "gmail 1a06" in cmd["args"]["description"]
    assert ref is None or isinstance(ref, str)
    # dedupe on (payee_key, due_on): second call never hits Todoist
    connector.commands.reset_mock()
    await ActivityEnvironment().run(acts.capture_due, DUE, "arshad-personal", "1a06-dup")
    connector.commands.assert_not_awaited()


@pytest.mark.asyncio
async def test_capture_due_failed_kind_and_past_due_date(db_pool, monkeypatch):
    await db_pool.execute("DELETE FROM todoist_capture_idempotency WHERE source_tag = '#bill'")
    acts, connector = _acts(db_pool)
    monkeypatch.setattr(
        "aegis.connectors.todoist.TodoistConnector.check_sync_status",
        staticmethod(lambda result, uuids: {"ok": True, "retryable": False, "rejected_retryable": False,
                                            "rejected": {}, "envelope_error": None}),
    )
    connector.commands = AsyncMock(return_value={"data": {"temp_id_mapping": {}}})
    ev = {**DUE, "kind": "failed", "payee": "Medium", "payee_key": "medium", "amount": "199.00",
          "due_on": (date.today() - timedelta(days=3)).isoformat(), "entity": "hikmah"}
    await ActivityEnvironment().run(acts.capture_due, ev, "arshad-hikmah", "m2")
    cmd = connector.commands.await_args.args[0][0]
    assert cmd["args"]["content"] == "Fix payment: Medium ₹199.00"
    assert cmd["args"]["due"] == {"date": date.today().isoformat()}
    assert "project_id" in cmd["args"]  # Inbox fallback when the entity has no project


@pytest.mark.asyncio
async def test_capture_due_ignores_non_dues(db_pool):
    acts, connector = _acts(db_pool)
    assert await ActivityEnvironment().run(acts.capture_due, {**DUE, "kind": "transaction"}, "m", "x") is None
    assert await ActivityEnvironment().run(acts.capture_due, {**DUE, "due_on": None}, "m", "x") is None
    connector.commands.assert_not_awaited()


@pytest.mark.asyncio
async def test_complete_captured_task(db_pool, monkeypatch):
    acts, connector = _acts(db_pool)
    monkeypatch.setattr(
        "aegis.connectors.todoist.TodoistConnector.check_sync_status",
        staticmethod(lambda result, uuids: {"ok": True, "retryable": False, "rejected_retryable": False,
                                            "rejected": {}, "envelope_error": None}),
    )
    assert await ActivityEnvironment().run(acts.complete_captured_task, "task-123") is True
    cmd = connector.commands.await_args.args[0][0]
    assert cmd["type"] == "item_complete" and cmd["args"]["id"] == "task-123"
    assert await ActivityEnvironment().run(acts.complete_captured_task, "item-temp") is False
    assert await ActivityEnvironment().run(acts.complete_captured_task, "") is False
```

Check `build_item_complete_command`'s exact `args` shape in `core/src/aegis/connectors/todoist.py:419` and match the assertion (`cmd["args"]["id"]` or `cmd["args"]["ids"]`). If the capture path's inbox id must exist in `settings` for the Inbox fallback, insert `todoist_managed_project_ids = {"inbox": "inbox-1"}` into the `settings` table in a fixture (see how `tests/worker/activities/test_capture*.py` does it, if such a file exists; otherwise insert directly with `INSERT INTO settings (key, value) VALUES ('todoist_managed_project_ids', '{"inbox": "inbox-1"}') ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value` and also `todoist_capture_enabled` = `true`).

```python
# tests/worker/activities/test_money_v2.py
"""MoneyActivities v2 (spec §2, §5.4, §7.1) against a temp books repo."""

from __future__ import annotations

import shutil
import subprocess
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aegis.services import books, journal_index as ji
from aegis_worker.activities.money import MoneyActivities
from temporalio.testing import ActivityEnvironment

HAS_HLEDGER = shutil.which("hledger") is not None and shutil.which("git") is not None
pytestmark = pytest.mark.skipif(not HAS_HLEDGER, reason="hledger/git not installed")

ACCOUNTS = """commodity ₹ 1,00,000.00
commodity $ 1000.00
account assets:bank:hdfc:1225
account assets:unknown
account liabilities:card:axis:1313
account expenses:unknown
account expenses:saas
account expenses:media
account expenses:hikmah:unknown
account expenses:hikmah:saas
account income:unknown
account income:hikmah:other
account equity:transfers
"""
RULES = "- match: 'stockopedia\\.com'\n  ignore: true\n- match: 'eleven labs'\n  entity: hikmah\n  account: expenses:hikmah:saas\n  payee: Eleven Labs\n"


def _repo(tmp_path: Path) -> books.BooksConfig:
    root = tmp_path / "books"
    (root / "personal").mkdir(parents=True)
    (root / "hikmah").mkdir()
    (root / "rules").mkdir()
    (root / "accounts.journal").write_text(ACCOUNTS)
    (root / "prices.journal").write_text("")
    (root / "recurring.journal").write_text("")
    (root / "personal" / "2026.journal").write_text("; p\n")
    (root / "hikmah" / "2026.journal").write_text("; h\n")
    (root / "rules" / "accounts.yaml").write_text(RULES)
    (root / "main.journal").write_text(
        "include accounts.journal\ninclude prices.journal\ninclude personal/2026.journal\n"
        "include hikmah/2026.journal\ninclude recurring.journal\n"
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"], cwd=root, check=True)
    return books.BooksConfig(path=root)


@pytest_asyncio.fixture(autouse=True)
async def _clean(db_pool):
    await db_pool.execute("DELETE FROM finance.journal_index WHERE mailbox LIKE 'v2-%'")
    await db_pool.execute("DELETE FROM finance.receipt_email WHERE message_id LIKE 'v2-%'")
    yield
    await db_pool.execute("DELETE FROM finance.journal_index WHERE mailbox LIKE 'v2-%'")
    await db_pool.execute("DELETE FROM finance.receipt_email WHERE message_id LIKE 'v2-%'")


def _act(db_pool, cfg, llm=None, capture=None) -> MoneyActivities:
    return MoneyActivities(
        db_pool=db_pool, llm=llm, delivery=None, fx_rates={}, books_cfg=cfg,
        ignored_mailboxes=frozenset({"v2-stpd"}), mailbox_entities={"v2-hikmah": "hikmah"},
        capture=capture,
    )


def _receipt(mailbox="v2-personal", sender="HDFC Bank InstaAlerts <alerts@hdfcbank.bank.in>",
             subject="UPI txn", body="", message_id="v2-m1", rid="00000000-0000-0000-0000-000000000001"):
    return {"id": rid, "account": mailbox, "message_id": message_id, "sender": sender,
            "subject": subject, "body_plain": body, "received_at": "2026-09-02T10:00:00+00:00"}


HDFC_BODY = ("Rs.10.00 is debited from your account ending 1225 towards VPA q2@ybl (Jai shree nakoda) "
             "on 02-09-26. UPI transaction reference no.: 128932002048.")


@pytest.mark.asyncio
async def test_parse_uses_bank_parser_then_rules_then_defaults(db_pool, tmp_path):
    act = _act(db_pool, _repo(tmp_path))
    ev = await ActivityEnvironment().run(act.parse_money_email, _receipt(body=HDFC_BODY))
    assert ev["parser"] == "hdfc_upi" and ev["entity"] == "personal"
    assert ev["account"] == "expenses:unknown" and ev["payee_key"] == "jai shree nakoda"
    assert ev["occurred_on"] == "2026-09-02"


@pytest.mark.asyncio
async def test_parse_ignored_mailbox_and_ignore_rule(db_pool, tmp_path):
    act = _act(db_pool, _repo(tmp_path))
    ev = await ActivityEnvironment().run(act.parse_money_email, _receipt(mailbox="v2-stpd", body=HDFC_BODY))
    assert ev["kind"] == "ignore" and ev["entity"] == "none" and ev["parser"] == "mailbox"
    stripe = _receipt(
        sender='"LSEG Billing via Data" <data@stockopedia.com>', subject="New Invoice",
        body="Receipt from LSEG £22269.97 Paid August 2, 2026 Payment method - 1313",
    )
    llm = AsyncMock()
    llm.extract_money_batch = AsyncMock(return_value=[{
        "kind": "transaction", "direction": "out", "amount": "22269.97", "currency": "GBP",
        "payee": "LSEG Billing", "payee_key": "lseg billing", "channel": "receipt", "confidence": 0.9,
        "parser": "llm", "source_class": "receipt", "entity": "personal",
    }])
    act = _act(db_pool, _repo(tmp_path / "b"), llm=llm)
    ev = await ActivityEnvironment().run(act.parse_money_email, stripe)
    assert ev["kind"] == "ignore" and ev["entity"] == "none" and ev["parser"] == "llm+rule"


@pytest.mark.asyncio
async def test_parse_llm_low_confidence_lands_in_unknown_and_rule_sets_entity(db_pool, tmp_path):
    llm = AsyncMock()
    llm.extract_money_batch = AsyncMock(return_value=[{
        "kind": "transaction", "direction": "out", "amount": "1936.00", "currency": "INR",
        "payee": "Eleven Labs Inc.", "payee_key": "eleven labs inc", "channel": "receipt",
        "category": "saas", "confidence": 0.5, "parser": "llm", "source_class": "receipt",
        "entity": "personal", "occurred_on": "2026-08-25",
    }])
    act = _act(db_pool, _repo(tmp_path), llm=llm)
    ev = await ActivityEnvironment().run(
        act.parse_money_email, _receipt(sender="invoice+statements@stripe.com", subject="Receipt", body="x")
    )
    # the rule wins over the low-confidence unknown: entity hikmah, account from the rule
    assert ev["entity"] == "hikmah" and ev["account"] == "expenses:hikmah:saas" and ev["payee"] == "Eleven Labs"
    llm.extract_money_batch = AsyncMock(return_value=[{
        "kind": "transaction", "direction": "out", "amount": "50.00", "currency": "INR",
        "payee": "Some Shop", "payee_key": "some shop", "channel": "other", "category": "shopping",
        "confidence": 0.5, "parser": "llm", "source_class": "other", "entity": "personal",
    }])
    ev = await ActivityEnvironment().run(act.parse_money_email, _receipt(sender="x@y.com", subject="s", body="b"))
    assert ev["account"] == "expenses:unknown" and ev["occurred_on"] == "2026-09-02"


@pytest.mark.asyncio
async def test_parse_flags_llm_failure(db_pool, tmp_path):
    llm = AsyncMock()
    llm.extract_money_batch = AsyncMock(return_value=[{"kind": "ignore", "parser": "llm", "_parse_failed": True}])
    act = _act(db_pool, _repo(tmp_path), llm=llm)
    ev = await ActivityEnvironment().run(act.parse_money_email, _receipt(sender="a@b.c", subject="s", body="b"))
    assert ev.get("_parse_failed") is True


def _bank_event(**kw):
    base = {"kind": "transaction", "direction": "out", "amount": "10.00", "currency": "INR",
            "payee": "Jai shree nakoda", "payee_key": "jai shree nakoda", "channel": "upi",
            "instrument": "hdfc-1225", "occurred_on": "2026-09-02", "entity": "personal",
            "account": "expenses:unknown", "parser": "hdfc_upi", "confidence": 1.0, "source_class": "bank",
            "ref": "128932002048"}
    base.update(kw)
    return base


@pytest.mark.asyncio
async def test_post_then_receipt_links_and_enriches(db_pool, tmp_path):
    cfg = _repo(tmp_path)
    act = _act(db_pool, cfg)
    r = await ActivityEnvironment().run(act.post_money_event, "rid1", "v2-personal", "m-bank", _bank_event())
    assert r["status"] == "posted" and r["journal_file"] == "personal/2026.journal"
    receipt = _bank_event(payee="Apple Music Individual", payee_key="apple music individual",
                          channel="receipt", instrument=None, account="expenses:media",
                          parser="apple_receipt", source_class="receipt", occurred_on="2026-09-03", ref=None)
    r2 = await ActivityEnvironment().run(act.post_money_event, "rid2", "v2-personal", "m-rcpt", receipt)
    assert r2["status"] == "linked" and r2["linked"] == "v2-personal/m-bank" and r2["journal_file"] is None
    text = (cfg.path / "personal" / "2026.journal").read_text()
    assert "2026-09-02 * Apple Music Individual" in text
    assert "    expenses:media                          ₹10.00\n    assets:bank:hdfc:1225\n" in text
    assert "receipt: v2-personal/m-rcpt" in text
    assert text.count("; msgid:") == 1
    bank = await ji.get(db_pool, "v2-personal/m-bank")
    assert bank["linked_message_id"] == "v2-personal/m-rcpt" and bank["account"] == "expenses:media"
    assert bank["payee"] == "Apple Music Individual"


@pytest.mark.asyncio
async def test_receipt_then_bank_links_and_fixes_instrument(db_pool, tmp_path):
    cfg = _repo(tmp_path)
    act = _act(db_pool, cfg)
    receipt = _bank_event(payee="Eleven Labs", payee_key="eleven labs", channel="receipt", instrument="card-1313",
                          account="expenses:saas", parser="stripe_receipt", source_class="receipt", ref=None)
    r = await ActivityEnvironment().run(act.post_money_event, "rid1", "v2-personal", "m-rcpt", receipt)
    assert r["status"] == "posted"
    text = (cfg.path / "personal" / "2026.journal").read_text()
    assert "    liabilities:card:axis:1313\n" in text
    bank = _bank_event(payee="ELEVENLABS", payee_key="elevenlabs", channel="card", instrument="axis-cc-1313",
                       parser="axis_card_spend", occurred_on="2026-09-03")
    r2 = await ActivityEnvironment().run(act.post_money_event, "rid2", "v2-personal", "m-bank", bank)
    assert r2["status"] == "linked" and r2["linked"] == "v2-personal/m-rcpt"
    text = (cfg.path / "personal" / "2026.journal").read_text()
    assert text.count("; msgid:") == 1 and "bank: v2-personal/m-bank" in text
    assert "2026-09-02 * Eleven Labs" in text


@pytest.mark.asyncio
async def test_post_is_idempotent_and_indexes_dues_and_info(db_pool, tmp_path):
    cfg = _repo(tmp_path)
    act = _act(db_pool, cfg)
    await ActivityEnvironment().run(act.post_money_event, "rid1", "v2-personal", "m-bank", _bank_event())
    r = await ActivityEnvironment().run(act.post_money_event, "rid1", "v2-personal", "m-bank", _bank_event())
    assert r["status"] == "posted"
    assert (cfg.path / "personal" / "2026.journal").read_text().count("; msgid:") == 1
    due = _bank_event(kind="due", due_on="2026-09-07", channel="statement", payee="Axis credit card XX13",
                      payee_key="axis credit card xx13", amount="100308.53")
    r = await ActivityEnvironment().run(act.post_money_event, "rid3", "v2-personal", "m-due", due, "task-9")
    assert r["status"] == "indexed" and (await ji.get(db_pool, "v2-personal/m-due"))["todoist_ref"] == "task-9"
    info = _bank_event(kind="info", amount=None, currency=None, direction=None)
    r = await ActivityEnvironment().run(act.post_money_event, "rid4", "v2-personal", "m-info", info)
    assert r["status"] == "indexed"


@pytest.mark.asyncio
async def test_payment_closes_its_open_due(db_pool, tmp_path):
    cfg = _repo(tmp_path)
    capture = AsyncMock()
    capture.complete_captured_task = AsyncMock(return_value=True)
    act = _act(db_pool, cfg, capture=capture)
    due = _bank_event(kind="due", due_on="2026-09-07", channel="statement", payee="Axis credit card XX13",
                      payee_key="axis credit card xx13", amount="100308.53")
    await ActivityEnvironment().run(act.post_money_event, "rid3", "v2-personal", "m-due", due, "task-9")
    paid = _bank_event(payee="Axis credit card XX13", payee_key="axis credit card xx13", amount="100308.53",
                       channel="imps", occurred_on="2026-09-06", account="equity:transfers")
    r = await ActivityEnvironment().run(act.post_money_event, "rid5", "v2-personal", "m-paid", paid)
    assert r["closed_due"] == "v2-personal/m-due"
    capture.complete_captured_task.assert_awaited_once_with("task-9")
    assert (await ji.get(db_pool, "v2-personal/m-due"))["linked_message_id"] == "v2-personal/m-paid"


@pytest.mark.asyncio
async def test_books_disabled_still_indexes(db_pool, tmp_path):
    act = _act(db_pool, books.BooksConfig(path=tmp_path / "nowhere"))
    r = await ActivityEnvironment().run(act.post_money_event, "rid1", "v2-personal", "m-bank", _bank_event())
    assert r["status"] == "books_disabled" and (await ji.get(db_pool, "v2-personal/m-bank")) is not None


@pytest.mark.asyncio
async def test_store_money_result_marks_version_2(db_pool, tmp_path):
    act = _act(db_pool, _repo(tmp_path))
    async with db_pool.acquire() as conn:
        rid = await conn.fetchval(
            "INSERT INTO finance.receipt_email (message_id, account, sender, subject, received_at, parsed) "
            "VALUES ('v2-store', 'v2-personal', 's', 'j', now(), '{\"body_text\": \"b\"}') RETURNING id"
        )
    await ActivityEnvironment().run(act.store_money_result, str(rid), _bank_event(), "personal/2026.journal")
    parsed = await db_pool.fetchval("SELECT parsed FROM finance.receipt_email WHERE id = $1", rid)
    assert parsed["version"] == 2 and parsed["body_text"] == "b" and parsed["journal_file"] == "personal/2026.journal"
    assert parsed["event"]["payee"] == "Jai shree nakoda"
```

Add to `tests/worker/activities/test_money_store_receipt.py`:

```python
@pytest.mark.asyncio
async def test_store_receipt_email_returns_existing_id_for_v1_rows(db_pool):
    act = _make_act(db_pool)
    msg = {"id": "rt-v1", "sender": "a@b", "subject": "s", "internal_date_ms": 1700000000000}
    first = await act.store_receipt_email(msg, "sebas")
    assert first
    assert await act.store_receipt_email(msg, "sebas") == first  # no version → re-process
    await db_pool.execute(
        "UPDATE finance.receipt_email SET parsed = parsed || '{\"version\": 2}' WHERE message_id = 'rt-v1'"
    )
    assert await act.store_receipt_email(msg, "sebas") == ""  # v2 → duplicate


@pytest.mark.asyncio
async def test_find_stuck_receipts_selects_rows_below_version_2(db_pool):
    act = _make_act(db_pool)
    async with db_pool.acquire() as conn:
        v1 = await _insert_receipt_email(conn, message_id="stuck-v1", parsed={"is_receipt": True}, received_days_ago=_ANCIENT_DAYS)
        await _insert_receipt_email(conn, message_id="stuck-v2", parsed={"version": 2}, received_days_ago=_ANCIENT_DAYS)
    ids = await act.find_stuck_receipts(limit=50, older_than_days=1)
    assert v1 in ids and all(i != "stuck-v2" for i in ids)
```

Existing `find_stuck_receipts` tests that assert on the `is_receipt` key (`test_find_stuck_receipts_selects_missing_is_receipt_key`) must be updated to the version-2 rule: a row with `parsed={"is_receipt": True}` is now stuck too. Update their expectations rather than deleting them.

- [ ] **Step 2: Run to verify failure** — `AttributeError` on the new activities.

- [ ] **Step 3: Implement `capture.py`**

Rename the body of `capture_to_inbox` into `async def _capture(self, source_tag, external_id, title, description, labels, project_id, due_date) -> str | None` with two changes: `inbox_id` lookup only when `project_id is None` (then `project_id = inbox_id`; if neither, the existing `capture_skipped_no_inbox_id` warning and `return None`), and `build_create_item_command(project_id=project_id, content=title[:120], description=description, labels=item_labels, due_date=due_date)`. Then:

```python
    @activity.defn
    async def capture_to_inbox(self, source_tag, external_id, title, description=None, extra_labels=None):
        """Idempotent Inbox capture. See module docstring."""
        return await self._capture(source_tag, external_id, title, description, extra_labels, None, None)

    @activity.defn
    async def capture_task(
        self,
        source_tag: str,
        external_id: str,
        title: str,
        description: str | None = None,
        labels: list[str] | None = None,
        project_id: str | None = None,
        due_date: str | None = None,
    ) -> str | None:
        """Idempotent capture into any project with an optional due date (spec §7.1)."""
        return await self._capture(source_tag, external_id, title, description, labels, project_id, due_date)

    @activity.defn
    async def capture_due(self, event: dict, mailbox: str, message_id: str) -> str | None:
        """A bill, statement, autopay reminder or failed payment → one dated task (spec §7.1)."""
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        from aegis.api.models.money import MoneyEvent
        from aegis.services.money_format import fmt_money

        ev = MoneyEvent(**{k: v for k, v in event.items() if not k.startswith("_")})
        if ev.kind not in ("due", "failed") or ev.due_on is None or ev.amount is None:
            return None
        today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        due = max(ev.due_on - timedelta(days=1), today)
        prefix = "Fix payment:" if ev.kind == "failed" else "Pay"
        title = f"{prefix} {ev.payee} {fmt_money(ev.amount, ev.currency or '')}"
        description = (
            f"Due {ev.due_on.isoformat()}\n{ev.channel} · {ev.instrument or '-'}\n"
            f"{mailbox} · gmail {message_id}"
        )
        return await self._capture(
            "#bill", f"{ev.payee_key}:{ev.due_on.isoformat()}", title, description, None,
            self.todoist_projects.get(ev.entity), due.isoformat(),
        )

    @activity.defn
    async def complete_captured_task(self, task_ref: str) -> bool:
        """Close a captured task (a due that got paid). False for an unresolved temp ref."""
        if not task_ref or task_ref.startswith("item-") or self.connector is None:
            return False
        from aegis.connectors.todoist import TodoistConnector

        cmd = TodoistConnector.build_item_complete_command(task_ref)
        result = await self.connector.commands([cmd])
        status = TodoistConnector.check_sync_status(result, [cmd["uuid"]])
        if status["ok"]:
            return True
        if (status["retryable"] or status["rejected_retryable"]) and self.db_pool is not None:
            async with self.db_pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO todoist_outbox (temp_id, command, status) VALUES ($1, $2, 'pending') "
                    "ON CONFLICT (temp_id) DO NOTHING",
                    cmd["uuid"], cmd,
                )
            return True
        activity.logger.warning("complete_captured_task_rejected ref=%s err=%s", task_ref, status["envelope_error"])
        return False
```

(`todoist_outbox.temp_id` for a complete command: use `cmd["uuid"]` since the command has no temp_id; check the outbox drain tolerates that — `drain_outbox` replays `command` as stored.)

- [ ] **Step 4: Implement `money.py`**

Imports to add: `from datetime import datetime`, `from zoneinfo import ZoneInfo`, `from aegis.api.models.money import MoneyEvent, payee_key`, `from aegis.services import books, journal_index as ji`, `from aegis.services.bank_parsers import parse_any`, `from aegis.services.books import UNKNOWN, account_for, instrument_account`.

`store_receipt_email`: after the `INSERT … RETURNING id` returns no row, add:

```python
        if row is None:
            async with self.db_pool.acquire() as conn:
                existing = await conn.fetchval(
                    "SELECT id FROM finance.receipt_email WHERE message_id = $1 "
                    "  AND COALESCE((parsed->>'version')::int, 0) < 2",
                    msg.get("id", ""),
                )
            return str(existing) if existing else ""
        return str(row["id"])
```

`find_stuck_receipts`: predicate becomes `"WHERE COALESCE((parsed->>'version')::int, 0) < 2 "`; update the docstring (v2: anything not yet through the books pipeline).

New activities (place after `classify_and_extract`):

```python
    def _rules(self) -> list[dict]:
        if self.books_cfg is None:
            return []
        return books.load_rules(self.books_cfg.path / "rules" / "accounts.yaml")

    @activity.defn
    async def parse_money_email(self, receipt: dict) -> dict:
        """One MoneyEvent for one stored email (spec §2 step 3): deterministic
        parsers, else the LLM on the full body; then mailbox entity, rules,
        account fallback, date fallback."""
        mailbox = receipt.get("account", "")
        if mailbox in self.ignored_mailboxes:
            return MoneyEvent(kind="ignore", entity="none", parser="mailbox").model_dump(mode="json")
        sender, subject = receipt.get("sender", ""), receipt.get("subject", "")
        body = receipt.get("body_plain") or ""
        ev = parse_any(sender, subject, body)
        if ev is None:
            system_prompt = None
            if self.agent_id:
                from aegis.services.personalities import get_personality

                system_prompt = _format_agent_persona(await get_personality(self.db_pool, self.agent_id))
            out = await self.llm.extract_money_batch(
                [receipt], model=self.extract_model, system_prompt=system_prompt,
                db_pool=self.db_pool, agent_id=self.agent_id or None,
            )
            item = out[0] if out else {"_parse_failed": True}
            if item.get("_parse_failed"):
                return {"kind": "ignore", "parser": "llm", "_parse_failed": True}
            ev = MoneyEvent(**{k: v for k, v in item.items() if not k.startswith("_")})

        if ev.entity != "hikmah":
            ev.entity = self.mailbox_entities.get(mailbox, "personal")  # type: ignore[assignment]
        rule = books.apply_rules(self._rules(), sender, ev.payee)
        if rule:
            if rule.get("ignore"):
                ev.kind, ev.entity, ev.parser = "ignore", "none", f"{ev.parser}+rule"
                ev.payee_key = payee_key(ev.payee)
                return ev.model_dump(mode="json")
            if rule.get("entity") in ("personal", "hikmah"):
                ev.entity = rule["entity"]
            if rule.get("payee"):
                ev.payee = str(rule["payee"])
            if rule.get("account"):
                ev.account = str(rule["account"])
        if not ev.account and ev.kind == "transaction":
            side = "in" if ev.direction == "in" else "out"
            low = ev.parser == "llm" and ev.confidence < 0.8
            ev.account = (
                UNKNOWN["hikmah" if ev.entity == "hikmah" else "personal"][side]
                if low else account_for(ev.category, ev.direction, ev.entity)
            )
        if ev.occurred_on is None and ev.kind == "transaction" and receipt.get("received_at"):
            received = datetime.fromisoformat(receipt["received_at"])
            ev.occurred_on = received.astimezone(ZoneInfo(self.home_tz)).date()
        ev.payee_key = payee_key(ev.payee)
        return ev.model_dump(mode="json")

    @staticmethod
    def _looks_raw(payee: str) -> bool:
        p = payee or ""
        return "@" in p or p.startswith("a/c") or (p.isupper() and len(p) > 3)

    @activity.defn
    async def post_money_event(
        self, receipt_id: str, mailbox: str, message_id: str, event: dict, todoist_ref: str | None = None
    ) -> dict:
        """Route one event (spec §2 step 4, §5.4, §7.1). Transactions are
        posted or linked; everything else is indexed only."""
        ev = MoneyEvent(**{k: v for k, v in event.items() if not k.startswith("_")})
        msgid = ji.msgid_for(mailbox, message_id)
        result: dict = {"msgid": msgid, "status": "indexed", "journal_file": None, "linked": None, "closed_due": None}
        if ev.kind != "transaction" or ev.entity == "none":
            await ji.upsert(self.db_pool, msgid, mailbox, ev, todoist_ref=todoist_ref)
            return result
        cfg = self.books_cfg
        match = await ji.find_match(self.db_pool, ev, msgid)
        try:
            if cfg is None:
                raise books.BooksDisabled("no books config")
            if match is not None:
                other = match["message_id"]
                if ev.source_class == "receipt":
                    kwargs: dict = {"add_tags": {"receipt": msgid}}
                    if self._looks_raw(match["payee"] or "") and ev.payee:
                        kwargs["payee"] = ev.payee
                    if (match["account"] or "").endswith(":unknown") and ev.account and not ev.account.endswith(":unknown"):
                        kwargs["account"] = ev.account
                    await books.rewrite_event(other, cfg, **kwargs)
                    fixed = MoneyEvent(**{**match_to_event(match), **{k: v for k, v in kwargs.items() if k in ("payee", "account")}})
                    await ji.upsert(self.db_pool, other, match["mailbox"], fixed)
                else:
                    declared = await asyncio.to_thread(books._declared_accounts_sync, cfg)
                    inst = instrument_account(ev.instrument, declared)
                    try:
                        await books.rewrite_event(other, cfg, instrument_account=inst, add_tags={"bank": msgid})
                    except books.BooksCheckError:
                        await books.rewrite_event(other, cfg, add_tags={"bank": msgid})
                await ji.upsert(self.db_pool, msgid, mailbox, ev, linked=other)
                await ji.link(self.db_pool, msgid, other)
                result.update(status="linked", linked=other)
            else:
                rel = await books.post_event(ev, msgid, cfg)
                await ji.upsert(self.db_pool, msgid, mailbox, ev, journal_file=rel)
                result.update(status="posted", journal_file=rel)
        except books.BooksDisabled:
            await ji.upsert(self.db_pool, msgid, mailbox, ev)
            result["status"] = "books_disabled"
            return result

        if ev.amount is not None and ev.currency and ev.occurred_on is not None:
            due = await ji.find_open_due(self.db_pool, ev.payee_key, ev.amount, ev.currency, ev.occurred_on)
            if due is not None:
                closed = True
                if self.capture is not None:
                    closed = await self.capture.complete_captured_task(due["todoist_ref"])
                if closed:
                    await ji.link(self.db_pool, due["message_id"], msgid)
                    result["closed_due"] = due["message_id"]
        return result

    @activity.defn
    async def store_money_result(self, receipt_id: str, event: dict, journal_file: str | None) -> None:
        """Stamp the row as v2-processed (spec §2 step 5)."""
        async with self.db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE finance.receipt_email SET parsed = COALESCE(parsed, '{}'::jsonb) || $2 WHERE id = $1::uuid",
                receipt_id,
                {"version": 2, "event": {k: v for k, v in event.items() if not k.startswith("_")}, "journal_file": journal_file},
            )
```

with a module-level helper:

```python
def match_to_event(row: dict) -> dict:
    """journal_index row → MoneyEvent kwargs (for re-upserting an enriched row)."""
    keys = ("kind", "direction", "amount", "currency", "payee", "payee_key", "channel", "instrument",
            "occurred_on", "due_on", "entity", "account", "parser", "confidence", "source_class")
    return {k: row[k] for k in keys if k in row and row[k] is not None}
```

`asyncio` must be imported. Note `ji.link` after `ji.upsert(..., linked=other)`: `link` sets both sides; the upsert's `linked` is belt-and-braces for the new row.

- [ ] **Step 5: Run the tests, lint, commit**

Run: `PYTHONPATH=core/src:worker/src:comms/src .venv/bin/python -m pytest tests/worker/activities/test_money_v2.py tests/worker/activities/test_capture_due.py tests/worker/activities/test_money_store_receipt.py tests/worker/activities/test_money.py tests/worker/activities/test_money_bundle_e.py -n 4 --dist loadfile --timeout=300 -q 2>&1 | tail -5` — all passed, none of the v2 tests skipped on meem.
Falsifiability: in `post_money_event` swap the `if ev.source_class == "receipt":` branch condition to `== "bank"`, run `test_post_then_receipt_links_and_enriches`, confirm FAIL, revert.
Run: `.venv/bin/ruff check worker/src/ tests/worker/` — clean.

```bash
git add worker/src/aegis_worker/activities/money.py worker/src/aegis_worker/activities/capture.py tests/worker/activities/test_money_v2.py tests/worker/activities/test_capture_due.py tests/worker/activities/test_money_store_receipt.py
git commit -m "feat(money): v2 activities — parse, post to the books, index, dated dues"
```

### Task 9: `MoneyProcessFlow` v2 and the safety-net sweep

**Files:**
- Modify: `worker/src/aegis_worker/flows/money_process.py` (whole `run`, docstring)
- Modify: `worker/src/aegis_worker/flows/receipt_ingest.py` (`_SENDER_FILTER` → `DEFAULT_SENDER_FILTER`, `ReceiptIngestInput.sender_filter`, `_sweep_stuck_receipts` v2)
- Modify: `worker/src/aegis_worker/registry.py:461-470` (`ReceiptIngestFlow` FlowSpec maps `sender_filter`)
- Modify: `config/seed/activities.yaml:70-82` (comment + `sender_filter` left to the default)
- Test: `tests/worker/flows/test_money_process.py` (rewrite), `tests/worker/flows/test_receipt_ingest.py` (sweep tests), `tests/worker/test_registry.py` (count rows if it asserts activity/flow counts — the flow list is unchanged, so probably nothing)

**Interfaces:**
- Consumes: `parse_money_email(receipt) -> dict`, `capture_due(event, mailbox, message_id) -> str | None`, `post_money_event(receipt_id, mailbox, message_id, event, todoist_ref) -> dict`, `store_money_result(receipt_id, event, journal_file)` from Task 8; `fetch_message_body`, `store_receipt_body`, `load_receipts`, `store_receipt_email` (v2 return semantics).
- `MoneyProcessFlow.run` result: `{"status": duplicate | load_failed | body_failed | extract_failed | parse_failed | ignored | indexed | posted | linked | books_disabled, "receipt_id", "msgid", "kind"}`. `capture_due` runs only for `kind in ("due", "failed")`; its failure is logged and the event is still indexed.
- `ReceiptIngestInput.sender_filter: str = DEFAULT_SENDER_FILTER`; `query` = `f"{sender_filter} {query_window}"`. The default filter is the v1 list plus the section-3 senders:

```python
DEFAULT_SENDER_FILTER = (
    "(from:billing@ OR from:receipts@ OR from:no-reply@stripe.com OR from:invoice+statements "
    "OR from:*@amazon.com OR from:*@razorpay.com OR from:*@vercel.com "
    "OR from:alerts@hdfcbank.bank.in OR from:alerts@axis.bank.in OR from:cc.statements@axis.bank.in "
    "OR from:eforexservices@axis.bank.in OR from:alerts@nkgsb-bank.com "
    "OR from:google-pay-noreply@google.com OR from:payments-noreply@google.com "
    "OR from:googleplay-noreply@google.com OR from:no_reply@email.apple.com "
    "OR from:do_not_reply@email.apple.com OR from:ebill@airtel.com OR from:update@airtel.com "
    "OR from:invoicing@aws.com OR from:no-reply@amazonaws.com OR from:noreply@github.com "
    "OR from:notify.cloudflare.com OR from:donotreply@intechonline.net OR from:no-reply@amazonpay.in)"
)
```

- [ ] **Step 1: Rewrite the flow tests**

Replace `tests/worker/flows/test_money_process.py` wholesale:

```python
"""MoneyProcessFlow v2 — store → body → parse → route → index (spec §2)."""

from __future__ import annotations

import pytest
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from aegis_worker.flows.money_process import MoneyProcessFlow, MoneyProcessInput

_calls: dict[str, list] = {k: [] for k in ("store", "body", "store_body", "load", "parse", "due", "post", "result")}


def _reset() -> None:
    for v in _calls.values():
        v.clear()


_MSG = {"id": "gmail-msg-1", "sender": "alerts@hdfcbank.bank.in", "subject": "UPI txn", "thread_id": "t1",
        "to": "", "date": "", "snippet": "Rs.10.00 is debited", "internal_date_ms": 1700000000000}

_TXN = {"kind": "transaction", "direction": "out", "amount": "10.00", "currency": "INR", "payee": "Shop",
        "payee_key": "shop", "channel": "upi", "instrument": "hdfc-1225", "occurred_on": "2026-09-02",
        "entity": "personal", "account": "expenses:unknown", "parser": "hdfc_upi", "confidence": 1.0,
        "source_class": "bank"}
_DUE = {**_TXN, "kind": "due", "due_on": "2026-09-07", "channel": "statement", "parser": "axis_cc_statement"}
_IGN = {"kind": "ignore", "entity": "none", "parser": "mailbox", "payee": "", "payee_key": "",
        "channel": "other", "confidence": 1.0, "source_class": "other"}


@activity.defn(name="store_receipt_email")
async def stub_store(msg: dict, account: str) -> str:
    _calls["store"].append((msg["id"], account))
    return f"uid-{msg['id']}"


@activity.defn(name="store_receipt_email")
async def stub_store_dup(msg: dict, account: str) -> str:
    return ""


@activity.defn(name="fetch_message_body")
async def stub_body(account_label: str, message_id: str, max_chars: int = 6000) -> str:
    _calls["body"].append((account_label, message_id))
    return "full body"


@activity.defn(name="store_receipt_body")
async def stub_store_body(receipt_id: str, body_text: str) -> None:
    _calls["store_body"].append((receipt_id, body_text))


@activity.defn(name="load_receipts")
async def stub_load(ids: list[str]) -> list[dict]:
    _calls["load"].append(list(ids))
    return [{"id": i, "account": "user-personal", "message_id": i.replace("uid-", ""), "sender": _MSG["sender"],
             "subject": _MSG["subject"], "body_plain": "full body", "received_at": ""} for i in ids]


def _parser(event: dict):
    @activity.defn(name="parse_money_email")
    async def stub_parse(receipt: dict) -> dict:
        _calls["parse"].append(receipt["id"])
        return dict(event)
    return stub_parse


@activity.defn(name="parse_money_email")
async def stub_parse_boom(receipt: dict) -> dict:
    raise RuntimeError("llm down")


@activity.defn(name="capture_due")
async def stub_due(event: dict, mailbox: str, message_id: str) -> str | None:
    _calls["due"].append((event["kind"], mailbox, message_id))
    return "task-42"


@activity.defn(name="capture_due")
async def stub_due_boom(event: dict, mailbox: str, message_id: str) -> str | None:
    raise RuntimeError("todoist down")


@activity.defn(name="post_money_event")
async def stub_post(receipt_id: str, mailbox: str, message_id: str, event: dict, todoist_ref: str | None = None) -> dict:
    _calls["post"].append((receipt_id, mailbox, message_id, event["kind"], todoist_ref))
    status = "indexed" if event["kind"] != "transaction" else "posted"
    return {"msgid": f"{mailbox}/{message_id}", "status": status,
            "journal_file": "personal/2026.journal" if status == "posted" else None, "linked": None, "closed_due": None}


@activity.defn(name="store_money_result")
async def stub_result(receipt_id: str, event: dict, journal_file: str | None) -> None:
    _calls["result"].append((receipt_id, event["kind"], journal_file))


def _stubs(parse=None, store=stub_store, due=stub_due):
    return [store, stub_body, stub_store_body, stub_load, parse or _parser(_TXN), due, stub_post, stub_result]


async def _run(stubs, wid: str) -> dict:
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(env.client, task_queue="tq", workflows=[MoneyProcessFlow], activities=stubs),
    ):
        return await env.client.execute_workflow(
            MoneyProcessFlow.run,
            MoneyProcessInput(agent_id="maou", msg=_MSG, account_label="user-personal"),
            id=wid, task_queue="tq",
        )


@pytest.mark.asyncio
async def test_transaction_is_posted_and_stamped():
    _reset()
    result = await _run(_stubs(), "mp-txn")
    assert result == {"status": "posted", "receipt_id": "uid-gmail-msg-1", "msgid": "user-personal/gmail-msg-1", "kind": "transaction"}
    assert _calls["body"] == [("user-personal", "gmail-msg-1")]
    assert _calls["store_body"] == [("uid-gmail-msg-1", "full body")]
    assert _calls["parse"] == ["uid-gmail-msg-1"]
    assert _calls["due"] == []
    assert _calls["post"] == [("uid-gmail-msg-1", "user-personal", "gmail-msg-1", "transaction", None)]
    assert _calls["result"] == [("uid-gmail-msg-1", "transaction", "personal/2026.journal")]


@pytest.mark.asyncio
async def test_due_captures_a_task_then_indexes_with_the_ref():
    _reset()
    result = await _run(_stubs(parse=_parser(_DUE)), "mp-due")
    assert result["status"] == "indexed" and result["kind"] == "due"
    assert _calls["due"] == [("due", "user-personal", "gmail-msg-1")]
    assert _calls["post"][0][4] == "task-42"
    assert _calls["result"] == [("uid-gmail-msg-1", "due", None)]


@pytest.mark.asyncio
async def test_capture_failure_still_indexes():
    _reset()
    result = await _run(_stubs(parse=_parser(_DUE), due=stub_due_boom), "mp-due-fail")
    assert result["status"] == "indexed"
    assert _calls["post"][0][4] is None and len(_calls["result"]) == 1


@pytest.mark.asyncio
async def test_ignore_is_indexed_and_reported_as_ignored():
    _reset()
    result = await _run(_stubs(parse=_parser(_IGN)), "mp-ign")
    assert result["status"] == "ignored" and result["kind"] == "ignore"
    assert _calls["post"] and _calls["result"]


@pytest.mark.asyncio
async def test_duplicate_short_circuits():
    _reset()
    result = await _run(_stubs(store=stub_store_dup), "mp-dup")
    assert result["status"] == "duplicate"
    assert _calls["body"] == [] and _calls["post"] == []


@pytest.mark.asyncio
async def test_parse_failure_leaves_row_unstamped():
    _reset()
    result = await _run(_stubs(parse=_parser({**_IGN, "_parse_failed": True})), "mp-pf")
    assert result["status"] == "parse_failed"
    assert _calls["post"] == [] and _calls["result"] == []


@pytest.mark.asyncio
async def test_parser_exception_is_extract_failed():
    _reset()
    result = await _run(_stubs(parse=stub_parse_boom), "mp-boom")
    assert result["status"] == "extract_failed"
    assert _calls["result"] == []
```

(`stub_parse_boom` raises on every attempt; `ACT_RETRY` retries three times, which the time-skipping environment completes quickly. If it takes more than a few seconds, wrap the stub in a `RetryPolicy(maximum_attempts=1)`-free way by making it raise `temporalio.exceptions.ApplicationError("llm down", non_retryable=True)`.)

In `tests/worker/flows/test_receipt_ingest.py`, the two sweep tests (`test_receipt_flow_sweeps_stuck_receipts`, `test_receipt_flow_sweep_leaves_still_failing_rows_unparsed`): replace their `classify_and_extract`/`upsert_charges` stubs with `parse_money_email` / `post_money_event` / `store_money_result` stubs of the shapes above (keep the `fetch_message_body`/`store_receipt_body` stubs PR1 added), and assert: first test — `post` called once with kind `transaction`, `result` called once; second test — the parse stub returns `{"kind": "ignore", "parser": "llm", "_parse_failed": True}` and neither `post` nor `result` is called. Also add:

```python
def test_default_sender_filter_includes_the_bank_senders():
    from aegis_worker.flows.receipt_ingest import DEFAULT_SENDER_FILTER, ReceiptIngestInput

    assert "alerts@hdfcbank.bank.in" in DEFAULT_SENDER_FILTER
    assert "alerts@axis.bank.in" in DEFAULT_SENDER_FILTER
    inp = ReceiptIngestInput(query_window="after:2026/06/30", sender_filter="(from:x@y.z)")
    assert inp.query == "(from:x@y.z) after:2026/06/30"
    assert ReceiptIngestInput().query.startswith(DEFAULT_SENDER_FILTER)
```

- [ ] **Step 2: Run to verify failure** — the v2 flow tests fail on missing activities/status values.

- [ ] **Step 3: Rewrite `MoneyProcessFlow.run`**

```python
@workflow.defn(name="MoneyProcessFlow")
class MoneyProcessFlow:
    @workflow.run
    async def run(self, input: MoneyProcessInput) -> dict:
        msg_id = input.msg.get("id", "")
        receipt_id = await workflow.execute_activity(
            "store_receipt_email", args=[input.msg, input.account_label],
            start_to_close_timeout=_ACT_TIMEOUT, retry_policy=ACT_RETRY,
        )
        if not receipt_id:
            return {"status": "duplicate", "message_id": msg_id}
        out = {"receipt_id": receipt_id, "msgid": f"{input.account_label}/{msg_id}", "kind": None}

        body = await workflow.execute_activity(
            "fetch_message_body", args=[input.account_label, msg_id],
            start_to_close_timeout=_ACT_TIMEOUT, retry_policy=ACT_RETRY,
        )
        if body:
            await workflow.execute_activity(
                "store_receipt_body", args=[receipt_id, body],
                start_to_close_timeout=_ACT_TIMEOUT, retry_policy=ACT_RETRY,
            )

        receipts = await workflow.execute_activity(
            "load_receipts", [receipt_id],
            start_to_close_timeout=_ACT_TIMEOUT, retry_policy=ACT_RETRY,
        )
        if not receipts:
            return {**out, "status": "load_failed"}

        try:
            event = await workflow.execute_activity(
                "parse_money_email", args=[receipts[0]],
                start_to_close_timeout=_CLASSIFY_TIMEOUT, retry_policy=ACT_RETRY,
            )
        except Exception as exc:
            # Persistent parser/LLM failure: leave the row below version 2 so
            # the weekly sweep re-drives it.
            workflow.logger.warning("money_extract_failed receipt_id=%s err=%s", receipt_id, str(exc)[:200])
            return {**out, "status": "extract_failed"}
        if not event or event.get("_parse_failed"):
            workflow.logger.warning("money_parse_failed receipt_id=%s — leaving unstamped", receipt_id)
            return {**out, "status": "parse_failed"}
        kind = event.get("kind", "ignore")
        out["kind"] = kind

        todoist_ref = None
        if kind in ("due", "failed"):
            try:
                todoist_ref = await workflow.execute_activity(
                    "capture_due", args=[event, input.account_label, msg_id],
                    start_to_close_timeout=_ACT_TIMEOUT, retry_policy=ACT_RETRY,
                )
            except Exception as exc:
                workflow.logger.warning("money_capture_due_failed receipt_id=%s err=%s", receipt_id, str(exc)[:200])

        posted = await workflow.execute_activity(
            "post_money_event", args=[receipt_id, input.account_label, msg_id, event, todoist_ref],
            start_to_close_timeout=_CLASSIFY_TIMEOUT, retry_policy=ACT_RETRY,
        )
        await workflow.execute_activity(
            "store_money_result", args=[receipt_id, event, posted.get("journal_file")],
            start_to_close_timeout=_ACT_TIMEOUT, retry_policy=ACT_RETRY,
        )
        status = "ignored" if kind == "ignore" else posted.get("status", "indexed")
        return {**out, "status": status}
```

Rewrite the module docstring to the v2 pipeline (store → body → load → parse → capture_due? → post → stamp; `ParentClosePolicy.ABANDON`; idempotent on message_id via `store_receipt_email`'s version-2 rule).

- [ ] **Step 4: Rewrite the sweep and the sender filter**

`receipt_ingest.py`: rename `_SENDER_FILTER` to `DEFAULT_SENDER_FILTER` with the value above; `ReceiptIngestInput` gains `sender_filter: str = DEFAULT_SENDER_FILTER` and `query` returns `f"{self.sender_filter} {self.query_window.strip()}"`; delete `_build_query`. `_sweep_stuck_receipts`: after `load_receipts` and the body fetch PR1 added, replace the `classify_and_extract` + `upsert_charges` calls with:

```python
                event = await workflow.execute_activity(
                    "parse_money_email", args=[receipts[0]],
                    start_to_close_timeout=_CLASSIFY_TIMEOUT, retry_policy=ACT_RETRY,
                )
                if not event or event.get("_parse_failed"):
                    continue
                todoist_ref = None
                if event.get("kind") in ("due", "failed"):
                    try:
                        todoist_ref = await workflow.execute_activity(
                            "capture_due", args=[event, receipts[0]["account"], receipts[0]["message_id"]],
                            start_to_close_timeout=_ACT_TIMEOUT, retry_policy=ACT_RETRY,
                        )
                    except Exception as exc:
                        workflow.logger.warning("receipt_sweep_capture_failed receipt_id=%s err=%s", receipt_id, str(exc)[:200])
                posted = await workflow.execute_activity(
                    "post_money_event",
                    args=[receipt_id, receipts[0]["account"], receipts[0]["message_id"], event, todoist_ref],
                    start_to_close_timeout=_CLASSIFY_TIMEOUT, retry_policy=ACT_RETRY,
                )
                await workflow.execute_activity(
                    "store_money_result", args=[receipt_id, event, posted.get("journal_file")],
                    start_to_close_timeout=_ACT_TIMEOUT, retry_policy=ACT_RETRY,
                )
                swept += 1
```

Update the module and method docstrings (the sweep re-drives every row below version 2, which is also the backfill path; `sender_filter` is configurable).

`registry.py` `ReceiptIngestFlow` FlowSpec: add `sender_filter=act["config"].get("sender_filter", DEFAULT_SENDER_FILTER)` (import it from the flow module) and `sweep_limit=int(act["config"].get("sweep_limit", 20))`.

`config/seed/activities.yaml` (`receipt-ingest-weekly` comment): "The sender filter defaults to the bank + vendor senders in the flow; override with `sender_filter`; `query_window` and `sweep_limit` are the knobs for a backfill."

- [ ] **Step 5: Run, lint, commit**

Run: `PYTHONPATH=core/src:worker/src:comms/src .venv/bin/python -m pytest tests/worker/flows/test_money_process.py tests/worker/flows/test_receipt_ingest.py tests/worker/flows/test_gmail_ingest.py tests/worker/test_registry.py tests/worker/test_schedule_sync_mappers.py -n 4 --dist loadfile --timeout=300 -q 2>&1 | tail -5` — all passed.
Run: `.venv/bin/ruff check worker/src/ tests/worker/` — clean.

```bash
git add worker/src/aegis_worker/flows/money_process.py worker/src/aegis_worker/flows/receipt_ingest.py worker/src/aegis_worker/registry.py config/seed/activities.yaml tests/worker/flows/test_money_process.py tests/worker/flows/test_receipt_ingest.py
git commit -m "feat(money): MoneyProcessFlow v2 posts to the books; sweep and sender filter for backfill"
```

---

### Task 10: Docs, env example, full runs, PR

**Files:**
- Modify: `docs/how-it-works.md` (the `MoneyProcessFlow` bullet ~line 179; the `receipt-ingest-weekly` row of the schedule table ~line 176)
- Modify: `docs/infrastructure.md` (new subsection "The books (hledger)")
- Modify: `config/.env.example` (the six `AEGIS_BOOKS_*` keys with one-line comments)
- Modify: `CLAUDE.md` Key paths bullet list: one line "**Books:** `core/src/aegis/services/books.py` + `bank_parsers.py` + `journal_index.py`; spec `docs/superpowers/specs/2026-09-05-maou-books-design.md`".

- [ ] **Step 1: Write the docs**

`docs/how-it-works.md` `MoneyProcessFlow` bullet, replace with:

```
- `MoneyProcessFlow` (Maou, child) — one money email → the books: `store_receipt_email`
  → `fetch_message_body` (full text, not the snippet) → `parse_money_email` (deterministic
  parsers for HDFC/NKGSB/Axis alerts, Google Pay bills, Stripe/Apple/Airtel receipts; the
  LLM for everything else) → by kind: a `transaction` is posted to the hledger journal in
  `hikmahtech/books` (or linked to the bank posting it duplicates), a `due`/`failed` becomes a
  dated Todoist task in the entity's project, `info`/`ignore` are indexed only. Spawned by
  `GmailIngestFlow` on `financial`/`payments` tags and by the weekly `ReceiptIngestFlow`
  safety net, which also re-drives every `receipt_email` row below `parsed.version = 2`
  (the backfill path). Design: `docs/superpowers/specs/2026-09-05-maou-books-design.md`.
```

`docs/infrastructure.md`, new subsection after the coding-host material:

```
### The books (hledger)

Maou keeps double-entry books as an hledger journal in a private git repo. Both images ship
`hledger` 1.52.3 and `git`; the working copy lives under the shared `aegis_config` volume at
`AEGIS_BOOKS_PATH` (default `/app/config/books`), serialised by a file lock, so core (tools)
and worker (flows) write the same checkout.

Configure on the admin **Integrations** page, group *Books* (DB-configured; core + worker
restart to apply):

| key | value |
|---|---|
| `books_repo_url` | `git@github.com:<org>/books.git` — empty disables posting (events are still indexed) |
| `books_deploy_key` | the private half of an ed25519 deploy key with write access (PEM or base64) |
| `books_ignored_mailboxes` | comma-separated mailbox labels whose money is not yours |
| `books_mailbox_entities` | `label=personal|hikmah,...`; unlisted mailboxes are `personal` |
| `books_todoist_projects` | `personal=<project id>,hikmah=<project id>` for dated dues; unset = Inbox |

Deploy key: `ssh-keygen -t ed25519 -N "" -f books_deploy_key -C aegis-books`, add the `.pub`
as a deploy key with write access (`gh api repos/<org>/books/keys -f title=aegis -f key="$(cat books_deploy_key.pub)" -F read_only=false`),
paste the private key into the setting, restart. At boot the key is written to
`<gmail_token_dir>/books_deploy_key` (0600); it is never logged.

Backfill: trigger `receipt-ingest-weekly` once with config
`{"query_window": "after:<date>", "max_per_account": 600, "sweep_limit": 500}`; every stored
receipt row below `parsed.version = 2` is re-driven through the v2 pipeline and new mail
from the bank senders is fetched.
```

`config/.env.example`:

```
# The books (Maou's hledger journal). Prefer the admin Integrations page (DB-configured).
AEGIS_BOOKS_PATH=/app/config/books
AEGIS_BOOKS_REPO_URL=            # git@github.com:org/books.git; empty = posting disabled
AEGIS_BOOKS_DEPLOY_KEY=          # private ed25519 deploy key (PEM or base64); never commit a real one
AEGIS_BOOKS_IGNORED_MAILBOXES=   # comma-separated mailbox labels whose money is not yours
AEGIS_BOOKS_MAILBOX_ENTITIES=    # label=personal|hikmah,...
AEGIS_BOOKS_TODOIST_PROJECTS=    # personal=<id>,hikmah=<id>
```

- [ ] **Step 2: Full per-package runs and lint**

```bash
PYTHONPATH=core/src:worker/src:comms/src .venv/bin/python -m pytest tests/core/ -n 8 --dist loadfile --timeout=300 2>&1 | tee logs/test-core.log | tail -3
PYTHONPATH=core/src:worker/src:comms/src .venv/bin/python -m pytest tests/worker/ -n 8 --dist loadfile --timeout=300 2>&1 | tee logs/test-worker.log | tail -3
PYTHONPATH=core/src:worker/src:comms/src .venv/bin/python -m pytest tests/comms/ -n 8 --dist loadfile --timeout=300 2>&1 | tee logs/test-comms.log | tail -3
.venv/bin/ruff check core/src/ tests/core/ && .venv/bin/ruff check worker/src/ tests/worker/ && .venv/bin/ruff check comms/src/ tests/comms/
```
Expected: three green runs (sequential, never concurrent), ruff clean. Fix any regression in the package it belongs to before committing.

- [ ] **Step 3: Commit and open the PR**

```bash
git add docs/how-it-works.md docs/infrastructure.md config/.env.example CLAUDE.md
git commit -m "docs(money): the books — configuration, backfill and pipeline"
git push -u origin <branch>
gh pr create --title "feat(money): the books — hledger journal, bank parsers, dated dues (books PR2)" --body-file /tmp/claude-1000/-home-arshad-Workspace-hikmah-aegis/4bab0db5-7fb2-462d-91e9-41a63ca50390/scratchpad/pr2-body.md
```

The body file: what changed (Tasks 1–9 in one bullet each), the spec path, "PR2 of 3", the rollout steps (deploy key, integration keys, projects, backfill trigger), and the three test summary lines.

