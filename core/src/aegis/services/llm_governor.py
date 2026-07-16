"""LLM spend governor — rolling token budget + a kill switch for generation.

Two plaintext rows in the ``settings`` table (no secrets, so both are
editable through the generic admin Settings editor — neither is in
``routes/settings.py::_HIDDEN_KEYS``):

``llm_governor``    ``{"daily_token_budget": 0, "model_filter": ""}``
    ``daily_token_budget`` 0 (the default, and the state of any deployment
    that never configures this) **disables the governor entirely** — the
    scheduled flow becomes a no-op. ``model_filter`` is a comma-separated
    list of case-insensitive substrings matched against ``llm_calls.model``
    (e.g. ``"claude,gpt-4"`` to budget only the paid models); empty = all.

``llm_kill_switch`` ``{"active": false, "reason": "", "set_by": ""}``
    While ``active``, ``LLMClient.think``/``chat``/``extract_receipts_batch``
    raise ``LLMKillSwitchError`` instead of calling the model. Embeddings are
    deliberately exempt (knowledge search must survive a spend freeze).
    ``set_by`` is ``"governor"`` for an automatic trip and anything else
    (typically ``"manual"``) for a human one — the governor only ever
    auto-clears a switch it set itself.

``get_kill_switch`` is read on the hot path of every generation call, so it
goes through a 30s TTL cache (mirrors ``services/api_key.py``) and **never
raises**: any read failure resolves to "inactive". Failing open is
deliberate — a governor bug must not be able to take AEGIS offline.

Token-based rather than $-based on purpose: this exists to catch runaway
loops, which is the real risk. True cost ground truth lives in the LiteLLM
proxy's own spend DB (whose native ``max_budget`` is the complementary
ops-side control).
"""

from __future__ import annotations

import time
from typing import Any

import structlog

logger = structlog.get_logger()

GOVERNOR_KEY = "llm_governor"
KILL_KEY = "llm_kill_switch"

_DEFAULT_GOVERNOR: dict[str, Any] = {"daily_token_budget": 0, "model_filter": ""}
_INACTIVE: dict[str, Any] = {"active": False, "reason": "", "set_by": ""}

_CACHE_TTL = 30.0
_kill_cache: dict[str, Any] = {"value": None, "ts": 0.0}


def invalidate_kill_cache() -> None:
    _kill_cache.update(value=None, ts=0.0)


def _coerce_int(value: Any) -> int:
    """Best-effort int from a hand-edited settings value; 0 on anything odd."""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


async def get_governor_config(pool: Any) -> dict[str, Any]:
    """Governor knobs, with defaults for a missing/garbage row. Never raises."""
    cfg = dict(_DEFAULT_GOVERNOR)
    try:
        row = await pool.fetchrow("SELECT value FROM settings WHERE key = $1", GOVERNOR_KEY)
    except Exception as exc:  # noqa: BLE001 — a settings read must not break the flow
        logger.warning("llm_governor_config_read_failed", error=str(exc)[:200])
        return cfg
    value = row["value"] if row else None
    if isinstance(value, dict):
        cfg["daily_token_budget"] = _coerce_int(value.get("daily_token_budget"))
        model_filter = value.get("model_filter")
        cfg["model_filter"] = model_filter if isinstance(model_filter, str) else ""
    return cfg


async def get_kill_switch(pool: Any, *, use_cache: bool = True) -> dict[str, Any]:
    """The kill-switch state; ``{"active": False, ...}`` when unset.

    NEVER raises. This is called before every generation call — a DB blip
    must fail OPEN (allow the call), never hard-fail the whole system.
    """
    now = time.monotonic()
    if use_cache and _kill_cache["value"] is not None and now - _kill_cache["ts"] < _CACHE_TTL:
        return _kill_cache["value"]

    state = dict(_INACTIVE)
    try:
        row = await pool.fetchrow("SELECT value FROM settings WHERE key = $1", KILL_KEY)
    except Exception as exc:  # noqa: BLE001 — fail open, see docstring
        logger.warning("llm_kill_switch_read_failed", error=str(exc)[:200])
        return dict(_INACTIVE)

    value = row["value"] if row else None
    if isinstance(value, dict):
        state = {
            "active": bool(value.get("active", False)),
            "reason": str(value.get("reason") or ""),
            "set_by": str(value.get("set_by") or ""),
        }
    _kill_cache.update(value=state, ts=now)
    return state


async def set_kill_switch(pool: Any, *, active: bool, reason: str, set_by: str) -> None:
    """Upsert the kill switch and invalidate the cache so it applies at once."""
    value = {"active": bool(active), "reason": reason or "", "set_by": set_by or ""}
    await pool.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ($1, $2, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()",
        KILL_KEY,
        value,
    )
    invalidate_kill_cache()
    logger.info("llm_kill_switch_set", active=bool(active), set_by=set_by, reason=reason[:200])


def _filter_patterns(model_filter: str) -> list[str]:
    """``"claude, gpt-4"`` → ``["%claude%", "%gpt-4%"]``."""
    return [f"%{part.strip()}%" for part in model_filter.split(",") if part.strip()]


async def llm_tokens_last_24h(pool: Any, model_filter: str = "") -> int:
    """Total input+output tokens recorded in ``llm_calls`` over the last 24h.

    ``model_filter`` is a comma-separated list of case-insensitive substrings;
    empty counts every model. Failure rows (status='error'/'timeout') carry 0
    tokens, so they contribute nothing without needing a status predicate.
    """
    sql = (
        "SELECT COALESCE(SUM(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)), 0) "
        "AS total FROM llm_calls WHERE created_at > NOW() - INTERVAL '24 hours'"
    )
    patterns = _filter_patterns(model_filter)
    if patterns:
        sql += " AND model ILIKE ANY($1::text[])"
        row = await pool.fetchrow(sql, patterns)
    else:
        row = await pool.fetchrow(sql)
    return int(row["total"] or 0) if row else 0
