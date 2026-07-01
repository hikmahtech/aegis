"""Market summary endpoint — aggregated trading system data for briefings."""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends, Request

from aegis.api.auth import verify_auth

logger = structlog.get_logger()

router = APIRouter(prefix="/api/market", dependencies=[Depends(verify_auth)])


@router.get("/summary")
async def market_summary(request: Request) -> dict[str, Any]:
    """Return aggregated market data for briefings.

    Calls ClickHouseConnector for regime, signals, changes, decisions, sectors.
    Returns {"available": false} if ClickHouse is not configured or errors.
    """
    ch = getattr(request.app.state, "clickhouse_connector", None)
    if not ch:
        return {"available": False}

    try:
        return await _build_summary(ch)
    except Exception as exc:
        logger.warning("market_summary_failed", error=str(exc))
        return {"available": False}


async def _build_summary(ch) -> dict[str, Any]:
    """Build the full market summary from ClickHouse queries."""
    # Equity regime
    equity_rows = await ch.query(
        "SELECT date, close, regime_label FROM index_statistics FINAL"
        " WHERE index_name = 'Nifty 50' ORDER BY date DESC LIMIT 1"
    )
    equity_regime = None
    if equity_rows:
        r = equity_rows[0]
        equity_regime = {
            "regime": r.get("regime_label"),
            "close": r.get("close"),
            "date": str(r.get("date")),
        }

    # Crypto
    crypto_rows = await ch.query(
        "SELECT date, fgi_value, fgi_classification, total_market_cap_usd"
        " FROM crypto_global_market FINAL ORDER BY date DESC LIMIT 1"
    )
    crypto = None
    if crypto_rows:
        r = crypto_rows[0]
        crypto = {
            "fear_greed": r.get("fgi_value"),
            "fear_greed_label": r.get("fgi_classification"),
            "market_cap_usd": r.get("total_market_cap_usd"),
            "date": str(r.get("date")),
        }

    # Top equity signals (halal only)
    top_equity = await ch.query(
        "SELECT nse_symbol AS symbol, combined_forecast, confidence, close_price, is_halal"
        " FROM equity_forecasts AS f FINAL"
        " WHERE f.date = (SELECT max(date) FROM equity_forecasts) AND f.is_halal = 1"
        " ORDER BY abs(f.combined_forecast) DESC LIMIT {count:UInt32}",
        {"count": 5},
    )

    # Top crypto signals
    top_crypto = await ch.query(
        "SELECT symbol, combined_forecast, prediction_confidence AS confidence, close_price"
        " FROM crypto_forecasts AS f FINAL"
        " WHERE f.date = (SELECT max(date) FROM crypto_forecasts)"
        " ORDER BY abs(f.combined_forecast) DESC LIMIT {count:UInt32}",
        {"count": 5},
    )

    # Notable forecast changes (5-day, threshold 3.0)
    notable = await ch.query(
        "WITH latest AS ("
        "  SELECT nse_symbol, date, combined_forecast FROM equity_forecasts AS f FINAL"
        "  WHERE date = (SELECT max(date) FROM equity_forecasts)"
        "), prior AS ("
        "  SELECT nse_symbol, date, combined_forecast FROM equity_forecasts AS f FINAL"
        "  WHERE date = (SELECT max(date) FROM equity_forecasts WHERE date <= today() - {days:UInt32})"
        ") SELECT l.nse_symbol AS symbol,"
        "  l.combined_forecast AS current_forecast, p.combined_forecast AS prior_forecast,"
        "  l.combined_forecast - p.combined_forecast AS change"
        " FROM latest l JOIN prior p ON l.nse_symbol = p.nse_symbol"
        " WHERE abs(l.combined_forecast - p.combined_forecast) >= {threshold:Float64}"
        " ORDER BY abs(l.combined_forecast - p.combined_forecast) DESC LIMIT 5",
        {"days": 5, "threshold": 3.0},
    )

    # Active trade decisions
    date_rows = await ch.query("SELECT max(data_date) AS max_date FROM trade_decisions")
    trade_decisions = []
    if date_rows and date_rows[0].get("max_date"):
        trade_decisions = await ch.query(
            "SELECT symbol, direction, target_weight, combined_forecast, asset_class, halal_status"
            " FROM trade_decisions FINAL WHERE data_date = {date:String}"
            " ORDER BY abs(combined_forecast) DESC",
            {"date": str(date_rows[0]["max_date"])},
        )

    # Top sectors
    sectors = await ch.query(
        "SELECT sector, momentum_signal, sector_rank, mean_z_score_20d"
        " FROM sector_statistics FINAL"
        " WHERE date = (SELECT max(date) FROM sector_statistics)"
        " ORDER BY sector_rank ASC LIMIT 5"
    )

    return {
        "available": True,
        "equity_regime": equity_regime,
        "crypto": crypto,
        "top_equity_signals": top_equity,
        "top_crypto_signals": top_crypto,
        "notable_changes": notable,
        "trade_decisions": trade_decisions,
        "sectors": sectors,
    }
