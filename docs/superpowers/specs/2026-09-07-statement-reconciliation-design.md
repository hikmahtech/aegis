# Bank statement ingest and reconciliation — design

**Date:** 2026-09-07
**Status:** steps 1-3 built and deployed; steps 4-9 not started
**Amended:** 2026-09-09 — see §15, which supersedes §11 and revises §9.4 and the build order
**Owner lane:** Maou / money
**Builds on:** `2026-09-05-maou-books-design.md` (the books), PR #409 (`ref` column, instrument
resolution), PR #418 (`drive.file` scope)
**Companion:** `hdfc-smartstatement-recipe.md` — the verified HDFC retrieval procedure

---

## 1. Why

The books record what the banks **emailed**. Nothing checks them against what the banks
actually **did**. Two consequences, both live today:

- **The books are incomplete.** Cash withdrawals, bank charges, interest credits and auto-debits
  that send no alert are simply absent, and `assets:unknown` holds ₹53,774.56 — the largest rupee
  balance in the journal.
- **The books overstate their own certainty.** `render_transaction` hardcodes `*` (cleared) on every
  block, so all 34 journal transactions claim to be bank-cleared though not one has ever been
  reconciled with a bank, and `hledger bal --cleared` returns everything.

A bank statement fixes both: it is the complete record for its account and period, and the only
artefact that can promote a guess to a fact.

## 2. Scope

**In scope:** Axis and HDFC, savings/current and credit card, FY2026-27 to date and forward; intake
from a Drive folder and from statement emails; parsing, matching, posting, one balance check per
statement, a monthly digest and monitoring.

Three changes to existing code are in scope, because the lane cannot work without them: a
status-aware `rewrite_block`, `render_transaction` emitting `!`, and six report filters added to
`books._ALLOWED_OPTIONS` (`-P`, `-C`, `-U`, `--pending`, `--cleared`, `--unmarked`) — all in §9.1.

**Out of scope, tracked separately:** backfilling `ref`/`instrument` on the 264 existing index rows
(#406); reposting the blocks already written against `assets:unknown` (#407); the index rows with no
instrument (#408); the Notion Income/Expense import; invoice generation.

## 3. Decisions already taken

| Decision | Choice | Rationale |
|---|---|---|
| Posting engine | `books.post_event`, the path the email lane already uses | An `hledger import` block lands in `main.journal` with its msgid on the header line, so no `books.py` function can find it and no index row exists — §9.2 |
| Balance checking | One closing-balance check per statement | Per-row assertions turn a late email into a books-wide write outage — §9.3 |
| Unmatched rows | Rules first, one digest per statement | ~40–60/month would arrive uncategorised; a card per row is a chore that gets abandoned by month two |
| Cards | In from the start | The transfer trap has to be designed once, correctly; retrofitting it risks double-counting |
| Intake | Drive folder **and** email attachments | AEGIS holds statement password components; the owner accepted that trade explicitly |
| History depth | FY2026-27 to date, then forward | Overlaps the live email lane, so the matcher is exercised on real data immediately |
| Kids' accounts | Assets, in the books | Guardian-managed; transfers not expenses. Already how `hdfc:0236` was declared |

## 4. Architecture

Two subsystems separated by a filesystem boundary. That seam is deliberate: when a number is
wrong you look in the folder and tell an intake problem from a parsing one.

```
A1 — keep the folder true
  a statement email (tagged `statement`), a Gmail backfill, or a file dropped in by hand
    → fetch → decrypt → identify the account from its header
    → file into Drive: aegis-accounting/<instrument>/
A2 — turn the folder into books
  parse (deterministic) → finance.statement_rows → match against journal_index
    matched   → rewrite_event(status="*"), fixing assets:unknown
    ambiguous → digest only: nothing posted, no candidate promoted
    unmatched → books.post_event, msgid `stmt/<row_id>`, plus a journal_index row
  then one closing-balance check per statement, inside books.py's flock +
  `hledger check --strict` + revert envelope
```

### 4.1 The Drive folder

`aegis-accounting/`, one subfolder per declared account, **named exactly as the chart's instrument
spelling** so `aegis-accounting/nkgsb-843/` and `assets:bank:nkgsb:843` share one string:
`axis-9640`, `axis-cc-1313`, `axis-cc-1747`, `hdfc-1225`, `hdfc-0236`, `hdfc-0325`, `nkgsb-843`,
`icici-143`. The instrument → folder-id map is a `settings` row, not a constant in the code: this
repo is public and folder ids are one operator's. Each entry also carries a **default entity**
(`personal` or `hikmah`), because asset and liability accounts are entity-neutral and
`journal_rel(entity, date)` needs one to pick a file — without it a row on the Hikmah current account
posts `expenses:unknown` into `personal/2026.journal`. An empty subfolder says visibly that no
statement has arrived for that account, so do not delete empty ones. **The folder name is a
cross-check, never the identifier**: the account is read from inside the statement, and a file whose
contents name a different account than its folder is a misfile, to be reported and not imported.

**Hard constraint.** This folder must never be the folder `DriveSyncFlow` ingests: that flow chunks
and embeds its folder into the knowledge store, and statements carry full account numbers, customer
IDs and a PAN in the clear. The ingest must **refuse to run** against `DriveSyncFlow`'s configured
folder id, read from `activities.config` at run time — never against a hardcoded id.

## 5. A1 — intake

### 5.1 Triggers

One implementation, three ways in. **Live:** `GmailIngestFlow` already fans out per tag
(`financial`/`payments` → `MoneyProcessFlow`, `meeting` → `MeetingNotesFlow`), so a `statement` tag →
`StatementFileFlow` is the third instance of that pattern; apply the tag through `sender_overrides`,
which short-circuits the LLM, so tagging a bank costs nothing per email and no model can get it
wrong. **Backfill:** the same activity over a Gmail history query, reaching back only as far as the
bank keeps the data (§5.4). **By hand:** the owner drops a file in; A2 reads the folder, not mail.

### 5.2 Per-bank retrieval

**Axis** — an attached, encrypted PDF. Decrypt in memory with `pikepdf`, then extract text with
`pdftotext -layout` reading the decrypted bytes from **stdin**. Two measured reasons:
`pdftotext -layout` reconstructs table rows, while `pdfminer` — already a dependency — returns the
table column-by-column and rebuilt **zero** complete rows from three real statements; and
`-upw <password>` puts the password in argv, readable from `/proc`, whereas `-layout - -` reads
stdin and writes no decrypted PDF to disk. **New dependencies:** `pikepdf` and `poppler-utils` (one
line in `worker/Dockerfile`'s apt list, today only `openssh-client curl ffmpeg openssl tini git`).

**HDFC** — the statement email carries **no attachment**. It links to a JSP behind a password form, a
server token and two encryption layers; `hdfc-smartstatement-recipe.md` has the full procedure,
verified end to end. The response is an **HTML table**, so HDFC is the easier bank to parse.

### 5.3 Passwords — derive, do not store

Every scheme observed is `<first 4 letters of a name, uppercase, spaces and periods removed>` plus
one variable part:

| Statement | Variable part |
|---|---|
| Axis current | 9-digit customer ID (13 chars total; the only option Axis offers) |
| Axis card | DDMM of birth — or the card's last four |
| HDFC | DDMM of birth — or the first four digits of the customer ID |

Store the **components** encrypted (`crypto.encrypt_secret`, the `{"enc": {...}}` shape every other
AEGIS secret uses), not password strings, and derive candidates at use time, trying each in order:
both banks offer two options, so a stored string breaks the day a bank switches which one it uses
while a derived list falls through to the second. Verified 2026-09-07: 15 of 15 real statements
opened from derived components. The password never enters a log, an error or the digest — a failure
reads "the September Axis card statement could not be opened" and names the account.

This is build step 1, not step 7: the 12 Axis statements already in the folder are the emailed PDFs,
so nothing parses until derivation works.

### 5.4 HDFC links expire

A statement job is purged server-side after roughly three months. The page still renders and the
token still issues, but the POST returns `input XML file not existed` with **HTTP 200**. Therefore
**HDFC ingest runs on arrival, driven by the statement email — never as a periodic sweep over an old
mailbox.** A sweep that falls behind loses statements permanently, and fails silently at that.

### 5.5 Drive scopes

Two scopes, two jobs. `drive.file` (granted on `arshad-hikmah` by PR #418) lets AEGIS add a file;
`drive.readonly` lets it see a file the owner dropped in, since `drive.file` sees only files the app
itself created — so build step 1 needs `drive.readonly` on the hikmah token first. A token minted
before a scope existed lacks it: check granted scopes, and degrade with `doc_status=no_drive_scope`.

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

**Cards use the same check with different arithmetic:** `closing due = opening due + purchases −
payments`. They carry opening due, purchases, payments and closing due, and usually no per-row
running balance at all — which is why §8.3 keys their rows differently.

### 6.3 No model, anywhere in the parse

Both banks print fixed, labelled columns. A language model near a number is how a ledger becomes
confidently wrong, and this lane has already burned 522,846 tokens in one day to conclude nothing.
The only optional model use is *suggesting* accounts in the digest — a convenience over the rules
engine, not a dependency — so the lane adds no model spend and should eventually remove some: 125 of
186 model calls in production `finance.journal_index` on 2026-09-06 (67%) produced rows that are
neither a transaction nor a bill — and statements cover every transaction, where email covers few.

## 7. Data model

```sql
CREATE TABLE IF NOT EXISTS finance.statement_rows (
    row_id        text PRIMARY KEY,   -- see §8.3
    instrument    text NOT NULL,      -- canonical spelling, matches the chart
    occurred_on   date NOT NULL,      -- the transaction date, never the value date
    narration     text NOT NULL,      -- normalised: uppercase, whitespace collapsed
    ref           text,               -- UTR / RRN parsed out of the narration
    direction     text NOT NULL,      -- 'in' | 'out'
    amount        numeric(14,2) NOT NULL,
    balance_after numeric(14,2),      -- running balance; NULL on cards
    statement_id  text NOT NULL,      -- bank + account + period
    file_sha256   text NOT NULL,      -- the source file; a regenerated period is a 2nd file
    matched_msgid text,               -- journal_index.message_id when matched
    candidates    jsonb,              -- msgids an ambiguous row could not choose between
    posted_at     timestamptz,        -- set when post_event wrote a block for this row
    skip_reason   text,               -- 'ambiguous' | 'transfer_counterpart' | 'reversal' | …
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON finance.statement_rows (instrument, occurred_on);
CREATE INDEX ON finance.statement_rows (ref) WHERE ref IS NOT NULL;
```

`posted_at` is a marker for the digest, not the idempotency ledger. Idempotency is the msgid inside
the block: `post_event` finds `stmt/<row_id>` and will not write it twice, which survives a crash
between the journal commit and the Postgres stamp — a `posted_at` check alone does not.

## 8. The matcher

### 8.1 Passes

One statement row matches at most one journal transaction, and each journal transaction can be
claimed only once; passes run strongest key first, and a claimed transaction leaves the pool. Never
match across direction or on amount alone, and compare instruments through `canonical_instrument()`
on both sides — live index rows carry `card-1313`, `nkgsb-0843`, `nkgsb-8443` and `axis-1`.

| Pass | Key | Outcome |
|---|---|---|
| 1 | `ref` — the UPI RRN in the narration against `journal_index.ref` | exact match |
| 2 | instrument + direction + amount + date window, exactly one candidate | match |
| 2b | as pass 2, over candidates with `instrument IS NULL`, scoped to the instrument's entity | match |
| 3 | more than one candidate in pass 2 or 2b | **no match — ambiguous** |

**Pass 2b is what makes §1 true.** Ten of the 34 live blocks carry no instrument at all
(`channel: receipt`/`other`, posted against `assets:unknown`) — the rows this lane exists to fix.
Without it each stays unmatched, posts a second block against the real bank account, and the expense
is counted twice, uncaught by any balance check because the receipt's block sits on
`assets:unknown`. Promotion has to move it (§9.1).

**Pass 3 is deliberate:** two ₹500 UPI payments in one week are indistinguishable, and choosing one
mis-attributes a payment silently. **Pass 1 is nearly dead for the backfill period**, because
`journal_index.ref` is filled only by the deterministic parsers and the LLM path never sets it — so
pass 2 does almost all the work for FY2026-27, and §8.2's report must count pass-2 matches or it
describes a few dozen rows while reading as evidence about all of them.

### 8.2 The date window

One window for the whole lane: reuse `journal_index._MATCH_DAYS` (3 days, symmetric) rather than a
second constant, because the receipt↔bank and statement↔journal pairs are the same guess about the
same lag — a POS swipe emails at swipe time and posts one to three days later, a UPI transfer
same-day. It is still a guess, so the first run must report the observed distribution of date deltas
per bank, and `_MATCH_DAYS` changes only from that measurement. The report is a deliverable.

### 8.3 `row_id`

```
row_id = sha256(instrument, occurred_on, direction, amount, balance_after, occurrence_index)
```

`occurred_on` is the **transaction date**, not the value date: HDFC prints both, and the running
balance is in transaction-date order. **The running balance is in the key because it is
bank-authoritative and layout-independent, and narration is not** — §6.1 documents two Axis layouts
for one account, `pdftotext -layout` wraps and truncates long UPI narrations by column width, and
the backfill (netbanking) overlaps the live lane (mailed monthly) by design, so a narration-keyed id
hashes the same row twice across that overlap and posts the money twice. Keep a normalised narration
for display and the rules, nothing else. `direction` is in the key because a same-day debit and
credit sharing one narration would otherwise share a group and take ids by order.

`occurrence_index` counts within the `(instrument, occurred_on, direction, amount, balance_after)`
group; without it two genuinely separate ₹50 payments on one day collapse into a single row and
money vanishes from the books. The group is content-defined rather than file-defined, so overlapping
statements covering the same day produce identical ids and dedupe correctly. **Cards have no running
balance** and fall back to the normalised narration in that slot. **This assumes statements begin
and end on day boundaries** — both banks do; a statement starting mid-day must be rejected.

### 8.4 Cards — the transfer trap

A bank statement's `CreditCard Payment XXXX 1313` and the card statement's payment credit are the
same money seen twice: post both independently and the payment is double-counted while every card
purchase goes missing. **Rule: a transfer between two accounts the owner holds is posted from the
bank side only.** The card-side row matches that posting rather than creating its own, marked
`skip_reason = 'transfer_counterpart'`. This row ↔ row matching is a distinct component from row ↔
journal, and the one to write tests for first.

**Own-account detection runs before the rules, not after.** Pull the last four digits out of the
narration and look them up against the declared `liabilities:card:*` and `assets:bank:*` accounts
with `_declared_with_tail`; a hit sets the counter account directly. Left to the rules,
`CreditCard Payment XXXX 1313` matches nothing, lands in `expenses:unknown`, and the card liability
drifts by the full bill every month. The email lane posts card bills and IMPS transfers to
`equity:transfers`, so promotion of such a block rewrites it to the far side the pair proves.

### 8.5 Foreign currency, and reversals

**Foreign currency.** The live journal posts `$200.00`, `$4.00`, `$29.00`, `$5.89` and `-£6285.01`
against `liabilities:card:axis:1313` and `assets:bank:axis:9640`, while the card statement shows
them in ₹ with a markup. Amount equality never matches across currencies, so each would become an
unmatched row and post again in rupees — and nothing catches it downstream, because hledger's `=`
assertion is **per commodity**: an account can hold a permanent dollar balance beside a correct
rupee one and still pass. Match a foreign-currency candidate when
`abs(stmt − journal × latest_prices[symbol]) ≤ 5%`, the FX markup band, and on promotion rewrite the
posting to cost notation, `$4.00 @@ ₹338.12`. The £ remittances are the same case in reverse.

**Reversals.** A failed UPI is a debit and a same-day re-credit with the same narration, and the
email lane records it as `kind='failed'`, which is not a transaction — so both rows would be
unmatched and post as an `expenses:unknown` + `income:unknown` pair that sits in the digest forever.
Pair same-day, equal-amount, opposite-direction rows sharing a ref or narration and post both to one
counter account tagged `reversal`: net zero, balance intact. Refunds already match on pass 2.

## 9. Posting

### 9.1 Pending until proven

Email-sourced transactions post as `!` (pending) — an honest claim, since nothing has verified them;
a statement row with no email counterpart posts `*`, because the bank is the source. **This changes
`render_transaction`, which hardcodes `*` today.** `render_manual` stays `*`: a hand-typed
`ledger_post` is the owner asserting the fact, and its `manual_msgid` is a hash of the rendered
block, so changing that rendering breaks idempotency for a retry straddling the deploy. A matching
statement row promotes `!` → `*` through
`rewrite_event(msgid, status="*", add_tags={"stmt": statement_id})` — same flock, same
`hledger check --strict`, same revert on failure — and **also fixes the account**: when the matched
block's second posting is `assets:unknown` (pass 2b), rewrite it to the statement's account, as
`money.py:652–658` does for the receipt↔bank pair, or the promoted block leaves the bank account
short and §9.3's check fails.

**`rewrite_block` must parse the status first.** Today it splits the header on the literal `" * "`;
on a pending block it finds nothing, keeps the whole header as the date part and writes
`2026-09-02 ! Jai shree nakoda * Corner Store` — a pending transaction whose description has
swallowed the old payee, which `check --strict` accepts and nothing reverts. Every `rewrite_event`
caller hits this on the first `!` block: receipt↔bank enrichment, `ledger_reclassify`, the
`ledger_add_rule` sweep, the curiosity answer hook. Parse with
`^(\d{4}-\d{2}-\d{2})\s+([*!])?\s*(.*)$` and add a `status=` kwarg. **The 34 blocks already written
stay `*` unless rewritten**, so the step that flips the rendering also does a one-off `*`→`!` pass
over them; without it §1's complaint survives for everything posted so far.

`hledger bal -P --pending assets:bank:hdfc:1225` then answers "what does AEGIS believe the bank has
not confirmed?", and a transaction never promoted stays pending forever — a report you can run
rather than a silent wrong number. That is why `_ALLOWED_OPTIONS` gains the six filters from §2.

### 9.2 Unmatched rows post through `post_event`

Each unmatched row becomes a `MoneyEvent(kind='transaction', channel='statement',
source_class='bank', instrument=…, ref=…)` with msgid `stmt/<row_id>`, written by `books.post_event`
and indexed by `journal_index.upsert(mailbox='statement')` — the same path the email lane uses.
`hledger import` is not the engine, for three measured reasons: it appends to `main.journal`, which
`books.journal_files()` never globs, and writes the msgid on the header line where `find_block`'s
needle (`    ; msgid: <id>`) can never match; it writes no `journal_index` row, so
`ledger_reclassify`, the `ledger_add_rule` sweep and the brief's Unexplained list cannot see an
imported row at all; and it cannot choose a file per row, which §4.1's entity map requires.

**The account comes from `books.apply_rules(rules, "", narration, direction=…)`, in Python.** One
rule vocabulary is right; generating an hledger `.rules` file to get it is not, because hledger's
`if` conditions cannot express the rules `accounts.yaml` already holds:

| yaml / Python regex | hledger `if` |
|---|---|
| `(?i)`, `(?:…)`, `(?=…)`, `(?<!…)`, `.*?` | **hard error** — the file is refused, so one such rule fails every import |
| `\d`, `\w` | **silently never match** |
| `$` on a payee pattern | anchors the whole CSV record, not the field |
| bare `if` scope | matches any column — `\-50` hit the amount, `^2026` hit the date |
| `\|[^\|]*…`, which `rule_match_for` writes | 0 hits — a CSV record has no pipe |
| `direction` | needs an extra `& %withdrawal .` condition per rule |
| `entity` | picks a file; hledger cannot |

The live rules file happens to translate today, using only `|`, `\.` and `.*`; the next rule
`ledger_add_rule` or the curiosity detector writes may not, and nothing validates that.
`apply_rules` is direction-aware and runs the haystack the sweep runs. Own-account detection (§8.4)
runs before it; a row that neither resolves lands in `expenses:unknown` and in the digest, where
`ledger_reclassify` can move it — which works only because there is an index row.

### 9.3 One closing-balance check per statement

After posting a statement, inside the write envelope, compare hledger's balance for the account at
the statement's closing date against the statement's closing balance and raise `BooksCheckError` on
a mismatch; `books.py` then reverts the whole write. Cards use the arithmetic in §6.2.

**No per-row balance assertions**, for one reason above all: `_check_sync` runs
`hledger check --strict` on **every** write, so one stale assertion is a books-wide write outage —
the email lane, `ledger_post`, `ledger_add_rule` and prices all fail and revert until a human edits
the journal by hand. They go stale routinely, because a matched row keeps its email date, up to
`_MATCH_DAYS` before the bank's, while hledger evaluates an assertion against every posting to that
account dated on or before it. Verified: the bank shows −100 then −50 on the 10th, the email for the
second is dated the 9th, and `check --strict` fails on the *first* row ("asserted ₹900, calculated
₹850"). On equal dates the outcome depends on include and append order, which nothing controls. One
check per statement survives both.

**The ordering rule that follows:** once a period is reconciled for an account, an email-lane
transaction dated inside it is index-only — matched to a statement row, or flagged in the digest —
never posted. The statement is complete, so anything later is already in it or is a discrepancy.

**Caveat.** HDFC's own footnote says its closing balance includes funds under clearing and excludes
anything under lien, so check Axis first and treat HDFC's as advisory for a few months.

### 9.4 Ambiguous rows are not posted

> **Revised by §15.6.** The rule stands. What is added is a
> destination for the uncertainty — a hub finding, and eventually a card — instead of a digest
> line nobody actions.

An ambiguous row means two or more journal transactions already carry this amount on this instrument
in the window. **One of them is this row**, so posting it adds a third copy of the money that the
balance already includes through the candidate. Store `skip_reason='ambiguous'` with the candidate
msgids in `candidates`, list the row and its candidates in the digest, and promote none until a
person picks: the books stay balanced, §9.3's check still passes, the uncertainty stays visible.

## 10. Failure modes

| Failure | Detection | Behaviour |
|---|---|---|
| Wrong password | All derived candidates fail | Report the statement and account; never silently skip |
| Bank changes narration format | §6.2 arithmetic check fails | Refuse the whole statement |
| Unknown header, or a file in the wrong folder | Matches no pattern, or its contents name another account | `UNIDENTIFIED` or misfile — reported, never guessed at and never imported |
| Closing balance disagrees | The §9.3 check, inside the write envelope | `BooksCheckError`; the whole statement reverts; alert |
| A commodity hledger cannot price | The §9.3 check's `hledger balance -X ₹` returns a multi-commodity cell, exit 0 | `BooksCheckError` naming the commodity; that statement reverts and the run carries on. Never take the ₹ part alone — that understates the movement and passes a wrong ledger |
| HDFC job purged | `input XML file not existed`, HTTP 200 | Permanent — do not retry |
| Lost session on HDFC fetch | `Internal Error occured` | Retry from the `CRSGetToken` step |
| Drive/Gmail scope missing | Granted-scope check before use | Degrade like `no_drive_scope`, never a silent zero |
| Duplicate ingestion, or a flow dying mid-post | `row_id`, then the msgid already in a block | `post_event` skips; the re-run is idempotent whether or not `posted_at` was stamped |

Only a short body after a valid token is worth another password — distinguish the three HDFC bodies.

## 11. Monitoring

> **Superseded by §15.4.** The "existing machinery" this section assumed did not
> exist for the money lane. It does now — the problem hub — and both alerts plus their recovery
> collapse into one `reconcile_findings` call. The requirements below still hold; the mechanism
> does not.

Two alerts, both on existing machinery. **Coverage** — did a statement arrive last month for each
declared account? That catches a bank silently stopping, which would otherwise pass unnoticed for a
year. **Closing-balance mismatch** — immediate, not a log line; the point of the lane. The match rate
per bank, and any file left unparsed for over a day, are digest lines rather than alerts.

## 12. Testing

Every test must be **falsifiable**: break the code it covers, watch it fail, revert — this session
found thirteen tests in this repo that passed while proving nothing. Fixtures come from real
statements, structurally faithful and numerically altered. The tests this design earns:

- **A dropped row fails the arithmetic check** — remove one row, the statement is refused.
- **The `Credit Card` trap.** A current-account fixture containing a `CreditCard Payment` narration
  still identifies as the current account.
- **The subject-line trap.** A statement whose subject says August and header says July files as
  July, and **both Axis header formats** identify the same account.
- **A matched row dated before its bank row** does not fail the next write.
- **`rewrite_block` on a `!` block** leaves a well-formed header: status still `!`, payee replaced,
  description not swallowed.
- **A `$4.00` journal candidate matches a `₹338` statement row**, and promotion writes cost notation.
- **An ambiguous row is not posted** — two candidates at equal distance give no match, no block and
  an unchanged journal.
- **A `CreditCard Payment` bank row credits the card liability**, not `expenses:unknown`, and a bank
  + card statement pair for the same payment posts it once.
- **Double import.** The same statement twice, and overlapping statements in two different layouts,
  leave the row count unchanged — while **two identical payments on one day** produce two rows.

## 13. Open questions

1. **Is there an Axis personal savings account, and should it send statements?** The monthly
   `statements@axis.bank.in` mail is the Hikmah Technologies *current* account (9640). No Axis
   personal savings statement arrives.
2. **`axis-cc-1747`, `icici-143`, `nkgsb-843`** have declared accounts and no statements. Live
   accounts to register for e-statements, or dormant?
3. **HSBC** — declared in the chart (`assets:bank:hsbc`, `liabilities:card:hsbc`), no instrument ever
   seen in production, no folder created. Live or not?
4. **The date window** stays at `_MATCH_DAYS` until step 3's measurement says otherwise.

## 14. Build order

> **Revised by §15.9.** Steps 1-3 are built. Step 4b is new, step 8 mostly
> disappears, and an optional step 9 is added.

1. The migration, password derivation (§5.3), the `drive.readonly` scope check (§5.5), the Axis PDF
   parser and header-anchor identification (§6.1), with the arithmetic check (§6.2).
2. The HDFC HTML parser, per `hdfc-smartstatement-recipe.md`, against the 3 statements already in
   the folder.
3. The matcher in report-only mode against the live journal, **including the NULL-instrument (pass
   2b) and foreign-currency (§8.5) candidate classes**, so the published date-delta distribution
   covers the rows that will match. **Nothing is written to the books in steps 1–3.**
4. `rewrite_block` status support, `render_transaction` emitting `!`, and the one-off `*`→`!` rewrite
   of the existing email-sourced blocks (§9.1).
5. Posting through `post_event`, with the closing-balance check (§9.2, §9.3).
6. The transfer matcher and own-account detection for cards (§8.4).
7. A1 intake: the `statement` tag fan-out, HDFC retrieval, Drive upload.
8. The digest and the two alerts (§11).

Steps 1–3 are safe to run against production data without touching the ledger, and they are where
every remaining unknown lives — deliberately, because they buy the evidence the rest assumes.

---

## 15. Amendment 2026-09-09 — this lane rides the problem hub

### 15.1 Why this is an amendment

The books shipped 2026-09-05. The problem hub's design landed 2026-09-07. This spec was written
the same day, and the two never met: no file in the money lane calls `hub.ingest_event`, money
never opens an `interactions` card, and `hub.SOURCES` — a closed vocabulary — has no money entry.

So §9 and §11 hand-roll four primitives the hub already owns, and one of them was a live bug.
`post_money_event` gated `mark_due_paid` on the Todoist close succeeding; `mark_due_paid` has one
caller and nothing re-drives it, so every close failure left a paid bill in every "dues open"
count forever (#449, fixed in #450). The hub's rule — the task is a projection, never the
identity — is exactly what was missing.

Steps 1–3 are built and unaffected. This amendment changes steps 5, 8 and 9.4, and adds a small
step 4b.

### 15.2 What the hub already owns

| Concern | Hub mechanism |
|---|---|
| Identity | `correlation_key = '{class}:{subject_kind}:{subject}'`, one pure function |
| Idempotency | `UNIQUE (source, external_id)` on `problem_events`, read under an advisory lock taken **first** |
| One-open-per-thing | Partial unique index `problems_open_key … WHERE closed_at IS NULL`, never application code |
| Human surface | `hub_project.project` renders a Todoist task; `problem_links` is the only reverse lookup; nothing is ever parsed back |
| Lifecycle | `decide()`, a pure transition table; every transition also writes a `state_change` row, so the timeline is a query, never a diff |
| Recovery | `hub_watch.reconcile_findings` resolves the **complement** of the current findings set, in the same call |
| Many victims, one cause | `hub_group` folds them into one problem |
| Noise | `service_state` windows and `muted_until` record and count everything, and withhold only projection |

### 15.3 Prerequisite — new build step 4b

Small, and everything below depends on it:

1. Add `"money"` to `hub.SOURCES`. Precedent: `expiry` (cert_radar) and `social` (stuck posts)
   already ride the hub and neither is infrastructure.
2. One `Event` builder in the money lane mapping a reconciliation finding to a hub event.
   Classes, all deterministic: `closing_balance`, `unmatched_rows`, `ambiguous_row`,
   `statement_missing`, `unparsed_file`, `unscoped_instrument`, `missing_rate`,
   `password_failed`. `subject_kind` is `instrument` for account-scoped findings and
   `statement` for file-scoped ones.

### 15.4 §11 Monitoring is replaced, not extended

§11 named two alerts and assumed "existing machinery" that does not exist for this lane. It does
now, and it is one call rather than two alert paths.

`match_statements` **already returns the findings set** — `MatchRun.unscoped_instruments`,
`MatchRun.missing_rates`, and the per-statement `StatementSummary` counts. The reconciliation
sweep hands them to `hub_watch.reconcile_findings(source="money", subject_kind="instrument",
classes=[…], findings=[…])`, and three things follow for free:

- a new finding becomes a problem and earns a Todoist task;
- a finding that has **gone** since the last tick resolves itself — there is no close path to
  write, which is the half every hand-rolled watchdog gets wrong;
- coverage ("no statement last month for a declared account") is a finding like any other, and
  stops being one the day the statement lands.

The closing-balance mismatch of §9.3 is an event rather than a finding: it happens on arrival,
not on a sweep, so `BooksCheckError` becomes an `ingest_event` with `klass="closing_balance"`,
`severity="critical"`, `subject=<statement_id>`.

**That distinction is load-bearing and was left ambiguous in the first draft of this section.**
`reconcile_findings` resolves any problem whose class is in its `classes` list and which is not
among this tick's findings. An arrival-time class therefore must **not** appear in the sweep's
`classes`: include it and every mismatch is resolved on the very next tick, because the sweep never
"finds" a thing it does not produce. So `closing_balance` is ingested directly, resolved directly
(by the next statement for that account reconciling), and never handed to the sweep. Sweep-produced
classes — `unmatched_rows`, `unscoped_instrument`, `statement_missing` — are the only ones in
`classes`.

The same care applies to `subject_kind`. `reconcile_findings` takes exactly one per call, so the
`statement`-scoped classes (`unparsed_file`, `password_failed`) need their **own** call with their
own `classes` list. Mixing them into the instrument-scoped call would let each set resolve the
other, because neither appears among the other's findings.

**Deleted from the plan:** the bespoke alert wiring. **Kept:** the monthly digest, which is a
report, not an alert.

### 15.5 Grouping is what makes step 5 survivable

Step 5 posts roughly 959 transactions into books that hold 34. The number was frightening
because 959 things cannot be reviewed. Grouped, they are one problem per account —
"214 unmatched rows on `axis-cc-1313`" — with a live count that falls as the chart and the rules
improve, and the 215th row joins it instead of opening another task. Without grouping, step 5 is
a nagging machine and would be abandoned in week one, exactly as §3 predicted for per-row cards.

**Correction (2026-09-09, after review).** The paragraph above originally said to reuse
`hub_group` with the judge omitted. That was wrong twice, and the mistake is worth keeping visible
because it is easy to make again.

First, **the grouping is already done by the correlation key.** A finding whose subject is the
instrument produces one problem per account by construction —
`unmatched_rows:instrument:axis-cc-1313` — with the row count in the title and `payload`. That IS
"214 unmatched rows on axis-cc-1313". `hub_group` adds nothing to it.

Second, **`hub_group` would actively make it worse.** `hub_group.candidates` clusters live problems
by `(class, subject_kind)` across three or more *different subjects*, and it has **no source
filter**. So once three instruments carry `unmatched_rows`, the existing `HubSweepFlow` offers the
cluster to the billed `judge_group` on its next tick, and a "yes" folds every account into a single
`unmatched_rows:instrument:*` group — the exact opposite of one problem per account, and reached
without this lane doing anything at all.

So the requirement is the reverse of what was written: **money findings must be kept out of the
sweep's grouping**, by a `source` filter in `hub_group.candidates` or by an explicit non-groupable
rule of the kind `class = 'manual'` already has. Whichever is chosen, it is a change to
`hub_group`, and it belongs to step 8 rather than being free.

The one rule that does carry over unchanged: a group's subject is `*` and can never appear among
the findings, so it recovers only when its class stops being found **at all** — check membership by
class, not by subject.

### 15.6 §9.4 gains a destination, and keeps its rule

The rule does not change: an ambiguous row is still not posted, for the reason §9.4 gives — one
of the candidates already carries this money. What changes is that the uncertainty now has
somewhere to go instead of a digest line nobody actions. An ambiguous row is a
`klass="ambiguous_row"` finding; resolution is a person picking a candidate, through an
`InteractionFlow` card carrying the numbered candidates.

Sequence it, though: a card per row is the chore §3 rejected, and at 959 rows it would be worse
than the digest. Group first, keep ambiguous rows report-only, and turn the card on once the
residue is small enough to be a handful a month.

### 15.7 What NOT to take from the hub

- **`AlertInvestigationFlow`'s investigation half.** A closing-balance mismatch is not a code
  bug, so the coding-CLI step and the repo resolver are wrong here. Take the identity and, if
  anything, the Gate-2 card. The simplest correct version starts no flow at all: ingest, project,
  done.
- **`class='manual'` non-groupability.** That rule protects a hand-written task's sessions and PR
  links. Nothing here has those.
- **The 30-minute collapse window.** Statements arrive monthly; there is nothing to collapse.

### 15.8 Two rules the hub learned the hard way, which apply here

- **A money-lane comment needs its own recognisable footer.** Every hub comment carries
  `Workflow run: problem-hub` so clarify's loop guard and `work_sessions.is_user_note` exclude
  it. A projector without one re-reads its own comments as human signal — the self-grading loop
  that made 39 of 39 "user corrections" fake.
- **Every guard fails open, and records what it suppresses.** `service_state` failing to read
  suppresses nothing rather than hiding real occurrences. #449 was the opposite: a Todoist API
  failure decided whether the books believed a bill was paid. When a check about *noise* can
  change what the *record* says, it is in the wrong place.

### 15.9 The build order this replaces

| Step | §14 said | Now |
|---|---|---|
| 4 | `!` status, `rewrite_block`, one-off rewrite | **not unchanged** — see §15.10 |
| **4b** | — | **new:** `"money"` in `hub.SOURCES` + the money `Event` builder |
| 5 | post through `post_event` + closing-balance check | + `ingest_event` on mismatch; + grouped `unmatched_rows` findings |
| 6 | transfer matcher, own-account detection | unchanged |
| 7 | A1 intake | unchanged; `password_failed` and `unparsed_file` become findings |
| 8 | digest and two alerts | **mostly deleted** — `reconcile_findings` + deterministic grouping. Digest stays |
| **9** | — | **new, optional:** the ambiguous-row card, once the residue is small |

Net: one step of bespoke alerting removed, one small step added, and step 5 becomes reviewable at
959 rows instead of unusable.


### 15.10 What step 4 shipped, and what it left (2026-09-09)

PRs #453 and the follow-up did the code: `rewrite_block` parses the header and takes `status=`,
`rewrite_events` takes `status=` and `message=` so the one-off pass has a bulk tool,
`render_transaction` takes `status` (defaulting to `!`), and `_ALLOWED_OPTIONS` gained the six
status filters §2 named. A review caught three things the first cut got wrong, all now fixed:

- **`render_transaction` hardcoded `!`.** §9.1 says a statement row with no email counterpart posts
  `*`, because the bank is the source. Step 5 posts those through `post_event`, so every
  bank-proven row would have been written "unverified" — the lane's purpose inverted on its first
  run. It is a parameter now.
- **The reference key did not join.** `llm._ref_from_body` verified the model's answer on
  alphanumerics but stored it verbatim, while `statement_match._norm_ref` only stripped and
  uppercased — so a reference copied with the bank's own spacing never equalled the statement's
  bare digits. Pass 1 silently found nothing, which is indistinguishable from "no counterpart". One
  normalisation now runs on both sides. A leading label is deliberately **not** stripped: real
  references begin with letters (`SBIN0000123456`, the Axis SWIFT `GBC…`).
- **Pass 1 could join the wrong payment.** It matched on reference and direction alone, which was
  right while only deterministic parsers set `ref` — those lift it from a fixed slot in a bank's own
  alert. An extracted reference is a different object with the same name: `_ref_from_body` can only
  check the characters are in the mail, never whose payment they name. An extracted reference now
  needs the amount to agree; a parsed one still matches alone, because a card auth and its
  settlement can differ by a tip and the reference is what knows they are one payment.

**Still outstanding, and both need the owner's say-so** because they write to `hikmahtech/books`:
the one-off `*`→`!` pass over the blocks written before this lane existed (§9.1 — without it an old
unproven `*` is indistinguishable from a step-5-proven one), and step 5 itself.

**Two gaps this review named that are still open**, and the next builder will hit them:

1. **§9.3's closing-balance check has a date problem.** A promoted block keeps its *email* date, up
   to `_MATCH_DAYS` before the bank's, so hledger's balance at the statement close counts a block
   dated 31 July whose bank posting is 2 August, and excludes the reverse. The check then misses by
   exactly those amounts and reverts the whole statement's write. `rewrite_block` has no `date=` to
   align a promoted block to the bank's date. This is the one that makes step 5 un-shippable rather
   than merely noisy, and it is unsolved.
2. **The "reconciled through" watermark has no home.** §9.3's ordering rule — an email transaction
   dated inside a reconciled period is index-only — needs a per-account watermark. Nothing in
   `migrations/` stores one and `post_money_event` has no such gate.

### 15.11 What steps 5–8 shipped, and what the real data changed (2026-09-10)

**Both gaps §15.10 left open are closed.**

1. **The closing-balance date problem is solved, and not the way §15.10 assumed.** It named the
   fix as a `date=` on `rewrite_block` so a promoted block could be pulled onto the bank's date.
   `rewrite_block` gained that (`on=`) and step 5 uses it — but the check itself was redesigned as
   well, and that is the half that mattered. §9.3's original "balance at the statement close" is
   cumulative, so an account's very first statement could never pass and one gap anywhere broke
   every statement after it. It is now **movement over the rows** — `hledger balance --cleared`
   between the first and last row dates — which stands alone, so a backfill runs in any order and
   a missing month costs only that month. `--cleared` is what makes it honest: an unproven `!`
   block does not count toward a figure whose whole job is to say whether the bank agrees.

   **The window is the row span, not the printed period, and not the union of the two.**
   `expected_movement` is `closing_balance - opening_balance`, and in both layouts the opening
   figure is the balance immediately before the first row — a bank statement's is re-derived from
   the first row's `balance_after` minus that row, a card's `Previous Balance` is the previous
   statement's closing. So the movement the bank claims is the ROWS' movement. The printed period
   alone drops a card's first two days (its rows run `period_start - 2` to `period_end - 1`), and
   the union runs to `period_end`, which is the day the NEXT statement's rows begin: measured on
   the three real Axis card statements, whose row spans are contiguous and never overlap
   (18/05-17/06, 18/06-17/07, 18/07-17/08) while every union window ends on the next one's first
   row. That union broke the order-independence this same paragraph claims.

   **The same window answers "did the far side of this transfer land?"** A `transfer_counterpart`
   is added back only when its block is outside the figure hledger returned, so that question must
   be asked of the dates hledger was asked. Asked of the printed period instead, a far block dated
   in a day the window covers and the period does not is counted in the movement AND added back on
   top of it. `post_statement` computes one window and passes it to both.

   **And a card's sign is flipped in exactly one place.** hledger reports a liability negative when
   you owe and a card statement prints what you owe as positive, so BOTH figures the check compares
   — the cleared movement and the unwritten total, which `signed()` also builds in hledger's
   convention — are turned round together, inside `movement_disagreement`. Flipping one and not the
   other makes the check wrong by twice any skipped row, and a card's own payment row is skipped on
   every statement.
2. **The watermark has a home**: `finance.reconciled_through`, one row per account, advanced only
   forward and only in SQL — a backfill posts statements in whatever order the operator has them,
   so reconciling June after July must not un-reconcile July. `post_money_event` reads it and
   indexes rather than posts an email transaction dated inside a reconciled period.

   The gate sits at the site that writes a NEW block, deliberately **not** at the top of the
   method. A late email that links to a block its counterpart already posted (§5.4) is the lane
   working as designed and must still enrich it; only a fresh block is a duplicate. An early
   return turns away both, and reads as correct.

**A credit-card statement supports the closing-balance check after all.** The lane was built
assuming it could not — `statement_post._NO_BALANCE` exists for exactly that case — because a card
prints no running balance. It prints something better: `Previous Balance` and `Total Payment Due`
as figures of their own, and its own row of totals (`- Payments - Credits + Purchase + Cash Advance
+ Other Debit&Charges`) which reconciles to the rupee. So §6.2's self-validating check on a card is
stronger than a running balance, not weaker: the parsed `Cr` rows must sum to the bank's payments
plus credits and the `Dr` rows to its purchases plus charges, and only a figure the bank printed
itself can falsify the parse.

Two consequences worth stating, because both were assumptions the code carried:

- `finance.statement_rows` stored no opening or closing balance. A bank statement's could be
  re-derived from the first row's running balance; a card's cannot. Those figures were read at
  intake and thrown away. `finance.statements` (migration 042) is where they now live, and it is
  also what makes §15.4's `statement_missing` finding answerable — coverage is a question about
  statements, and only their rows were stored.
- A card row prints its foreign original beside the rupee charge (`( USD 5.89 )`). §8.5 otherwise
  has to reach it through `prices.journal` and a 5% band, and the live rates are current rates:
  a 2024 remittance whose narration says `GBP 6293.48` implies about ₹102 against a file that says
  ₹127.76 — 25% apart, so historical foreign rows never match through the price file. Where the
  bank printed the original, use the original.

**Own-account detection (§8.4) cannot be a tail scan, and the production narrations say why.**
Both failure directions are real and both were found in live data:

- `POS/GOOGLE PLAY SER C/…/010325/14:22/…` — `010325` is a date written ddmmyy. It contains
  `0325`, which is a declared account. A tail scan files a Google Play purchase into a child's
  savings account, and §9.3's check still passes, because both accounts are real. **A wrong
  own-account match is invisible to every downstream guard.**
- `IMPS/P2A/…/MOHAMMEDARSHADANS AR/X071225/HDFCBANKLTD/` — a genuine transfer to the owner's own
  `hdfc-1225`, where `1225` sits inside the longer run `071225`. The obvious guard against the
  first case ("the digits must not be part of a longer run") throws this one away.

What separates them is not the digits. It is that a real own-account reference carries a **mask
marker** — and the marker's width varies within one bank: `X071225`, `CREDITCARD PAYMENT XX 1313`,
`CREDITCARD PAYMENT XXXX 1313`, `HIKMAHTECHNOLOGIES.-UTIB-XXXXXXXXXXX9640-IMPS`. A regex fixed at
four `X`s misses half the rows. The second condition is the **bank named in the narration**, which
is present far more often than not (`HDFCBANKLTD`, `ALLAHABADBANK`, `CANARABANK`) and is what
refuses a third party whose masked tail happens to collide.

The card side of a card payment names neither account nor bank — `BBPS PAYMENT RECEIVED - BD…` —
so it gets its own rule rather than being forced through the tail matcher: a credit on a card
statement announcing a payment received is the counterpart of a bank-side `CREDITCARD PAYMENT`
row, because a card bill is never paid by a third party.

**Row↔row pairing must not be what decides the counter account.** Production holds at least six
`IMPS/P2A/…/X071225/HDFCBANKLTD/` rows at exactly ₹100,005.90, and more at ₹5,005.90, ₹10,005.90,
₹20,005.90 (twice), ₹25,005.90 and ₹30,005.90. Amount-and-window pairing across two statements
cannot tell those apart and does not need to: own-account detection already gives both sides the
right accounts, and pairing is left with the one job it can do reliably — deciding which side
posts and which side skips as `transfer_counterpart`. A wrong pairing among identical transfers
must therefore be harmless, and that is a property to test rather than to assume.

**One correction to §15.10's `unwritten` accounting**, which step 6 makes reachable. §9.3 adds
deliberately-unwritten rows back into the comparison so a statement containing an ambiguous row
does not fail by exactly its amount. A `transfer_counterpart` row is the opposite case: the far
side of the pair put that money in the books, so it IS in the cleared total, and adding it back
would revert a statement that was never wrong. "Skipped" is not one category — the question is
whether the money reached the journal, and the two answers differ per reason.

**Open question 2 has half an answer.** `axis-cc-1747` has a configured Drive folder and zero
files in it, so `axis-cc-1313` is the only card that sends statements. `icici-143` and `nkgsb-843`
are still unanswered.


### 15.12 Correction — what §15.11 got wrong, and the review that found it (2026-09-10)

§15.11 was written from the design and not from the code. An adversarial review executed against
this branch, and two of its claims were false. Both corrections are here rather than edited into
§15.11, because a spec that quietly rewrites itself teaches nothing.

**§15.11 claimed a "bank named in the narration" guard on `own_account`. There was none.** The
function required a transfer marker and a 3-to-6-digit run and nothing else, so ordinary invoice,
PO, flat and reference numbers matched the declared three-digit tails. Executed against the real
chart:

```
NEFT/…/ACME PVT LTD/INV 143              -> assets:bank:icici:143
RTGS/…/CLIENT CO/PO 236                  -> assets:bank:hdfc:0236
IMPS/…/RAVI KUMAR/FLAT 325/              -> assets:bank:hdfc:0325
IMPS/…/RAVI KUMAR/XXXXXX1225/ICICIBANK/  -> assets:bank:hdfc:1225
```

A client paying an invoice is then recorded as a transfer from the owner's own account: the income
disappears, the other account drifts, and **both closing-balance checks still pass**, because both
accounts are real and the journal balances. This is the failure mode the module docstring already
named as its worst case, and the code did not implement the guard against it.

The rule now has five conditions, and two are new: a mask must **introduce** the digits (with a
lookbehind, because a bare `X` inside a word makes `TRF TO MAX 843` a transfer), and a bank named
in the field straight after the account must be **our** bank — tested structurally by whether that
field contains `BANK`, with the chart supplying which bank is ours. `_MAX_TAIL` is gone: the cap
existed to stop a bare reference being read as a tail, the mask does that now for a run of any
length, and 6 happened to be exactly the longest run in evidence.

**§15.11 claimed the check window's overlap broke "every card statement after the first". It does
not.** Measured: posting May then June in order passes even against the unfixed code, because the
union window's end overlaps the *next* statement's first row, so the damage needs the newer
statement to be in the journal already. That happens when a backfill posts out of order — post
June then May and May reverts by June's first row. The bug is real; the claim about when it fires
was wrong, and the difference matters because order-independence is a property §15.11 claims for
this lane and that overlap was quietly breaking.

**The window is the row span alone**, not its union with the printed period. Same reasoning as
before and one step further: `expected_movement` is `closing − opening`, and in both layouts the
opening figure is the balance immediately before the first row, so the movement is the rows'
movement and nothing else.

**Three defects the review found that §15.11 did not anticipate at all:**

- **`unwritten` was never flipped for a liability.** `cleared` was, in `post_statement`; the
  add-back was not, so any card statement with a skipped row missed by twice that row. A suite of
  28 tests passed with the liability logic deleted, because none ran the check on a card that had
  printed balances. `movement_disagreement` now takes `liability` and flips both figures itself —
  one place to remember instead of two.
- **`cleared_movement_sync` parsed a multi-commodity cell into a wrong number.** With `-X ₹` and no
  price, hledger prints `"$4.00, ₹0"`, which a strip-to-digits parser reads as `4.000` — a clean
  parse, not a crash, so a try/except around `Decimal` would not have caught it. Detection is
  structural now, and an unpriceable commodity raises `BooksCheckError` naming it.
- **A statement-posted block was never indexed.** §7 and §9.2 both require it, and without it a
  vendor receipt arriving after the statement posts finds no counterpart — `find_match` requires
  `journal_file IS NOT NULL` — and posts a second block for money the books already hold, invisible
  to §9.3 because both sit inside the period. It also meant a posted row stayed a matcher candidate
  for ever, so §15.5's promise that the unmatched count falls was false.

**The lesson worth keeping.** Every one of these lived in the seam between components whose own
tests were thorough: the activity that calls the poster had no tests at all, and a `TypeError` on
an argument `post_statement` does not take would have killed the whole lane on its first scheduled
run. Component tests do not compose into integration tests, and the review's most useful single
observation was a coverage note, not a defect: `worker/src/aegis_worker/activities/statements.py`
had zero.
