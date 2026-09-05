"""Rendering of the weekly money brief and the monthly close (spec §7.2, §7.3).

These are pure-function tests: no DB, no hledger, no Temporal. They assert the
rendered TEXT, numbers included, because the whole point of this layer is that
a person reads it — a test that only checked "non-empty" would pass while the
brief printed `amount_cents` or the word "None" at the user.
"""

from __future__ import annotations

from aegis_worker.activities.money import HOME_SYMBOL
from aegis_worker.activities.money_render import render_money_brief, render_month_close

BRIEF = {
    "as_of": "2026-09-06",
    "since": "2026-08-30",
    "books_ok": True,
    "entities": {
        "personal": {"income": "-1500.00", "expenses": "6250.00"},
        "hikmah": {"income": "-100000.00", "expenses": "262.30"},
    },
    "by_account": [],
    "top_payees": [{"payee": "Unknown Big", "amount": "₹ 6,000.00"}],
    "unknowns": [
        {
            "msgid": "m/a",
            "payee": "Unknown Big",
            "amount": "6000.00",
            "currency": "INR",
            "occurred_on": "2026-09-02",
            "channel": "upi",
        }
    ],
    "large_unexplained": [
        {
            "msgid": "m/a",
            "payee": "Unknown Big",
            "amount": "6000.00",
            "currency": "INR",
            "occurred_on": "2026-09-02",
            "channel": "upi",
        }
    ],
    "dues": [
        {
            "msgid": "m/d",
            "payee": "Axis credit card XX13",
            "amount": "100308.53",
            "currency": "INR",
            "due_on": "2026-09-07",
            "kind": "due",
            "todoist_ref": "t1",
        },
        {
            "msgid": "m/f",
            "payee": "Medium",
            "amount": "199.00",
            "currency": "INR",
            "due_on": "2026-09-15",
            "kind": "failed",
            "todoist_ref": None,
        },
    ],
    "forecast": [{"date": "2026-09-15", "description": "MSEDCL Suncity 501", "amount": "₹ 7,170.00"}],
    "closed_dues": [
        {
            "msgid": "m/c",
            "payee": "Airtel",
            "amount": "5306.46",
            "currency": "INR",
            "due_on": "2026-09-06",
        }
    ],
    "unpushed": 2,
    "low_confidence": 1,
    "bal_text": "  ₹ 6,250.00  expenses\n",
    "fx_unconverted": [],
    "fx_stale": False,
}

CLOSE = {
    "month": "2026-08",
    "books_ok": True,
    "is_text": "IS",
    "bs_text": "BS",
    "is_rows": [],
    "recurring_total": "15625.76",
    "unknown_count": 3,
    "dues_paid": 2,
    "dues_open": 1,
    "fx_unconverted": [],
    "fx_stale": False,
}


# --------------------------------------------------------------------------
# the brief
# --------------------------------------------------------------------------


def test_brief_html_has_every_section_and_major_units():
    out = render_money_brief(BRIEF)
    html = out["html"]
    assert html.startswith("<b>Money brief · 2026-08-30 → 2026-09-06</b>")
    assert "Personal: in ₹1,500.00 · out ₹6,250.00" in html
    assert "Hikmah: in ₹1,00,000.00 · out ₹262.30" in html
    assert "<pre>  ₹ 6,250.00  expenses\n</pre>" in html
    assert "Unknown Big — ₹ 6,000.00" in html
    assert "2026-09-07 · Axis credit card XX13 · ₹1,00,308.53 · (task)" in html
    assert "2026-09-15 · Medium · ₹199.00 · fix payment · (no task)" in html
    assert "2026-09-15 · MSEDCL Suncity 501 · ₹ 7,170.00" in html
    assert "2026-09-02 · Unknown Big · ₹6,000.00 · upi" in html
    assert "ledger_add_rule" in html
    assert "Airtel · ₹5,306.46" in html
    assert "2 unpushed commits" in html and "1 low-confidence" in html
    assert "amount_cents" not in html


def test_brief_markdown_mirrors_html():
    md = render_money_brief(BRIEF)["markdown"]
    assert md.startswith("# Money brief · 2026-08-30 → 2026-09-06")
    assert "```\n  ₹ 6,250.00  expenses\n```" in md and "## Due in the next 14 days" in md
    assert "- 2026-09-07 · Axis credit card XX13 · ₹1,00,308.53 · (task)" in md
    assert "- 2026-09-02 · Unknown Big · ₹6,000.00 · upi" in md
    assert "- Airtel · ₹5,306.46" in md


def test_brief_without_books():
    out = render_money_brief(
        {**BRIEF, "books_ok": False, "bal_text": "", "top_payees": [], "forecast": []}
    )
    assert "Books unavailable — index only." in out["html"] and "<pre>" not in out["html"]
    assert "_Books unavailable — index only._" in out["markdown"]


def test_brief_escapes_payee_markup():
    """Slack renders the message as HTML, so a payee is never raw markup."""
    hostile = {
        **BRIEF,
        "top_payees": [{"payee": "<b>Ac & Me</b>", "amount": "₹ 1.00"}],
        "unknowns": [
            {
                "payee": "<i>x</i>",
                "amount": "1.00",
                "currency": "INR",
                "occurred_on": "2026-09-02",
                "channel": "upi",
            }
        ],
    }
    html = render_money_brief(hostile)["html"]
    assert "&lt;b&gt;Ac &amp; Me&lt;/b&gt; — ₹ 1.00" in html
    assert "<b>Ac & Me</b>" not in html
    assert "&lt;i&gt;x&lt;/i&gt;" in html


def test_brief_nothing_due():
    out = render_money_brief({**BRIEF, "dues": [], "forecast": []})
    assert "Nothing due." in out["html"] and "Nothing due." in out["markdown"]


def test_brief_housekeeping_omitted_when_clean():
    out = render_money_brief({**BRIEF, "unpushed": 0, "low_confidence": 0})
    assert "Housekeeping" not in out["html"] and "Housekeeping" not in out["markdown"]


# --------------------------------------------------------------------------
# fx: hledger could not value every commodity in ₹
# --------------------------------------------------------------------------


def test_brief_names_the_unconverted_commodities_near_the_top():
    """`fx_stale` means the totals understate reality. Detecting that and then
    rendering the numbers silently is the bug the detection exists to prevent,
    so the warning must be present, name the commodities, and come before the
    first figure it qualifies."""
    out = render_money_brief({**BRIEF, "fx_unconverted": ["$", "£"], "fx_stale": True})
    for text in (out["html"], out["markdown"]):
        assert "$, £" in text
        assert "exchange rate" in text
        assert "⚠" in text
        assert text.index("⚠") < text.index("Personal: in")


def test_brief_fx_warning_survives_an_empty_commodity_list():
    out = render_money_brief({**BRIEF, "fx_unconverted": [], "fx_stale": True})
    assert "⚠" in out["html"] and "could not be converted to ₹" in out["html"]
    assert "⚠" in out["markdown"]


def test_brief_has_no_fx_warning_when_everything_converted():
    out = render_money_brief(BRIEF)
    assert "⚠" not in out["html"] and "⚠" not in out["markdown"]


def test_month_close_names_the_unconverted_commodities_near_the_top():
    out = render_month_close({**CLOSE, "fx_unconverted": ["$"], "fx_stale": True})
    for text in (out["html"], out["markdown"]):
        assert "$" in text and "exchange rate" in text and "⚠" in text
        assert text.index("⚠") < text.index("Recurring commitments")


def test_month_close_has_no_fx_warning_when_everything_converted():
    out = render_month_close(CLOSE)
    assert "⚠" not in out["html"] and "⚠" not in out["markdown"]


def test_renderer_default_home_symbol_matches_the_conversion_target():
    """The default has to be the commodity `build_money_brief` passes to
    `hledger -X`, or the warning would name the wrong currency."""
    out = render_money_brief({**BRIEF, "fx_unconverted": [], "fx_stale": True})
    assert HOME_SYMBOL in out["html"]
    assert render_money_brief.__defaults__[0] == HOME_SYMBOL


# --------------------------------------------------------------------------
# `journal_index.amount` is nullable and the dues queries do not filter on it
# --------------------------------------------------------------------------


def test_dues_and_closed_dues_survive_a_null_amount():
    """`str(None)` is what reaches the renderer, so `Decimal(amount)` would
    raise and naive interpolation would print the word "None" at the user."""
    out = render_money_brief(
        {
            **BRIEF,
            "dues": [
                {
                    "payee": "Axis credit card XX13",
                    "amount": "None",
                    "currency": None,
                    "due_on": "2026-09-07",
                    "kind": "due",
                    "todoist_ref": None,
                }
            ],
            "closed_dues": [
                {"payee": "Airtel", "amount": "None", "currency": "INR", "due_on": "2026-09-06"}
            ],
        }
    )
    for text in (out["html"], out["markdown"]):
        assert "None" not in text
        assert "2026-09-07 · Axis credit card XX13 · amount unknown · (no task)" in text
        assert "Airtel · amount unknown" in text


def test_unexplained_survives_a_null_amount():
    out = render_money_brief(
        {
            **BRIEF,
            "unknowns": [
                {
                    "payee": "Mystery",
                    "amount": "None",
                    "currency": "INR",
                    "occurred_on": "2026-09-02",
                    "channel": None,
                }
            ],
        }
    )
    assert "2026-09-02 · Mystery · amount unknown · -" in out["html"]
    assert "None" not in out["html"]


# --------------------------------------------------------------------------
# the month close
# --------------------------------------------------------------------------


def test_month_close_render():
    out = render_month_close(CLOSE)
    assert out["html"].startswith("<b>Month close · 2026-08</b>")
    assert "<pre>IS</pre>" in out["html"] and "<pre>BS</pre>" in out["html"]
    assert "Recurring commitments: ₹15,625.76" in out["html"]
    assert "Unexplained postings: 3" in out["html"] and "Dues paid: 2 · still open: 1" in out["html"]
    assert out["markdown"].startswith("# Month close · 2026-08")
    assert "- Recurring commitments: ₹15,625.76" in out["markdown"]
    assert "```\nIS\n```" in out["markdown"] and "```\nBS\n```" in out["markdown"]


def test_month_close_without_books():
    out = render_month_close({**CLOSE, "books_ok": False, "is_text": "", "bs_text": ""})
    assert "Books unavailable — index only." in out["html"]
    assert "<pre>" not in out["html"]
    # The index half still renders — that is the point of shipping without books.
    assert "Unexplained postings: 3" in out["html"]


def test_month_close_escapes_statement_text():
    out = render_month_close({**CLOSE, "is_text": "a < b & c", "bs_text": "BS"})
    assert "<pre>a &lt; b &amp; c</pre>" in out["html"]
