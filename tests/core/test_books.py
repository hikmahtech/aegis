"""The journal writer (spec §5). Pure block tests always run; the round-trip
tests need hledger + git and skip without them."""

from __future__ import annotations

import base64
import shutil
import subprocess
import time
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from aegis.api.models.money import MoneyEvent
from aegis.services import books
from structlog.testing import capture_logs

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
HOSTILE_INSTRUMENT = (
    "x\n\n2026-09-01 * ATTACKER PAYOUT\n"
    "    ; msgid: personal/HIJACKED\n"
    "    expenses:unknown                        \u20b999999.00\n"
    "    assets:bank:hdfc:1225\n"
    "\n2026-09-02 * decoy"
)
HOSTILE_CURRENCY = "₹\n    ; hijacked: true"
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


def test_sanitize_tag_strips_journal_syntax():
    """A tag value must stay one tag value on one line. Measured against
    hledger 1.52.3: a comma inside a value starts a NEW tag, a `;` starts a new
    comment and a newline can start a whole new transaction. A colon does not —
    the value runs to the next comma or end of line — so refs like `UTR:1234`
    are left intact."""
    assert books.sanitize_tag("upi") == "upi"
    assert books.sanitize_tag("UTR:1234") == "UTR:1234"
    assert books.sanitize_tag("a\nb") == "a b"
    assert books.sanitize_tag("a; b, c\r\nd") == "a b c d"
    assert books.sanitize_tag("  a   b  ") == "a b"
    assert books.sanitize_tag("") == "" and books.sanitize_tag(None) == ""
    assert len(books.sanitize_tag("x" * 200)) == 80
    hostile = books.sanitize_tag(HOSTILE_INSTRUMENT)
    assert not set(hostile) & set(";,\r\n")


def test_safe_account_strips_a_lone_tab():
    """Measured against hledger 1.52.3: a lone tab does NOT separate an account
    from its amount — hledger folds the amount into the account name and the
    transaction comes out unbalanced ("There can't be more than one real
    posting with no amount"). So a tab anywhere in an account breaks the write
    rather than injecting one; it is stripped with the rest of the controls."""
    assert books._safe_account("expenses:a\tb") == "expenses:a b"
    assert books._safe_account("expenses:a\x00b") == "expenses:a b"
    line = books._posting("expenses:a\tb", "₹10.00")
    assert "\t" not in line and line.startswith("    expenses:a b") and line.endswith("₹10.00")


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
    assert books.apply_rules(
        rules, "invoicing@aws.com", "AMAZON WEB SERVICES", direction="out"
    )["payee"] == "Amazon Web Services"
    assert books.apply_rules(
        rules, "x@amazon.in", "Amazon", direction="out"
    )["account"] == "expenses:shopping"
    assert books.apply_rules(
        rules, "data@stockopedia.com", "LSEG Billing", direction="in"
    )["ignore"] is True
    assert books.apply_rules(rules, "a@b.com", "Nobody", direction="out") is None
    assert books.load_rules(tmp_path / "missing.yaml") == []


def test_load_rules_skips_a_pattern_that_is_unsafe_to_run(tmp_path):
    """The guarantee has to be about what RUNS, not about what was written
    (issue #390).

    `ledger_add_rule` checks these bounds at write time, but `apply_rules`
    runs every rule in the file with `re.search` against every incoming money
    event, synchronously, in the worker. A pattern hand-written or written
    before the bounds existed is not covered by a write-time check, and `re`
    has no timeout: a catastrophic one hangs the ingest lane permanently,
    across processes.

    The surviving rules matter as much as the dropped one — a `load_rules`
    that returned `[]` on any bad file would "pass" this by breaking the
    books entirely.
    """
    p = tmp_path / "accounts.yaml"
    p.write_text(
        "- match: '(a+)+$'\n  account: expenses:groceries\n"          # repeats a group
        "- match: 'a*a*a*a*a*a*a*$'\n  account: expenses:groceries\n"  # stacks quantifiers
        "- match: '" + "x" * 201 + "'\n  account: expenses:groceries\n"  # over the length cap
        "- match: '('\n  account: expenses:groceries\n"                # not a regex at all
        "- match: 'mahavitaran.*suncity 501'\n  account: expenses:utilities:electricity\n"
        "- match: 'amazon web services|invoicing@aws\\.com'\n  account: expenses:hikmah:infra\n"
    )

    with capture_logs() as logs:
        rules = books.load_rules(p)

    assert [r["match"] for r in rules] == [
        "mahavitaran.*suncity 501",
        "amazon web services|invoicing@aws\\.com",
    ]
    # Named, not merely dropped: a rule that silently stops applying is a
    # miscategorisation with nothing pointing at its cause.
    skipped = [ln for ln in logs if ln["event"] == "books_rule_skipped"]
    assert [ln["match"][:12] for ln in skipped] == ["(a+)+$", "a*a*a*a*a*a*", "x" * 12, "("]
    assert "repeats a group" in skipped[0]["reason"]
    assert "stacks more than 6 quantifiers" in skipped[1]["reason"]
    assert skipped[2]["reason"] == "is longer than 200 characters"
    assert skipped[3]["reason"].startswith("is not a valid regex:")


def test_load_rules_accepts_every_shape_a_real_rule_uses(tmp_path):
    """The live `rules/accounts.yaml` of 2026-09-05, in shape: alternation, an
    escaped dot, a wildcard, and the anchored pattern `rule_match_for`
    generates. A validator that refused any of these would silently
    de-categorise the whole ledger on the next email."""
    p = tmp_path / "accounts.yaml"
    p.write_text(
        "- match: 'lseg billing|lseg helpdesk|@stockopedia\\.com'\n  ignore: true\n"
        "- match: 'apple.*icloud|icloud'\n  account: expenses:saas\n"
        "- match: 'amazonpay.*toll|toll'\n  account: expenses:transport\n"
        "- match: 'mahavitaran.*suncity 501|msedcl.*suncity 501'\n"
        "  account: expenses:utilities:electricity\n"
        "- match: '\\|[^|]*mahavitaran[^a-z0-9]+msedcl'\n"
        "  account: expenses:utilities:electricity\n"
    )

    assert len(books.load_rules(p)) == 5


def test_a_rule_with_no_direction_matches_money_moving_either_way(tmp_path):
    """The backwards-compatibility guarantee for issue #396.

    Every rule in the live `rules/accounts.yaml` — all 28 of them on
    2026-09-06 — is written without a `direction`, so an absent field has to go
    on meaning "either way" exactly as it did before the field existed.
    """
    p = tmp_path / "accounts.yaml"
    p.write_text("- match: 'amazon'\n  account: expenses:shopping\n")
    rules = books.load_rules(p)

    for direction in ("out", "in", None):
        assert books.apply_rules(rules, "", "Amazon", direction=direction) == rules[0], direction


def test_a_directional_rule_only_fires_on_its_own_direction(tmp_path):
    """`apply_rules` had no notion of direction, and `parse_money_email` sets
    `ev.account` from whatever it returns — so a rule written from an inbound
    curiosity answer (`income:people`) filed the next payment TO that name into
    `income:people`. Balanced, `check --strict` clean, not `%:unknown`, so it
    never reached the Unexplained queue or the detector again.
    """
    p = tmp_path / "accounts.yaml"
    p.write_text(
        "- match: 'muhammed amin'\n  account: income:people\n  direction: in\n"
        "- match: 'jai shree'\n  account: expenses:groceries\n  direction: out\n"
    )
    rules = books.load_rules(p)

    assert books.apply_rules(rules, "", "Muhammed Amin", direction="in") == rules[0]
    assert books.apply_rules(rules, "", "Muhammed Amin", direction="out") is None
    assert books.apply_rules(rules, "", "Jai Shree Nakoda", direction="out") == rules[1]
    assert books.apply_rules(rules, "", "Jai Shree Nakoda", direction="in") is None


def test_an_event_of_unknown_direction_never_matches_a_directional_rule(tmp_path):
    """`MoneyEvent.direction` is `Literal["in","out"] | None`, so "we do not
    know" is a real input.

    It must not satisfy a rule that names a direction: a rule that misses falls
    back to `account_for`, which files the row in `:unknown` where the brief
    and the detector both find it. A rule that fires on a guess files it
    somewhere plausible and silently wrong, which nothing ever surfaces.
    """
    p = tmp_path / "accounts.yaml"
    p.write_text(
        "- match: 'muhammed amin'\n  account: income:people\n  direction: in\n"
        "- match: 'amazon'\n  account: expenses:shopping\n"
    )
    rules = books.load_rules(p)

    assert books.apply_rules(rules, "", "Muhammed Amin", direction=None) is None
    # ...while the agnostic rule in the same file still matches.
    assert books.apply_rules(rules, "", "Amazon", direction=None) == rules[1]


def test_load_rules_skips_a_rule_whose_direction_is_not_a_direction(tmp_path):
    """Same reasoning as the regex bounds (issue #390): the guarantee is about
    what RUNS. `direction: sideways` cannot be honoured, and reading it as
    "either way" would silently widen a rule its author meant to narrow.
    """
    p = tmp_path / "accounts.yaml"
    p.write_text(
        "- match: 'sideways shop'\n  account: expenses:shopping\n  direction: sideways\n"
        "- match: 'listy shop'\n  account: expenses:shopping\n  direction: [in, out]\n"
        "- match: 'good shop'\n  account: expenses:shopping\n  direction: out\n"
    )

    with capture_logs() as logs:
        rules = books.load_rules(p)

    assert [r["match"] for r in rules] == ["good shop"]
    skipped = [ln for ln in logs if ln["event"] == "books_rule_skipped"]
    assert [ln["match"] for ln in skipped] == ["sideways shop", "listy shop"]
    assert all("direction" in ln["reason"] for ln in skipped), skipped


def test_latest_prices_reads_the_newest_rate_per_commodity(tmp_path):
    """`prices.journal` is append-only and seeded with config defaults, so the
    same symbol appears many times and only the newest line is the rate. Used
    to RANK index rows across currencies (issue #387) — hledger stays the
    authority on every figure that is reported."""
    (tmp_path / "prices.journal").write_text(
        "; FX prices, appended weekly.\n"
        "P 2026-09-01 $ ₹84.50\n"
        "P 2026-09-01 £ ₹108.00\n"
        "P 2026-09-05 $ ₹94.49\n"
        "P 2026-09-05 € ₹109.73\n"
        "P 2026-09-05 £ 127.76\n"          # no symbol on the rate
        "P not-a-date $ ₹1.00\n"           # not a price line
        "P 2026-09-06 ¥ ₹0\n"              # a zero rate divides nothing
        "not a price line at all\n"
    )

    rates = books.latest_prices(books.BooksConfig(path=tmp_path))

    assert rates == {
        "$": Decimal("94.49"), "£": Decimal("127.76"), "€": Decimal("109.73")
    }
    # No checkout at all is `{}`, not a crash: that is a fresh deployment.
    assert books.latest_prices(books.BooksConfig(path=tmp_path / "gone")) == {}


def test_rule_haystack_keeps_exactly_one_pipe():
    """`|` separates the two halves, so it cannot also appear inside one.

    The generated payee rules anchor on the payee half with `\\|[^|]*`, which
    only pins the match to the payee if the haystack holds exactly one pipe. A
    From display name may carry a literal one — `"Google Pay | Bills"
    <noreply@…>` is the live shape — and that would put the anchor's `[^|]*`
    inside the SENDER, re-opening the sender-matching hole the anchor exists to
    close. `sanitize_payee` already strips `|`, but the sender is never
    sanitized and `apply_rules` is handed the RAW extracted payee, so the fix
    belongs here at the join.
    """
    anchored = [{"match": r"\|[^|]*mahavitaran", "account": "expenses:utilities:electricity"}]
    # A pipe in the sender must not let the anchored pattern match on the
    # sender's own text.
    assert books.apply_rules(
        anchored, '"Google Pay | mahavitaran" <no@reply>', "Zomato", direction="out"
    ) is None
    # ...while the payee it was written for still matches.
    assert books.apply_rules(
        anchored, '"Google Pay | Bills" <no@reply>', "Mahavitaran", direction="out"
    ) is not None
    # And a pipe inside the payee neither splits the haystack nor swallows the
    # words around it.
    assert books.apply_rules(
        [{"match": r"\|[^|]*acme[^a-z0-9]+corp", "account": "expenses:saas"}],
        "billing@acme.example",
        "Acme | Corp",
        direction="out",
    ) is not None


def test_rule_haystack_is_bounded():
    """The rule guards bound the PATTERN; this bounds what it is run against.

    `ledger_add_rule` refuses a slow pattern by timing it against probes at
    "a length a real payee can reach (80 chars)", and `rule_match_problem`
    caps the pattern's length and quantifiers. None of that bounds the
    HAYSTACK, and `MoneyEvent.payee` has no cap at all — it comes back from an
    extractor allowed 4,000 tokens. Measured on `a.*b.*c$`, which passes every
    one of those guards: 1.1s on a 2,000-character payee, 9s on 4,000 and
    ~71s on 8,000, synchronously on the worker's event loop, for every
    incoming money event.

    80 is not an arbitrary number: `sanitize_payee` clips the payee to it, so
    text past the cap is text the journal will never carry. The sender half
    gets its own, looser bound — it is a From header, whose discriminating
    part is the address at the END (`invoicing@aws\\.com`, `ebill@airtel\\.com`
    and four more live rules key on one), so clipping it as tightly would
    silently stop those rules matching behind a long display name. Measured
    over 400 live receipts, the longest sender is 77 characters.
    """
    rules = [{"match": "needle", "account": "expenses:groceries"}]
    assert books.apply_rules(rules, "", "x" * 60 + " needle", direction="out") is not None
    assert books.apply_rules(rules, "", "x" * 200 + " needle", direction="out") is None
    assert books.apply_rules(rules, "x" * 400 + " needle", "Zomato", direction="out") is None
    # A real From header is nowhere near either bound, address included.
    assert books.apply_rules(
        rules, '"Needle Bank Alerts" <alerts@needle.example>', "Zomato", direction="out"
    ) is not None

    # And the bound is what makes the slow pattern above harmless. Without it
    # this call takes about nine seconds.
    slow = [{"match": "a.*b.*c$", "account": "expenses:groceries"}]
    started = time.perf_counter()
    assert books.apply_rules(slow, "", "ab" * 2000, direction="out") is None
    assert time.perf_counter() - started < 1.0, "the haystack reached the rule uncapped"


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
        # Bundled short flags and long-flag abbreviations, which are why the
        # filter is an exact-match allowlist and not a deny-prefix list.
        ["bal", "-Ef/etc/passwd"], ["bal", "-No/tmp/pwned.txt"],
        ["bal", "--fil=/etc/passwd"], ["bal", "--outp=/tmp/pwned.txt"],
    ]
    for args in refused:
        # `match` matters: cfg.path holds no main.journal, so an argument that
        # slipped through would ALSO raise BooksError — carrying hledger's own
        # error. Only the sandbox's own message proves it never ran.
        with pytest.raises(books.BooksError, match="not allowed"):
            await books.run_hledger(args, cfg)

    # ...and the options this codebase actually uses must still get through.
    # cfg.path has no journal, so hledger itself fails; the point is that the
    # failure is hledger's, not the sandbox's.
    for args in (["bal", "--depth"], ["bal", "-X", "₹", "expenses"],
                 ["bal", "--depth=2", "-b", "2026-01-01"], ["print", "tag:msgid=a/b"]):
        with pytest.raises(books.BooksError) as excinfo:
            await books.run_hledger(args, cfg)
        assert "not allowed" not in str(excinfo.value)


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
async def test_run_hledger_refuses_bundled_and_abbreviated_file_flags(tmp_path):
    """Against the real binary. `-Ef<path>` is `-E -f <path>` and leaks the file
    through the error; `-No<path>` is `-N -o <path>` and WRITES one; `--fil=` is
    an accepted abbreviation of `--file=`. All three sail past a deny-prefix
    filter, so this is the test that pins the allowlist."""
    cfg = _repo(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET-LINE-ONE\nsecond line\n")
    target = tmp_path / "pwned.txt"
    for args in (["bal", f"-Ef{secret}"], ["bal", f"-No{target}"], ["bal", f"--fil={secret}"]):
        with pytest.raises(books.BooksError, match="not allowed") as excinfo:
            await books.run_hledger(args, cfg)
        assert "TOPSECRET" not in str(excinfo.value)
    assert not target.exists()


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
async def test_hostile_instrument_cannot_forge_a_transaction(tmp_path):
    """`instrument` is model-supplied (`_LLM_EVENT_FIELDS`), so a steered email
    can put newlines in it. Unsanitized it forged a whole ₹99,999 block, gave it
    a msgid of its own and PASSED `check --strict`, so nothing reverted it."""
    cfg = _repo(tmp_path)
    ev = EV_OUT.model_copy(update={"instrument": HOSTILE_INSTRUMENT})
    rel = await books.post_event(ev, "arshad-personal/hostile", cfg)
    text = (cfg.path / rel).read_text()

    # One transaction, one msgid, and the forged one resolves nowhere.
    assert sum(1 for ln in text.splitlines() if ln[:1].isdigit()) == 1
    assert text.count("; msgid:") == 1
    assert books.find_block(text, "personal/HIJACKED") is None
    assert books.find_block(text, "arshad-personal/hostile") is not None
    # The real postings are still the real postings.
    assert "    expenses:unknown                        ₹10.00\n    assets:unknown\n" in text

    # ...and hledger agrees: one payee, no ₹99,999, no invented tag.
    assert (await books.run_hledger(["payees"], cfg)).strip() == "Jai shree nakoda"
    assert set((await books.run_hledger(["tags"], cfg)).split()) == {
        "channel", "instrument", "msgid", "ref"
    }
    bal = await books.run_hledger(["bal", "expenses"], cfg)
    assert "99999" not in bal and "10.00" in bal
    await books.run_hledger(["check", "--strict"], cfg)  # raises if the file is broken


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


@pytest.mark.skipif(not HAS_HLEDGER, reason="hledger/git not installed")
@pytest.mark.asyncio
async def test_hostile_currency_cannot_forge_a_tag(tmp_path):
    """`currency` is the same hole as `instrument`, one field over: it is in
    `_LLM_EVENT_FIELDS` and `render_amount` used to interpolate it raw into the
    posting line, so a newline landed an attacker-chosen `; hijacked: true`
    inside a committed block with `check --strict` clean.

    `model_copy` skips validators on purpose here — this test is about the
    WRITER, which has to hold for a caller that never passed through
    `MoneyEvent` at all. The model gate is tested separately.
    """
    cfg = _repo(tmp_path)
    await books.post_event(EV_OUT, "arshad-personal/clean", cfg)
    hostile = EV_OUT.model_copy(update={"currency": HOSTILE_CURRENCY})

    raised = False
    try:
        await books.post_event(hostile, "arshad-personal/hostilecur", cfg)
    except books.BooksCheckError:
        # Sanitized, the code is three inert letters — an UNDECLARED commodity,
        # so the strict check rejects the write and it reverts. The poisoned
        # event never reaches the journal at all.
        raised = True

    text = (cfg.path / "personal" / "2026.journal").read_text()
    assert "hijacked" not in text.lower(), text
    assert sum(1 for ln in text.splitlines() if ln[:1].isdigit()) == 1
    assert text.count("; msgid:") == 1
    # hledger's own view, not just the file text: no invented tag.
    assert set((await books.run_hledger(["tags"], cfg)).split()) == {
        "channel", "instrument", "msgid", "ref"
    }
    await books.run_hledger(["check", "--strict"], cfg)  # raises if the file is broken
    assert raised and _commits(cfg) == 2


@pytest.mark.skipif(not HAS_HLEDGER, reason="hledger/git not installed")
@pytest.mark.asyncio
async def test_a_null_byte_reverts_and_raises_a_books_error(tmp_path):
    """A NUL reaches subprocess as `ValueError: embedded null byte`, not
    `OSError`, so it slips past `_spawn`'s guard and escapes raw — and it
    escapes from the COMMIT, after `git add`, leaving the checkout
    staged-dirty. Every books failure must leave the working copy clean.

    The rules file is the honest probe: it is the one write hledger never
    reads, so `check --strict` cannot reject the file first and mask the
    commit. A NUL in a journal-bound field is caught earlier by the strict
    account check, which is why that variant proves nothing here.
    """
    cfg = _repo(tmp_path)
    await books.post_event(EV_OUT, "m/nul", cfg)
    rules = cfg.path / "rules" / "accounts.yaml"

    with pytest.raises(books.BooksError, match="null"):
        await books.append_rule({"match": "corner\x00store", "account": "expenses:groceries"}, cfg)

    assert not rules.exists(), "the half-written rules file survived the failure"
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"], cwd=cfg.path, capture_output=True, text=True
    ).stdout.strip()
    assert staged == "", f"left staged: {staged}"
    assert _commits(cfg) == 2


@pytest.mark.skipif(not HAS_HLEDGER, reason="hledger/git not installed")
@pytest.mark.asyncio
async def test_the_first_write_clones_with_the_lock_already_held(tmp_path):
    """The clone now runs INSIDE the flock, so a simultaneous first write from
    core and worker cannot both clone. The lock file lives in the checkout and
    a clone refuses a non-empty destination (measured: exit 128, "already
    exists and is not an empty directory"), so it stages in a sibling
    directory and moves the result in."""
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(_repo(tmp_path).path), str(origin)], check=True
    )
    dest = tmp_path / "checkout"
    cfg = books.BooksConfig(path=dest, repo_url=str(origin))

    rel = await books.post_event(EV_OUT, "m/first", cfg)

    assert (dest / ".git").exists(), "the clone never landed"
    assert (dest / ".aegis.lock").exists(), "the lock the write held is gone"
    posted = (dest / rel).read_text()
    assert "; msgid: m/first" in posted and posted.endswith("    assets:bank:hdfc:1225\n")
    assert not (dest.parent / f".{dest.name}.cloning").exists()  # staging cleaned up
    assert await books.unpushed_commits(cfg) == 0  # it really pushed
