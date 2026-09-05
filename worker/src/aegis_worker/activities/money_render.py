"""Pure renderers for the money brief and the month close (spec §7.2, §7.3).

Light HTML for Slack (`<b>`, `<pre>`); Markdown for the books repo. hledger
text is quoted verbatim; only index-derived amounts go through `fmt_money`.

**The two outputs are built from the same PARTS, never from each other.**
`html.escape` defaults to `quote=True`, so an escaped string carries `&#x27;`
and `&amp;` — fine inside a Slack message, permanent damage inside
`reports/weekly/*.md`, where `Domino's Pizza` would be committed to the user's
books repo as `Domino&#x27;s Pizza` every week forever. Every row below
therefore composes its plain text once and escapes only on the way into the
HTML list.

Three shapes of bad input arrive here as normal traffic, not as caller bugs:

* `fx_stale` — hledger's `-X ₹` converts nothing it has no price for and then
  writes every commodity into one cell, so the ₹ figures understate the truth.
  The data layer detects that; this layer has to SAY it, at the top, naming the
  commodities. Detecting it and rendering the numbers silently would recreate
  the exact bug the detection exists to prevent.
* a missing amount — `finance.journal_index.amount` is nullable and neither the
  dues nor the closed-dues query filters on it, so an amount reaches us as the
  *string* `"None"`. `Decimal("None")` raises, and interpolating it prints the
  word "None" at the reader; both are wrong, so such a row renders without a
  fabricated amount instead.
* a missing payee — `journal_index.payee` is nullable too (migration 026), one
  column over, and would print the word `None` the same way.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from html import escape

from aegis.services.money_format import fmt_money

# What a row says instead of an amount when the index has none for it.
NO_AMOUNT = "amount unknown"
# ...and instead of a name.
NO_NAME = "—"

# How many lines of hledger's balance table the CHAT message carries. The
# filed Markdown report is never truncated — it is the complete record, and the
# pointer line below sends the reader to it.
#
# The cap exists because the Slack adapter chunks at 2800 chars
# (`_SLACK_MAX_CHARS`) by splitting on line boundaries with no awareness of
# code fences, while `<pre>` becomes a triple-backtick fence. A brief that
# overflows therefore ends message 1 mid-table with an unclosed fence and opens
# message 2 with raw rows — the centrepiece renders mangled, and nothing logs
# it. 20 lines of a `bal --depth 2` table is roughly 800 chars, which keeps a
# realistic brief (payees + dues + unknowns + closed dues) inside one message.
MAX_LEDGER_LINES = 20

# Marks a row hledger PREDICTED from a `~ periodic` rule, as opposed to a due
# the index recorded from a real bill. Both belong in one "money leaving soon"
# list, but the reader has to be able to tell a forecast from a fact.
FORECAST_MARK = "forecast"


def _money(value: object, currency: str | None = "INR") -> str:
    """Formatted major units, or `""` when there is no amount to show.

    `""` (not a zero, and not a raised exception) is the honest answer for a
    NULL `journal_index.amount`: the obligation is real, its size is unknown.
    """
    if value is None:
        return ""
    text = str(value).strip()
    # The known production shape, spelled out so a reader need not know that
    # `Decimal("None")` raises. The `except` below is the backstop for
    # anything else the column could hold.
    if text in ("", "None"):
        return ""
    try:
        return fmt_money(abs(Decimal(text)), currency or "INR")
    except (InvalidOperation, ArithmeticError, ValueError):
        return ""


def _amount(value: object, currency: str | None = "INR") -> str:
    """`_money`, but never blank — a row still has to read as a sentence."""
    return _money(value, currency) or NO_AMOUNT


def _name(value: object) -> str:
    """A payee/description as PLAIN text — never the word "None"."""
    text = "" if value is None else str(value).strip()
    return text if text and text != "None" else NO_NAME


def _clip(text: str, limit: int = MAX_LEDGER_LINES) -> tuple[str, int]:
    """`text` cut to `limit` lines, plus how many lines were dropped."""
    lines = (text or "").rstrip("\n").split("\n")
    if len(lines) <= limit:
        return text, 0
    return "\n".join(lines[:limit]) + "\n", len(lines) - limit


def _fx_warning(data: dict, home_symbol: str) -> tuple[str, str] | None:
    """The caveat for figures hledger could not value in the home commodity.

    Returns `(html_line, markdown_line)`, or None when every commodity was
    converted. Bold in both: this qualifies every number under it.
    """
    if not data.get("fx_stale"):
        return None

    def say(esc: Callable[[str], str]) -> str:
        sym = esc(home_symbol)
        names = ", ".join(esc(str(c)) for c in (data.get("fx_unconverted") or []) if str(c).strip())
        if names:
            return (
                f"⚠ No exchange rate for {names} — those amounts are not "
                f"included in the {sym} figures below."
            )
        return (
            f"⚠ Some amounts could not be converted to {sym} — "
            f"the {sym} figures below leave them out."
        )

    return f"<b>{say(escape)}</b>", f"**{say(str)}**"


def render_money_brief(brief: dict, home_symbol: str = "₹") -> dict:
    ent = brief.get("entities") or {}
    p = ent.get("personal") or {}
    h = ent.get("hikmah") or {}
    html: list[str] = [f"<b>Money brief · {brief['since']} → {brief['as_of']}</b>"]
    md: list[str] = [f"# Money brief · {brief['since']} → {brief['as_of']}", ""]

    # Above the numbers, deliberately: a caveat under a wrong headline is a
    # footnote nobody reads.
    fx = _fx_warning(brief, home_symbol)
    if fx:
        html += [fx[0], ""]
        md += [fx[1], ""]

    line = f"Personal: in {_money(p.get('income') or '0')} · out {_money(p.get('expenses') or '0')}"
    line2 = f"Hikmah: in {_money(h.get('income') or '0')} · out {_money(h.get('expenses') or '0')}"
    html += [line, line2, ""]
    # Bulleted in Markdown: two adjacent unbulleted lines are ONE paragraph to
    # every Markdown renderer, so GitHub would run the two entities together.
    md += [f"- {line}", f"- {line2}", ""]

    if brief.get("books_ok") and brief.get("bal_text"):
        clipped, dropped = _clip(brief["bal_text"])
        html += ["<b>Where it went</b>", f"<pre>{escape(clipped)}</pre>"]
        if dropped:
            html.append(
                f"…{dropped} more lines — the full table is in "
                f"reports/weekly/{escape(str(brief['as_of']))}.md."
            )
        html.append("")
        # The filed report is the record: never clipped.
        md += ["## Where it went", "```", brief["bal_text"].rstrip("\n"), "```", ""]
    elif not brief.get("books_ok"):
        html += ["Books unavailable — index only.", ""]
        md += ["_Books unavailable — index only._", ""]

    if brief.get("top_payees"):
        html.append("<b>Top payees</b>")
        md.append("## Top payees")
        for r in brief["top_payees"][:10]:
            # hledger's own amount string — quoted, never re-formatted.
            payee, amount = _name(r.get("payee")), str(r.get("amount") or "")
            html.append(f"{escape(payee)} — {escape(amount)}")
            md.append(f"- {payee} — {amount}")
        html.append("")
        md.append("")

    html.append("<b>Due in the next 14 days</b>")
    md.append("## Due in the next 14 days")
    rows = 0
    for d in brief.get("dues") or []:
        kind = " · fix payment" if d.get("kind") == "failed" else ""
        task = "(task)" if d.get("todoist_ref") else "(no task)"
        amount = _amount(d.get("amount"), d.get("currency"))
        payee = _name(d.get("payee"))
        tail = f" · {amount}{kind} · {task}"
        html.append(f"{d['due_on']} · {escape(payee)}{tail}")
        md.append(f"- {d['due_on']} · {payee}{tail}")
        rows += 1
    for f in brief.get("forecast") or []:
        # A forecast is hledger's PREDICTION from a `~ periodic` rule, not a
        # bill that arrived. Marked so the reader can tell it from a due.
        desc, amount = _name(f.get("description")), str(f.get("amount") or "")
        html.append(f"{f['date']} · {escape(desc)} · {escape(amount)} · {FORECAST_MARK}")
        md.append(f"- {f['date']} · {desc} · {amount} · {FORECAST_MARK}")
        rows += 1
    if rows == 0:
        html.append("Nothing due.")
        md.append("Nothing due.")
    html.append("")
    md.append("")

    if brief.get("unknowns"):
        html.append("<b>Unexplained</b>")
        md.append("## Unexplained")
        for u in brief["unknowns"][:10]:
            amount = _amount(u.get("amount"), u.get("currency"))
            payee, channel = _name(u.get("payee")), _name(u.get("channel"))
            tail = f" · {amount} · "
            html.append(f"{u['occurred_on']} · {escape(payee)}{tail}{escape(channel)}")
            md.append(f"- {u['occurred_on']} · {payee}{tail}{channel}")
        hint = 'Reply with "<payee> is <account>" or use ledger_add_rule.'
        html += [escape(hint), ""]
        md += [hint, ""]

    if brief.get("closed_dues"):
        html.append("<b>Paid this week</b>")
        md.append("## Paid this week")
        for c in brief["closed_dues"]:
            payee = _name(c.get("payee"))
            amount = _amount(c.get("amount"), c.get("currency"))
            html.append(f"{escape(payee)} · {amount}")
            md.append(f"- {payee} · {amount}")
        html.append("")
        md.append("")

    house = []
    if brief.get("unpushed"):
        house.append(f"{brief['unpushed']} unpushed commits")
    if brief.get("low_confidence"):
        house.append(f"{brief['low_confidence']} low-confidence LLM postings")
    if house:
        html += ["<b>Housekeeping</b>", " · ".join(house)]
        md += ["## Housekeeping", f"- {' · '.join(house)}"]

    return {"html": "\n".join(html).rstrip("\n"), "markdown": "\n".join(md).rstrip("\n") + "\n"}


def render_month_close(close: dict, home_symbol: str = "₹") -> dict:
    html = [f"<b>Month close · {close['month']}</b>"]
    md = [f"# Month close · {close['month']}", ""]

    fx = _fx_warning(close, home_symbol)
    if fx:
        html.append(fx[0])
        md += [fx[1], ""]

    if close.get("books_ok"):
        # Same fence-chunking hazard as the brief, and two statements this
        # time. The filed report under reports/monthly/ keeps both in full.
        for key in ("is_text", "bs_text"):
            clipped, dropped = _clip(close.get(key) or "")
            html.append(f"<pre>{escape(clipped)}</pre>")
            if dropped:
                html.append(
                    f"…{dropped} more lines — the full statement is in "
                    f"reports/monthly/{escape(str(close['month']))}.md."
                )
        md += [
            "```",
            (close.get("is_text") or "").rstrip("\n"),
            "```",
            "",
            "```",
            (close.get("bs_text") or "").rstrip("\n"),
            "```",
            "",
        ]
    else:
        html.append("Books unavailable — index only.")
        md.append("_Books unavailable — index only._")
    lines = [
        f"Recurring commitments: {_money(close.get('recurring_total') or '0')}",
        f"Unexplained postings: {close.get('unknown_count', 0)}",
        f"Dues paid: {close.get('dues_paid', 0)} · still open: {close.get('dues_open', 0)}",
    ]
    html += lines
    md += [f"- {ln}" for ln in lines]
    return {"html": "\n".join(html), "markdown": "\n".join(md) + "\n"}
