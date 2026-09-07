# Bank statement ingest and reconciliation — design

**Date:** 2026-09-07
**Status:** design, approved in outline; not implemented
**Owner lane:** Maou / money
**Builds on:** `2026-09-05-maou-books-design.md` (the books), PR #409 (`ref` column, instrument
resolution), PR #418 (`drive.file` scope)
**Companion:** `hdfc-smartstatement-recipe.md` — the verified HDFC retrieval procedure

---

## 1. Why

The books record what the banks **emailed**. Nothing checks them against what the banks
actually **did**.

Two consequences, both live today:

- **The books are incomplete.** Cash withdrawals, bank charges, interest credits and
  auto-debits that send no alert are simply absent. `assets:unknown` currently holds
  ₹53,774.56 — the largest rupee balance in the journal.
- **The books overstate their own certainty.** `render_transaction` hardcodes `*` (cleared)
  on every block. All 34 journal transactions claim to be bank-cleared and not one has ever
  been reconciled with a bank. `hledger bal --cleared` therefore returns everything, which
  makes the flag meaningless.

A bank statement fixes both. It is the complete record for its account and period, and it is
the only artefact that can promote a guess to a confirmed fact.

## 2. Scope

**In scope:** Axis and HDFC, savings/current and credit card, from FY2026-27 to date and
forward. Intake from a Drive folder and from statement emails. Parsing, matching, posting,
balance assertions, a monthly digest, and monitoring.

**Out of scope, tracked separately:**

- Backfilling `ref`/`instrument` on the 264 existing index rows (issue #406).
- Reposting the journal blocks already written against `assets:unknown` (issue #407).
- The index rows carrying no instrument at all (issue #408).
- The Notion Income/Expense import (subsystem B of the wider plan).
- Invoice generation (subsystem D).

## 3. Decisions already taken

| Decision | Choice | Rationale |
|---|---|---|
| Unmatched rows | Rules first, one digest per statement | ~40–60/month would arrive uncategorised; a card per row is a chore that gets abandoned by month two |
| Cards | In from the start | The transfer trap has to be designed once, correctly; retrofitting it risks double-counting |
| Intake | Drive folder **and** email attachments | AEGIS holds statement password components; the owner accepted that trade explicitly |
| History depth | FY2026-27 to date, then forward | Overlaps the live email lane, so the matcher is exercised on real data immediately |
| Kids' accounts | Assets, in the books | Guardian-managed; transfers not expenses. Already how `hdfc:0236` was declared |
| Posting engine | `hledger import` for unmatched rows | Native CSV rules, and it converts a running-balance column into a per-row balance assertion for free |

## 4. Architecture

Two subsystems separated by a filesystem boundary. That seam is deliberate: when a number is
wrong you can look in the folder and immediately tell an intake problem from a parsing one.
The current money lane has no such seam, which is why every past defect took so long to place.

```
A1 — keep the folder true
   statement email (tagged `statement`)  ─┐
   Gmail history backfill                 ├─→ fetch → decrypt → identify account → file
   a file the owner drops in by hand     ─┘                                          │
                                                                                     ▼
                                              Drive: aegis-accounting/<instrument>/<period>
                                                                                     │
A2 — turn the folder into books                                                      ▼
   parse (deterministic) → finance.statement_rows → match against journal_index
                                                    ╱                      ╲
                                            matched                     unmatched
                                               │                             │
                                       promote `!` → `*`            generated CSV + rules
                                       (text surgery)                        │
                                                                   hledger import (inside
                                                                   books.py's write envelope)
                                                                             │
                                                                     digest of rows that
                                                                     landed in :unknown
```

### 4.1 The Drive folder

`aegis-accounting/` — id `1vqjqowKcLJAyKMwLkIgNX4eaUoSvE7cR`, owned by
`arshad@hikmahtechnologies.com`. One subfolder per declared bank or card account, **named
exactly as the chart's instrument spelling** so `aegis-accounting/nkgsb-843/` and
`assets:bank:nkgsb:843` share one string:

| Subfolder | Drive id |
|---|---|
| `axis-9640` | `1FyKPdq4EqYtqaHJ2R7wXppLbV6enIzQ9` |
| `axis-cc-1313` | `11K8nGoLUJzmYdXT0TbAxeKnLzxDvtV1-` |
| `axis-cc-1747` | `1cIp_95LRFx3O_UBue8QRIwnnCwHI_Mja` |
| `hdfc-1225` | `13qktwynA_z49E03pLh_HLb8KfQHoLmKS` |
| `hdfc-0236` | `1GCorasyvI7li9CxS_H02KGA_k4sZkQ_6` |
| `hdfc-0325` | `13Go71BAN9K7wvObMdq_SMIY2FZ9tCkqH` |
| `nkgsb-843` | `1QogU8hKkoTvKCSSf8wQMQJOMrSbWYQy7` |
| `icici-143` | `109M2zVUcJLYz6jdNKsT2Hi3UUVLOu0JN` |

An empty subfolder is a visible statement that no statement has ever arrived for that
account. Do not delete empty ones.

**The folder name is a cross-check, never the identifier.** The account is read from inside
the statement; a file whose contents name a different account than its folder is a misfile
and must be reported, not imported.

**Hard constraint.** This folder must never be the folder `DriveSyncFlow` ingests. That flow
chunks and embeds its folder into the knowledge store, and statements carry full account
numbers, customer IDs and a PAN in the clear. Today the separation holds structurally —
`DriveSyncFlow` watches `1ijgXkU7CYv-LN6ljDR2HIpGWYWgHCOnQ` on `arshad-personal`, a
different folder on a different account. The ingest must nevertheless **refuse to run**
against the tracked folder id rather than trust configuration to stay correct.

## 5. A1 — intake

### 5.1 Triggers

One implementation, three ways in:

1. **Live.** `GmailIngestFlow` already fans out per tag: `financial`/`payments` →
   `MoneyProcessFlow`, `meeting` → `MeetingNotesFlow`. A `statement` tag →
   `StatementFileFlow` is the third instance of that pattern. Apply the tag through
   `sender_overrides`, which short-circuits the LLM entirely, so tagging a bank costs
   nothing per email and no model can get it wrong.
2. **Backfill.** The same activity over a Gmail history query. Bounded, and it can only
   reach back as far as the bank keeps the data (see §5.4).
3. **By hand.** The owner drops a file in. Nothing special: A2 reads the folder, not the
   mailbox.

### 5.2 Per-bank retrieval

**Axis** — the statement is an attached, encrypted PDF. Decrypt in memory with `pikepdf`,
extract text with `pdftotext -layout` reading the decrypted bytes from **stdin**.

Two reasons for that shape, both measured:

- `pdftotext -layout` reconstructs table rows; `pdfminer` — already a dependency — returns
  the table column-by-column and rebuilt **zero** complete rows from three real statements.
  A line regex over `pdfminer` output pairs a narration with someone else's amount.
- `pdftotext -upw <password>` puts the password in argv, readable from `/proc` by anything
  sharing the PID namespace. `pdftotext -layout - -` reads from stdin, and no decrypted PDF
  is ever written to disk.

**New dependencies:** `pikepdf` (Python) and `poppler-utils` (one line in
`worker/Dockerfile`'s apt list, which today installs only
`openssh-client curl ffmpeg openssl tini git`).

**HDFC** — the statement email carries **no attachment**. It links to a JSP behind a password
form, a server token and two encryption layers. The full procedure is in
`hdfc-smartstatement-recipe.md` and is verified end to end. The response is an **HTML table**,
which needs no column reconstruction at all — HDFC is the easier bank to parse.

### 5.3 Passwords — derive, do not store

Every scheme observed is `<first 4 letters of a name, uppercase, spaces and periods removed>`
plus one variable part:

| Statement | Variable part |
|---|---|
| Axis current | 9-digit customer ID (13 chars total; the only option Axis offers) |
| Axis card | DDMM of birth — or the card's last four |
| HDFC | DDMM of birth — or the first four digits of the customer ID |

Store the **components** encrypted (`crypto.encrypt_secret`, the `{"enc": {...}}` shape every
other AEGIS secret uses), not password strings, and derive candidates at use time, trying
each in order. Both banks offer two options; a stored string breaks the day a bank switches
which one it uses, whereas a derived list falls through to the second. One date of birth
covers every bank instead of being retyped per statement.

Verified on 2026-09-07: 15 of 15 real statements opened from derived components.

The password never enters a log, an error message or the digest. A failure reads "the
September Axis card statement could not be opened" and names the account, nothing more.

### 5.4 HDFC links expire

A statement job is purged server-side after roughly three months. The page still renders and
the token still issues, but the POST returns `input XML file not existed` with **HTTP 200**.

Therefore **HDFC ingest runs on arrival, driven by the statement email — never as a periodic
sweep over an old mailbox.** A sweep that falls behind loses statements permanently and fails
silently, because the fetch succeeds.

### 5.5 Drive write

Requires `drive.file`, granted on `arshad-hikmah` only (PR #418). `drive.readonly` is what
lets AEGIS see files the owner dropped in; `drive.file` is what lets it add one. A token
minted before a scope existed simply lacks it — check granted scopes and degrade the way
`MeetingNotesFlow` does with `doc_status=no_drive_scope`, rather than failing.

## 6. A2 — parsing

### 6.1 Identify by header anchor only

**Never identify an account or period from a string that can appear in a transaction line.**
This is not hypothetical: a first attempt classified nine Axis *current account* statements as
credit-card, because the fallback checked for the substring `Credit Card` and the current
account statement contains a `CreditCard Payment` narration row.

**Never take the period from the email subject.** Axis names each monthly statement for the
month it was *sent*: "Statement for August 2026" covers **01-07-2026 to 31-07-2026**. Trusting
the subject shifts the entire history by one month.

**One bank emits several header formats.** Axis alone has two for the same account:

```
mailed monthly:    STATEMENT BETWEEN dd/mm/yyyy AND dd/mm/yyyy FOR A/C: XXXXXXXXXXX9640
netbanking period: Statement of Account No :<full number> for the period (From : dd-mm-yyyy To : dd-mm-yyyy)
```

So each bank gets a **list** of header patterns, and anything matching none goes to an
`UNIDENTIFIED` bucket that stays visible. That bucket is what caught the second Axis format
instead of guessing at it.

### 6.2 The self-validating check

Every statement carries its own proof. **Closing balance − opening balance must equal
deposits − withdrawals. If it does not, refuse the whole statement** and report it.

This is the strongest guard available, because it fails whenever rows were dropped,
mis-columned or double-read. Verified on a real HDFC statement: deposits minus withdrawals
came to 98,999.65 against a closing-balance delta of 99,000.00, differing by exactly the 0.35
charge on the row the delta excludes.

### 6.3 No model, anywhere in the parse

Both banks print fixed, labelled columns. A language model near a number is how a ledger
becomes confidently wrong, and this lane has already burned 522,846 tokens in one day to
conclude nothing. The only optional model use in the whole design is *suggesting* accounts in
the digest, and even that is a convenience over the rules engine, not a dependency.

## 7. Data model

```sql
CREATE TABLE IF NOT EXISTS finance.statement_rows (
    row_id        text PRIMARY KEY,   -- see §8.3
    instrument    text NOT NULL,      -- canonical spelling, matches the chart
    occurred_on   date NOT NULL,
    value_date    date,
    narration     text NOT NULL,
    ref           text,               -- UTR / RRN parsed out of the narration
    direction     text NOT NULL,      -- 'in' | 'out'
    amount        numeric(14,2) NOT NULL,
    balance       numeric(14,2),      -- running balance, feeds the assertion
    statement_id  text NOT NULL,      -- bank + account + period
    matched_msgid text,               -- journal_index.message_id when matched
    posted_at     timestamptz,        -- set when hledger import wrote it
    skip_reason   text,               -- 'ambiguous' | 'transfer_counterpart' | …
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON finance.statement_rows (instrument, occurred_on);
CREATE INDEX ON finance.statement_rows (ref) WHERE ref IS NOT NULL;
```

`posted_at` is the single record of what has been posted. It is the idempotency ledger, not
hledger's `.latest` file — see §8.4.

## 8. The matcher

### 8.1 Passes

One statement row matches at most one journal transaction, and each journal transaction can
be claimed only once. Passes run strongest key first; a claimed transaction leaves the pool.

| Pass | Key | Outcome |
|---|---|---|
| 1 | `ref` — the UPI RRN in the narration against `journal_index.ref` | exact match |
| 2 | instrument + direction + amount + date window, exactly one candidate | match |
| 3 | as pass 2, more than one candidate | **no match — ambiguous** |

Never match across instruments, never across direction, never on amount alone.

Pass 3 is deliberate. Two ₹500 UPI payments in one week are indistinguishable, and choosing
one silently mis-attributes a payment.

### 8.2 The date window

A POS swipe emails at swipe time and posts one to three days later; a UPI transfer posts
same-day. The default is asymmetric — journal date within `[statement_date − 4, +1]` — and it
is **configurable, because it is a guess.** The first run must report the observed
distribution of date deltas per bank so the window is tuned from evidence. That report is a
deliverable, not a nicety.

### 8.3 `row_id`, and why the occurrence index matters

```
row_id = sha256(instrument, occurred_on, amount, narration, occurrence_index)
```

`occurrence_index` counts within the `(instrument, occurred_on, amount, narration)` group.
Without it, two genuinely separate ₹50 payments on the same day collapse into one row and
money vanishes from the books. Because the group is defined by content rather than by which
file it came from, overlapping statements covering the same day produce identical ids and
therefore dedupe correctly.

**This assumes statements begin and end on day boundaries.** Both banks do. A statement
starting mid-day would break the index and must be rejected.

### 8.4 Do not also rely on hledger's `.latest`

`hledger import` maintains a `.latest` marker and skips by **date**, not content — an
overlapping import would silently drop legitimately new rows on a date it has already seen.
Two idempotency systems in disagreement is worse than one. The generated CSV is therefore
written to a temp directory **outside the books repo**, so `.latest` lands there and is
discarded, and `statement_rows.posted_at` is authoritative.

### 8.5 Cards — the transfer trap

A bank statement's `CreditCard Payment XXXX 1313` and the card statement's payment credit are
the same money seen twice. Post both independently and the payment is double-counted while
every card purchase goes missing.

**Rule: a transfer between two accounts the owner holds is posted from the bank side only.**
The card-side row matches against that posting rather than creating its own, and is marked
`skip_reason = 'transfer_counterpart'`.

This is statement-row ↔ statement-row matching, a distinct component from row ↔ journal, and
the one to write tests for first.

## 9. Posting

### 9.1 Pending until proven

- Email-sourced transactions post as `!` (pending) — an honest claim, since nothing has
  verified them. **This changes `render_transaction`, which hardcodes `*` today.**
- A matching statement row promotes `!` → `*` by text surgery on the block found by msgid,
  adding a `stmt:` tag for provenance — same flock, same `hledger check --strict`, same
  revert on failure.
- Statement rows with no email counterpart post as `*` directly. The bank is the source.

`hledger bal -P assets:bank:hdfc:1225` then answers "what does AEGIS believe that the bank
has not confirmed?", and a transaction that never gets promoted stays pending forever — a
report you can run, rather than a silent wrong number.

### 9.2 Balance assertions come free

`hledger import` converts a `balance` CSV field into a per-row balance assertion:

```
2026-08-14 * UPI/P2M/312228170275/AUTO POINT/Paytm Pay/UPI
    assets:bank:hdfc:1225      ₹-11500.00 = ₹114256.58
    expenses:transport          ₹11500.00
```

That is far stronger than one assertion per statement: the books fail at the exact row where
they diverge from the bank.

Matched rows carry no assertion, leaving holes in the chain. That is acceptable — balances are
cumulative, so the next asserted row still catches any drift the holes let through.

**Caveat.** HDFC's own footnote says its closing balance includes funds under clearing and
excludes anything under lien. Assert on Axis first, where opening and closing rows are clean,
and treat HDFC's as advisory until a few months have been observed.

### 9.3 One rule vocabulary, generated

`hledger import` needs a `.csv.rules` file mapping narration → account. Hand-maintaining one
per bank would create a second rule system beside `rules/accounts.yaml`, and "Airtel" would
have to be taught twice.

**Generate the `.rules` file from `accounts.yaml` at import time.** One source of truth, still
user-editable and version-controlled, and a rule added through the existing `ledger_add_rule`
chat tool starts applying to statements automatically.

### 9.4 Inside the write envelope

`hledger import` writes to the journal, so it runs inside `books.py`'s flock +
`check --strict` + revert envelope. It must **not** go through `books.run_hledger`, whose
exact-match option allowlist exists to police *model-authored* arguments. This argv is built
entirely by our code, exactly as `books.declared_accounts` already is.

### 9.5 Ambiguous rows still post

Forced by §9.2, not preference: a row left unposted breaks the running-balance chain and every
later assertion fails. An ambiguous row posts to `expenses:unknown` with
`skip_reason='ambiguous'` and appears in the digest. The books stay balanced and the
uncertainty is visible rather than hidden.

## 10. Failure modes

| Failure | Detection | Behaviour |
|---|---|---|
| Wrong password | All derived candidates fail | Report the statement and account; never silently skip |
| Bank changes narration format | §6.2 arithmetic check fails | Refuse the whole statement |
| Unknown header format | Matches no pattern | `UNIDENTIFIED`, reported, not guessed |
| Statement in the wrong folder | Contents name a different account than the folder | Report the misfile; do not import |
| Balance assertion fails | `hledger check --strict` rejects the write | `books.py` reverts; surface which row |
| HDFC job purged | `input XML file not existed`, HTTP 200 | Permanent — do not retry |
| Lost session on HDFC fetch | `Internal Error occured` | Retry from the `CRSGetToken` step |
| Drive/Gmail scope missing | Granted-scope check before use | Degrade like `no_drive_scope`, never a silent zero |
| Duplicate ingestion | `row_id` collision | Skip; three dedupe layers (file, statement, row) |
| Flow dies mid-import | Temporal retry | `posted_at` makes the re-run idempotent |

Distinguishing the three HDFC failure bodies matters: only a short body after a valid token is
worth trying another password for.

## 11. Monitoring

Four checks, all on existing alert machinery:

1. **Coverage** — for each declared account, did a statement arrive for last month? Catches a
   bank silently stopping, which would otherwise go unnoticed for a year.
2. **Assertion failures** — an immediate alert, not a log line. This is the point of the lane.
3. **Match rate** — tracked per bank per month; a sharp drop means a narration format changed.
4. **Stuck files** — a file in the folder unparsed for more than a day.

## 12. Testing

Every test must be **falsifiable**: break the code it covers, watch it fail, revert. This
session has found thirteen tests that passed while proving nothing. The recurring shapes are
an assertion inside a swallowing `try/except`; an assertion routed through a lenient reader;
an assertion that stops one word short; and a test that passes because a fallback happens to
give the right answer for the wrong reason.

Specific tests this design earns:

- **Fixtures from real statements, structurally faithful and numerically altered.** Real
  layout, substituted account numbers and amounts.
- **A dropped row fails the arithmetic check.** Remove one row from a fixture; the statement
  must be refused.
- **The `Credit Card` trap.** A current-account fixture containing a `CreditCard Payment`
  narration must still identify as the current account.
- **The subject-line trap.** A statement whose subject says August and whose header says July
  must be filed as July.
- **Both Axis header formats** identify the same account.
- **The transfer trap.** A bank + card statement pair for the same payment posts it once.
- **Double import.** The same statement twice leaves the row count unchanged.
- **Overlapping statements.** A monthly and an annual covering the same period produce no
  duplicate rows.
- **Two identical payments on one day** produce two rows, not one.
- **Ambiguity never guesses.** Two candidates at equal distance produce no match.

## 13. Cost, and what "lean" means

**This lane adds no meaningful model spend.** Parsing is fully deterministic.

It also creates the opportunity to reduce the existing spend, and the numbers say where:

Production `finance.journal_index`, counted 2026-09-06 (the backfill was still running, so
absolute counts drift; the ratio is the point):

```
llm → info          92      125 of 186 model calls — 67% — produce
llm → ignore        33      rows that are neither a transaction nor
llm → due           27      a bill, and so never reach the books
llm → transaction   26
llm → failed         8
deterministic       20      rows won by the 13 hand-written parsers
free (gate/mailbox) 56      has_money_shape and mailbox rules: no call at all
```

Two thirds of the money lane's model spend produces nothing that reaches the books. Every
statement narration format taught to the parser is a deterministic parser that replaces a
model call, and statements cover 100% of transactions where email covers a fraction. Once
statements are authoritative for completeness, the email lane's job narrows to payee
enrichment and bill detection — which is where the leaning should be aimed, in a separate
piece of work, once this lane is carrying the load.

## 14. Open questions

1. **Is there an Axis personal savings account, and should it send statements?** The monthly
   `statements@axis.bank.in` mail is the Hikmah Technologies *current* account (9640) —
   established 2026-09-07 by reading the covering email, which addresses "MS. HIKMAH
   TECHNOLOGIES" and names a Current Account. No Axis personal savings statement arrives.
2. **`axis-cc-1747`, `icici-143`, `nkgsb-843`** have declared accounts and no statements. Are
   these live accounts to register for e-statements, or dormant?
3. **HSBC** — declared in the chart (`assets:bank:hsbc`, `liabilities:card:hsbc`), no
   instrument ever seen in production, no folder created. Live or not?
4. **The date window default** must be replaced by a measured value after the first run.

## 15. Build order

1. `finance.statement_rows` + the Axis PDF parser + the header-anchor identification, with the
   arithmetic check. No posting yet — parse the 12 Axis statements already in the folder and
   report what they contain.
2. The HDFC HTML parser, against the 3 statements already in the folder.
3. The matcher, run in report-only mode against the live journal. Publish the date-delta
   distribution. **Nothing is written to the books in steps 1–3.**
4. `render_transaction` emits `!`; the promotion pass; balance assertions.
5. `hledger import` of unmatched rows, with the generated rules file.
6. The transfer matcher for cards.
7. A1 intake: `statement` tag fan-out, the HDFC retrieval, Drive upload.
8. The digest and the four monitoring checks.

Steps 1–3 are safe to build and run against production data without touching the ledger, and
they are where every remaining unknown lives. That is deliberate: the first three steps buy
the evidence the rest of the design is currently assuming.
