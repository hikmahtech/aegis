# Maou keeps the books: hledger-backed finance lane

**Date:** 2026-09-05
**Status:** approved design, not yet implemented
**Books repo:** `hikmahtech/books` (private, created 2026-09-05, cloned at
`~/Workspace/hikmah/books` on meem)

## Problem

Maou is a subscription tracker that reads the first 200 characters of each
email. Measured on prod on 2026-09-05 (data since 2026-07-01):

1. **The extractor never sees the email.** `fetch_emails` pulls each message
   with `format=full`, then keeps only `snippet[:500]`; `store_receipt_email`
   stores that, and `classify_and_extract` reads it back as `body_plain`. Of 61
   emails the LLM judged to be receipts, 30 have an amount. 19 of the 35
   `recurring_charge` rows carry `amount_cents = 0`.
2. **The richest money stream is thrown away on purpose.** `bank_alert_senders`
   (Axis, HDFC, ICICI, SBI, Kotak) stops bank alerts from minting fake
   subscriptions, which is right for subscriptions and wrong for a finance
   agent. In the 30 days to 2026-09-05 the personal mailbox alone received 46
   HDFC UPI debit alerts, IMPS transfers, NKGSB UPI credits, Axis card spends,
   an Axis credit card statement and the GBP inward remittance from
   Stockopedia to Hikmah. None of it is recorded anywhere.
3. **Nothing dated reaches the user.** Open in the inbox right now: an Axis
   credit card statement of ₹1,00,308.53 due 07-09-2026, advance tax 2nd
   instalment due 15-09-2026 (from the CA), a Medium subscription payment
   declined with fix-by 15-09-2026, an MSEDCL bill for "Suncity 501" due
   15-09-2026, an AWS "past due". AEGIS created a Todoist task for none.
4. **What it does send is noise.** 73 `Anomaly: ? Apple`-style Inbox tasks in
   nine weeks (most with no amount; Sebas parks them as `@maou @next
   @waiting`), 27 Slack renewal pings in 30 days about the same four charges,
   the same "what is Mahavitaran for?" curiosity card six times (vendor-name
   variants make different novelty keys, and archived cards do not count as
   asked).
5. **Paise leak.** `MoneyHygieneDailyFlow._scan_renewals` puts the raw
   `amount_cents` column in the task title: "Renewal in 18.6 days: Airtel
   Xstream Fiber (530646 INR)". The user pointed this out on 2026-08-18.
6. **The monthly digest is wrong.** MSEDCL is three vendors (₹8,100 + ₹7,200 +
   ₹7,170), so "total monthly burn ₹28,832" is mostly one electricity account
   counted three times. Stockopedia's own LSEG invoices (£22,270) in the work
   mailbox count as a subscription.

The plumbing is healthy: 242 extraction calls in 30 days, 0 failures, 212
`MoneyProcessFlow` runs completed. The design is what is wrong.

## Goals

- One book of record for money, kept by Maou, readable and editable by the
  user: an hledger journal in a git repo.
- Every money email parsed from its full body; bank and card alerts are a
  source, not a blocklist.
- Things that need paying become dated Todoist tasks. Everything else is read
  in a weekly brief and a monthly close, never as per-email tasks.
- Amounts in major units everywhere a human reads them.
- Maou (and the operator's own Claude Code sessions, through the MCP mount)
  can query and maintain the books with tools.

## Non-goals

- Bank statement PDFs (password protected) are not parsed.
- No budgeting UI. hledger's own `bal --budget` is available through the query
  tool if the user adds budget lines by hand.
- No investment tracking (Groww, Zerodha, ZebPay statements are `info`).
- Chat conversation redesign. Chat is a dead surface; tools are exposed for the
  MCP operator mount and for Maou's flows, not for a chat-first workflow.
- Dropping the legacy `finance.recurring_charge`, `renewal_alert`,
  `subscription_digest` tables. They stop being written; a follow-up issue
  drops them once the books have run for a month.

## 1. The books repo

`hikmahtech/books`, default branch `master`. Layout:

```
main.journal              ; include lines only, in this order
accounts.journal          ; commodity + account directives (the chart)
prices.journal            ; P directives, appended weekly by AEGIS
recurring.journal         ; periodic transactions (~) for forecasts, human-edited
personal/2026.journal     ; one file per entity per calendar year
hikmah/2026.journal
rules/accounts.yaml       ; payee → account rules, AEGIS-appended, human-edited
reports/weekly/2026-09-06.md
reports/monthly/2026-08.md
README.md
.gitignore                ; .aegis.lock
```

`main.journal`:

```
include accounts.journal
include prices.journal
include personal/2026.journal
include hikmah/2026.journal
include recurring.journal
```

When a new year starts, the writer creates `<entity>/<year>.journal` and adds
its include line before `recurring.journal`.

### Transaction format (written by AEGIS, parsed by AEGIS)

```
2026-09-02 * Jai shree nakoda
    ; msgid: arshad-personal/1a06cf5a22881051
    ; channel: upi, ref: 128932002048, instrument: hdfc-1225
    expenses:unknown                        ₹10.00
    assets:bank:hdfc:1225
```

Rules the writer and parser share:

- Blocks are separated by exactly one blank line. A block is one transaction.
- Line 1: `YYYY-MM-DD * <payee>`. The payee is the canonical name from the
  rules file when one matches, else the parser's best name (VPA name, merchant
  name, sender name). `*` always (bank-sourced or receipt-sourced is in tags).
- Line 2: `    ; msgid: <mailbox>/<gmail message id>`. Always present. It is
  the idempotency key and the address every rewrite uses. A manual posting
  through the tool gets `msgid: manual/<hash>` — a SHA-256 of the rendered
  block, never a `uuid4`, so a retried write is a retry and not a duplicate
  transaction (§8).
- Line 3: `    ; channel: <c>, ref: <r>, instrument: <i>` — `channel` always,
  `ref` and `instrument` when known. Extra tags allowed: `receipt: <msgid>`
  (a vendor receipt linked to this bank posting), `bank: <msgid>` (the
  reverse), `note: <text>`.
- Then 2..n postings, four-space indent, account padded to column 44, amount
  as `<symbol><number>` with no digit grouping (`₹1234.56`, `$5.89`, `£6285.01`,
  `€10.00`); any other currency as `<number> <ISO>` (`12.00 SGD`). The last
  posting may omit the amount.
- Amounts are `Decimal` with 2 places. Never floats, never minor units.
- Dates are the transaction date in Asia/Kolkata (the date in the alert body
  when present, else `received_at` converted).

`hledger check --strict` must pass after every write (declared accounts and
commodities only). The throwaway journal in the design session verified on
hledger 1.52.3: strict check, `print tag:msgid=<x>`, `bal -X ₹`, `-O json`,
`is -M`, `--forecast` from `recurring.journal`, `-O csv`.

### Chart of accounts (initial `accounts.journal`)

```
commodity ₹ 1,00,000.00
commodity $ 1000.00
commodity £ 1000.00
commodity € 1000.00

account assets:bank:hdfc:1225
account assets:bank:hdfc:0236
account assets:bank:nkgsb:843
account assets:bank:axis:9640          ; Hikmah current account
account assets:bank:icici:143
account assets:bank:hsbc
account assets:unknown                 ; receipt with no known paying instrument
account liabilities:card:axis:1313
account liabilities:card:axis:1747
account liabilities:card:hsbc
account liabilities:emi:bajaj
account expenses:unknown
account expenses:groceries
account expenses:food
account expenses:transport
account expenses:utilities:electricity
account expenses:utilities:internet
account expenses:utilities:mobile
account expenses:saas
account expenses:media
account expenses:shopping
account expenses:health
account expenses:insurance
account expenses:fees:bank
account expenses:tax
account expenses:people                ; transfers to individuals
account expenses:cash
account expenses:hikmah:unknown
account expenses:hikmah:infra
account expenses:hikmah:saas
account expenses:hikmah:internet
account expenses:hikmah:ads
account expenses:hikmah:fees:bank
account expenses:hikmah:tax
account expenses:hikmah:professional   ; CA, legal
account income:unknown
account income:salary
account income:interest
account income:refunds
account income:hikmah:stockopedia
account income:hikmah:other
account equity:opening
account equity:transfers               ; between own accounts when the far side is unknown
```

Account tails are the last four digits the bank prints; nothing longer is ever
written to the books. The user extends this file by hand; `ledger_add_rule`
refuses an account that is not declared.

### Rules file `rules/accounts.yaml`

First match wins. `match` is a case-insensitive regex tested against the
string `"<sender> | <payee>"` where `sender` is the email From header and
`payee` is the parser's raw payee.

```yaml
- match: "amazon web services|invoicing@aws\\.com|amazonaws\\.com"
  entity: hikmah
  account: expenses:hikmah:infra
  payee: Amazon Web Services
- match: "lseg billing|stockopedia\\.com"
  ignore: true          # Stockopedia's money, not ours
- match: "mahavitaran.*suncity 501"
  account: expenses:utilities:electricity
  payee: MSEDCL Suncity 501
```

Fields: `match` (required), `account`, `entity` (`personal` | `hikmah`),
`direction` (`in` | `out`), `payee` (canonical display name), `ignore: true`.
A rule with only `match` and `payee` normalises the name without deciding the
account.

`direction` is optional and means "only when the money moved this way"
(issue #396). Left out, the rule fires either way, which is what every rule
written before the field existed means. Give it when the same name moves money
both ways and the two belong in different accounts — a person you both pay and
are paid by — so a payment is not filed into the income account you picked for
a credit. It is never inferred from the account: a refund credited back to
`expenses:shopping` is a legitimate inbound posting, and an `assets:*` or
`equity:transfers` rule has to match a transfer moving either way. The
curiosity answer hook is the exception and always stamps one, because it knows
which way the card asked and no human reviews what it writes. A rule whose
`direction` is neither value is skipped on load, with a warning naming it.

The initial file ships with the rules that follow directly from the evidence
gathered on 2026-09-05 (AWS, Google Workspace, Airtel Xstream, Eleven Labs,
Anthropic, Cloudflare, GitHub, Meta ads, Mach1 broadband → hikmah; Apple,
Amazon, Musaffa, 1Password, MSEDCL, Bajaj → personal; LSEG → ignore).

## 2. Pipeline: `MoneyProcessFlow` v2

Triggers are unchanged: `GmailIngestFlow` fans out any message tagged
`financial` or `payments` to `MoneyProcessFlow`; `ReceiptIngestFlow` is the
weekly safety net and the backfill vehicle. The `arshad-stpd` mailbox is not
ours: the flow returns `{"status": "ignored_mailbox"}` for any mailbox listed
in the setting `books_ignored_mailboxes` (default `["arshad-stpd"]`).

Steps:

1. `store_receipt_email(msg, account)` — as today, but returns the existing
   row id when the stored `parsed` lacks `"version": 2`, so a v1 row is
   re-processed instead of short-circuiting as a duplicate. Returns `""` only
   for an already-v2 row.
2. `fetch_message_body(account, message_id)` — new `GmailActivities` activity.
   Gmail `get(format="full")`, first `text/plain` part, else `text/html`
   stripped to text (style/script blocks removed, tags removed, entities
   unescaped, whitespace collapsed, URLs replaced by `<url>`). Returns at most
   6,000 characters. Stored into `receipt_email.parsed.body_text` by
   `store_money_result` (step 5). The triage path is untouched; only this flow
   fetches bodies.
3. `parse_money_email(receipt)` — tries the deterministic parsers in order
   (section 3), first non-`None` wins; otherwise the LLM extractor with the
   full body (section 4). Applies the rules file. Returns a `MoneyEvent`.
4. Route on `event.kind`:
   - `transaction` → `post_money_event(event)` (section 5).
   - `due` → `capture_due(event)`: a Todoist task (section 7.1).
   - `failed` → `capture_due(event)` with title prefix `Fix payment:`.
   - `info` | `ignore` → nothing.
5. `store_money_result(receipt_id, event, journal_ref)` — writes
   `receipt_email.parsed` = `{"version": 2, "body_text": …, "event": {…}}`
   and upserts `finance.journal_index` (section 5.3).

Every step that fails leaves `parsed.version` unset so the weekly sweep
re-drives the row. The `recurring_charge` upsert and the "Anomaly" Inbox
capture are gone.

### `MoneyEvent` (pydantic, `core/src/aegis/api/models/money.py`)

```python
class MoneyEvent(BaseModel):
    kind: Literal["transaction", "due", "failed", "info", "ignore"]
    direction: Literal["in", "out"] | None = None
    amount: Decimal | None = None          # major units, 2 places
    currency: str | None = None            # ISO-4217
    payee: str = ""                        # raw name from the email
    payee_key: str = ""                    # normalised: lowercase, [a-z0-9 ] only, collapsed
    channel: Literal["upi", "imps", "neft", "card", "autopay", "remittance",
                     "receipt", "bill", "statement", "other"] = "other"
    instrument: str | None = None          # hdfc-1225, axis-cc-1313, nkgsb-843
    occurred_on: date | None = None
    due_on: date | None = None
    entity: Literal["personal", "hikmah", "none"] = "personal"
    account: str | None = None             # counter account, e.g. expenses:saas
    category: str | None = None            # LLM's closed vocab, mapped to account
    ref: str | None = None                 # UPI/IMPS/SWIFT reference
    is_recurring: bool | None = None
    parser: str = "llm"                    # which parser produced it
    confidence: float = 1.0
    source_class: Literal["bank", "receipt", "other"] = "other"
```

`payee_key` is `re.sub(r"[^a-z0-9]+", " ", payee.lower()).strip()`.

## 3. Deterministic parsers

`core/src/aegis/services/bank_parsers.py`, pure functions
`parse_<name>(sender: str, subject: str, body: str) -> MoneyEvent | None`,
tried in this order by `parse_any(sender, subject, body)`. Each is anchored on
the phrases the real emails use (bodies read on 2026-09-05); fixtures in
`tests/core/fixtures/money/*.txt` are those bodies with names and numbers
altered.

| parser | sender anchor | body anchor | produces |
|---|---|---|---|
| `hdfc_upi` | `alerts@hdfcbank.bank.in` | `Rs.<amt> is debited from your account ending <4> towards VPA <vpa> (<name>) on <dd-mm-yy>. UPI transaction reference no.: <ref>` (also `credited to … from VPA`) | transaction, upi, out/in, instrument `hdfc-<4>`, payee `<name>` else `<vpa>` |
| `hdfc_imps` | `alerts@hdfcbank.bank.in` | `INR <amt> has been debited from your account ending x…<4> on <dd-mm-yy> and credited to the account ending x…<4b> via IMPS. IMPS Reference No: <ref>` | transaction, imps, out, payee `a/c ••<4b>`, account `equity:transfers` |
| `nkgsb` | `alerts@nkgsb-bank.com` | `Received Rs.<amt> in NKGSB Bank A/C X<3> on <dd-mm-yy> UPI/CREDIT/<ref>/<vpa>/` (also `Paid`/`Debited`) | transaction, upi, in/out, instrument `nkgsb-<3>` |
| `axis_card_spend` | `alerts@axis.bank.in`, subject `<CUR> <amt> spent on credit card no. XX<4>` | `Merchant Name: <m>` … `Date & Time: <dd-mm-yyyy>` | transaction, card, out, instrument `axis-cc-<4>` |
| `axis_autopay_done` | `alerts@axis.bank.in`, body `successful AutoPay transaction` | `Transaction Amount: <CUR> <amt>` `Merchant Name: <m>` `Card No. XX<4>` | transaction, autopay, out |
| `axis_autopay_reminder` | subject `Upcoming AutoPay txn. reminder` | `Transaction Amount: INR <amt>` `Merchant Name: <m>` `To be debited by: <dd-mm-yyyy>` | due |
| `axis_cc_statement` | `cc.statements@axis.bank.in` | `Total Amount Due … Payment Due Date … <total> Dr <min> Dr <dd/mm/yyyy>` | due, statement, amount `<total>`, payee `Axis credit card XX<4>` |
| `axis_remittance` | `eforexservices@axis.bank.in`, subject `Inward Remittance Notification` | `received foreign currency funds of <CUR> <amt>` … `Value Date (F32A) : <dd-MON-yy>` … `Ordering Customer … 1/<name>` | transaction, remittance, in, entity hikmah, instrument `axis-9640`, account `income:hikmah:stockopedia` when name contains Stockopedia else `income:hikmah:other` |
| `gpay_bill` | `google-pay-noreply@google.com`, subject `New bill from <biller>` | `Bill Amount: Rs. <amt>` `Account Name: <acct>` `Due Date: <Mon d, yyyy>` | due, bill, payee `<biller> <acct>` |
| `stripe_receipt` | sender contains `invoice+statements` | `Receipt from <vendor> <sym><amt> Paid <Month d, yyyy>` … `Payment method - <4>` | transaction, receipt, out, instrument `card-<4>` |
| `apple_receipt` | `no_reply@email.apple.com`, subject `Your receipt from Apple` | `<product> (<cadence>)` … `₹<amt>` (the last `₹` amount in the body is the total) … `<vpa>@ok<bank>` or `•••• <4>` | transaction, receipt, out, payee `Apple <product>`, is_recurring True |
| `airtel_bill` | `ebill@airtel.com` | `Due amount* (Rs.) <amt>` … `Due date* <dd-Mon-yyyy>` | due, bill |
| `airtel_receipt` | `update@airtel.com` | `received a payment of Rs <amt> for your Bill Payment` | transaction, receipt, out |

Common helpers: `_amount("1,00,308.53") -> Decimal`, `_date_dmy`,
`_date_dmy4`, `_date_dmon`, `_date_mdy_text`, `_currency("Rs."|"INR"|"₹"|
"USD"|"$"|"GBP"|"£"|"EUR"|"€")`. Any parser that matches its anchors but
cannot read the amount returns `None` so the LLM gets a try.

Parsers set `entity` from the mailbox (`arshad-hikmah` → hikmah, else
personal), then the rules file may override it. `axis_remittance` sets hikmah
directly.

## 4. LLM extractor v2

`LLMClient.extract_money_batch(receipts, model, system_prompt, db_pool,
agent_id)` replaces `extract_receipts_batch`. Same batching and failure
semantics (`_parse_failed` stubs on truncation, raise on batch failure). The
prompt receives `From`, `Subject`, `Body (up to 4000 chars)` and asks for the
`MoneyEvent` fields as JSON, with these instructions carried over from the v1
prompt: a number in an advertisement is not a charge; failed/declined/reversed
means `kind: failed` (with `due_on` = the fix-by date when stated); a bank
autopay reminder or card statement is `kind: due`, not a charge; `kind: info`
for statements-available, KYC and balance notices; `kind: ignore` for
newsletters and marketing.

`category` is a closed vocabulary mapped to accounts by
`services/books.py::account_for(category, direction, entity)`:

| category | personal | hikmah |
|---|---|---|
| saas | expenses:saas | expenses:hikmah:saas |
| media | expenses:media | expenses:hikmah:saas |
| infra | expenses:saas | expenses:hikmah:infra |
| internet | expenses:utilities:internet | expenses:hikmah:internet |
| electricity | expenses:utilities:electricity | expenses:hikmah:unknown |
| mobile | expenses:utilities:mobile | expenses:hikmah:unknown |
| groceries / food / transport / shopping / health / insurance | expenses:<same> | expenses:hikmah:unknown |
| fees | expenses:fees:bank | expenses:hikmah:fees:bank |
| tax | expenses:tax | expenses:hikmah:tax |
| professional | expenses:unknown | expenses:hikmah:professional |
| ads | expenses:unknown | expenses:hikmah:ads |
| people | expenses:people | expenses:hikmah:unknown |
| salary / interest / refund (direction in) | income:<same> | income:hikmah:other |
| other / missing | expenses:unknown or income:unknown | expenses:hikmah:unknown or income:hikmah:other |

An LLM event with `confidence < 0.8` posts to the unknown account regardless
of category. A rules-file `account` always wins over the category map.

## 5. The journal writer: `core/src/aegis/services/books.py`

Shared by core (tools) and worker (flows). Both containers mount the same
`aegis_config` volume, so the working copy at `books_path` (default
`/app/config/books`) is one directory. A `fcntl.flock` on
`<books_path>/.aegis.lock` serialises every write across both processes;
inside a process an `asyncio.Lock` wraps it.

`# ponytail: shared working copy + flock; a books HTTP service if a third
writer ever appears.`

### 5.1 API

```python
async def ensure_checkout(settings) -> Path          # clone if missing, else pull --rebase --autostash
async def post_event(event: MoneyEvent, settings) -> str      # returns "<entity>/<year>.journal"
async def rewrite_block(msgid: str, *, account=None, payee=None, add_tags: dict | None = None,
                        counter_account=None, settings) -> None
async def remove_block(msgid: str, settings) -> None
async def append_prices(lines: list[str], settings) -> None
async def append_rule(rule: dict, settings) -> None
async def write_report(rel_path: str, text: str, settings) -> None
async def run_hledger(args: list[str], settings, *, output_format="text") -> str
def render_transaction(event: MoneyEvent, counter_account: str) -> str
def find_block(text: str, msgid: str) -> tuple[int, int] | None   # char offsets, block incl. trailing blank
def load_rules(path) -> list[Rule]; def apply_rules(rules, sender, payee) -> Rule | None
def fmt_money(amount: Decimal, currency: str) -> str
```

`fmt_money` renders `₹1,00,308.53` (Indian grouping for INR), `$5.89`,
`£6,285.01`, `€10.00`, `12.00 SGD`. It is the only formatter any user-facing
string may use. `amount_cents` never appears in a title, message or report.

Posting sides: a `transaction` with `direction=out` posts `<account> <amt>`
then `<instrument account>` (no amount); `direction=in` posts `<instrument
account> <amt>` then `<account>`. The instrument account comes from
`instrument_account(instrument)`: `hdfc-1225 → assets:bank:hdfc:1225`,
`axis-cc-1313 → liabilities:card:axis:1313`, `card-1313 →
liabilities:card:axis:1313` when that tail is declared under
`liabilities:card:*`, else `assets:unknown`; `nkgsb-843 → assets:bank:nkgsb:843`;
`axis-9640 → assets:bank:axis:9640`. Unknown instrument → `assets:unknown`.

### 5.2 Write protocol

Every mutating call:

1. take the flock; `git pull --rebase --autostash` (skip when no remote);
2. edit the file(s);
3. `hledger -f main.journal check --strict`; on failure `git checkout -- . &&
   git clean -fd` and raise `BooksCheckError(stderr)`;
4. `git add -A && git commit -q -m "<summary>"` with author
   `Maou <maou@aegis.local>`; summaries: `post <entity> <date> <payee>
   <amount>`, `reclassify <msgid> -> <account>`, `rule: <match> -> <account>`,
   `prices <date>`, `report <path>`;
5. `git push -q` (failure is logged, not raised; `unpushed_commits()` reports
   `git rev-list --count @{u}..HEAD` for the brief and the admin page).

Git runs with `GIT_SSH_COMMAND="ssh -i <token_dir>/books_deploy_key -o
StrictHostKeyChecking=accept-new -o IdentitiesOnly=yes"`. The deploy key is
written from the encrypted setting at boot (section 10).

### 5.3 `finance.journal_index` (migration `026_journal_index.sql`)

```sql
CREATE TABLE IF NOT EXISTS finance.journal_index (
    message_id        text PRIMARY KEY,          -- "<mailbox>/<gmail id>" or "manual/<hash>"
    mailbox           text NOT NULL,
    entity            text NOT NULL,             -- personal | hikmah | none
    kind              text NOT NULL,             -- transaction | due | failed | info | ignore
    direction         text,
    amount            numeric(14,2),
    currency          text,
    payee             text,
    payee_key         text,
    account           text,                      -- counter account as posted
    channel           text,
    instrument        text,
    occurred_on       date,
    due_on            date,
    parser            text NOT NULL,
    confidence        real,
    source_class      text NOT NULL DEFAULT 'other',
    journal_file      text,                      -- null when not posted (due/info/ignore or linked receipt)
    linked_message_id text,                      -- receipt<->bank link
    todoist_ref       text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS journal_index_payee_day ON finance.journal_index (payee_key, occurred_on);
CREATE INDEX IF NOT EXISTS journal_index_match ON finance.journal_index (currency, amount, occurred_on)
    WHERE kind = 'transaction' AND linked_message_id IS NULL;
ALTER TABLE finance.receipt_email ADD COLUMN IF NOT EXISTS journal_msgid text;
```

The journal is the record; the index exists for idempotency, matching, dues
dedupe, the admin page and curiosity. Re-posting the same `message_id` is a
no-op (`ON CONFLICT DO NOTHING` on the index, and `find_block` before append).

### 5.4 Matching receipts to bank postings

A vendor receipt and the bank or card alert for the same payment must produce
one posting. At `post_event` time, for a `transaction` event, look in the
index for an unlinked `transaction` row of the opposite `source_class`
(`bank` vs `receipt`) with the same `currency` and `amount` and
`occurred_on` within ±3 days:

- Bank event arrives, receipt already posted: rewrite the receipt's block
  (`counter_account` = the bank instrument account, add tag `bank: <msgid>`),
  index the bank event with `journal_file = NULL`, `linked_message_id` = the
  receipt's id, and set the receipt row's `linked_message_id` too. Do not
  post a second transaction.
- Receipt arrives, bank event already posted: rewrite the bank block (payee =
  receipt payee when the bank payee is a VPA, all-caps merchant string or
  account tail; account = receipt account when the bank account is an unknown
  account; add tag `receipt: <msgid>`), index the receipt with
  `journal_file = NULL` and the link.
- No match: post normally. A receipt with no instrument posts against
  `assets:unknown`.

There is no separate reconcile sweep; both arrival orders are handled here.

## 6. Curiosity and rules

`CuriosityCardFlow`'s `_detect_recurring_charge` becomes
`_detect_unknown_payee`: the top 50 `payee_key`s by summed amount in the
last 60 days whose index `account` ends in `:unknown`, novelty key
`payee:<payee_key>`, question "You paid <fmt_money> to <payee> (<n> times,
last <date>, <channel>). What was it for?". The query takes outbound
home-currency postings only (`direction = 'out'`, `currency = 'INR'`):
`income:unknown` ends in `:unknown` too, so without that clause an
uncategorised CREDIT is carded as money the owner paid — false, on the one
surface allowed to interrupt them, and it steers the account-picking model
toward an expense account for money that came in. `_already_asked` counts
archived cards too: an unanswered question is asked again only through the
weekly brief's unknown list, never as a fresh card.

When the user answers the card, the existing `InteractionFlow` post-resolve
hook `apply_curiosity_answer` (`CuriosityActivities`, not `MoneyActivities`)
asks the balanced-tier model to pick one account from the DECLARED chart for
the answer text; with confidence ≥ 0.8 **and** the account actually in that
chart it calls `append_rule` with that account and payee, and a `match`
built from the payee_key's escaped words joined by `[^a-z0-9]+` and prefixed
with `\|[^|]*` — the prefix pins the pattern to the payee half of the
`<sender> | <payee>` haystack, because an unanchored rule from a payee
called "Google" would re-file and rename every Google-Pay-mirrored bill. It
then rewrites every outbound `:unknown` index row for that `payee_key` in
ONE commit (`rewrite_events`, not a `rewrite_block` loop, which would hold
the flock for the length of the sweep and leave one commit per posting);
otherwise the answer stays in `agent_memory` as today. The rule carries the
`entity` the account states (`books.account_entity`: only the expense and
income trees have one, and `:hikmah:` is the business side), and the sweep is
filtered on it, because a rewrite changes the account and never the file the
block lives in — so an AWS bill that arrived in the personal mailbox and is
answered "Hikmah infra" must leave `personal/2026.journal` alone and correct
only the FUTURE mail. Both ledger tools refuse the same move. Two refusals
matter.
The account is model-chosen and NOT trusted: one the chart does not declare
is dropped, because a rule built on it would misfile that payee's mail
forever. And a payee whose pattern would breach the regex bounds gets NO
rule rather than a clipped one — a truncated pattern is a prefix rule,
broader than the question the owner answered, written unattended; the
backlog still moves, because the sweep selects on `payee_key` and never
consults the pattern. The books half runs after the memory write and
swallows its own failures: the owner answered a question, and that answer
must not be lost to a bad pull or a bad model day.

## 7. Outputs

### 7.1 Dues → Todoist

`CaptureActivities.capture_task(source_tag, external_id, title, description,
project_id, due_date, labels)` generalises `capture_to_inbox` (which becomes a
thin wrapper passing the Inbox id and no date). Same
`todoist_capture_idempotency` table.

`capture_due(event)`:

- `project_id` from setting `books_todoist_projects`
  (`{"personal": "<id>", "hikmah": "<id>"}`); missing → Inbox.
- `external_id` = `f"{event.payee_key}:{event.due_on}"`, `source_tag` `#bill`.
- title `Pay <payee> <fmt_money>` (or `Fix payment: <payee> <fmt_money>`),
  description `Due <due_on>\n<channel> · <instrument>\n<mailbox> · gmail
  <message id>`.
- `due_date` = `due_on - 1 day`, or today if that is past. No GTD state label
  (the date surfaces it).
- `close_due_if_paid`: when a `transaction` is posted whose `payee_key`
  matches an open due with the same `due_on` within 45 days and an amount
  within 1%, complete its task through the outbox (`item_complete`), mark the
  due paid, and note it in the brief. Open is `linked_message_id IS NULL` and
  nothing else. `capture_due` withholds the task for a zero invoice, a twin
  already tasked under another payee's name and an autopay notice, and all
  three are still indexed as dues — so a due with no task ref closes exactly
  the same way, the task completion alone being skipped. Requiring a task ref
  would make every guarded due permanently open, and "dues open" is a count
  that could then only rise.

### 7.2 Weekly brief: `MoneyBriefFlow`

Agent maou, slug `money-brief-weekly`, cron `0 3 * * 0` (08:30 IST Sunday,
before `gtd-weekly-review` at `0 3:30`). Steps:

1. `refresh_fx_prices`: `FinanceConnector.get_quotes(["USDINR=X", "GBPINR=X",
   "EURINR=X"])` → `append_prices(["P <today> $ ₹<px>", …])`; on failure keep
   the last prices (the config defaults seed `prices.journal` on first run).
2. `build_money_brief(days=7)` → dict from `run_hledger` and the index:
   - per entity: `bal -X ₹ -p "last 7 days" income expenses --depth 2 -O json`
     on `main.journal`; the entity split is by account prefix
     (`expenses:hikmah`, `income:hikmah` are hikmah, the rest personal);
   - `reg -X ₹ -p "last 7 days" expenses --sort-amount` top 10 outflows;
   - unknowns: index rows in `*:unknown` in the last 7 days;
   - dues: index rows `kind IN ('due','failed')` with `due_on` in the next 14
     days and still unpaid (`linked_message_id IS NULL`), plus
     `reg --forecast=<today>..<+14d>` from `recurring.journal`;
   - large unexplained: unknown-account rows ≥ ₹5,000;
   - housekeeping: `unpushed_commits()`, rows with `parser='llm'` and
     `confidence < 0.8` count.
3. `render_money_brief(brief)` → light HTML; tables inside `<pre>` (the comms
   `html_to_mrkdwn` gains `<pre>` → triple backticks).
4. `safe_send_message` to maou's channel; `write_report("reports/weekly/<sunday>.md")`.

### 7.3 Month close: `MonthCloseFlow`

Replaces `SubscriptionAuditFlow`; slug `money-close-monthly`, cron
`0 4 1 * *` (09:30 IST on the 1st). `is -X ₹ -M -b <prev-prev month> -e <this
month> --depth 2` (this month vs last), `bs -X ₹ -e <first of month>`,
recurring total from `recurring.journal` (`bal --forecast=<month>` on
`recurring.journal` only), unknown count for the month, dues paid/unpaid from
the index. Sent to maou's channel and written to `reports/monthly/<YYYY-MM>.md`.

### 7.4 What stops

- `MoneyHygieneDailyFlow` (renewal + cancellation sweeps) and
  `SubscriptionAuditFlow` are deleted, with their `FlowSpec`s, seed rows and
  a migration `DELETE FROM activities WHERE slug IN ('money-hygiene-daily',
  'subscription-audit-monthly')`.
- The per-receipt "Anomaly" Inbox capture in `MoneyProcessFlow` is deleted.
- `upsert_charges`, `detect_cancellations`, `evaluate_renewal_alerts`,
  `notify_renewal_alert`, `notify_cancellation`,
  `build_subscription_digest`, `notify_subscription_digest` are deleted.
- `bank_alert_senders` stays as a setting but now means "senders whose mail
  goes to the deterministic bank parsers first"; it no longer blocks anything.

## 8. Tools (`core/src/aegis/services/tools/ledger.py`, `@aegis_tool`)

| tool | signature | behaviour |
|---|---|---|
| `ledger_query` | `(command: str, args: list[str] = [], output: str = "text")` | `run_hledger([command, *args, …])`; `command` in `{bal, balance, reg, register, print, is, incomestatement, bs, balancesheet, cf, cashflow, accounts, payees, tags, stats, activity, aregister, check}`; every argument that starts with `-` must appear verbatim in an **exact-match allowlist** (`books._ALLOWED_OPTIONS`, checked on the token before any `=`), and an `@argsfile` argument is refused outright. **The allowlist is not negotiable and must never become a deny-list:** measured against the real binary, hledger bundles short flags (`-Ef<path>` reads a file, `-No<path>` WRITES one), abbreviates long ones (`--fil=` is `--file=`) and splices an `@argsfile`'s lines in as arguments — so every one of those walks straight past a `startswith` list of `-f`/`-o` prefixes. `output` in `{text, json, csv}` adds `-O`. Output capped at 12,000 chars with a `… (truncated)` tail. |
| `ledger_post` | `(date: str, payee: str, postings: list[dict], entity: str = "personal", note: str = "")` | Each posting `{"account", "amount", "currency"}`; at most one posting without amount; every account must be declared; writes `msgid: manual/<sha256 of the entity + the rendered block, 16 hex chars>`, `channel: manual`; indexes with `parser='manual'`. **The msgid is derived from the content, never a `uuid4`:** a books write (pull, strict check, commit, push) can outlive the chat loop's timeout, and the model's natural response to a timed-out call is to make it again — with a fresh id that second call posts a DUPLICATE TRANSACTION, while a content-derived one finds the first block and writes nothing. Digesting the rendered block rather than the raw arguments is what makes `"245.50"` and `"245.5"` the same transaction. Two genuinely identical payments on one day need a distinguishing `note`. |
| `ledger_reclassify` | `(message_id: str, account: str, payee: str | None = None)` | `rewrite_event` + index update, the index only after the journal. Refuses an undeclared account, and refuses a cross-entity move: the block is located in the journal first (`locate_event`), so an `expenses:hikmah:*` account cannot be attached to a posting filed in `personal/` — that block still balances and still passes `check --strict`, so nothing else would catch it. |
| `ledger_add_rule` | `(match: str, account: str, entity: str | None = None, payee: str | None = None, apply: bool = True)` | Validates the regex compiles and the account is declared; `append_rule`; when `apply`, reclassifies index rows in an unknown account, in ONE commit (`rewrite_events`). Returns the count. Three bounds beyond "it compiles", because the pattern is PERSISTED and the worker then runs it against every money event, in another process, forever: at most 200 characters, no repeated group (`(a+)+`, `((a+))+`), at most 6 stacked quantifiers, and a behavioural probe in a killable subprocess as the backstop — `re` has no timeout and `apply_rules` runs on the event loop, so a slow pattern is a durable cross-process hang no caller-side timeout can interrupt. The sweep matches on the payee alone (the index has no sender), skips rows of another `entity`, and stops at 200 postings, telling the caller to narrow the rule or run it again. |

Grants: all four to `maou`; `ledger_query` also to `sebas`. In
`mcp_server.py`, `ledger_post`, `ledger_reclassify`, `ledger_add_rule` join
`_UNSERVED_TOOLS` (a coding run has no business in the books); the operator
mount serves all four. Rollout grants are DB writes to
`agents.metadata.tool_set` — the yaml seed only applies to an agent that has
no tool set yet, and the SQL is in `docs/infrastructure.md` (§Ledger tools).

## 9. Admin

`GET /api/admin/money/state` returns `{events: [last 100 index rows], unknown_count,
unpushed_commits, last_brief: {path, sent_at}, home_currency}`;
`/api/admin/money/digest` returns the latest `reports/monthly/*.md`; the run
buttons trigger `money-brief-weekly`, `money-close-monthly`,
`receipt-ingest-weekly` (`_FLOW_NAMES` updated). `Money.tsx` shows the events
table (date, entity, payee, amount via `fmt_money` equivalent in TS, account,
kind, parser) and the two counters. The charges and renewal tables go.

## 10. Configuration, image, deployment

**Settings (`Settings`, env `AEGIS_*`):** `books_path: str = "/app/config/books"`,
`books_repo_url: str = ""` (empty ⇒ books disabled: `MoneyProcessFlow` still
indexes, `post_event` raises `BooksDisabled` which the flow records as
`status="books_disabled"`), `books_ignored_mailboxes: list[str] =
["arshad-stpd"]`, `books_todoist_projects: dict = {}`.

**Integration registry (`CONFIG_REGISTRY`, group "Books"):**
`books_repo_url` (non-secret, help text names the SSH form),
`books_deploy_key` (secret; the private half of an ed25519 deploy key with
write access). At boot (core `api/app.py` lifespan and worker `bootstrap`),
`books.install_deploy_key(settings)` writes it to
`<gmail_token_dir>/books_deploy_key` with mode 0600 when set. Never logged.

**Images:** both `core/Dockerfile` and `worker/Dockerfile` add `git` to the
apt line and:

```dockerfile
ARG HLEDGER_VERSION=1.52.3
ARG HLEDGER_SHA256=d14a4fc2ac804b556f481b64e8c54efa380db1ac85b3723c9df7b1eeade74b3a
RUN curl -fsSL -o /tmp/hledger.tgz \
      "https://github.com/simonmichael/hledger/releases/download/${HLEDGER_VERSION}/hledger-linux-x64.tar.gz" \
    && echo "${HLEDGER_SHA256}  /tmp/hledger.tgz" | sha256sum -c - \
    && tar -xzf /tmp/hledger.tgz -C /tmp hledger \
    && install -m 0755 /tmp/hledger /usr/local/bin/hledger \
    && rm -f /tmp/hledger.tgz /tmp/hledger
```

The release binary is static (verified: `statically linked` on meem). meem has
the same binary at `~/.local/bin/hledger` for local tests.

**No homelab-gitops change** is needed: the working copy lives under the
existing `aegis_config` bind volume. `make aegis-release` ships it.

**Rollout order (after PR2 and PR3 deploy):**

1. Generate an ed25519 key pair on meem; add the public key as a write deploy
   key on `hikmahtech/books` (`gh api repos/hikmahtech/books/keys`); store the
   private key as `integration:books_deploy_key` and
   `integration:books_repo_url = git@github.com:hikmahtech/books.git` through
   the admin Integrations API from inside `aegis_core`; roll core and worker.
2. Set `books_todoist_projects` = `{"personal": "6h2fmwvJJX483GvF",
   "hikmah": "6h2fmvh44hmR7HhJ"}` (Todoist projects Finance and Hikmah).
3. Grant the tools (`agents.metadata.tool_set`).
4. Backfill: trigger `receipt-ingest-weekly` once with config `{"query_window":
   "after:2026/06/30", "max_per_account": 600}`. `ReceiptIngestFlow`'s sender
   filter becomes the config field `sender_filter`, default the union of the
   bank senders and the vendor senders in section 3 plus the v1 list.
   `find_stuck_receipts` sweeps rows whose `parsed` lacks `"version": 2`, so
   the 388 v1 rows re-run through the v2 pipeline as bodies are refetched.
5. Verify: `hledger -f main.journal stats` in the clone on meem shows
   postings from July; the first brief arrives Sunday; the Axis statement,
   MSEDCL and advance-tax dues exist as dated tasks.

## 11. Testing

- `tests/core/test_bank_parsers.py`: one fixture file per parser, table-driven
  expected `MoneyEvent` fields; a negative case per parser (marketing mail
  from the same sender returns `None`); break-and-revert on one regex to prove
  the tests bite.
- `tests/core/test_books.py`: `render_transaction` exact text; `find_block`
  and `rewrite_block` round trip on a three-block journal; `fmt_money`
  (Indian grouping, foreign symbols, ISO suffix); `apply_rules` first-match
  and `ignore`; `instrument_account`. hledger-backed tests (`check
  --strict` after write, `run_hledger` whitelist) are marked
  `skipif(shutil.which("hledger") is None)`; CI's self-hosted runner gets the
  same static binary (documented in `docs/development.md`).
- `tests/worker/test_money_process_flow.py`: `WorkflowEnvironment` with
  stubbed activities for each `kind`, the receipt↔bank match in both orders,
  `books_disabled`, ignored mailbox, v1 row re-processing.
- `tests/worker/test_money_brief.py`: `render_money_brief` on a fixed dict;
  `build_money_brief` against a temp books repo with `hledger` present.
- `tests/core/test_ledger_tools.py`: whitelist refusals, `ledger_post`
  balance/declared-account validation, `ledger_add_rule` apply count.
- Existing tests for deleted flows and activities are deleted with them.

## 12. PR split

- **PR1 — stop the bleeding** (this worktree, with this spec and the plan):
  `fetch_message_body` and full body to the v1 extractor; `fmt_money` and the
  paise fix in the renewal title; delete the per-receipt Anomaly capture and
  the renewal/cancellation Inbox captures (Slack pings stay until PR3);
  curiosity archived-counts-as-asked and vendor-key normalisation; `<pre>`
  in `html_to_mrkdwn`. Ships alone so the noise stops this week.
- **PR2 — the books:** books repo skeleton (already pushed to
  `hikmahtech/books`), images, `books.py`, migration 026, `bank_parsers.py`,
  `MoneyEvent`, v2 extractor, `MoneyProcessFlow` v2 with routing and matching,
  `capture_task` and `capture_due`, config keys and deploy-key install, docs.
- **PR3 — outputs and tools:** `MoneyBriefFlow`, `MonthCloseFlow`, FX prices,
  the four tools and grants, curiosity detector and `apply_books_answer`,
  admin page, `sender_filter` backfill config, deletion of the old flows and
  activities, docs.

## 13. Decisions and risks

- **Journal is the record, Postgres is the index.** Amounts are never
  authoritative in Postgres. Anyone who wants a number runs hledger.
- **Shared working copy under `aegis_config`, flock-serialised.** Two
  containers, one directory, one lock. Cheap; revisit if a third writer
  appears.
- **Rewrite-in-place is text surgery on our own format.** Guarded by
  `check --strict` and revertible with git. Hand edits that break the block
  grammar (a blank line inside a block) make that block invisible to
  `find_block`; the brief reports "N index rows with no journal block" as a
  housekeeping line.
- **Entity is a guess for the personal mailbox.** Rules fix it per vendor;
  the remittance parser is the one hard-coded hikmah case.
- **Deploy key, not a personal token.** Scoped to one repo, write-only what it
  needs, stored encrypted, rotated by replacing the setting.
- **meem's power domain is irrelevant.** The worker on pop-think-os holds the
  working copy; the user's clone is a mirror.
- **Legacy tables are left in place**, unwritten. Dropping them is a separate
  issue after a month of books.
