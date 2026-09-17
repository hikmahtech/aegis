"""Fixtures shared by all tests/core/ subdirectories.

Child conftests inherit these. The real-Postgres `db_pool` fixture lives in
the root tests/conftest.py, which every package's tests share.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from aegis.services.desk_math import Rules


@pytest.fixture
def seeded_desk_config() -> dict:
    """The `trading-desk-daily` config exactly as `config/seed/activities.yaml`
    ships it.

    The desk's code defaults deliberately name no market, no currency and no
    tax law, so the numbers the desk actually produces come from a config row.
    Reading the seed file rather than retyping its values is what makes the
    arithmetic tests evidence that the SHIPPED example still behaves the way it
    always did — retyped constants would agree with themselves for ever while
    the seed drifted.
    """
    path = Path(__file__).resolve().parents[2] / "config" / "seed" / "activities.yaml"
    rows = yaml.safe_load(path.read_text())["activities"]
    return next(r for r in rows if r["slug"] == "trading-desk-daily")["config"]


@pytest.fixture
def seeded_desk_rules(seeded_desk_config) -> Rules:
    """The desk's rules as the seeded example configures them."""
    return Rules.from_config(seeded_desk_config)
