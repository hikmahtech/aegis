"""The owner's answer to an unknown-payee card becomes a permanent rule.

Every test here drives a REAL hledger over a REAL git checkout and a real
Postgres, because the interesting assertions are refusals — "the account was
not declared, so no rule was written" — and a fixture that never reached the
LLM branch would pass every one of them for the wrong reason.

So each refusal pins `out["reason"]`, which only the branch itself can set: a
`books_cfg` that was `None`, a `gap_type` that never matched, or an exception
before the check would leave the key absent (or say `books_failed`) and the
assertion fails. The happy-path test alongside them proves the same fixture
does reach the write.
"""

from __future__ import annotations

import inspect
import shutil
import subprocess
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from aegis.api.models.money import MoneyEvent, payee_key
from aegis.services import books
from aegis.services import journal_index as ji
from aegis_worker.activities.curiosity import CuriosityActivities
from aegis_worker.activities.delivery import DeliveryActivities
from temporalio.testing import ActivityEnvironment

HAS_HLEDGER = shutil.which("hledger") is not None and shutil.which("git") is not None
pytestmark = pytest.mark.skipif(not HAS_HLEDGER, reason="hledger/git not installed")

AGENT = "sebas"
MAILBOX = "cur-books-t"
# Unique to this file, for the same reason test_ledger_tools.py carries one:
# the reclassify sweep selects across the WHOLE `finance.journal_index`, and
# the test databases are keyed on the xdist worker alone — so they are shared
# between tests/core and tests/worker and between two agents in two worktrees.
# A common payee would let a foreign `%:unknown` row match this rule and be
# rewritten against a tmp repo that does not hold it.
PAYEE = "Jai shree zzt5nakoda"
KEY = payee_key(PAYEE)
# Eight words, so `rule_match_for` refuses to build a pattern for it — a
# six-word clip would be a prefix rule matching every "payment to merchant via
# zzt5long pay" anything, forever, with nobody having authored it.
LONG_PAYEE = "Payment to merchant via zzt5long Pay UPI Zomato"

ACCOUNTS = """commodity ₹ 1,00,000.00
account assets:bank:hdfc:1225
account assets:unknown
account expenses:unknown
account expenses:groceries
account expenses:hikmah:unknown
account income:unknown
"""


def _repo(tmp_path: Path) -> books.BooksConfig:
    root = tmp_path / "books"
    (root / "personal").mkdir(parents=True)
    (root / "hikmah").mkdir()
    (root / "rules").mkdir()
    (root / "accounts.journal").write_text(ACCOUNTS)
    (root / "prices.journal").write_text("")
    (root / "recurring.journal").write_text("")
    (root / "rules" / "accounts.yaml").write_text("")
    (root / "personal" / "2026.journal").write_text("; p\n")
    (root / "hikmah" / "2026.journal").write_text("; h\n")
    (root / "main.journal").write_text(
        "include accounts.journal\ninclude prices.journal\ninclude personal/2026.journal\n"
        "include hikmah/2026.journal\ninclude recurring.journal\n"
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"], cwd=root, check=True
    )
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"],
        cwd=root,
        check=True,
    )
    return books.BooksConfig(path=root)


def _commits(cfg: books.BooksConfig) -> int:
    proc = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=cfg.path,
        capture_output=True,
        text=True,
        check=True,
    )
    return int(proc.stdout.strip())


def _rules(cfg: books.BooksConfig) -> list[dict]:
    return books.load_rules(cfg.path / "rules" / "accounts.yaml")


def _rules_text(cfg: books.BooksConfig) -> str:
    """The raw file, because `load_rules` drops any entry with a falsy `match`.

    "no rule was written" asserted through `load_rules` alone would be blind to
    a rule that WAS appended with an empty pattern — inert, but still a junk
    line committed to the books on every refused answer.
    """
    return (cfg.path / "rules" / "accounts.yaml").read_text()


def _journal(cfg: books.BooksConfig) -> str:
    return (cfg.path / "personal" / "2026.journal").read_text()


class FakeLLM:
    """Records what it was asked; returns canned text or raises."""

    def __init__(self, response: str | None = None, exc: Exception | None = None):
        self.response = response
        self.exc = exc
        self.calls: list[dict] = []

    async def think(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc:
            raise self.exc
        return {
            "response": self.response,
            "model": "fake-model",
            "prompt_tokens": 3,
            "completion_tokens": 2,
        }


@pytest_asyncio.fixture(loop_scope="function")
async def clean_db(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO agents (id, name, role, system_prompt_path, active) "
            "VALUES ($1, 'Sebas', 'assistant', 'personalities/sebas', TRUE) "
            "ON CONFLICT (id) DO NOTHING",
            AGENT,
        )
        await _wipe(conn)
    yield db_pool
    async with db_pool.acquire() as conn:
        await _wipe(conn)


async def _wipe(conn):
    await conn.execute("DELETE FROM finance.journal_index WHERE mailbox = $1", MAILBOX)
    await conn.execute("DELETE FROM agent_memory WHERE agent_id = $1", AGENT)
    await conn.execute("DELETE FROM agent_memory_ops_log WHERE agent_id = $1", AGENT)
    await conn.execute("DELETE FROM interactions WHERE origin = 'curiosity'")


def _event(payee: str = PAYEE, day: int = 2) -> MoneyEvent:
    return MoneyEvent(
        kind="transaction",
        direction="out",
        amount=Decimal("310.00"),
        currency="INR",
        payee=payee,
        payee_key=payee_key(payee),
        channel="upi",
        instrument="hdfc-1225",
        occurred_on=date(2026, 9, day),
        entity="personal",
        account="expenses:unknown",
        parser="hdfc_upi",
        source_class="bank",
    )


async def _post(pool, cfg: books.BooksConfig, payee: str = PAYEE, day: int = 2) -> str:
    """One real posting in the journal AND its index row."""
    ev = _event(payee, day)
    msgid = ji.msgid_for(MAILBOX, uuid4().hex)
    rel = await books.post_event(ev, msgid, cfg)
    await ji.upsert(pool, msgid, MAILBOX, ev, journal_file=rel)
    return msgid


async def _post_hikmah(pool, cfg: books.BooksConfig, day: int = 2) -> str:
    """The same payee in the OTHER set of books, in `hikmah/2026.journal`."""
    ev = _event(day=day)
    ev.entity = "hikmah"
    ev.account = "expenses:hikmah:unknown"
    msgid = ji.msgid_for(MAILBOX, uuid4().hex)
    rel = await books.post_event(ev, msgid, cfg)
    assert rel.startswith("hikmah/"), rel
    await ji.upsert(pool, msgid, MAILBOX, ev, journal_file=rel)
    return msgid


def _declare(cfg: books.BooksConfig, account: str) -> None:
    """Add one account to the chart and commit it, so a clean checkout has it."""
    path = cfg.path / "accounts.journal"
    path.write_text(path.read_text() + f"account {account}\n")
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "chart"],
        cwd=cfg.path,
        check=True,
    )


async def _card(pool) -> str:
    row = await pool.fetchrow(
        "INSERT INTO interactions (flow_run_id, agent_id, kind, origin, prompt, status) "
        "VALUES ($1, $2, 'input', 'curiosity', $3, 'resolved') RETURNING id",
        f"run-{uuid4()}",
        AGENT,
        f"You paid ₹310.00 to {PAYEE}. What was it for?",
    )
    return str(row["id"])


def _meta(**over) -> dict:
    meta = {
        "gap_type": "unknown_payee",
        "subject": PAYEE,
        "question": "q",
        "agent_id": AGENT,
        "payee_key": KEY,
    }
    meta.update(over)
    return meta


class FakeDelivery:
    """Stand-in for DeliveryActivities as `safe_send_message` uses it.

    `test_fake_delivery_matches_the_real_class` pins this against the real
    class so the fake cannot drift into testing nothing.
    """

    channel = "slack"
    db_pool = None  # skips the notification-budget path in safe_send_message

    def __init__(self):
        self.sent: list[str] = []

    async def send_message(self, *, agent_id: str, message: str, chat_id: int = 0) -> dict:
        self.sent.append(message)
        return {"ok": True}


def test_fake_delivery_matches_the_real_class():
    """The fake must expose what safe_send_message actually reads off the real
    DeliveryActivities: a `channel` attribute, a `db_pool` attribute and a
    keyword-only send_message(agent_id, message, chat_id)."""
    real = inspect.signature(DeliveryActivities.send_message).parameters
    fake = inspect.signature(FakeDelivery.send_message).parameters
    for name in ("agent_id", "message", "chat_id"):
        assert name in real, f"DeliveryActivities.send_message lost {name}"
        assert name in fake, f"FakeDelivery.send_message lost {name}"
    fields = set(DeliveryActivities.__dataclass_fields__)
    assert {"channel", "db_pool"} <= fields
    assert hasattr(FakeDelivery, "channel") and hasattr(FakeDelivery, "db_pool")


async def _apply(pool, cfg, llm, meta=None, answer="that's my grocer", delivery=None):
    acts = CuriosityActivities(
        db_pool=pool, llm_client=llm, model="m", books_cfg=cfg, delivery=delivery
    )
    iid = await _card(pool)
    return await ActivityEnvironment().run(
        acts.apply_curiosity_answer, iid, {"value": answer}, meta or _meta()
    )


async def _memories(pool) -> list[str]:
    rows = await pool.fetch(
        "SELECT content FROM agent_memory WHERE agent_id = $1 AND source = 'curiosity'", AGENT
    )
    return [r["content"] for r in rows]


async def _account_of(pool, msgid: str) -> str:
    return await pool.fetchval(
        "SELECT account FROM finance.journal_index WHERE message_id = $1", msgid
    )


# ------------------------------------------------------------------ happy path


async def test_confident_answer_writes_a_rule_and_reclassifies_the_backlog(clean_db, tmp_path):
    cfg = _repo(tmp_path)
    msgid = await _post(clean_db, cfg)
    assert "expenses:unknown" in _journal(cfg), "the fixture did not post an unknown posting"
    llm = FakeLLM('{"account": "expenses:groceries", "confidence": 0.9}')

    out = await _apply(clean_db, cfg, llm)

    assert out["recorded"] is True
    assert out["rule"] == "expenses:groceries"
    assert out["reclassified"] == 1
    assert out["failed"] == 0

    # The rule is in the books, and it matches the payee it was made from.
    rules = _rules(cfg)
    assert len(rules) == 1, rules
    assert rules[0]["account"] == "expenses:groceries"
    assert rules[0]["payee"] == PAYEE
    # The account states which set of books it belongs to, so the rule says so
    # too — see `test_a_business_account_never_rewrites_the_personal_books`.
    assert rules[0]["entity"] == "personal"
    assert books.apply_rules(rules, "alerts@hdfcbank.net", PAYEE) == rules[0]

    # The journal block itself moved — this is the record, not the index.
    text = _journal(cfg)
    assert "expenses:groceries" in text
    assert "expenses:unknown" not in text

    # ...and the index followed it.
    assert await _account_of(clean_db, msgid) == "expenses:groceries"

    # The answer is still banked as memory, as it was before the books lane.
    assert len(await _memories(clean_db)) == 1


async def test_a_business_account_never_rewrites_the_personal_books(clean_db, tmp_path):
    """The unattended twin of `ledger_reclassify`'s cross-entity refusal.

    Live shape: AWS bills arrive in the personal mailbox, so they are
    `entity=personal` in `personal/2026.journal`. The card asks about them, the
    owner says "that is the Hikmah infra bill", and the model returns a
    declared `expenses:hikmah:*`. Every check downstream passes — the account
    is in the chart and each block still balances, so `check --strict` is happy
    and nothing reverts — and the business account is now sitting in the
    personal journal. `ledger_reclassify` then REFUSES to move it back, so the
    repair path is narrower than the path that made the mess.

    Two things have to hold: the sweep leaves the other set of books alone, and
    the rule carries the entity so `post_event` files FUTURE mail from this
    payee in the right journal instead of repeating this forever.
    """
    cfg = _repo(tmp_path)
    _declare(cfg, "expenses:hikmah:infra")
    msgid = await _post(clean_db, cfg)  # entity=personal
    llm = FakeLLM('{"account": "expenses:hikmah:infra", "confidence": 0.95}')

    out = await _apply(clean_db, cfg, llm)

    assert out["rule"] == "expenses:hikmah:infra"
    assert out["reclassified"] == 0, "a personal block was rewritten to a business account"
    assert out["failed"] == 0
    # The journal is the record: the personal block is exactly as it was.
    assert "expenses:hikmah:infra" not in _journal(cfg)
    assert "expenses:unknown" in _journal(cfg)
    assert await _account_of(clean_db, msgid) == "expenses:unknown"
    # And the rule stops the next AWS mail repeating it.
    assert _rules(cfg)[0]["entity"] == "hikmah"


async def test_the_sweep_stays_inside_the_account_s_own_books(clean_db, tmp_path):
    """Same payee, one posting in each set of books, and a personal account
    answered. Only the personal one moves — rewriting changes the account, never
    the file the block lives in."""
    cfg = _repo(tmp_path)
    mine = await _post(clean_db, cfg)
    theirs = await _post_hikmah(clean_db, cfg)
    llm = FakeLLM('{"account": "expenses:groceries", "confidence": 0.9}')

    out = await _apply(clean_db, cfg, llm)

    assert out["reclassified"] == 1
    assert await _account_of(clean_db, mine) == "expenses:groceries"
    assert await _account_of(clean_db, theirs) == "expenses:hikmah:unknown"
    assert "expenses:groceries" not in (cfg.path / "hikmah" / "2026.journal").read_text()


async def test_an_entity_neutral_account_sweeps_both_books(clean_db, tmp_path):
    """Assets, liabilities and equity belong to BOTH sets of books —
    `post_event` writes `assets:*` into either through `instrument_account`,
    which has no notion of entity — so neither the rule nor the sweep may
    narrow to one. Stamping them personal would refuse a real correction."""
    cfg = _repo(tmp_path)
    mine = await _post(clean_db, cfg)
    theirs = await _post_hikmah(clean_db, cfg)
    llm = FakeLLM('{"account": "assets:unknown", "confidence": 0.9}')

    out = await _apply(clean_db, cfg, llm)

    assert out["reclassified"] == 2, out
    assert await _account_of(clean_db, mine) == "assets:unknown"
    assert await _account_of(clean_db, theirs) == "assets:unknown"
    assert "entity" not in _rules(cfg)[0], _rules(cfg)[0]


async def test_the_prompt_carries_the_declared_chart_and_is_billed(clean_db, tmp_path):
    """The account list must come from the books, and the call must be logged.

    `db_pool` + `purpose` is what makes `LLMClient._record_call` write the
    `llm_calls` row; without them this call is invisible spend.
    """
    cfg = _repo(tmp_path)
    await _post(clean_db, cfg)
    llm = FakeLLM('{"account": "expenses:groceries", "confidence": 0.95}')

    await _apply(clean_db, cfg, llm)

    assert len(llm.calls) == 1
    call = llm.calls[0]
    assert call["purpose"] == "books_answer_account"
    assert call["db_pool"] is clean_db
    assert call["agent_id"] == AGENT
    assert call["model"] == "m"
    blob = f"{call['prompt']}\n{call.get('system_prompt') or ''}"
    assert "expenses:groceries" in blob, "the declared chart never reached the prompt"
    assert PAYEE in blob
    assert "that's my grocer" in blob, "the owner's answer never reached the prompt"


async def test_the_whole_backlog_is_one_commit(clean_db, tmp_path):
    """Three postings, one rewrite. A loop over `rewrite_event` would take the
    flock, pull, strict-check, commit and push once PER posting — serialising
    every other writer of the books behind it and leaving three commits."""
    cfg = _repo(tmp_path)
    msgids = [await _post(clean_db, cfg, day=d) for d in (2, 3, 4)]
    before = _commits(cfg)
    llm = FakeLLM('{"account": "expenses:groceries", "confidence": 0.9}')

    out = await _apply(clean_db, cfg, llm)

    assert out["reclassified"] == 3
    # One commit for the rule, one for the whole reclassify batch.
    assert _commits(cfg) - before == 2
    assert "expenses:unknown" not in _journal(cfg)
    for msgid in msgids:
        assert await _account_of(clean_db, msgid) == "expenses:groceries"


async def test_payee_key_is_derived_when_the_card_predates_the_metadata(clean_db, tmp_path):
    """A card already in flight when this shipped carries no `payee_key`; its
    answer must still reclassify rather than silently do nothing."""
    cfg = _repo(tmp_path)
    msgid = await _post(clean_db, cfg)
    meta = _meta()
    meta.pop("payee_key")
    llm = FakeLLM('{"account": "expenses:groceries", "confidence": 0.9}')

    out = await _apply(clean_db, cfg, llm, meta=meta)

    assert out["rule"] == "expenses:groceries"
    assert out["reclassified"] == 1
    assert await _account_of(clean_db, msgid) == "expenses:groceries"


# -------------------------------------------------------------------- refusals


async def test_a_low_confidence_answer_writes_no_rule(clean_db, tmp_path):
    cfg = _repo(tmp_path)
    msgid = await _post(clean_db, cfg)
    before = _commits(cfg)
    llm = FakeLLM('{"account": "expenses:groceries", "confidence": 0.5}')

    out = await _apply(clean_db, cfg, llm)

    # `reason` is set only by the confidence check itself: a skipped branch
    # leaves no `reason` at all, so this cannot pass by never getting here.
    assert out["reason"] == "low_confidence"
    assert out["rule"] is None
    assert _rules(cfg) == []
    assert "expenses:unknown" in _journal(cfg)
    assert await _account_of(clean_db, msgid) == "expenses:unknown"
    assert _commits(cfg) == before
    # The owner's answer is never lost because the ledger declined to act.
    assert len(await _memories(clean_db)) == 1


async def test_the_threshold_is_inclusive_at_the_boundary(clean_db, tmp_path):
    """0.8 passes and 0.79 does not — pinning the comparison, not just its
    direction. A `>` instead of `>=` flips the first of these."""
    cfg = _repo(tmp_path)
    await _post(clean_db, cfg)
    at = await _apply(
        clean_db, cfg, FakeLLM('{"account": "expenses:groceries", "confidence": 0.8}')
    )
    assert at["rule"] == "expenses:groceries"

    cfg2 = _repo(tmp_path / "second")
    await _post(clean_db, cfg2)
    below = await _apply(
        clean_db, cfg2, FakeLLM('{"account": "expenses:groceries", "confidence": 0.79}')
    )
    assert below["reason"] == "low_confidence"
    assert below["rule"] is None


async def test_an_undeclared_account_is_refused_however_confident(clean_db, tmp_path):
    """The chart is the allowlist. `expenses:snacks` is not in `accounts.journal`,
    so an `hledger check --strict` would reject the rewritten block — and a rule
    naming it would mis-file every future payment from this payee."""
    cfg = _repo(tmp_path)
    msgid = await _post(clean_db, cfg)
    llm = FakeLLM('{"account": "expenses:snacks", "confidence": 1.0}')

    out = await _apply(clean_db, cfg, llm)

    assert out["reason"] == "undeclared"
    assert out["rule"] is None
    assert _rules(cfg) == []
    assert await _account_of(clean_db, msgid) == "expenses:unknown"


async def test_a_declared_account_from_the_same_chart_is_accepted(clean_db, tmp_path):
    """The other half of the membership check: with `expenses:snacks` DECLARED,
    the identical call writes the rule. Without this, the refusal above would
    also pass against a membership test that rejects everything."""
    cfg = _repo(tmp_path)
    accounts = cfg.path / "accounts.journal"
    accounts.write_text(accounts.read_text() + "account expenses:snacks\n")
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "declare"],
        cwd=cfg.path,
        check=True,
    )
    msgid = await _post(clean_db, cfg)
    llm = FakeLLM('{"account": "expenses:snacks", "confidence": 1.0}')

    out = await _apply(clean_db, cfg, llm)

    assert out["rule"] == "expenses:snacks"
    assert await _account_of(clean_db, msgid) == "expenses:snacks"


async def test_an_explicit_none_answer_writes_no_rule(clean_db, tmp_path):
    """"NONE" is the model's way of saying it could not tell — it is not an
    account, and `NONE` must never reach the chart membership check as a hit."""
    cfg = _repo(tmp_path)
    llm = FakeLLM('{"account": "NONE", "confidence": 0.99}')
    await _post(clean_db, cfg)

    out = await _apply(clean_db, cfg, llm)

    assert out["rule"] is None
    assert out["reason"] == "undeclared"
    assert _rules(cfg) == []


async def test_unparseable_model_output_writes_no_rule(clean_db, tmp_path):
    cfg = _repo(tmp_path)
    llm = FakeLLM("I think this is groceries, honestly")
    await _post(clean_db, cfg)

    out = await _apply(clean_db, cfg, llm)

    assert out["rule"] is None
    assert _rules(cfg) == []
    assert len(await _memories(clean_db)) == 1


async def test_a_failing_llm_still_banks_the_memory(clean_db, tmp_path):
    """The books lane is an extra; the owner's answer is the point. Anything
    that throws in there is logged and the memory write stands."""
    cfg = _repo(tmp_path)
    msgid = await _post(clean_db, cfg)
    llm = FakeLLM(exc=RuntimeError("proxy down"))

    out = await _apply(clean_db, cfg, llm)

    assert out["recorded"] is True
    assert out["rule"] is None
    assert out["reason"] == "books_failed"
    assert len(await _memories(clean_db)) == 1
    assert "that's my grocer" in (await _memories(clean_db))[0]
    assert await _account_of(clean_db, msgid) == "expenses:unknown"


async def test_a_broken_checkout_still_banks_the_memory(clean_db, tmp_path):
    """`declared_accounts` raises when hledger cannot read the chart."""
    cfg = books.BooksConfig(path=tmp_path / "nowhere")
    llm = FakeLLM('{"account": "expenses:groceries", "confidence": 0.9}')

    out = await _apply(clean_db, cfg, llm)

    assert out["recorded"] is True
    assert out["reason"] == "books_failed"
    assert len(await _memories(clean_db)) == 1


# --------------------------------------------------------------- lane is gated


async def test_another_gap_type_never_touches_the_books(clean_db, tmp_path):
    cfg = _repo(tmp_path)
    await _post(clean_db, cfg)
    llm = FakeLLM('{"account": "expenses:groceries", "confidence": 0.9}')

    out = await _apply(clean_db, cfg, llm, meta=_meta(gap_type="calendar_attendee"))

    assert "rule" not in out
    assert llm.calls == []
    assert _rules(cfg) == []
    assert len(await _memories(clean_db)) == 1


async def test_no_books_config_skips_the_lane_without_losing_the_answer(clean_db, tmp_path):
    cfg = _repo(tmp_path)
    await _post(clean_db, cfg)
    acts = CuriosityActivities(
        db_pool=clean_db,
        llm_client=FakeLLM('{"account": "expenses:groceries", "confidence": 0.9}'),
        model="m",
    )
    iid = await _card(clean_db)

    out = await ActivityEnvironment().run(
        acts.apply_curiosity_answer, iid, {"value": "grocer"}, _meta()
    )

    assert out["recorded"] is True
    assert "rule" not in out
    assert _rules(cfg) == []
    assert len(await _memories(clean_db)) == 1


async def test_a_row_already_classified_is_not_rewritten(clean_db, tmp_path):
    """The sweep is scoped to `%:unknown`. A posting the owner (or an earlier
    rule) already filed must not be dragged into the new account, even when it
    is the same payee — the index is what the sweep reads, so it is the index
    the filter has to be on."""
    cfg = _repo(tmp_path)
    unknown = await _post(clean_db, cfg)
    settled = await _post(clean_db, cfg, day=5)
    await clean_db.execute(
        "UPDATE finance.journal_index SET account = 'assets:bank:hdfc:1225' "
        "WHERE message_id = $1",
        settled,
    )
    assert await clean_db.fetchval(
        "SELECT count(DISTINCT payee_key) FROM finance.journal_index WHERE message_id = ANY($1)",
        [unknown, settled],
    ) == 1, "the two rows must share a payee_key or this proves nothing"
    llm = FakeLLM('{"account": "expenses:groceries", "confidence": 0.9}')

    out = await _apply(clean_db, cfg, llm)

    assert out["reclassified"] == 1
    assert await _account_of(clean_db, unknown) == "expenses:groceries"
    assert await _account_of(clean_db, settled) == "assets:bank:hdfc:1225"
    # The settled row's block is untouched, so the journal still carries one
    # `expenses:unknown` posting.
    assert _journal(cfg).count("expenses:unknown") == 1


async def test_another_payee_is_left_alone(clean_db, tmp_path):
    """The sweep is keyed on `payee_key`, not on "everything unknown"."""
    cfg = _repo(tmp_path)
    mine = await _post(clean_db, cfg)
    other = await _post(clean_db, cfg, payee="Someone zzt5else", day=6)
    llm = FakeLLM('{"account": "expenses:groceries", "confidence": 0.9}')

    out = await _apply(clean_db, cfg, llm)

    assert out["reclassified"] == 1
    assert await _account_of(clean_db, mine) == "expenses:groceries"
    assert await _account_of(clean_db, other) == "expenses:unknown"


async def test_a_refused_rule_still_applies_the_backlog(clean_db, tmp_path):
    """No pattern does not mean no work.

    The sweep selects index rows on `payee_key`, an exact match — it never
    consults the regex. So when the word cap refuses to persist a pattern, the
    money already sitting in `expenses:unknown` can still move to the account
    the owner just explained. Only FUTURE events from this payee miss out,
    which is exactly the cost the cap is meant to buy: neither an over-broad
    persisted prefix nor an answer spent for nothing.
    """
    cfg = _repo(tmp_path)
    msgids = [await _post(clean_db, cfg, payee=LONG_PAYEE, day=d) for d in (2, 3)]
    llm = FakeLLM('{"account": "expenses:groceries", "confidence": 0.9}')

    out = await _apply(
        clean_db,
        cfg,
        llm,
        meta=_meta(subject=LONG_PAYEE, payee_key=payee_key(LONG_PAYEE)),
    )

    # Distinguishable from a clean success, and from a refusal that did nothing.
    assert out["rule"] is None
    assert out["reason"] == "rule_refused_backlog_applied"
    assert out["reclassified"] == 2
    assert out["failed"] == 0

    assert _rules(cfg) == [], "a prefix rule was persisted after all"
    assert _rules_text(cfg) == "", "the rules file was written to anyway"
    assert "expenses:unknown" not in _journal(cfg)
    for msgid in msgids:
        assert await _account_of(clean_db, msgid) == "expenses:groceries"
    assert len(await _memories(clean_db)) == 1


async def test_a_payee_with_no_usable_key_touches_nothing(clean_db, tmp_path):
    """An empty key must not reach the sweep — not even to apply a backlog.

    `WHERE payee_key = ''` matches every blank-key row in the index, so the
    "apply the backlog anyway" path above has to stop short of it or one
    unparseable payee reclassifies a pile of unrelated postings.
    """
    cfg = _repo(tmp_path)
    blank = ji.msgid_for(MAILBOX, uuid4().hex)
    await clean_db.execute(
        "INSERT INTO finance.journal_index "
        "(message_id, mailbox, entity, kind, direction, amount, currency, payee, payee_key, "
        " account, channel, occurred_on, parser, source_class, journal_file) "
        "VALUES ($1, $2, 'personal', 'transaction', 'out', 10, 'INR', '!!!', '', "
        " 'expenses:unknown', 'upi', '2026-09-02', 'test', 'bank', 'personal/2026.journal')",
        blank,
        MAILBOX,
    )
    llm = FakeLLM('{"account": "expenses:groceries", "confidence": 0.9}')

    out = await _apply(clean_db, cfg, llm, meta=_meta(subject="!!!", payee_key=""))

    assert out["rule"] is None
    assert out["reason"] == "no_payee_key"
    assert "reclassified" not in out
    assert await _account_of(clean_db, blank) == "expenses:unknown"
    assert _rules_text(cfg) == ""
    assert len(await _memories(clean_db)) == 1


async def test_a_credit_from_the_same_payee_is_left_alone(clean_db, tmp_path):
    """The owner explained money they PAID this payee. A credit from the same
    name — a refund, a transfer back — is not what they explained, and filing
    it to an expense account would put income in the wrong half of the books."""
    cfg = _repo(tmp_path)
    paid = await _post(clean_db, cfg)
    refund = await _post(clean_db, cfg, day=6)
    await clean_db.execute(
        "UPDATE finance.journal_index SET direction = 'in', account = 'income:unknown' "
        "WHERE message_id = $1",
        refund,
    )
    llm = FakeLLM('{"account": "expenses:groceries", "confidence": 0.9}')

    out = await _apply(clean_db, cfg, llm)

    assert out["reclassified"] == 1
    assert await _account_of(clean_db, paid) == "expenses:groceries"
    assert await _account_of(clean_db, refund) == "income:unknown"


async def test_an_inbound_answer_files_the_credit_and_leaves_the_debit(clean_db, tmp_path):
    """The mirror of the test above, and the half that used to be unreachable.

    Money that came in is now carded as money that came in (issue #387
    review), so the hook has to sweep the half the card actually asked about.
    The `direction` in the card's metadata is the only thing that says which —
    without it this would file a credit to an expense account, which is money
    in the wrong half of the books.
    """
    cfg = _repo(tmp_path)
    _declare(cfg, "income:people")
    paid = await _post(clean_db, cfg)
    received = await _post(clean_db, cfg, day=6)
    await clean_db.execute(
        "UPDATE finance.journal_index SET direction = 'in', account = 'income:unknown' "
        "WHERE message_id = $1",
        received,
    )
    llm = FakeLLM('{"account": "income:people", "confidence": 0.9}')

    out = await _apply(clean_db, cfg, llm, meta=_meta(direction="in"))

    assert out["rule"] == "income:people"
    assert out["reclassified"] == 1
    assert await _account_of(clean_db, received) == "income:people"
    # The debit from the same name is not what the owner explained.
    assert await _account_of(clean_db, paid) == "expenses:unknown"
    # The prompt told the model which way the money went, or it would be
    # picking an account for a payment that never happened.
    blob = f"{llm.calls[0]['prompt']}\n{llm.calls[0].get('system_prompt') or ''}"
    assert "money the owner received" in blob, blob


async def test_a_card_with_no_direction_is_treated_as_money_going_out(clean_db, tmp_path):
    """Every card raised before the inbound lane shipped carries no
    `direction`, and every one of them was outbound. Anything unrecognised
    takes the same road — the income half is never reached by a guess.

    A repo and a payee of its own per case: the backlog sweep selects across
    the whole index by `payee_key`, so a shared payee would have each run
    trying to rewrite the previous run's blocks in a repo that does not hold
    them.
    """
    for i, value in enumerate((None, "", "sideways")):
        cfg = _repo(tmp_path / f"d{i}")
        payee = f"Zzt5dir{i} Shop"
        msgid = await _post(clean_db, cfg, payee=payee)
        llm = FakeLLM('{"account": "expenses:groceries", "confidence": 0.9}')
        meta = _meta(subject=payee, payee_key=payee_key(payee))
        if value is not None:
            meta["direction"] = value

        out = await _apply(clean_db, cfg, llm, meta=meta)

        assert out["reclassified"] == 1, value
        assert await _account_of(clean_db, msgid) == "expenses:groceries", value
        blob = f"{llm.calls[0]['prompt']}\n{llm.calls[0].get('system_prompt') or ''}"
        assert "a payment the owner made" in blob, value
        assert "money the owner received" not in blob, value


async def test_a_rule_from_an_answer_never_fires_on_the_sender(clean_db, tmp_path):
    """`apply_rules` matches "<sender> | <payee>", and this is the only place a
    rule is written with no human authoring the regex — so the pattern it
    persists has to be pinned to the payee half of that haystack."""
    cfg = _repo(tmp_path)
    await _post(clean_db, cfg)
    llm = FakeLLM('{"account": "expenses:groceries", "confidence": 0.9}')

    await _apply(clean_db, cfg, llm)

    rules = _rules(cfg)
    assert books.apply_rules(rules, "", PAYEE) == rules[0]
    # A mail whose FROM happens to carry the payee's words, about someone else.
    assert books.apply_rules(rules, f"{KEY.replace(' ', '-')}@bank.example", "Mahavitaran") is None


async def test_a_payee_spelling_variant_matches_the_rule(clean_db, tmp_path):
    """One biller arrives under several spellings — that is why the detector
    groups by `payee_key` at all. A rule escaping ONE spelling would leave the
    others in `expenses:unknown` forever, and the novelty key means they are
    never asked about again."""
    cfg = _repo(tmp_path)
    await _post(clean_db, cfg)
    llm = FakeLLM('{"account": "expenses:groceries", "confidence": 0.9}')

    await _apply(clean_db, cfg, llm)

    rules = _rules(cfg)
    for variant in (PAYEE, PAYEE.upper(), "JAI-SHREE-ZZT5NAKODA", "Jai  Shree  Zzt5nakoda"):
        assert books.apply_rules(rules, "", variant) == rules[0], variant
    assert books.apply_rules(rules, "", "Some Other Shop") is None


# ------------------------------------------- what the owner is told (issue #384)


async def test_a_cross_entity_answer_says_what_it_left_behind(clean_db, tmp_path):
    """The refusal above is correct and was invisible (issue #384).

    `InteractionFlow` discards the post-resolve activity's return value and
    the card is an ABANDONED child nobody awaits, so nothing carried
    `reclassified: 0` anywhere a person could see it. The owner answered "that
    is the Hikmah infra bill" and watched nothing happen — and no automated
    path could recover: the novelty key retires the card, `ledger_reclassify`
    refuses the same move by design, and the brief's Unexplained list is a
    7-day window.
    """
    cfg = _repo(tmp_path)
    _declare(cfg, "expenses:hikmah:infra")
    await _post(clean_db, cfg)  # entity=personal
    delivery = FakeDelivery()
    llm = FakeLLM('{"account": "expenses:hikmah:infra", "confidence": 0.95}')

    out = await _apply(clean_db, cfg, llm, delivery=delivery)

    assert out["reclassified"] == 0 and out["skipped_other_entity"] == 1
    assert len(delivery.sent) == 1, delivery.sent
    said = delivery.sent[0]
    assert PAYEE in said and "expenses:hikmah:infra" in said
    # The three things the owner needs: the rule stands, one posting did not
    # move, and only a person can move it.
    #
    # Asserted PAST the noun, deliberately. "1 existing posting" alone stops
    # one word short of the verb and passes on "1 existing posting sit in the
    # other set of books" — which is what this file asserted, in the same
    # commit as the fix for "1 low-confidence LLM postings".
    assert "1 existing posting sits in the other set of books" in said, said
    assert "rule is saved" in said and "hand edit" in said, said


async def test_an_answer_that_lands_completely_says_nothing(clean_db, tmp_path):
    """Silence is the success signal. The payee leaves the Unexplained list in
    the next brief and the card is resolved in the admin, so an extra push per
    answered question would spend the notification budget on nothing."""
    cfg = _repo(tmp_path)
    await _post(clean_db, cfg)
    delivery = FakeDelivery()
    llm = FakeLLM('{"account": "expenses:groceries", "confidence": 0.9}')

    out = await _apply(clean_db, cfg, llm, delivery=delivery)

    assert out["reclassified"] == 1 and out["skipped_other_entity"] == 0
    assert delivery.sent == []


async def test_an_answer_the_model_could_not_file_is_reported(clean_db, tmp_path):
    """Every refusal in this hook was equally silent, not only the
    cross-entity one: an undeclared account, an unsure model and an
    unreachable checkout all returned a dict nobody read."""
    # The loop index, not `hash(response)`: `hash()` on a str is per-process
    # randomised, so two of the three could collide and `_repo`'s
    # `mkdir(parents=True)` would raise on an existing directory.
    for i, (response, phrase) in enumerate((
        ('{"account": "expenses:snacks", "confidence": 1.0}', "an account in the chart"),
        ('{"account": "expenses:groceries", "confidence": 0.4}', "not sure enough"),
        ('{"account": "NONE", "confidence": 1.0}', "an account in the chart"),
    )):
        cfg = _repo(tmp_path / f"r{i}")
        await _post(clean_db, cfg)
        delivery = FakeDelivery()

        out = await _apply(clean_db, cfg, FakeLLM(response), delivery=delivery)

        assert out["rule"] is None, response
        assert len(delivery.sent) == 1, (response, delivery.sent)
        assert phrase in delivery.sent[0], (response, delivery.sent[0])
        # The answer is not lost, and the message says so.
        assert "saved" in delivery.sent[0]


async def test_a_refused_rule_that_moved_the_backlog_says_both_halves(clean_db, tmp_path):
    """The backlog moved but nothing was persisted, so future mail from this
    payee still lands in `:unknown`. Only this message can tell the owner
    that."""
    cfg = _repo(tmp_path)
    await _post(clean_db, cfg, payee=LONG_PAYEE)
    delivery = FakeDelivery()
    llm = FakeLLM('{"account": "expenses:groceries", "confidence": 0.9}')

    out = await _apply(
        clean_db, cfg, llm,
        meta=_meta(subject=LONG_PAYEE, payee_key=payee_key(LONG_PAYEE)),
        delivery=delivery,
    )

    assert out["reason"] == "rule_refused_backlog_applied" and out["reclassified"] == 1
    assert len(delivery.sent) == 1, delivery.sent
    assert "1 existing posting" in delivery.sent[0], delivery.sent[0]
    assert "no rule" in delivery.sent[0].lower(), delivery.sent[0]


async def test_a_dead_delivery_never_costs_the_answer(clean_db, tmp_path):
    """`safe_send_message` swallows everything, and the hook must too: the
    memory, the rule and the reclassification are all already done by the time
    this runs, so a comms outage costs the message and nothing else."""
    class Exploding(FakeDelivery):
        async def send_message(self, **kwargs):
            raise RuntimeError("comms is down")

    cfg = _repo(tmp_path)
    _declare(cfg, "expenses:hikmah:infra")
    msgid = await _post(clean_db, cfg)
    llm = FakeLLM('{"account": "expenses:hikmah:infra", "confidence": 0.95}')

    out = await _apply(clean_db, cfg, llm, delivery=Exploding())

    assert out["rule"] == "expenses:hikmah:infra"
    assert out["skipped_other_entity"] == 1
    assert await _account_of(clean_db, msgid) == "expenses:unknown"
    assert len(await _memories(clean_db)) == 1


async def test_no_delivery_wired_still_answers_and_never_raises(clean_db, tmp_path):
    """A fork with no comms server, and the default in every existing test."""
    cfg = _repo(tmp_path)
    _declare(cfg, "expenses:hikmah:infra")
    await _post(clean_db, cfg)
    llm = FakeLLM('{"account": "expenses:hikmah:infra", "confidence": 0.95}')

    out = await _apply(clean_db, cfg, llm, delivery=None)

    assert out["rule"] == "expenses:hikmah:infra" and out["skipped_other_entity"] == 1


def test_the_report_agrees_with_itself_on_number():
    """Noun AND verb, both ways.

    `books_answer_report` is pure, so the plural side needs no fixture — which
    matters, because reaching it through the books would take two postings in
    the other set of books, and the SINGULAR is the case that shipped wrong.
    """
    from aegis_worker.activities.curiosity import books_answer_report

    one = books_answer_report(PAYEE, {"rule": "expenses:hikmah:infra", "skipped_other_entity": 1})
    assert "1 existing posting sits in the other set of books" in one, one
    assert "postings" not in one and " sit " not in one, one

    two = books_answer_report(PAYEE, {"rule": "expenses:hikmah:infra", "skipped_other_entity": 2})
    assert "2 existing postings sit in the other set of books" in two, two

    # The other counted nouns the same sentence-builder writes.
    assert "1 posting could not be rewritten" in books_answer_report(
        PAYEE, {"rule": "expenses:groceries", "failed": 1}
    )
    assert "2 postings could not be rewritten" in books_answer_report(
        PAYEE, {"rule": "expenses:groceries", "failed": 2}
    )
    assert "1 existing posting still moved" in books_answer_report(
        PAYEE, {"rule": None, "reason": "rule_refused_backlog_applied", "reclassified": 1}
    )
    assert "2 existing postings still moved" in books_answer_report(
        PAYEE, {"rule": None, "reason": "rule_refused_backlog_applied", "reclassified": 2}
    )
