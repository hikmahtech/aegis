"""Tests for knowledge decay post-filter."""

import pytest
from aegis.services.chat import DEFAULT_DECAY_WINDOW, _apply_knowledge_decay


def test_recent_item_keeps_high_score():
    """Item with 0 days old retains full similarity score."""
    items = [{"source_type": "chat", "similarity": 0.9, "days_since_referenced": 0}]
    result = _apply_knowledge_decay(items)
    # decay_factor = max(0.1, 1.0 - 0/30) = 1.0
    assert result[0]["effective_score"] == pytest.approx(0.9, abs=0.01)


def test_old_chat_item_reduced():
    """Chat item at 25 days (30-day window) gets reduced score."""
    items = [{"source_type": "chat", "similarity": 0.8, "days_since_referenced": 25}]
    result = _apply_knowledge_decay(items)
    # decay_factor = max(0.1, 1.0 - 25/30) ~= 0.167
    assert result[0]["effective_score"] == pytest.approx(0.8 * (1.0 - 25 / 30), abs=0.01)
    assert result[0]["effective_score"] < 0.8 * 0.5


def test_manual_item_decays_slowly():
    """Manual item at 100 days (365-day window) still has good score."""
    items = [{"source_type": "manual", "similarity": 0.8, "days_since_referenced": 100}]
    result = _apply_knowledge_decay(items)
    # decay_factor = 1.0 - 100/365 ~= 0.726
    expected = 0.8 * (1.0 - 100 / 365)
    assert result[0]["effective_score"] == pytest.approx(expected, abs=0.01)
    # Should still retain majority of score
    assert result[0]["effective_score"] > 0.5


def test_floor_at_0_1():
    """Very old items can't go below floor of 0.1 * similarity."""
    # Chat item 1000 days old → decay_factor floored at 0.1
    items = [{"source_type": "chat", "similarity": 0.9, "days_since_referenced": 1000}]
    result = _apply_knowledge_decay(items)
    # decay_factor = max(0.1, 1.0 - 1000/30) = 0.1
    assert result[0]["effective_score"] == pytest.approx(0.9 * 0.1, abs=0.001)


def test_unknown_source_uses_default_window():
    """Unknown source_type uses DEFAULT_DECAY_WINDOW."""
    days = DEFAULT_DECAY_WINDOW // 2
    items = [{"source_type": "unknown", "similarity": 0.6, "days_since_referenced": days}]
    result = _apply_knowledge_decay(items)
    expected_factor = max(0.1, 1.0 - days / DEFAULT_DECAY_WINDOW)
    assert result[0]["effective_score"] == pytest.approx(0.6 * expected_factor, abs=0.01)


def test_missing_days_defaults_to_fresh():
    """Item without days_since_referenced defaults to 0 days (assumed fresh)."""
    # No days_since_referenced — defaults to 0 (no penalty for unknown age)
    items = [{"source_type": "chat", "similarity": 0.8}]
    result = _apply_knowledge_decay(items)
    # decay_factor = max(0.1, 1.0 - 0/30) = 1.0 → no decay applied
    assert result[0]["effective_score"] == pytest.approx(0.8, abs=0.01)


def test_mutates_items_in_place():
    """_apply_knowledge_decay mutates the list items in place and returns same list."""
    items = [{"source_type": "triage", "similarity": 0.7, "days_since_referenced": 10}]
    result = _apply_knowledge_decay(items)
    assert result is items
    assert "effective_score" in items[0]
