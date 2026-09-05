"""Rendering of the weekly money brief and the monthly close (spec §7.2, §7.3).

These are pure-function tests: no DB, no hledger, no Temporal. They assert the
rendered TEXT, numbers included, because the whole point of this layer is that
a person reads it — a test that only checked "non-empty" would pass while the
brief printed `amount_cents` or the word "None" at the user.
"""

from __future__ import annotations

from aegis_worker.activities.money import HOME_SYMBOL
from aegis_worker.activities.money_render import (
    MAX_LEDGER_LINES,
    render_money_brief,
    render_month_close,
)

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
    assert "2026-09-15 · MSEDCL Suncity 501 · ₹ 7,170.00 · forecast" in html
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


def test_brief_markdown_bullets_every_standalone_line():
    """Two adjacent unbulleted lines are ONE paragraph to every Markdown
    renderer, so an unbulleted entity pair renders as `…₹6,250.00 Hikmah: in…`
    on the GitHub view of the filed report."""
    md = render_money_brief(BRIEF)["markdown"]
    assert "- Personal: in ₹1,500.00 · out ₹6,250.00" in md
    assert "- Hikmah: in ₹1,00,000.00 · out ₹262.30" in md
    assert "- 2 unpushed commits · 1 low-confidence LLM postings" in md
    # Nothing in the body is left as a bare line that would glue to its
    # neighbour. What may FOLLOW a line without folding into it is fixed by
    # CommonMark: a heading, a list item and a fence all interrupt a
    # paragraph, and everything else is a lazy continuation. A bullet is NOT
    # exempt as the FIRST line of the pair — a bare line after a bullet is
    # folded into that list item, which is exactly how the "<payee> is
    # <account>" hint came to render inside the last unexplained row. The old
    # `-` exemption here made this sweep blind to the one defect it was
    # written to catch.
    body = md.split("\n")
    glued = [
        (a, b)
        for a, b in zip(body, body[1:], strict=False)
        if a and b and not a.startswith(("#", "`")) and not b.startswith(("#", "-", "`"))
    ]
    assert glued == []


def test_brief_markdown_hint_is_a_bullet_with_backticked_placeholders():
    """The only "how to fix this" sentence in the report, and it is committed
    to the books repo every week. Unbulleted it is a lazy continuation of the
    last unexplained row and renders INSIDE it; unbackticked, CommonMark takes
    `<payee>` and `<account>` for HTML tags and drops them, leaving
    `Reply with " is "`."""
    md = render_money_brief(BRIEF)["markdown"]
    assert "- Reply with `<payee> is <account>`, or use `ledger_add_rule`." in md
    # The raw placeholders never reach the Markdown outside a code span.
    assert '"<payee> is <account>"' not in md


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


def test_markdown_never_carries_html_entities():
    """The Markdown is committed to the user's books repo and kept forever, so
    it must be the PLAIN text — not the escaped HTML reused. `html.escape`
    defaults to quote=True, so an apostrophe becomes `&#x27;` and ordinary
    merchants (Domino's, Levi's, Barnes & Noble) would be corrupted weekly."""
    payee = "Domino's Pizza & Barnes & Noble"
    out = render_money_brief(
        {
            **BRIEF,
            "top_payees": [{"payee": payee, "amount": "₹ 1.00"}],
            "dues": [
                {
                    "payee": payee,
                    "amount": "199.00",
                    "currency": "INR",
                    "due_on": "2026-09-15",
                    "kind": "due",
                    "todoist_ref": None,
                }
            ],
            "forecast": [{"date": "2026-09-15", "description": payee, "amount": "₹ 2.00"}],
            "unknowns": [
                {
                    "payee": payee,
                    "amount": "1.00",
                    "currency": "INR",
                    "occurred_on": "2026-09-02",
                    "channel": "upi",
                }
            ],
            "closed_dues": [{"payee": payee, "amount": "5.00", "currency": "INR"}],
        }
    )
    md = out["markdown"]
    for entity in ("&#x27;", "&amp;", "&quot;", "&lt;", "&gt;"):
        assert entity not in md, f"{entity} leaked into the filed report"
    assert md.count(payee) == 5  # top payee, due, forecast, unexplained, closed due
    # ...and the HTML still escapes all five.
    assert "Domino&#x27;s Pizza &amp; Barnes &amp; Noble" in out["html"]
    assert payee not in out["html"]


def test_forecast_rows_are_marked_as_predictions():
    """A forecast comes from a `~ periodic` rule, a due from a real bill. They
    share one list so the reader sees all money leaving soon — which only works
    if a prediction is distinguishable from a fact."""
    out = render_money_brief(BRIEF)
    for text in (out["html"], out["markdown"]):
        assert "MSEDCL Suncity 501 · ₹ 7,170.00 · forecast" in text
        # The real due is NOT marked.
        assert "Axis credit card XX13 · ₹1,00,308.53 · (task)" in text
        assert "Axis credit card XX13 · ₹1,00,308.53 · (task) · forecast" not in text


def test_long_ledger_table_is_clipped_in_chat_but_whole_in_the_report():
    """The Slack adapter chunks at 2800 chars on line boundaries with no fence
    awareness, and `<pre>` is a code fence — an overlong table would split
    mid-fence and render mangled. The filed report keeps every row."""
    rows = "".join(f"  ₹ {n},000.00  expenses:row{n}\n" for n in range(1, 61))
    out = render_money_brief({**BRIEF, "bal_text": rows})
    html, md = out["html"], out["markdown"]
    assert html.count("expenses:row") == MAX_LEDGER_LINES
    assert f"…{60 - MAX_LEDGER_LINES} more lines" in html
    assert "reports/weekly/2026-09-06.md" in html
    # Nothing is dropped from the record.
    assert md.count("expenses:row") == 60
    assert "more lines" not in md


def test_short_ledger_table_is_not_clipped_or_annotated():
    out = render_money_brief(BRIEF)
    assert "more lines" not in out["html"]
    assert "<pre>  ₹ 6,250.00  expenses\n</pre>" in out["html"]


def test_month_close_clips_long_statements_in_chat_only():
    long_is = "".join(f"  ₹ {n}.00  income:row{n}\n" for n in range(1, 41))
    out = render_month_close({**CLOSE, "is_text": long_is})
    assert out["html"].count("income:row") == MAX_LEDGER_LINES
    assert f"…{40 - MAX_LEDGER_LINES} more lines" in out["html"]
    assert "reports/monthly/2026-08.md" in out["html"]
    assert out["markdown"].count("income:row") == 40


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
    assert "2026-09-02 · Mystery · amount unknown · —" in out["html"]
    assert "None" not in out["html"]


def test_rows_survive_a_null_payee():
    """`journal_index.payee` is nullable too (migration 026), one column over
    from `amount` — `str(None)` would print the word "None" at the reader."""
    out = render_money_brief(
        {
            **BRIEF,
            "top_payees": [{"payee": None, "amount": "₹ 1.00"}],
            "dues": [
                {
                    "payee": None,
                    "amount": "199.00",
                    "currency": "INR",
                    "due_on": "2026-09-15",
                    "kind": "due",
                    "todoist_ref": None,
                }
            ],
            "unknowns": [
                {
                    "payee": None,
                    "amount": "1.00",
                    "currency": "INR",
                    "occurred_on": "2026-09-02",
                    "channel": "upi",
                }
            ],
            "closed_dues": [{"payee": None, "amount": "5.00", "currency": "INR"}],
            "forecast": [],
        }
    )
    for text in (out["html"], out["markdown"]):
        assert "None" not in text
        assert "2026-09-15 · — · ₹199.00 · (no task)" in text
        assert "2026-09-02 · — · ₹1.00 · upi" in text
        assert "— · ₹5.00" in text
        assert "— — ₹ 1.00" in text


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
