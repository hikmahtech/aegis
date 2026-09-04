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
# Exactly 39 chars: one short of the 40-column pad, so `:<40` leaves a SINGLE
# space before the amount and hledger reads the whole thing as an account name.
ACCT39 = "expenses:hikmah:professional:thirtynine"


def test_render_transaction_exact_text():
    assert books.render_transaction(EV_OUT, "expenses:unknown", "assets:bank:hdfc:1225",
                                    "arshad-personal/1a06cf5a") == BLOCK_OUT
    assert books.render_transaction(EV_IN, "income:hikmah:stockopedia", "assets:bank:axis:9640",
                                    "arshad-personal/1a0659e3") == BLOCK_IN


def test_render_transaction_pads_long_accounts_with_two_spaces():
    ev = EV_OUT.model_copy(update={"payee": "x"})
    block = books.render_transaction(ev, "expenses:hikmah:professional:something:long", "assets:unknown", "m/1")
    assert "    expenses:hikmah:professional:something:long  ₹10.00\n" in block


def test_render_transaction_pads_39_char_account_with_two_spaces():
    """hledger needs TWO spaces between account and amount; a 39-char account
    padded to column 40 leaves only one, so it must take the explicit branch."""
    assert len(ACCT39) == 39
    ev = EV_OUT.model_copy(update={"payee": "x"})
    block = books.render_transaction(ev, ACCT39, "assets:unknown", "m/39")
    assert f"    {ACCT39}  ₹10.00\n" in block


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


def test_config_from_settings(tmp_path):
    creds = tmp_path / "creds"
    creds.mkdir()
    s = SimpleNamespace(
        gmail_token_dir=str(creds),
        books_path=str(tmp_path / "books"),
        books_repo_url="git@example.com:me/books.git",
    )
    cfg = books.config_from_settings(s)
    assert cfg.path == tmp_path / "books"
    assert cfg.repo_url == "git@example.com:me/books.git"
    assert cfg.main == "main.journal"
    assert cfg.deploy_key is None  # not installed yet
    (creds / "books_deploy_key").write_text("x")
    assert books.config_from_settings(s).deploy_key == creds / "books_deploy_key"
    # Settings that carry none of the books fields must fall back, not raise.
    bare = books.config_from_settings(SimpleNamespace(gmail_token_dir=str(tmp_path / "none")))
    assert bare.path == Path("/app/config/books") and bare.repo_url == ""


@pytest.mark.asyncio
async def test_run_hledger_wraps_a_missing_working_copy(tmp_path):
    """A degraded host (no hledger, no checkout) must surface as BooksError,
    not as a bare FileNotFoundError out of an async activity."""
    cfg = books.BooksConfig(path=tmp_path / "gone")
    with pytest.raises(books.BooksError, match="could not run"):
        await books.run_hledger(["bal"], cfg)


@pytest.mark.asyncio
async def test_run_hledger_refuses_writes_and_file_overrides(tmp_path):
    cfg = books.BooksConfig(path=tmp_path)
    refused = [
        # not a read command
        ["import", "x.csv"], ["add"],
        # -f / --file, as a separate arg, joined, and with `=`
        ["bal", "-f", "/etc/passwd"], ["reg", "-f/etc/passwd"], ["reg", "--file=/etc/passwd"],
        ["print", "--file", "/etc/passwd"],
        # output redirection, separate and joined
        ["reg", "--output-file=x"], ["bal", "-o", "x"], ["bal", "-ox"],
        ["print", "--output-file", "x"],
        # rules and config files
        ["print", "--rules", "r"], ["bal", "--config", "c"],
        # @ARGSFILE: hledger opens the file and splices its lines in as
        # arguments, which re-admits -f/-o and leaks the file through stderr.
        ["bal", "@/etc/passwd"], ["bal", "@args.txt"], ["print", "@/etc/shadow"],
    ]
    for args in refused:
        # `match` matters: cfg.path holds no main.journal, so an argument that
        # slipped through would ALSO raise BooksError — carrying hledger's own
        # error. Only the sandbox's own message proves it never ran.
        with pytest.raises(books.BooksError, match="not allowed"):
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
""" + f"account {ACCT39}\n"


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
async def test_39_char_account_survives_hledger_check(tmp_path):
    """With a single separator space hledger folds the amount into the account
    name, so `check --strict` rejects the write and post_event never commits."""
    cfg = _repo(tmp_path)
    ev = EV_OUT.model_copy(update={"account": ACCT39})
    await books.post_event(ev, "m/pad39", cfg)
    assert f"    {ACCT39}  ₹10.00\n" in (cfg.path / "personal" / "2026.journal").read_text()
    # An exact-line match: a misparse would name the account "<ACCT39> ₹10.00".
    assert ACCT39 in (await books.run_hledger(["accounts", "--used"], cfg)).splitlines()
    assert _commits(cfg) == 2


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


def _committed_files(cfg) -> list[str]:
    out = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"],
                         cwd=cfg.path, capture_output=True, text=True).stdout
    return sorted(out.split())


@pytest.mark.skipif(not HAS_HLEDGER, reason="hledger/git not installed")
@pytest.mark.asyncio
async def test_writes_are_scoped_to_their_own_paths(tmp_path):
    """A human's unrelated uncommitted work must neither be swept into a Maou
    commit nor destroyed by a failed write's revert."""
    cfg = _repo(tmp_path)
    notes = cfg.path / "personal" / "notes.txt"          # untracked
    notes.write_text("human notes\n")
    chart = cfg.path / "accounts.journal"                # tracked, modified
    chart.write_text(ACCOUNTS + "account expenses:handedit\n")

    await books.post_event(EV_OUT, "arshad-personal/1a06cf5a", cfg)
    assert _committed_files(cfg) == ["personal/2026.journal"]
    assert notes.read_text() == "human notes\n"
    assert "expenses:handedit" in chart.read_text()

    with pytest.raises(books.BooksCheckError):
        await books.rewrite_event("arshad-personal/1a06cf5a", cfg, account="expenses:nope")
    assert notes.read_text() == "human notes\n"
    assert "expenses:handedit" in chart.read_text()
    assert _commits(cfg) == 2


@pytest.mark.skipif(not HAS_HLEDGER, reason="hledger/git not installed")
@pytest.mark.asyncio
async def test_post_event_is_idempotent_across_year_files(tmp_path):
    """A corrected date re-posts the same msgid into a different journal;
    per-file idempotency would leave the id in the books twice."""
    cfg = _repo(tmp_path)
    rel = await books.post_event(EV_OUT, "arshad-personal/dup", cfg)
    assert rel == "personal/2026.journal"
    moved = EV_OUT.model_copy(update={"occurred_on": date(2027, 3, 4)})
    assert await books.post_event(moved, "arshad-personal/dup", cfg) == rel
    assert not (cfg.path / "personal" / "2027.journal").exists()
    assert _commits(cfg) == 2
    out = await books.run_hledger(["print", "tag:msgid=arshad-personal/dup"], cfg)
    assert out.count("Jai shree nakoda") == 1


@pytest.mark.skipif(not HAS_HLEDGER, reason="hledger/git not installed")
@pytest.mark.asyncio
async def test_remove_event_leaves_its_neighbours_byte_intact(tmp_path):
    cfg = _repo(tmp_path)
    for n in (1, 2, 3):
        await books.post_event(
            EV_OUT.model_copy(update={"payee": f"Payee {n}", "ref": f"ref{n}"}), f"m/{n}", cfg
        )
    path = cfg.path / "personal" / "2026.journal"
    before = path.read_text()
    first = before[slice(*books.find_block(before, "m/1"))]
    third = before[slice(*books.find_block(before, "m/3"))]

    await books.remove_event("m/2", cfg)

    # Exactly one blank line between the survivors, both unchanged byte for byte.
    assert path.read_text() == "; Personal 2026\n\n" + first + "\n" + third
    assert books.find_block(path.read_text(), "m/2") is None
    assert _commits(cfg) == 5
    out = await books.run_hledger(["print"], cfg)
    assert "Payee 1" in out and "Payee 3" in out and "Payee 2" not in out


@pytest.mark.skipif(not HAS_HLEDGER, reason="hledger/git not installed")
@pytest.mark.asyncio
async def test_append_prices_and_rule_are_idempotent(tmp_path):
    """The push happens after the commit, so a timeout makes the caller retry a
    write that already landed."""
    cfg = _repo(tmp_path)
    line = "P 2026-09-05 £ ₹108.00"
    rule = {"match": "corner store", "account": "expenses:groceries"}
    await books.append_prices([line], cfg)
    await books.append_rule(rule, cfg)
    assert _commits(cfg) == 3

    await books.append_prices([line], cfg)
    await books.append_rule(dict(rule), cfg)
    assert _commits(cfg) == 3
    assert (cfg.path / "prices.journal").read_text().count(line) == 1
    assert len(books.load_rules(cfg.path / "rules" / "accounts.yaml")) == 1


def test_missing_checkout_without_repo_url_is_disabled(tmp_path):
    cfg = books.BooksConfig(path=tmp_path / "nowhere")
    with pytest.raises(books.BooksDisabled):
        books.ensure_checkout_sync(cfg)
