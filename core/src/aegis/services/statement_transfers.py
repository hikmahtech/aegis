"""Step 6 — row ↔ row matching: own accounts, transfers and reversals (§8.4, §8.5).

This is a different component from the matcher, which pairs a statement row with
a JOURNAL transaction. Here both sides are statement rows, and the question is
not "have the books already heard about this?" but "is this one movement printed
on two statements?". §8.4 says to write this one first, and it lives in its own
module for the same reason.

Two halves, and the first pays off on its own:

**Own-account detection.** A bank statement's `CREDITCARD PAYMENT XXXX 1313`
names the far account in its narration. Resolving it credits the card liability
directly, which works even when the card statement never arrives. Without it the
row reaches `books.apply_rules`, matches nothing, lands in `expenses:unknown`,
and the card liability drifts by the full bill every month. Measured on real
production data: a ₹1,00,000 IMPS naming `…9640` posted as `income:unknown`, and
two ₹1,000 transfers to the kids' accounts posted as `expenses:unknown`.

**Transfer pairing.** When both statements do arrive, the same movement is an
`out` row on one and an `in` row on the other. Post both and the money is
counted twice. One side writes the block, the other records
`skip_reason='transfer_counterpart'`.

Own-account detection is also the dangerous half, and §6.1 is the reason to say
so out loud: a substring check on `Credit Card` once misfiled nine current
account statements. A bare four-digit scan is the same class of bug, but worse,
because it fails SILENTLY — the money lands in an asset or liability account the
owner really holds, so §9.3's closing-balance check still passes and nothing
downstream ever asks again. Four conditions guard it, and every one of them was
written against a narration that really appears in the fixtures:

1. **A transfer marker** must be in the narration. See `_MARKER_RE`.
2. **The digits must be a whole run**, not a slice of a longer one. Every real
   reference is a long unbroken number — `UPI/P2A/612345678901/…`,
   `ATW-412345678901-…` — and a slice-based scan reads a tail out of every one
   of them.
3. **Exactly one declared account may match**, after the row's own account is
   removed. Two matches is a chart that spells one tail twice, and picking one
   is a coin toss.
4. **Never the row's own account.** `UPI-987654321012-SPECIMEN STORE.-EXAM-
   XXXXXXXXXX4321-PAYMENT` on the `…4321` statement echoes the payer's own
   masked number, which is the account the row is already posted against.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from aegis.services import books, journal_index
from aegis.services.statements import StatementRow, normalise_narration

#: `skip_reason` for the side of a transfer that does not write the block: the
#: other side put this money in the journal, or is about to (§8.4).
TRANSFER = "transfer_counterpart"

#: `skip_reason`/tag for §8.5's failed-payment pair. Both legs post, to one
#: account, so the pair nets to zero and the account's balance is untouched.
REVERSAL = "reversal"

#: Where a reversal's two legs meet. The chart declares `equity:transfers` for
#: money that is neither income nor expense, which a payment that failed and
#: came straight back is. The account matters far less than the fact that BOTH
#: legs use the same one — that is what makes the pair net to zero.
CLEARING_ACCOUNT = "equity:transfers"

#: Account trees a narration may name. Nothing else can be the far side of a
#: transfer between accounts the owner holds.
_OWN_PREFIXES = ("assets:bank:", "liabilities:card:")

#: A masked account tail is three to six digits: the chart declares `843`,
#: `143`, `1225`, `9640`, `1313`, and a bank writes a padded `X0843` for the
#: first of those. Nothing shorter is a tail and nothing longer is either — a
#: longer run is a reference, an amount or a date.
_MIN_TAIL, _MAX_TAIL = 3, 6

_DIGIT_RUN = re.compile(r"\d+")

#: A run of digits that a mask character introduces. Guard 2 refuses to read a
#: tail out of the END of a longer number, because every reference number in a
#: narration is a long unbroken run and a suffix scan reads a tail out of all of
#: them. But that refusal also threw away the commonest real shape:
#: `IMPS/P2A/612345678901/<name>/X071225/HDFCBANKLTD/` is a genuine transfer to
#: the owner's own `…1225`, and `books._same_tail` strips leading zeros only, so
#: `1225` never equalled `071225`. Production holds over ₹400,000 of these.
#:
#: The mask is what separates the two, and not by luck: a bank PRINTS a masked
#: account number with its mask and prints a reference bare after a separator.
#: So a suffix match is allowed only here — `X071225` yes, `/612345678901/` no,
#: which leaves guard 2 fully intact for every reference number.
#:
#: The mask width varies within one bank and a space may follow it: `X071225`,
#: `XX 1313`, `XXXX 1313`, `XXXXXXXXXXX9640` are all real. A regex fixed at four
#: `X`s misses half the rows.
_MASKED_RUN = re.compile(r"[X*]+\s*(\d+)")

#: The narration must say this is a transfer before a tail in it is read as an
#: account. This is a deliberate extra condition, beyond what §8.4 asks for.
#:
#: The reasoning is asymmetric cost. A MISS costs a digest line: the row falls
#: through to `books.apply_rules`, lands in `expenses:unknown`, and
#: `ledger_reclassify` can move it, because there is an index row. A FALSE
#: POSITIVE moves real money into a real account the owner holds — the balance
#: check still passes, the journal still balances, and nothing ever flags it.
#: So the detection that outranks the rules has to be the narrow one.
#:
#: Every narration in the class carries a marker, because a marker is how banks
#: label a transfer: `CREDITCARD PAYMENT XXXX 1313`, `IMPS/…`, `NEFT/…`,
#: `SWEEP TO DEPOSIT`. What the marker turns away is the shape that has no other
#: guard: a third-party UPI handle or merchant string that happens to end in a
#: standalone group of digits matching a declared tail.
#:
#: Word-bounded and case-sensitive against the normalised (uppercase) narration.
#: `\bFT\b` does not fire inside `GIFT`; `IGNORECASE` would let `ft` in a street
#: address fire, which is the `Rs`-inside-`hours` mistake the money lane already
#: made once.
_MARKER_RE = re.compile(
    r"\b(?:IMPS|NEFT|RTGS|FT|TRF|TRANSFER|SWEEP|CREDITCARD PAYMENT|CARD PAYMENT)\b"
)


@dataclass(frozen=True)
class PairedRow:
    """One statement row whose counter account a row ↔ row pair decided.

    `account` is the far side as THIS row sees it, so the two legs of a pair
    carry different accounts and each one renders a correct block on its own.
    `posts` says whether this row is the side that writes it — see
    `find_transfers` for how that is chosen, and why it is not simply "whoever
    runs first".
    """

    row_id: str
    peer_row_id: str
    account: str
    kind: str
    posts: bool


def own_account(
    narration: str, declared: Collection[str], *, exclude: str | None = None
) -> str | None:
    """The declared account this narration names, or None (§8.4).

    `CREDITCARD PAYMENT XXXX 1313` → `liabilities:card:axis:1313`. `exclude` is
    the row's own instrument account: a row cannot be a transfer to itself, and
    a UPI narration routinely echoes the payer's own masked number.

    The module docstring lists the four guards and the evidence for each. Two
    implementation notes:

    `books._same_tail` rather than `books._declared_with_tail`, which is the
    same comparison but returns the FIRST of several matches. Guard 3 needs to
    know there were several, so it counts them here instead.

    The tails are collected across the whole narration before anything is
    decided. A narration naming both accounts — the payer's own and the
    payee's, which is the common IMPS shape — must still resolve, and it does,
    because `exclude` removes the row's own account before the count.
    """
    text = normalise_narration(narration)
    if not _MARKER_RE.search(text):
        return None
    own = [a for a in sorted(declared) if a.startswith(_OWN_PREFIXES)]
    found: set[str] = set()
    for run in _DIGIT_RUN.findall(text):
        if not (_MIN_TAIL <= len(run) <= _MAX_TAIL):
            continue
        for account in own:
            if books._same_tail(account.rpartition(":")[2], run):
                found.add(account)
    # The masked suffix, which whole-run matching alone cannot reach.
    for run in _MASKED_RUN.findall(text):
        for account in own:
            tail = account.rpartition(":")[2]
            if len(run) > len(tail) and run.endswith(tail):
                found.add(account)
    found.discard(exclude or "")
    return found.pop() if len(found) == 1 else None


def find_transfers(
    rows: Sequence[StatementRow],
    peers: Sequence[StatementRow],
    declared: Collection[str],
    *,
    window_days: int = journal_index._MATCH_DAYS,
) -> dict[str, PairedRow]:
    """Rows of `rows` that are one half of a transfer with a row of `peers`.

    A pair is two rows on DIFFERENT instruments, opposite directions, equal
    amount, dates within `window_days`, where at least one narration names the
    other's account. `peers` is every statement row known for other accounts —
    in production one read of `finance.statement_rows`. When the far statement
    has not been ingested there are no peers and no pairs, which is correct
    rather than a degraded mode: `own_account` still gives the row the right
    counter account, and the far statement will find this row when it arrives.

    **The window is `journal_index._MATCH_DAYS`** (3 days), the one window for
    this lane. §8.2's reasoning applies unchanged: the two banks post a transfer
    a day or two apart, which is the same guess about the same lag as the
    receipt ↔ bank and statement ↔ journal pairs, and a second constant would
    drift away from the measurement that is allowed to move the first.

    **Which side posts.** The side whose narration NAMES the other account; when
    both do, the `out` side. That reduces to §8.4's "posted from the bank side
    only" for a card payment without special-casing cards: a card statement's
    payment credit reads `PAYMENT RECEIVED` and names nothing at all, while the
    bank statement writes `CREDITCARD PAYMENT XXXX 1313`, so the bank side is
    the only side that can name, and it is the side that posts. It also settles
    bank ↔ bank, where both sides usually name each other and the `out` side
    breaks the tie.

    **Why not "whoever runs first posts".** Because only one of the two sides
    can ever match the email lane's block for the same payment. An HDFC alert
    for a card bill carries instrument `hdfc-1225`, so the matcher offers it to
    the BANK row; the card row cannot see it on any pass. If the card statement
    ran first and posted its own block, the journal would hold that block AND
    the email's, for one payment, and no later run could tell they were the
    same. Fixing the posting side to the naming side removes that case
    entirely: the card row skips, the bank row promotes the email block, and
    §8.4's last sentence rewrites its `equity:transfers` posting to the card.

    Ordering still does not matter, because the poster ALSO checks the journal
    before it writes — see `statement_post.post_statement`. Whichever statement
    runs second finds the money already there and skips; whichever runs first
    and is not the posting side skips too, and its amount is added back to the
    balance check as money that never reached the journal.

    A row with more than one possible peer, or a peer wanted by more than one
    row, is left unpaired. Same reasoning as the matcher's pass 3: one of them
    is the right one and choosing mis-attributes money silently.
    """
    rows = list(rows)
    peers = list(peers)
    row_ids = {row.row_id for row in rows}
    everything = rows + peers
    own = {r.row_id: books.instrument_account(r.instrument, declared) for r in everything}
    canon = {
        r.row_id: books.canonical_instrument(r.instrument, declared) or r.instrument
        for r in everything
    }
    named = {
        r.row_id: own_account(r.narration, declared, exclude=own[r.row_id])
        for r in everything
    }

    links: list[tuple[StatementRow, StatementRow]] = []
    for row in sorted(rows, key=lambda r: r.row_id):
        for peer in sorted(peers, key=lambda r: r.row_id):
            if peer.row_id in row_ids:
                # The same statement. Its rows are one account's own record, so
                # two of them are never two views of one movement.
                continue
            if canon[peer.row_id] == canon[row.row_id]:
                continue
            if peer.direction == row.direction or peer.amount != row.amount:
                continue
            if abs((row.occurred_on - peer.occurred_on).days) > window_days:
                continue
            if named[row.row_id] != own[peer.row_id] and named[peer.row_id] != own[row.row_id]:
                continue
            links.append((row, peer))

    row_links = Counter(row.row_id for row, _ in links)
    peer_links = Counter(peer.row_id for _, peer in links)
    paired: dict[str, PairedRow] = {}
    for row, peer in links:
        if row_links[row.row_id] > 1 or peer_links[peer.row_id] > 1:
            continue
        row_names_peer = named[row.row_id] == own[peer.row_id]
        peer_names_row = named[peer.row_id] == own[row.row_id]
        posts = row_names_peer if row_names_peer != peer_names_row else row.direction == "out"
        paired[row.row_id] = PairedRow(
            row_id=row.row_id,
            peer_row_id=peer.row_id,
            account=own[peer.row_id],
            kind=TRANSFER,
            posts=posts,
        )
    return paired


def reversal_account(declared: Collection[str], entity: str) -> str:
    """Where both legs of a reversal post. `equity:transfers` when the chart
    declares it, otherwise the entity's unknown-OUT account — used for both
    legs, so the pair still nets to zero and stays visible in the digest."""
    if not declared or CLEARING_ACCOUNT in declared:
        return CLEARING_ACCOUNT
    return books.UNKNOWN["hikmah" if entity == "hikmah" else "personal"]["out"]


def find_reversals(
    rows: Sequence[StatementRow], declared: Collection[str], *, entity: str
) -> dict[str, PairedRow]:
    """§8.5's failed payments: a debit and its same-day re-credit.

    Same instrument, same day, equal amount, opposite directions, sharing a ref
    or a narration. The email lane records these as `kind='failed'`, which is
    not a transaction and posts no block, so both rows reach here unmatched and
    would otherwise become an `expenses:unknown` + `income:unknown` pair that
    sits in the digest forever.

    Both legs post, to one account, so the pair nets to zero on that account and
    on the bank account, and the statement's own balance check is untouched.
    They are not skipped: the money did leave and come back, the bank printed
    both rows, and a statement's rows are the account's complete record.

    A group holding several possible pairings takes them in `row_id` order,
    which is a content hash — so the answer is the same on every run, whatever
    order the rows were collected in. There is no honest way to do better: two
    identical failed payments on one day are indistinguishable, and either
    pairing nets to the same zero.
    """
    account = reversal_account(declared, entity)
    groups: dict[tuple[str, object, Decimal], list[StatementRow]] = {}
    for row in sorted(rows, key=lambda r: r.row_id):
        groups.setdefault((row.instrument, row.occurred_on, row.amount), []).append(row)

    paired: dict[str, PairedRow] = {}
    for group in groups.values():
        credits = [r for r in group if r.direction == "in"]
        taken: set[str] = set()
        for debit in (r for r in group if r.direction == "out"):
            for credit in credits:
                if credit.row_id in taken or not _same_event(debit, credit):
                    continue
                taken.add(credit.row_id)
                paired[debit.row_id] = PairedRow(
                    row_id=debit.row_id, peer_row_id=credit.row_id,
                    account=account, kind=REVERSAL, posts=True,
                )
                paired[credit.row_id] = PairedRow(
                    row_id=credit.row_id, peer_row_id=debit.row_id,
                    account=account, kind=REVERSAL, posts=True,
                )
                break
    return paired


def _same_event(a: StatementRow, b: StatementRow) -> bool:
    """Do these two rows name one payment? A shared reference, or the same
    narration. The reference is the stronger key and is why it is tried first;
    the narration carries the pair when the bank prints no reference on the
    re-credit."""
    if a.ref and b.ref and a.ref == b.ref:
        return True
    return normalise_narration(a.narration) == normalise_narration(b.narration)


def legs_of(paired: Mapping[str, PairedRow], kind: str) -> tuple[PairedRow, ...]:
    """Every leg of one kind, in `row_id` order. For reporting and tests."""
    return tuple(sorted((p for p in paired.values() if p.kind == kind), key=lambda p: p.row_id))
