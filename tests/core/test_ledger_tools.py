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
    out = await _exec_ledger_query(db_pool, {"command": "import", "args": ["x.csv"]}, ctx)
    assert out.startswith("error:")
    out = await _exec_ledger_query(db_pool, {"command": "bal", "args": ["-f", "/etc/passwd"]}, ctx)
    assert out.startswith("error:")


@pytest.mark.asyncio
async def test_query_refuses_the_argument_forms_hledger_would_honour(db_pool, tmp_path):
    """The allowlist is exact-match because hledger bundles short flags, expands
    `@argsfile` and abbreviates long flags. Each of these was measured to work
    against the real binary, so a deny-prefix list lets them through.

    The plain `bal` above every refusal is what makes this non-vacuous: the
    fixture reaches a working hledger, so the refusals are the allowlist and
    not a broken checkout.
    """
    cfg = _repo(tmp_path)
    ctx = _ctx(cfg)
    ok = await _exec_ledger_query(db_pool, {"command": "bal", "args": ["expenses"]}, ctx)
    assert not ok.startswith("error:"), ok

    victim = tmp_path / "written-by-hledger.txt"
    hostile = [
        ["-Ef/etc/passwd"],  # bundled: -E -f <path> — reads any file
        [f"-No{victim}"],  # bundled: -N -o <path> — arbitrary WRITE
        ["--fil=/etc/passwd"],  # long-flag abbreviation of --file=
        ["@/etc/passwd"],  # args-file expansion
        ["--rules-file=/etc/passwd"],
        ["--output-file", str(victim)],
    ]
    for args in hostile:
        out = await _exec_ledger_query(db_pool, {"command": "bal", "args": args}, ctx)
        assert out.startswith("error:"), (args, out)
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
    # The tool's own chart check, in its own words: `hledger check --strict`
    # would also reject `expenses:zzz`, but only after the write, the revert
    # and a message about a "strict check" that the model cannot act on.
    undeclared = await _exec_ledger_post(db_pool, bad[3], ctx)
    assert "is not declared in the chart" in undeclared, undeclared
    assert await db_pool.fetchval(
        "SELECT count(*) FROM finance.journal_index WHERE mailbox = 'manual'"
    ) == 0


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
    ev = MoneyEvent(
        kind="transaction",
        direction="out",
        amount=Decimal("10"),
        currency="INR",
        payee="Jai shree nakoda",
        payee_key="jai shree nakoda",
        channel="upi",
        instrument="hdfc-1225",
        occurred_on=date(2026, 9, 2),
        entity="personal",
        account="expenses:unknown",
        parser="hdfc_upi",
        source_class="bank",
    )
    await books.post_event(ev, "tool-t/a", cfg)
    await ji.upsert(db_pool, "tool-t/a", "tool-t", ev, journal_file="personal/2026.journal")
    out = await _exec_ledger_add_rule(
        db_pool,
        {"match": "jai shree", "account": "expenses:groceries", "payee": "Jai Shree Stores"},
        ctx,
    )
    assert out == "rule added; reclassified 1 postings"
    rules = books.load_rules(cfg.path / "rules" / "accounts.yaml")
    assert rules[-1] == {
        "match": "jai shree",
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
async def test_add_rule_refusals_write_nothing(db_pool, tmp_path):
    """A refused rule must not reach `rules/accounts.yaml` — the refusal is the
    only thing standing between a model-authored regex and a permanent
    classification rule."""
    cfg = _repo(tmp_path)
    ctx = _ctx(cfg)
    rules_path = cfg.path / "rules" / "accounts.yaml"
    before = rules_path.read_text()
    assert (
        await _exec_ledger_add_rule(db_pool, {"match": "(", "account": "expenses:groceries"}, ctx)
    ).startswith("error:")
    assert (
        await _exec_ledger_add_rule(db_pool, {"match": "x", "account": "expenses:nope"}, ctx)
    ).startswith("error:")
    assert rules_path.read_text() == before


@pytest.mark.asyncio
async def test_add_rule_without_apply_leaves_postings_alone(db_pool, tmp_path):
    cfg = _repo(tmp_path)
    ctx = _ctx(cfg)
    ev = MoneyEvent(
        kind="transaction",
        direction="out",
        amount=Decimal("10"),
        currency="INR",
        payee="Jai shree nakoda",
        payee_key="jai shree nakoda",
        channel="upi",
        instrument="hdfc-1225",
        occurred_on=date(2026, 9, 2),
        entity="personal",
        account="expenses:unknown",
        parser="hdfc_upi",
        source_class="bank",
    )
    await books.post_event(ev, "tool-t/b", cfg)
    await ji.upsert(db_pool, "tool-t/b", "tool-t", ev, journal_file="personal/2026.journal")
    out = await _exec_ledger_add_rule(
        db_pool, {"match": "jai shree", "account": "expenses:groceries", "apply": False}, ctx
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
    ev = MoneyEvent(
        kind="transaction",
        direction="out",
        amount=Decimal("10"),
        currency="INR",
        payee="Jai shree nakoda",
        payee_key="jai shree nakoda",
        channel="upi",
        instrument="hdfc-1225",
        occurred_on=date(2026, 9, 2),
        entity="personal",
        account="expenses:unknown",
        parser="hdfc_upi",
        source_class="bank",
    )
    await books.post_event(ev, "tool-t/c", cfg)
    await ji.upsert(db_pool, "tool-t/c", "tool-t", ev, journal_file="personal/2026.journal")
    out = await _exec_ledger_add_rule(
        db_pool,
        {"match": "jai shree", "account": "expenses:groceries", "entity": "hikmah"},
        ctx,
    )
    assert out == "rule added; reclassified 0 postings"
    assert (await ji.get(db_pool, "tool-t/c"))["account"] == "expenses:unknown"


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
