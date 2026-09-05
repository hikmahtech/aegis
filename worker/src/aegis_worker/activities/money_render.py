"""Pure renderers for the money brief and the month close (spec §7.2, §7.3).

Light HTML for Slack (`<b>`, `<pre>`); Markdown for the books repo. hledger
text is quoted verbatim; only index-derived amounts go through `fmt_money`.

Two shapes of bad input arrive here as normal traffic, not as caller bugs, and
both are handled rather than defended against:

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
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from html import escape

from aegis.services.money_format import fmt_money

# What a row says instead of an amount when the index has none for it.
NO_AMOUNT = "amount unknown"


def _money(value: object, currency: str | None = "INR") -> str:
    """Formatted major units, or `""` when there is no amount to show.

    `""` (not a zero, and not a raised exception) is the honest answer for a
    NULL `journal_index.amount`: the obligation is real, its size is unknown.
    """
    if value is None:
        return ""
    text = str(value).strip()
    if text in ("", "None"):
        return ""
    try:
        return fmt_money(abs(Decimal(text)), currency or "INR")
    except (InvalidOperation, ArithmeticError, ValueError):
        return ""


def _amount(value: object, currency: str | None = "INR") -> str:
    """`_money`, but never blank — a row still has to read as a sentence."""
    return _money(value, currency) or NO_AMOUNT


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
    md += [line, line2, ""]

    if brief.get("books_ok") and brief.get("bal_text"):
        html += ["<b>Where it went</b>", f"<pre>{escape(brief['bal_text'])}</pre>", ""]
        md += ["## Where it went", "```", brief["bal_text"].rstrip("\n"), "```", ""]
    elif not brief.get("books_ok"):
        html += ["Books unavailable — index only.", ""]
        md += ["_Books unavailable — index only._", ""]

    if brief.get("top_payees"):
        html.append("<b>Top payees</b>")
        md.append("## Top payees")
        for r in brief["top_payees"][:10]:
            # hledger's own amount string — quoted, never re-formatted.
            html.append(f"{escape(str(r['payee']))} — {escape(str(r['amount']))}")
            md.append(f"- {r['payee']} — {r['amount']}")
        html.append("")
        md.append("")

    html.append("<b>Due in the next 14 days</b>")
    md.append("## Due in the next 14 days")
    rows = 0
    for d in brief.get("dues") or []:
        kind = " · fix payment" if d.get("kind") == "failed" else ""
        task = "(task)" if d.get("todoist_ref") else "(no task)"
        amount = _amount(d.get("amount"), d.get("currency"))
        text = f"{d['due_on']} · {escape(str(d['payee']))} · {amount}{kind} · {task}"
        html.append(text)
        md.append(f"- {text}")
        rows += 1
    for f in brief.get("forecast") or []:
        text = f"{f['date']} · {escape(str(f['description']))} · {escape(str(f['amount']))}"
        html.append(text)
        md.append(f"- {text}")
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
            text = (
                f"{u['occurred_on']} · {escape(str(u['payee']))} · {amount} · "
                f"{escape(str(u.get('channel') or '-'))}"
            )
            html.append(text)
            md.append(f"- {text}")
        hint = 'Reply with "<payee> is <account>" or use ledger_add_rule.'
        html += [escape(hint), ""]
        md += [hint, ""]

    if brief.get("closed_dues"):
        html.append("<b>Paid this week</b>")
        md.append("## Paid this week")
        for c in brief["closed_dues"]:
            text = f"{escape(str(c['payee']))} · {_amount(c.get('amount'), c.get('currency'))}"
            html.append(text)
            md.append(f"- {text}")
        html.append("")
        md.append("")

    house = []
    if brief.get("unpushed"):
        house.append(f"{brief['unpushed']} unpushed commits")
    if brief.get("low_confidence"):
        house.append(f"{brief['low_confidence']} low-confidence LLM postings")
    if house:
        html += ["<b>Housekeeping</b>", " · ".join(house)]
        md += ["## Housekeeping", " · ".join(house)]

    return {"html": "\n".join(html).rstrip("\n"), "markdown": "\n".join(md).rstrip("\n") + "\n"}


def render_month_close(close: dict, home_symbol: str = "₹") -> dict:
    html = [f"<b>Month close · {close['month']}</b>"]
    md = [f"# Month close · {close['month']}", ""]

    fx = _fx_warning(close, home_symbol)
    if fx:
        html.append(fx[0])
        md += [fx[1], ""]

    if close.get("books_ok"):
        html += [
            f"<pre>{escape(close.get('is_text') or '')}</pre>",
            f"<pre>{escape(close.get('bs_text') or '')}</pre>",
        ]
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
