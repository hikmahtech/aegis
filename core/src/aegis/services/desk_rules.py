"""The trading desk's market and tax settings: read them, check them, save them.

AEGIS is forked and configured for someone else's life, so the desk's market —
its exchange calendar, its clock, its ticker shape, its currency, its financial
year and its tax rates — is configuration, not code. It lives in the `config`
of the `trading-desk-daily` activities row, which `trading_desk.load_rules`
reads on every run, so a change needs no redeploy.

This module owns the small, checked write path behind the admin Trading desk
page. Two things it deliberately is not:

* **Not a general config editor.** It touches only the settings that used to be
  Python constants — the market, the money, the tax model and the benchmarks.
  Every other knob in that row (the trading bands, the order cap, the asset
  classes, the benchmark price mappings) is left alone by a save, and the admin
  Flows page still edits the row as raw JSON for anyone who needs one of those.
* **Not a second reader.** The effective values come from `desk_math.Rules`,
  the same merge the daily run uses. A form that showed a second opinion of
  what the desk believes would be worse than no form.

Reading is lenient and writing is strict, the same split the email triage rules
use: the run must never stop because a number is odd, but a typo must not save
with a 200 and then quietly do nothing for months.
"""

from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import asyncpg

from aegis.services import desk_math as dm
from aegis.services.trading_desk import DESK_SLUG

# The settings this page owns, in the order the page shows them. Every one of
# them was a Python constant or a code default before, and every one names
# something only the operator can know.
EDITABLE = (
    "calendar_symbol",
    "market_tz",
    "symbol_suffix",
    "currency",
    "fy_start_month",
    "stale_calendar_days",
    "stale_price_days",
    "capital",
    "sell_charge",
    "tax_rate",
    "long_term_rate",
    "long_term_exemption",
    "long_term_exemption_classes",
    "benchmark",
    "context_benchmark",
    "expected_excess_pa",
)

_MAX_STALE_DAYS = 60


def _text(body: dict, key: str) -> str:
    value = body.get(key, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{key} must be text")
    return value.strip()


def _symbol(body: dict, key: str) -> str:
    value = _text(body, key)
    if any(c.isspace() for c in value):
        raise ValueError(f"{key} must be one symbol, with no spaces in it")
    return value


def _number(body: dict, key: str, *, low: float, high: float) -> float:
    value = body.get(key)
    if value in (None, ""):
        value = 0
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a number") from exc
    if not low <= out <= high:
        raise ValueError(f"{key} must be between {low:g} and {high:g}")
    return out


def _whole(body: dict, key: str, *, low: int, high: int) -> int:
    """A whole number, or the code default when the field was not sent.

    A form that omits a field means "leave it alone", not "clear it": the two
    day counts and the financial year's start month have neutral defaults that
    work in any market, so a partial body saves rather than 400s."""
    value = body.get(key)
    if value in (None, ""):
        return int(getattr(dm.Rules(), key))
    try:
        out = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a whole number") from exc
    if not low <= out <= high:
        raise ValueError(f"{key} must be between {low} and {high}")
    return out


def _classes(body: dict, key: str) -> list[str]:
    value = body.get(key) or []
    if isinstance(value, str):  # a comma-separated box is a fair thing to send
        value = value.split(",")
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list of asset classes")
    out = [str(item).strip() for item in value]
    if any(not item for item in out):
        raise ValueError(f"{key} must not contain an empty asset class")
    return out


def _tax_rate(body: dict) -> dict[str, float]:
    value = body.get("tax_rate") or {}
    if not isinstance(value, dict):
        raise ValueError("tax_rate must be one rate per asset class")
    out: dict[str, float] = {}
    for name, rate in value.items():
        asset_class = str(name).strip()
        if not asset_class:
            raise ValueError("tax_rate must not name an empty asset class")
        try:
            out[asset_class] = float(rate)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"the tax rate for {asset_class} must be a number") from exc
        if not 0.0 <= out[asset_class] <= 1.0:
            raise ValueError(f"the tax rate for {asset_class} must be between 0 and 1")
    return out


def validate(body: dict[str, Any]) -> dict[str, Any]:
    """The settings as they will be stored, or a ValueError naming what is wrong.

    An empty `calendar_symbol` is allowed and means exactly one thing: the desk
    is not configured and will do nothing. That is a real state a fork sits in,
    so it saves — it is a bad *timezone* or a rate outside 0 to 1 that gets
    refused, because those are answers nobody meant to give.
    """
    market_tz = _text(body, "market_tz") or "UTC"
    try:
        ZoneInfo(market_tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"market_tz: {market_tz!r} is not a timezone name — use one like Asia/Kolkata") from exc

    suffix = _symbol(body, "symbol_suffix")
    if len(suffix) > 10:
        raise ValueError("symbol_suffix is too long to be an exchange suffix")

    currency = _text(body, "currency").upper()
    if currency and (len(currency) != 3 or not currency.isalpha()):
        raise ValueError("currency must be a three-letter code like INR, USD or GBP")

    return {
        "calendar_symbol": _symbol(body, "calendar_symbol"),
        "market_tz": market_tz,
        "symbol_suffix": suffix,
        "currency": currency,
        "fy_start_month": _whole(body, "fy_start_month", low=1, high=12),
        "stale_calendar_days": _whole(body, "stale_calendar_days", low=1, high=_MAX_STALE_DAYS),
        "stale_price_days": _whole(body, "stale_price_days", low=1, high=_MAX_STALE_DAYS),
        "capital": _number(body, "capital", low=0, high=1e12),
        "sell_charge": _number(body, "sell_charge", low=0, high=1e9),
        "tax_rate": _tax_rate(body),
        "long_term_rate": _number(body, "long_term_rate", low=0, high=1),
        "long_term_exemption": _number(body, "long_term_exemption", low=0, high=1e12),
        "long_term_exemption_classes": _classes(body, "long_term_exemption_classes"),
        "benchmark": _symbol(body, "benchmark"),
        "context_benchmark": _symbol(body, "context_benchmark"),
        "expected_excess_pa": _number(body, "expected_excess_pa", low=-1, high=1),
    }


def _view(cfg: dict | None, *, capital_locked: bool = False) -> dict[str, Any]:
    """What the page shows: the effective settings, what is still stored under a
    retired key name, and whether `capital` can still be changed."""
    rules = dm.Rules.from_config(cfg)
    values = {key: getattr(rules, key) for key in EDITABLE}
    values["long_term_exemption_classes"] = list(rules.long_term_exemption_classes)
    return {
        "configured": rules.configured(),
        "values": values,
        # Named so the page can say "these are stored under an old name" rather
        # than the operator discovering it from a log line they never read.
        "retired_keys": dm.legacy_keys(cfg),
        # So the form can grey the field out rather than letting someone type a
        # number and meet a 400 they could not have predicted.
        "capital_locked": capital_locked,
    }


async def _has_filled_order(conn: Any) -> bool:
    """Whether the paper book has any history to restate."""
    return bool(await conn.fetchval("SELECT 1 FROM finance.desk_orders WHERE status = 'filled' LIMIT 1"))


async def read(pool: asyncpg.Pool) -> dict[str, Any]:
    """The desk's effective market and tax settings."""
    async with pool.acquire() as conn:
        cfg = await conn.fetchval("SELECT config FROM activities WHERE slug = $1", DESK_SLUG)
        locked = await _has_filled_order(conn)
    return _view(cfg if isinstance(cfg, dict) else None, capital_locked=locked)


async def save(pool: asyncpg.Pool, body: dict[str, Any]) -> dict[str, Any]:
    """Check the settings and write them into the desk's activities row.

    The write MERGES: every key this page does not own keeps its stored value,
    so saving a timezone cannot silently drop the trading bands or the
    benchmark price mappings. The three retired key names are dropped in the
    same statement that writes their replacements, which is what turns the
    rename from a thing an operator must remember into a thing a save fixes.

    One setting stops being editable: `capital`, once any paper order has
    filled. The book has no deposits table, so `desk_math.replay` starts it from
    this number on every past day — a new one would silently restate the whole
    history, cash and value and weekly gap alike. Refusing is the honest answer
    until a deposit is an event rather than a retrospective edit (#526). Every
    other setting on the page still saves, and nothing at all is written when
    this one is refused.

    Raises ValueError on bad input and LookupError when the desk has no row —
    a deployment that has never seeded one has nothing to configure yet.
    """
    settings = validate(body)
    async with pool.acquire() as conn, conn.transaction():
        cfg = await conn.fetchval(
            "SELECT config FROM activities WHERE slug = $1 FOR UPDATE", DESK_SLUG
        )
        if cfg is None:
            raise LookupError(DESK_SLUG)
        stored = dm.Rules.from_config(cfg if isinstance(cfg, dict) else None).capital
        locked = await _has_filled_order(conn)
        if locked and round(settings["capital"], 2) != round(stored, 2):
            raise ValueError(
                f"capital is {stored:g} and cannot be changed to {settings['capital']:g}: this desk "
                "has already filled orders, and the paper book's whole history is replayed from this "
                "number, so a new one would restate every past day. Reset the paper book (clear "
                "finance.desk_orders and finance.desk_plans), or wait for the deposits table. "
                "Everything else on this page still saves."
            )
        merged = {k: v for k, v in (cfg if isinstance(cfg, dict) else {}).items() if k not in dm.RENAMED.values()}
        merged.update(settings)
        await conn.execute(
            "UPDATE activities SET config = $2, updated_at = now() WHERE slug = $1", DESK_SLUG, merged
        )
    return _view(merged, capital_locked=locked)
