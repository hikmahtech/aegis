"""The four ledger chat tools (spec §8).

Every test here drives a REAL hledger over a REAL git checkout, because that
is the only way a refusal test can be trusted: a fixture that never reached
hledger would "pass" a security assertion by failing early for an unrelated
reason. Each refusal test therefore first proves the same call shape succeeds
(`test_query_refuses_the_argument_forms_hledger_would_honour` runs a plain
`bal`), and the write refusals additionally assert the journal did not change.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aegis.api.models.money import MoneyEvent
from aegis.services import books
from aegis.services import journal_index as ji
from aegis.services.chat import (
    _exec_ledger_add_rule,
    _exec_ledger_post,
    _exec_ledger_query,
    _exec_ledger_reclassify,
)
from aegis.services.tools.base import ToolContext

HAS_HLEDGER = shutil.which("hledger") is not None and shutil.which("git") is not None
pytestmark = pytest.mark.skipif(not HAS_HLEDGER, reason="hledger/git not installed")

ACCOUNTS = """commodity ₹ 1,00,000.00
account assets:bank:hdfc:1225
account assets:unknown
account expenses:unknown
account expenses:groceries
account expenses:saas
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


def _ctx(cfg: books.BooksConfig) -> ToolContext:
    return ToolContext(
        agent_id="maou",
        settings=SimpleNamespace(
            books_path=str(cfg.path), books_repo_url="", gmail_token_dir=str(cfg.path)
        ),
    )


def _journals(cfg: books.BooksConfig) -> dict[str, str]:
    return {str(p.relative_to(cfg.path)): p.read_text() for p in books.journal_files(cfg)}


def _commits(cfg: books.BooksConfig) -> int:
    proc = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=cfg.path,
        capture_output=True,
        text=True,
        check=True,
    )
    return int(proc.stdout.strip())


# Unique to this file. `ledger_add_rule`'s sweep selects across the WHOLE
# `finance.journal_index`, and this repo's test databases are keyed on the xdist
# worker alone — so they are shared between tests/core and tests/worker, and
# between two agents in two worktrees. A common payee like "Corner Store" would
# let a foreign `%:unknown` row match this test's rule, be rewritten against a
# tmp repo that does not hold it, and turn the exact-count assertions into
# "N failed".
TOKEN = "zzt4nakoda"


def _unknown_event(payee: str = f"Jai shree {TOKEN}") -> MoneyEvent:
    return MoneyEvent(
        kind="transaction",
        direction="out",
        amount=Decimal("10"),
        currency="INR",
        payee=payee,
        payee_key=payee.lower(),
        channel="upi",
        instrument="hdfc-1225",
        occurred_on=date(2026, 9, 2),
        entity="personal",
        account="expenses:unknown",
        parser="hdfc_upi",
        source_class="bank",
    )


@pytest_asyncio.fixture(loop_scope="function", autouse=True)
async def _clean(db_pool):
    await db_pool.execute("DELETE FROM finance.journal_index WHERE mailbox IN ('tool-t', 'manual')")
    yield
    await db_pool.execute("DELETE FROM finance.journal_index WHERE mailbox IN ('tool-t', 'manual')")


@pytest.mark.asyncio
async def test_query_runs_whitelisted_reports_and_refuses_others(db_pool, tmp_path):
    cfg = _repo(tmp_path)
    ctx = _ctx(cfg)
    out = await _exec_ledger_query(db_pool, {"command": "accounts", "args": ["--declared"]}, ctx)
    assert "expenses:groceries" in out
    # The wording, not just `error:` — see the next test for why a bare
    # `startswith("error:")` cannot fail here.
    out = await _exec_ledger_query(db_pool, {"command": "import", "args": ["x.csv"]}, ctx)
    assert out.startswith("error: hledger command not allowed"), out
    out = await _exec_ledger_query(db_pool, {"command": "bal", "args": ["-f", "/etc/passwd"]}, ctx)
    assert out.startswith("error: hledger argument not allowed"), out


@pytest.mark.asyncio
async def test_query_refuses_the_argument_forms_hledger_would_honour(db_pool, tmp_path):
    """The allowlist is exact-match because hledger bundles short flags, expands
    `@argsfile` and abbreviates long flags. Each of these was measured to work
    against the real binary, so a deny-prefix list lets them through.

    Asserting `startswith("error:")` here would be a BLIND test, which is why
    every case pins the refusal's own wording instead. Measured: with the
    allowlist deleted, `-Ef<path>` and `--fil=<path>` still make hledger exit
    non-zero — it reads the file and fails to parse it — so the tool returns an
    `error:` string either way, while hledger's stderr echoes the first line of
    the file it just read. A narrowing regression (someone re-permitting `-f` so
    a report can be scoped to one journal) would leave a bare-`error:` test
    green and hand the model the first line of any file on the host.

    So: the refusal must be `run_hledger`'s, the secret file's marker must not
    come back in the output, and the write case's target must not exist.

    The plain `bal` above every refusal is what makes this non-vacuous: the
    fixture reaches a working hledger, so the refusals are the allowlist and
    not a broken checkout.
    """
    cfg = _repo(tmp_path)
    ctx = _ctx(cfg)
    ok = await _exec_ledger_query(db_pool, {"command": "bal", "args": ["expenses"]}, ctx)
    assert not ok.startswith("error:"), ok

    marker = "zz-ledger-secret-marker-t4"
    secret = tmp_path / "secret.txt"
    secret.write_text(f"{marker}\nsecond line\n")
    victim = tmp_path / "written-by-hledger.txt"
    hostile = [
        [f"-Ef{secret}"],  # bundled: -E -f <path> — reads any file
        [f"-No{victim}"],  # bundled: -N -o <path> — arbitrary WRITE
        [f"--fil={secret}"],  # long-flag abbreviation of --file=
        [f"@{secret}"],  # args-file expansion
        [f"--rules-file={secret}"],
        ["--output-file", str(victim)],
        ["-o", str(victim)],
        ["-f", str(secret)],
        ["--file", str(secret)],
    ]
    for args in hostile:
        out = await _exec_ledger_query(db_pool, {"command": "bal", "args": args}, ctx)
        assert out.startswith("error: hledger argument not allowed"), (args, out)
        assert marker not in out, (args, out)
    assert not victim.exists(), "hledger was allowed to write a file"


@pytest.mark.asyncio
async def test_query_output_formats_and_default(db_pool, tmp_path):
    ctx = _ctx(_repo(tmp_path))
    out = await _exec_ledger_query(db_pool, {"command": "bal", "output": "csv"}, ctx)
    assert out.startswith('"account"')
    out = await _exec_ledger_query(db_pool, {"command": "accounts"}, ctx)
    assert "expenses:groceries" in out


@pytest.mark.asyncio
async def test_post_two_postings_then_reclassify(db_pool, tmp_path):
    cfg = _repo(tmp_path)
    ctx = _ctx(cfg)
    out = await _exec_ledger_post(
        db_pool,
        {
            "date": "2026-09-03",
            "payee": "Corner Store",
            "postings": [
                {"account": "expenses:unknown", "amount": "245.50", "currency": "INR"},
                {"account": "assets:bank:hdfc:1225"},
            ],
        },
        ctx,
    )
    assert out.startswith("posted manual/"), out
    msgid = out.split()[1]
    text = (cfg.path / "personal" / "2026.journal").read_text()
    assert "2026-09-03 * Corner Store" in text
    assert f"; msgid: {msgid}" in text
    assert "channel: manual" in text
    row = await ji.get(db_pool, msgid)
    assert row["parser"] == "manual" and row["amount"] == Decimal("245.50")
    assert row["direction"] == "out" and row["journal_file"] == "personal/2026.journal"

    out = await _exec_ledger_reclassify(
        db_pool, {"message_id": msgid, "account": "expenses:groceries"}, ctx
    )
    assert out.startswith("reclassified"), out
    assert (
        "    expenses:groceries                      ₹245.50\n"
        in (cfg.path / "personal" / "2026.journal").read_text()
    )
    assert (await ji.get(db_pool, msgid))["account"] == "expenses:groceries"

    before = _journals(cfg)
    out = await _exec_ledger_reclassify(
        db_pool, {"message_id": msgid, "account": "expenses:nope"}, ctx
    )
    assert out.startswith("error:") and "not declared" in out
    assert _journals(cfg) == before, "an undeclared account still reached the journal"
    assert (await ji.get(db_pool, msgid))["account"] == "expenses:groceries"


@pytest.mark.asyncio
async def test_reclassify_renames_the_payee(db_pool, tmp_path):
    cfg = _repo(tmp_path)
    ctx = _ctx(cfg)
    out = await _exec_ledger_post(
        db_pool,
        {
            "date": "2026-09-03",
            "payee": "PAYTM*NAKODA",
            "postings": [
                {"account": "expenses:unknown", "amount": "12", "currency": "INR"},
                {"account": "assets:bank:hdfc:1225"},
            ],
        },
        ctx,
    )
    msgid = out.split()[1]
    await _exec_ledger_reclassify(
        db_pool,
        {"message_id": msgid, "account": "expenses:groceries", "payee": "Jai Shree Stores"},
        ctx,
    )
    text = (cfg.path / "personal" / "2026.journal").read_text()
    assert "2026-09-03 * Jai Shree Stores" in text and "PAYTM*NAKODA" not in text
    assert (await ji.get(db_pool, msgid))["payee"] == "Jai Shree Stores"


@pytest.mark.asyncio
async def test_reclassify_refuses_to_cross_entities(db_pool, tmp_path):
    """The hazard `ledger_add_rule`'s sweep guards, reachable here in one call:
    `expenses:hikmah:saas` is declared and the block still balances, so neither
    the chart check nor `check --strict` objects — and a personal posting ends
    up counted in the business books while its block stays in
    `personal/2026.journal`."""
    cfg = _repo(tmp_path)
    (cfg.path / "accounts.journal").write_text(ACCOUNTS + "account expenses:hikmah:saas\n")
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "chart"],
        cwd=cfg.path,
        check=True,
    )
    ctx = _ctx(cfg)
    out = await _exec_ledger_post(
        db_pool,
        {
            "date": "2026-09-03",
            "payee": "Shop",
            "postings": [
                {"account": "expenses:unknown", "amount": "5", "currency": "INR"},
                {"account": "assets:bank:hdfc:1225"},
            ],
        },
        ctx,
    )
    msgid = out.split()[1]
    before = _journals(cfg)
    crossed = await _exec_ledger_reclassify(
        db_pool, {"message_id": msgid, "account": "expenses:hikmah:saas"}, ctx
    )
    assert crossed.startswith("error:") and "personal/2026.journal" in crossed, crossed
    assert _journals(cfg) == before
    assert (await ji.get(db_pool, msgid))["account"] == "expenses:unknown"
    # Same-entity moves are untouched by the guard.
    assert (
        await _exec_ledger_reclassify(
            db_pool, {"message_id": msgid, "account": "expenses:groceries"}, ctx
        )
    ).startswith("reclassified")


@pytest.mark.asyncio
async def test_reclassify_allows_the_accounts_both_entities_share(db_pool, tmp_path):
    """Assets, liabilities and equity are entity-neutral by design — the chart
    declares ONE `assets:bank:hdfc:1225`, and `books.instrument_account` writes
    it into both sets of books with no notion of entity. Refusing a hikmah
    posting onto a shared account would be a false refusal on a real correction
    (a mis-filed transfer), and it is not the hazard the guard exists for."""
    cfg = _repo(tmp_path)
    ctx = _ctx(cfg)
    out = await _exec_ledger_post(
        db_pool,
        {
            "date": "2026-09-03",
            "payee": "Vendor",
            "entity": "hikmah",
            "postings": [
                {"account": "expenses:unknown", "amount": "9", "currency": "INR"},
                {"account": "assets:bank:hdfc:1225"},
            ],
        },
        ctx,
    )
    msgid = out.split()[1]
    # A shared account: allowed even though the block is in the hikmah books.
    shared = await _exec_ledger_reclassify(
        db_pool, {"message_id": msgid, "account": "assets:unknown"}, ctx
    )
    assert shared.startswith("reclassified"), shared
    assert "assets:unknown" in (cfg.path / "hikmah" / "2026.journal").read_text()
    # A personal EXPENSE account: still refused, which is the real hazard.
    crossed = await _exec_ledger_reclassify(
        db_pool, {"message_id": msgid, "account": "expenses:groceries"}, ctx
    )
    assert crossed.startswith("error:") and "hikmah/2026.journal" in crossed, crossed


@pytest.mark.asyncio
async def test_reclassify_unknown_message_id_is_an_error(db_pool, tmp_path):
    ctx = _ctx(_repo(tmp_path))
    out = await _exec_ledger_reclassify(
        db_pool, {"message_id": "manual/does-not-exist", "account": "expenses:groceries"}, ctx
    )
    assert out.startswith("error:"), out


@pytest.mark.asyncio
async def test_post_validation(db_pool, tmp_path):
    cfg = _repo(tmp_path)
    ctx = _ctx(cfg)
    before = _journals(cfg)
    bad = [
        {
            "date": "2026/09/03",
            "payee": "x",
            "postings": [
                {"account": "expenses:unknown", "amount": "1", "currency": "INR"},
                {"account": "assets:unknown"},
            ],
        },
        {
            "date": "2026-09-03",
            "payee": "x",
            "postings": [{"account": "expenses:unknown", "amount": "1", "currency": "INR"}],
        },
        {
            "date": "2026-09-03",
            "payee": "x",
            "postings": [{"account": "expenses:unknown"}, {"account": "assets:unknown"}],
        },
        {
            "date": "2026-09-03",
            "payee": "x",
            "postings": [
                {"account": "expenses:zzz", "amount": "1", "currency": "INR"},
                {"account": "assets:unknown"},
            ],
        },
        {
            "date": "2026-09-03",
            "payee": "x",
            "entity": "other",
            "postings": [
                {"account": "expenses:unknown", "amount": "1", "currency": "INR"},
                {"account": "assets:unknown"},
            ],
        },
        {
            "date": "2026-09-03",
            "payee": "x",
            "postings": [
                {"account": "expenses:unknown", "amount": "not-a-number", "currency": "INR"},
                {"account": "assets:unknown"},
            ],
        },
    ]
    for args in bad:
        assert (await _exec_ledger_post(db_pool, args, ctx)).startswith("error:"), args
    assert _journals(cfg) == before, "a refused post still wrote to the journal"
    # Three of these would ALSO be refused by `hledger check --strict` inside
    # the writer, after the write and the revert, so `startswith("error:")`
    # alone cannot tell whether this tool's own check still exists. Each one
    # therefore pins the wording only this tool produces.
    assert "at least two postings" in await _exec_ledger_post(db_pool, bad[1], ctx)
    assert "at most one posting may omit" in await _exec_ledger_post(db_pool, bad[2], ctx)
    assert "is not declared in the chart" in await _exec_ledger_post(db_pool, bad[3], ctx)
    assert await db_pool.fetchval(
        "SELECT count(*) FROM finance.journal_index WHERE mailbox = 'manual'"
    ) == 0


@pytest.mark.asyncio
async def test_post_refuses_a_decimal_that_is_not_a_number(db_pool, tmp_path):
    """`Decimal` builds NaN, Infinity and 1e400 without complaint; each one then
    raises `InvalidOperation` inside `quantize()` several frames down, and the
    model gets an exception repr instead of a sentence."""
    cfg = _repo(tmp_path)
    ctx = _ctx(cfg)
    before = _journals(cfg)
    for amount in ("NaN", "Infinity", "-Infinity", "1e400"):
        out = await _exec_ledger_post(
            db_pool,
            {
                "date": "2026-09-03",
                "payee": "x",
                "postings": [
                    {"account": "expenses:unknown", "amount": amount, "currency": "INR"},
                    {"account": "assets:bank:hdfc:1225"},
                ],
            },
            ctx,
        )
        assert "is not a usable amount" in out, (amount, out)
    assert _journals(cfg) == before


@pytest.mark.asyncio
async def test_reposting_the_same_transaction_is_a_retry_not_a_duplicate(db_pool, tmp_path):
    """The 30s chat-tool cap cannot cancel the thread `books._write` runs in, so
    a write can commit after the model was told it timed out. A `uuid4()` msgid
    would make the model's natural retry a SECOND copy of the transaction; a
    content-derived one makes it find the first block and write nothing."""
    cfg = _repo(tmp_path)
    ctx = _ctx(cfg)
    args = {
        "date": "2026-09-03",
        "payee": "Corner Store",
        "postings": [
            {"account": "expenses:unknown", "amount": "245.50", "currency": "INR"},
            {"account": "assets:bank:hdfc:1225"},
        ],
    }
    first = await _exec_ledger_post(db_pool, args, ctx)
    assert first.startswith("posted manual/"), first
    second = await _exec_ledger_post(db_pool, args, ctx)
    assert second.startswith("already posted as manual/"), second
    msgid = first.split()[1]
    assert msgid in second
    text = (cfg.path / "personal" / "2026.journal").read_text()
    assert text.count("* Corner Store") == 1
    assert text.count(f"; msgid: {msgid}") == 1
    # A genuine second identical transaction is recorded by distinguishing it.
    third = await _exec_ledger_post(db_pool, {**args, "note": "second coffee"}, ctx)
    assert third.startswith("posted manual/") and third.split()[1] != msgid, third
    assert (cfg.path / "personal" / "2026.journal").read_text().count("* Corner Store") == 2


@pytest.mark.asyncio
async def test_the_retry_key_is_the_block_not_the_caller_s_typing(db_pool, tmp_path):
    """The journal stores NORMALIZED values — a quantized amount, a defaulted
    currency, a sanitized payee and note — so a msgid digested from the raw
    arguments gives `"245.50"` and `"245.5"` different ids for a byte-identical
    block, and the duplicate lands anyway. An LLM re-issuing a timed-out call is
    not byte-stable, so these ARE the retry."""
    cfg = _repo(tmp_path)
    ctx = _ctx(cfg)
    variants = [
        [
            {"account": "expenses:unknown", "amount": "245.50", "currency": "INR"},
            {"account": "assets:bank:hdfc:1225"},
        ],
        # same amount, written differently
        [
            {"account": "expenses:unknown", "amount": "245.5", "currency": "INR"},
            {"account": "assets:bank:hdfc:1225"},
        ],
        # currency omitted — the journal defaults it to INR
        [
            {"account": "expenses:unknown", "amount": "245.50"},
            {"account": "assets:bank:hdfc:1225"},
        ],
        # currency in lower case — the journal upper-cases it
        [
            {"account": "expenses:unknown", "amount": "245.500", "currency": "inr"},
            {"account": "assets:bank:hdfc:1225"},
        ],
        # a blank amount is a blank amount however it is spelled
        [
            {"account": "expenses:unknown", "amount": "245.50", "currency": "INR"},
            {"account": "assets:bank:hdfc:1225", "amount": ""},
        ],
    ]
    outs = []
    for postings in variants:
        outs.append(
            await _exec_ledger_post(
                db_pool,
                {"date": "2026-09-03", "payee": "Corner  Store ", "postings": postings},
                ctx,
            )
        )
    assert outs[0].startswith("posted manual/"), outs[0]
    for out in outs[1:]:
        assert out.startswith("already posted as "), out
        assert outs[0].split()[1] in out, out
    text = (cfg.path / "personal" / "2026.journal").read_text()
    assert text.count("; msgid: ") == 1, text


@pytest.mark.asyncio
async def test_a_repost_never_drags_the_index_back(db_pool, tmp_path):
    """Post, reclassify, then re-post the identical transaction. The journal
    keeps the reclassified account, so an unconditional re-index would reset the
    row to the account the transaction was first filed under and leave the index
    disagreeing with the record."""
    cfg = _repo(tmp_path)
    ctx = _ctx(cfg)
    args = {
        "date": "2026-09-03",
        "payee": "Corner Store",
        "postings": [
            {"account": "expenses:unknown", "amount": "245.50", "currency": "INR"},
            {"account": "assets:bank:hdfc:1225"},
        ],
    }
    msgid = (await _exec_ledger_post(db_pool, args, ctx)).split()[1]
    await _exec_ledger_reclassify(
        db_pool, {"message_id": msgid, "account": "expenses:groceries"}, ctx
    )
    again = await _exec_ledger_post(db_pool, args, ctx)
    assert again.startswith("already posted as "), again
    assert "expenses:groceries" in (cfg.path / "personal" / "2026.journal").read_text()
    assert (await ji.get(db_pool, msgid))["account"] == "expenses:groceries"

    # …and the repair case still repairs: an index row lost after the journal
    # was written must come back on the retry.
    await db_pool.execute("DELETE FROM finance.journal_index WHERE message_id = $1", msgid)
    repaired = await _exec_ledger_post(db_pool, args, ctx)
    assert repaired.startswith("already posted as "), repaired
    assert (await ji.get(db_pool, msgid)) is not None


@pytest.mark.asyncio
async def test_the_write_tools_have_a_timeout_that_fits_a_books_write(db_pool, tmp_path):
    """`asyncio.wait_for` cannot cancel the thread the write runs in, so a cap
    below the writer's own budget does not prevent the commit — it only
    misreports it. Pinned the same way `aegis_self_diagnose`'s override is."""
    from aegis.services import books as books_mod
    from aegis.services.chat import _TOOL_TIMEOUT_OVERRIDES

    floor = books_mod.CLONE_TIMEOUT_S + 120 + 60 + 60 + 120
    for name in ("ledger_post", "ledger_reclassify", "ledger_add_rule"):
        assert _TOOL_TIMEOUT_OVERRIDES.get(name, 0) >= floor, name
    assert "ledger_query" not in _TOOL_TIMEOUT_OVERRIDES


@pytest.mark.asyncio
async def test_post_more_than_two_postings(db_pool, tmp_path):
    cfg = _repo(tmp_path)
    ctx = _ctx(cfg)
    out = await _exec_ledger_post(
        db_pool,
        {
            "date": "2026-09-04",
            "payee": "Split Bill",
            "note": "half groceries half saas",
            "postings": [
                {"account": "expenses:groceries", "amount": "100", "currency": "INR"},
                {"account": "expenses:saas", "amount": "145.50", "currency": "INR"},
                {"account": "assets:bank:hdfc:1225"},
            ],
        },
        ctx,
    )
    assert out.startswith("posted manual/"), out
    text = (cfg.path / "personal" / "2026.journal").read_text()
    assert "expenses:groceries" in text and "expenses:saas" in text
    assert "note: half groceries half saas" in text
    # `hledger check --strict` ran inside the write and did not revert it.
    bal = await _exec_ledger_query(db_pool, {"command": "bal", "args": ["expenses:saas"]}, ctx)
    assert "145.50" in bal


@pytest.mark.asyncio
async def test_post_that_does_not_balance_is_refused_and_reverted(db_pool, tmp_path):
    """Proof the write rode `books.py`'s protocol rather than a hand-rolled
    append: only `hledger check --strict` can catch this, and only the writer's
    revert puts the journal back."""
    cfg = _repo(tmp_path)
    ctx = _ctx(cfg)
    before = _journals(cfg)
    out = await _exec_ledger_post(
        db_pool,
        {
            "date": "2026-09-04",
            "payee": "Lopsided",
            "postings": [
                {"account": "expenses:groceries", "amount": "100", "currency": "INR"},
                {"account": "assets:bank:hdfc:1225", "amount": "-90", "currency": "INR"},
            ],
        },
        ctx,
    )
    assert out.startswith("error:"), out
    assert _journals(cfg) == before, "the unbalanced block survived in the journal"
    assert (
        await db_pool.fetchval(
            "SELECT count(*) FROM finance.journal_index WHERE mailbox = 'manual'"
        )
        == 0
    )


@pytest.mark.asyncio
async def test_post_keeps_the_sign_of_an_income_posting(db_pool, tmp_path):
    """A negative amount is money coming IN. Rendering it as a magnitude would
    balance and pass `check --strict` while pointing the money the wrong way —
    a silent corruption with no failing gate anywhere else."""
    cfg = _repo(tmp_path)
    ctx = _ctx(cfg)
    out = await _exec_ledger_post(
        db_pool,
        {
            "date": "2026-09-05",
            "payee": "Client",
            "postings": [
                {"account": "income:unknown", "amount": "-500", "currency": "INR"},
                {"account": "assets:bank:hdfc:1225"},
            ],
        },
        ctx,
    )
    assert out.startswith("posted manual/"), out
    msgid = out.split()[1]
    assert "-₹500.00" in (cfg.path / "personal" / "2026.journal").read_text()
    row = await ji.get(db_pool, msgid)
    assert row["direction"] == "in" and row["amount"] == Decimal("500.00")
    bal = await _exec_ledger_query(
        db_pool, {"command": "bal", "args": ["assets:bank:hdfc:1225", "--no-total"]}, ctx
    )
    assert "500.00" in bal and "-" not in bal, bal  # the bank account GAINED ₹500


@pytest.mark.asyncio
async def test_post_writes_to_the_named_entity(db_pool, tmp_path):
    cfg = _repo(tmp_path)
    ctx = _ctx(cfg)
    out = await _exec_ledger_post(
        db_pool,
        {
            "date": "2026-09-03",
            "payee": "Vendor",
            "entity": "hikmah",
            "postings": [
                {"account": "expenses:saas", "amount": "9", "currency": "INR"},
                {"account": "assets:bank:hdfc:1225"},
            ],
        },
        ctx,
    )
    assert out.endswith("hikmah/2026.journal"), out
    assert "Vendor" in (cfg.path / "hikmah" / "2026.journal").read_text()
    assert "Vendor" not in (cfg.path / "personal" / "2026.journal").read_text()


@pytest.mark.asyncio
async def test_post_cannot_forge_a_second_block_through_the_payee_or_note(db_pool, tmp_path):
    """The writer is the last gate: a newline in a model-supplied payee used to
    add a whole transaction, with its own `; msgid:` line, that `check --strict`
    accepted and nothing reverted."""
    cfg = _repo(tmp_path)
    ctx = _ctx(cfg)
    hostile = (
        "ok\n\n2026-09-01 * ATTACKER PAYOUT\n"
        "    ; msgid: manual/HIJACKED\n"
        "    expenses:groceries                       ₹99999.00\n"
        "    assets:bank:hdfc:1225\n"
    )
    out = await _exec_ledger_post(
        db_pool,
        {
            "date": "2026-09-03",
            "payee": hostile,
            "note": hostile,
            "postings": [
                {"account": "expenses:unknown", "amount": "1", "currency": "INR"},
                {"account": "assets:bank:hdfc:1225"},
            ],
        },
        ctx,
    )
    assert out.startswith("posted manual/"), out
    text = (cfg.path / "personal" / "2026.journal").read_text()
    # The hostile text survives only as inert one-line prose: one transaction,
    # one msgid, three tags, and hledger never sees the forged ₹99999 posting.
    assert text.count("; msgid: ") == 1
    assert sum(1 for line in text.splitlines() if line.startswith("2026-")) == 1
    bal = await _exec_ledger_query(
        db_pool, {"command": "bal", "args": ["expenses", "--no-total"]}, ctx
    )
    assert "99999" not in bal and "1.00" in bal
    tags = await _exec_ledger_query(db_pool, {"command": "tags"}, ctx)
    assert sorted(tags.split()) == ["channel", "msgid", "note"], tags


@pytest.mark.asyncio
async def test_post_note_cannot_declare_a_second_tag(db_pool, tmp_path):
    """A comma is the tag separator, so a note carrying one would declare a tag
    of the caller's choosing — `sanitize_payee` leaves commas alone, which is
    why the note goes through `sanitize_tag` instead."""
    cfg = _repo(tmp_path)
    ctx = _ctx(cfg)
    out = await _exec_ledger_post(
        db_pool,
        {
            "date": "2026-09-03",
            "payee": "Shop",
            "note": "lunch, hijacked: true",
            "postings": [
                {"account": "expenses:unknown", "amount": "1", "currency": "INR"},
                {"account": "assets:bank:hdfc:1225"},
            ],
        },
        ctx,
    )
    assert out.startswith("posted manual/"), out
    tags = await _exec_ledger_query(db_pool, {"command": "tags"}, ctx)
    assert sorted(tags.split()) == ["channel", "msgid", "note"], tags


@pytest.mark.asyncio
async def test_add_rule_applies_to_unknown_postings(db_pool, tmp_path):
    cfg = _repo(tmp_path)
    ctx = _ctx(cfg)
    ev = _unknown_event()
    await books.post_event(ev, "tool-t/a", cfg)
    await ji.upsert(db_pool, "tool-t/a", "tool-t", ev, journal_file="personal/2026.journal")
    out = await _exec_ledger_add_rule(
        db_pool,
        {
            "match": f"jai shree {TOKEN}",
            "account": "expenses:groceries",
            "payee": "Jai Shree Stores",
        },
        ctx,
    )
    assert out == "rule added; reclassified 1 postings"
    rules = books.load_rules(cfg.path / "rules" / "accounts.yaml")
    assert rules[-1] == {
        "match": f"jai shree {TOKEN}",
        "account": "expenses:groceries",
        "payee": "Jai Shree Stores",
    }
    text = (cfg.path / "personal" / "2026.journal").read_text()
    assert "* Jai Shree Stores" in text and "expenses:groceries" in text
    assert (await ji.get(db_pool, "tool-t/a"))["account"] == "expenses:groceries"
    assert (
        await _exec_ledger_add_rule(db_pool, {"match": "(", "account": "expenses:groceries"}, ctx)
    ).startswith("error:")
    assert (
        await _exec_ledger_add_rule(db_pool, {"match": "x", "account": "expenses:nope"}, ctx)
    ).startswith("error:")


@pytest.mark.asyncio
async def test_add_rule_sweeps_the_backlog_in_one_commit(db_pool, tmp_path):
    """Per-posting writes would take the flock, pull, strict-check, commit and
    push once EACH — serialising the worker's money flows behind the sweep and
    leaving one commit per posting. Two matching postings must cost exactly two
    commits: the rule, then the batch."""
    cfg = _repo(tmp_path)
    ctx = _ctx(cfg)
    for n in ("d", "e"):
        ev = _unknown_event()
        await books.post_event(ev, f"tool-t/{n}", cfg)
        await ji.upsert(db_pool, f"tool-t/{n}", "tool-t", ev, journal_file="personal/2026.journal")
    before = _commits(cfg)
    out = await _exec_ledger_add_rule(
        db_pool, {"match": f"jai shree {TOKEN}", "account": "expenses:groceries"}, ctx
    )
    assert out == "rule added; reclassified 2 postings"
    assert _commits(cfg) - before == 2, "one commit for the rule, one for the whole sweep"
    for n in ("d", "e"):
        assert (await ji.get(db_pool, f"tool-t/{n}"))["account"] == "expenses:groceries"


@pytest.mark.asyncio
async def test_add_rule_refusals_write_nothing(db_pool, tmp_path):
    """A refused rule must not reach `rules/accounts.yaml` — the refusal is the
    only thing standing between a model-authored regex and a permanent
    classification rule."""
    cfg = _repo(tmp_path)
    ctx = _ctx(cfg)
    rules_path = cfg.path / "rules" / "accounts.yaml"
    before = rules_path.read_text()
    for args in (
        {"match": "(", "account": "expenses:groceries"},
        {"match": "x", "account": "expenses:nope"},
        {"match": "x" * 201, "account": "expenses:groceries"},
        {"match": "x", "account": "expenses:groceries", "entity": "other"},
    ):
        assert (await _exec_ledger_add_rule(db_pool, args, ctx)).startswith("error:"), args
    assert rules_path.read_text() == before


@pytest.mark.asyncio
async def test_add_rule_refuses_a_catastrophic_regex_before_persisting_it(db_pool, tmp_path):
    """`re` has no timeout and `books.apply_rules` runs `re.search` on the event
    loop, so an exponential pattern hangs the whole Core process and
    `asyncio.wait_for` cannot interrupt it. Worse, the rule is then committed to
    `rules/accounts.yaml` and the WORKER runs it against every incoming money
    event from then on — a durable, cross-process hang authored by a model. So
    it has to be refused before it reaches the file."""
    cfg = _repo(tmp_path)
    ctx = _ctx(cfg)
    rules_path = cfg.path / "rules" / "accounts.yaml"
    before = rules_path.read_text()
    # Every quantified group, not only the self-nesting ones: `((a+))+` slipped
    # past a guard that required the group to hold no parens, and was measured
    # still running after 20 SECONDS against a 31-character input.
    for pattern in ("(a+)+$", "(?:a*)*b", "(a{1,3})+x", "(ab|ab)+c", "((a+))+$", "((ab)*)*"):
        out = await _exec_ledger_add_rule(
            db_pool, {"match": pattern, "account": "expenses:groceries", "apply": False}, ctx
        )
        assert "repeats a group" in out, (pattern, out)
    # Stacked quantifiers with NO group at all — no syntactic rule about groups
    # can see these, which is why the pattern is also timed before it is stored.
    for pattern in ("a*a*a*a*a*a*a*$", "a*a*a*a*a*$"):
        out = await _exec_ledger_add_rule(
            db_pool, {"match": pattern, "account": "expenses:groceries", "apply": False}, ctx
        )
        assert out.startswith("error:"), (pattern, out)
    assert rules_path.read_text() == before
    # The shapes a real rule uses must still be accepted, or the guard is just
    # a ban on regexes: alternation, a wildcard, a plain repeat, an inline flag.
    for pattern in (
        "amazon web services|invoicing@aws\\.com",
        "mahavitaran.*suncity 501",
        "a+b",
        "^(hdfc|icici).*upi",
        "[0-9]+ payment",
    ):
        out = await _exec_ledger_add_rule(
            db_pool, {"match": pattern, "account": "expenses:groceries", "apply": False}, ctx
        )
        assert out == "rule added; reclassified 0 postings", (pattern, out)


def test_the_regex_check_stops_a_pattern_that_never_finishes():
    """The backstop, tested directly because nothing that gets past the three
    cheap bounds can reach it.

    `"a*" * 40 + "$"` does not finish against a 25-character string — measured,
    it ran past 300 seconds and hung the whole suite when the quantifier cap was
    removed. `re` holds its thread and ignores every timeout Python can express,
    so the only thing that can end it is killing the process it runs in. This
    asserts that the check ANSWERS, which no in-process version can do.
    """
    from aegis.services.tools.ledger import _regex_too_slow

    started = time.perf_counter()
    reason = _regex_too_slow("a*" * 40 + "$", kill_after=2.0)
    elapsed = time.perf_counter() - started
    assert reason is not None and "did not finish" in reason, reason
    assert elapsed < 20, elapsed
    # And a real rule still comes back clean through the same path.
    assert _regex_too_slow("mahavitaran.*suncity 501") is None


@pytest.mark.asyncio
async def test_add_rule_regex_check_is_itself_bounded(db_pool, tmp_path):
    """The timing probe cannot be interrupted — `re` has no timeout — so the
    syntactic bounds in front of it are what stop the CHECK from becoming the
    hang it exists to prevent. Whatever the pattern, answering must be quick."""
    ctx = _ctx(_repo(tmp_path))
    started = time.perf_counter()
    for pattern in ("((a+))+$", "a*" * 40 + "$", "(a|a)+$", "a*a*a*a*a*a*$"):
        out = await _exec_ledger_add_rule(
            db_pool, {"match": pattern, "account": "expenses:groceries", "apply": False}, ctx
        )
        assert out.startswith("error:"), (pattern, out)
    assert time.perf_counter() - started < 10, "the guard took longer than the rules it refuses"


@pytest.mark.asyncio
async def test_add_rule_without_apply_leaves_postings_alone(db_pool, tmp_path):
    cfg = _repo(tmp_path)
    ctx = _ctx(cfg)
    ev = _unknown_event()
    await books.post_event(ev, "tool-t/b", cfg)
    await ji.upsert(db_pool, "tool-t/b", "tool-t", ev, journal_file="personal/2026.journal")
    out = await _exec_ledger_add_rule(
        db_pool,
        {"match": f"jai shree {TOKEN}", "account": "expenses:groceries", "apply": False},
        ctx,
    )
    assert out == "rule added; reclassified 0 postings"
    assert "expenses:unknown" in (cfg.path / "personal" / "2026.journal").read_text()
    assert (await ji.get(db_pool, "tool-t/b"))["account"] == "expenses:unknown"


@pytest.mark.asyncio
async def test_add_rule_skips_a_posting_in_the_other_entity(db_pool, tmp_path):
    """A rule that names an entity must not move a posting in the other set of
    books: the account would change but the block would stay in the wrong
    journal file, which is how an `expenses:hikmah:*` posting lands in
    `personal/2026.journal`."""
    cfg = _repo(tmp_path)
    ctx = _ctx(cfg)
    ev = _unknown_event()
    await books.post_event(ev, "tool-t/c", cfg)
    await ji.upsert(db_pool, "tool-t/c", "tool-t", ev, journal_file="personal/2026.journal")
    out = await _exec_ledger_add_rule(
        db_pool,
        {"match": f"jai shree {TOKEN}", "account": "expenses:groceries", "entity": "hikmah"},
        ctx,
    )
    assert out == "rule added; reclassified 0 postings"
    assert (await ji.get(db_pool, "tool-t/c"))["account"] == "expenses:unknown"


@pytest.mark.asyncio
async def test_add_rule_index_payee_matches_the_journal(db_pool, tmp_path):
    """`;` is journal syntax, so the writer strips it — and an index that kept
    the raw name would disagree with the record for `find_match` and the admin
    page."""
    cfg = _repo(tmp_path)
    ctx = _ctx(cfg)
    ev = _unknown_event()
    await books.post_event(ev, "tool-t/f", cfg)
    await ji.upsert(db_pool, "tool-t/f", "tool-t", ev, journal_file="personal/2026.journal")
    await _exec_ledger_add_rule(
        db_pool,
        {"match": f"jai shree {TOKEN}", "account": "expenses:groceries", "payee": "A;B"},
        ctx,
    )
    assert "* A B" in (cfg.path / "personal" / "2026.journal").read_text()
    assert (await ji.get(db_pool, "tool-t/f"))["payee"] == "A B"


@pytest.mark.asyncio
async def test_books_disabled_is_reported_not_raised(db_pool, tmp_path):
    """No checkout and no repo url: every tool answers with `error: …` rather
    than raising through the chat loop."""
    ctx = ToolContext(
        agent_id="maou",
        settings=SimpleNamespace(
            books_path=str(tmp_path / "nothing-here"),
            books_repo_url="",
            gmail_token_dir=str(tmp_path),
        ),
    )
    for out in (
        await _exec_ledger_query(db_pool, {"command": "bal"}, ctx),
        await _exec_ledger_post(
            db_pool,
            {
                "date": "2026-09-03",
                "payee": "x",
                "postings": [
                    {"account": "expenses:unknown", "amount": "1", "currency": "INR"},
                    {"account": "assets:unknown"},
                ],
            },
            ctx,
        ),
        await _exec_ledger_reclassify(db_pool, {"message_id": "manual/x", "account": "e:u"}, ctx),
        await _exec_ledger_add_rule(db_pool, {"match": "x", "account": "e:u"}, ctx),
    ):
        assert out.startswith("error:"), out


def test_tools_are_registered_and_gated():
    from aegis.api.routes.mcp_server import _UNSERVED_TOOLS
    from aegis.services.chat import AGENT_TOOL_SETS, CHAT_TOOLS, TOOL_EXECUTORS

    names = {t["function"]["name"] for t in CHAT_TOOLS}
    for n in ("ledger_query", "ledger_post", "ledger_reclassify", "ledger_add_rule"):
        assert n in names and n in TOOL_EXECUTORS and n in AGENT_TOOL_SETS["maou"]
    assert "ledger_query" in AGENT_TOOL_SETS["sebas"]
    assert {"ledger_post", "ledger_reclassify", "ledger_add_rule"} <= _UNSERVED_TOOLS
    assert "ledger_query" not in _UNSERVED_TOOLS


def test_seed_grants_match_the_code_defaults():
    """`config/seed/agents.yaml` seeds the DB `metadata.tool_set`, which WINS
    over the Python dict at runtime — a grant added only in code never reaches
    a fresh deployment."""
    import yaml

    root = Path(__file__).resolve().parents[2]
    agents = yaml.safe_load((root / "config" / "seed" / "agents.yaml").read_text())["agents"]
    by_id = {a["id"]: a for a in agents}
    maou = set(by_id["maou"]["metadata"]["tool_set"])
    assert {"ledger_query", "ledger_post", "ledger_reclassify", "ledger_add_rule"} <= maou
    assert "ledger_query" in set(by_id["sebas"]["metadata"]["tool_set"])
    for writer in ("ledger_post", "ledger_reclassify", "ledger_add_rule"):
        assert writer not in set(by_id["sebas"]["metadata"]["tool_set"])
