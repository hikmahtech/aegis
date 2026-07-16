"""LLM spend governor — settings-backed kill switch + rolling token usage.

The kill switch sits in front of every generation call in AEGIS, so these
tests pin the two properties that keep a bug here from taking the whole
system down: it fails OPEN (any read error ⇒ inactive), and it never
touches embeddings (knowledge search must survive a spend freeze).
"""

from __future__ import annotations

import pytest

_MODEL_PREFIX = "govtest-"


async def _cleanup(pool) -> None:
    await pool.execute("DELETE FROM llm_calls WHERE model LIKE $1", f"{_MODEL_PREFIX}%")
    await pool.execute(
        "DELETE FROM settings WHERE key = ANY($1::text[])",
        ["llm_governor", "llm_kill_switch"],
    )


async def test_kill_switch_roundtrip_and_cache(db_pool):
    from aegis.services import llm_governor as g

    try:
        await _cleanup(db_pool)
        g.invalidate_kill_cache()

        # Missing row ⇒ inactive (an unconfigured deploy must be a no-op).
        assert (
            await db_pool.fetchrow("SELECT 1 FROM settings WHERE key='llm_kill_switch'")
        ) is None
        assert (await g.get_kill_switch(db_pool))["active"] is False

        # set_kill_switch invalidates the cache, so the flip is visible at once.
        await g.set_kill_switch(db_pool, active=True, reason="budget", set_by="governor")
        ks = await g.get_kill_switch(db_pool)
        assert ks["active"] is True
        assert ks["reason"] == "budget"
        assert ks["set_by"] == "governor"

        await g.set_kill_switch(db_pool, active=False, reason="", set_by="governor")
        assert (await g.get_kill_switch(db_pool))["active"] is False
    finally:
        await _cleanup(db_pool)
        g.invalidate_kill_cache()


async def test_get_kill_switch_fails_open_on_db_error(db_pool):
    """A broken pool must NEVER raise — it would take down every LLM call."""
    from aegis.services import llm_governor as g

    class _BrokenPool:
        async def fetchrow(self, *a, **kw):
            raise RuntimeError("db is on fire")

    g.invalidate_kill_cache()
    assert (await g.get_kill_switch(_BrokenPool(), use_cache=False))["active"] is False


async def test_tokens_last_24h_filters_models(db_pool):
    from aegis.services import llm_governor as g

    try:
        await _cleanup(db_pool)
        await db_pool.execute(
            "INSERT INTO llm_calls (model, input_tokens, output_tokens, latency_ms, "
            "purpose, status) VALUES "
            "($1, 100, 50, 10, 't', 'success'), "
            "($2, 1000, 500, 10, 't', 'success'), "
            "($3, 200, 100, 10, 't', 'success')",
            f"{_MODEL_PREFIX}claude-sonnet-5",
            f"{_MODEL_PREFIX}gemma4-e2b",
            f"{_MODEL_PREFIX}claude-haiku-4.5",
        )

        # Substring filter — only the two claude rows: (100+50) + (200+100).
        assert await g.llm_tokens_last_24h(db_pool, f"{_MODEL_PREFIX}claude") == 450
        # Empty filter counts everything (this DB is shared with other tests).
        assert await g.llm_tokens_last_24h(db_pool, "") >= 1950
        # Comma-separated substrings union.
        assert (
            await g.llm_tokens_last_24h(
                db_pool, f"{_MODEL_PREFIX}claude-haiku, {_MODEL_PREFIX}gemma"
            )
            == 1800
        )
        # A filter matching nothing ⇒ 0, not "everything".
        assert await g.llm_tokens_last_24h(db_pool, "no-such-model-xyz") == 0
    finally:
        await _cleanup(db_pool)


async def test_tokens_last_24h_ignores_older_rows(db_pool):
    from aegis.services import llm_governor as g

    try:
        await _cleanup(db_pool)
        await db_pool.execute(
            "INSERT INTO llm_calls (model, input_tokens, output_tokens, latency_ms, "
            "purpose, status, created_at) VALUES ($1, 5000, 5000, 10, 't', 'success', "
            "NOW() - INTERVAL '25 hours')",
            f"{_MODEL_PREFIX}old",
        )
        assert await g.llm_tokens_last_24h(db_pool, f"{_MODEL_PREFIX}old") == 0
    finally:
        await _cleanup(db_pool)


async def test_governor_config_defaults_when_unset(db_pool):
    from aegis.services import llm_governor as g

    try:
        await _cleanup(db_pool)
        cfg = await g.get_governor_config(db_pool)
        assert cfg["daily_token_budget"] == 0  # 0 = disabled
        assert cfg["model_filter"] == ""

        await db_pool.execute(
            "INSERT INTO settings (key, value) VALUES ('llm_governor', $1)",
            {"daily_token_budget": 500, "model_filter": "claude"},
        )
        cfg = await g.get_governor_config(db_pool)
        assert cfg["daily_token_budget"] == 500
        assert cfg["model_filter"] == "claude"
    finally:
        await _cleanup(db_pool)


async def test_governor_config_tolerates_garbage(db_pool):
    """A hand-edited settings row must not crash the flow."""
    from aegis.services import llm_governor as g

    try:
        await _cleanup(db_pool)
        await db_pool.execute(
            "INSERT INTO settings (key, value) VALUES ('llm_governor', $1)",
            {"daily_token_budget": "not-a-number", "model_filter": None},
        )
        cfg = await g.get_governor_config(db_pool)
        assert cfg["daily_token_budget"] == 0
        assert cfg["model_filter"] == ""
    finally:
        await _cleanup(db_pool)


# --- LLMClient guard (Task B2) ------------------------------------------------


async def test_llm_client_refuses_when_killed(db_pool):
    from aegis.llm import LLMClient, LLMKillSwitchError
    from aegis.services import llm_governor as g

    try:
        await _cleanup(db_pool)
        await g.set_kill_switch(db_pool, active=True, reason="test", set_by="manual")
        client = LLMClient(base_url="http://localhost:1", db_pool=db_pool)

        with pytest.raises(LLMKillSwitchError):
            await client.think("hi", model="gemma4:e2b")

        with pytest.raises(LLMKillSwitchError):
            await client.chat([{"role": "user", "content": "hi"}], model="gemma4:e2b")

        # Embeddings stay up — knowledge search must keep working during a
        # spend freeze. The HTTP call fails (port 1 is dead); what matters is
        # that the guard did NOT pre-empt it.
        with pytest.raises(Exception) as exc:
            await client.embed(["x"])
        assert not isinstance(exc.value, LLMKillSwitchError)
    finally:
        await _cleanup(db_pool)
        g.invalidate_kill_cache()


async def test_llm_client_without_pool_never_blocks(db_pool):
    """comms-style construction (no pool) must never be governed."""
    from aegis.llm import LLMClient, LLMKillSwitchError
    from aegis.services import llm_governor as g

    try:
        await _cleanup(db_pool)
        await g.set_kill_switch(db_pool, active=True, reason="test", set_by="manual")
        client = LLMClient(base_url="http://localhost:1")

        with pytest.raises(Exception) as exc:
            await client.think("hi")
        assert not isinstance(exc.value, LLMKillSwitchError)
    finally:
        await _cleanup(db_pool)
        g.invalidate_kill_cache()


async def test_llm_client_allows_when_switch_inactive(db_pool):
    """The default/unconfigured deployment is a complete no-op."""
    from aegis.llm import LLMClient, LLMKillSwitchError
    from aegis.services import llm_governor as g

    try:
        await _cleanup(db_pool)
        g.invalidate_kill_cache()
        client = LLMClient(base_url="http://localhost:1", db_pool=db_pool)

        with pytest.raises(Exception) as exc:
            await client.think("hi", model="gemma4:e2b")
        assert not isinstance(exc.value, LLMKillSwitchError)
    finally:
        await _cleanup(db_pool)
        g.invalidate_kill_cache()
