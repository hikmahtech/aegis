"""The helpers #603 folded several copies into, each with the one check the
copies did not have between them.

`decode_jsonb` and the slug rule have their own files (`tests/worker/
test_shared_jsonb.py`, `tests/core/test_slugs.py`); this covers the rest.
"""

from __future__ import annotations

import pytest
from aegis.api.routes.webhooks import claim_idempotency
from aegis.connectors._base import envelope
from aegis.llm import route_for_purpose, tier_to_model
from aegis.services.llm_backend import install_llm_config


def test_the_envelope_is_the_shape_every_connector_returns():
    assert envelope(True, data={"a": 1}) == {
        "ok": True,
        "data": {"a": 1},
        "error": None,
        "retryable": False,
        "external_ref": None,
    }
    # `retryable` is the field the outbox reads: a queued write depends on it.
    assert envelope(False, error="timeout", retryable=True)["retryable"] is True


def test_install_llm_config_installs_both_halves():
    """The bug the helper exists to stop: refreshing the tier map and leaving
    an edited routing table behind."""
    install_llm_config(
        {
            "tiers": {"fast": "m-fast", "balanced": "m-bal", "smart": "m-smart"},
            "routes": {
                "categories": {"extract": {"model": "m-extract", "json": True}},
                "purposes": {"gmail_classification": "extract"},
            },
            "source": "test",
        }
    )
    try:
        assert tier_to_model("fast") == "m-fast"
        assert route_for_purpose("gmail_classification") == ("m-extract", True)
    finally:
        install_llm_config({"tiers": {}, "routes": None})


def test_a_routing_table_that_will_not_validate_never_blocks_a_boot():
    """It turns routing OFF and carries on — the tiers still land."""
    install_llm_config(
        {
            "tiers": {"balanced": "m-bal"},
            "routes": {"categories": {"extract": {}}, "purposes": {}},
        }
    )
    try:
        assert tier_to_model("balanced") == "m-bal"
        assert route_for_purpose("anything") == (None, False)
    finally:
        install_llm_config({"tiers": {}, "routes": None})


@pytest.mark.asyncio
async def test_claim_idempotency_is_true_once_and_false_after(db_pool):
    """The claim IS the row: the second delivery of the same id is a replay."""
    await db_pool.execute(
        "DELETE FROM ingest_idempotency WHERE source_type = 'test:claim'",
    )
    assert await claim_idempotency(db_pool, "test:claim", "d-1") is True
    assert await claim_idempotency(db_pool, "test:claim", "d-1") is False
    # A different id is a different delivery.
    assert await claim_idempotency(db_pool, "test:claim", "d-2") is True
    await db_pool.execute("DELETE FROM ingest_idempotency WHERE source_type = 'test:claim'")
