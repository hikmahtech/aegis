"""Behavior-tag vocabulary — the closed set of tags that drive agent routing.

Tags live in ``agents.capabilities`` (checkbox-edited in the admin Behavior
tab) alongside free-form descriptive entries. Code keys behavior on these
tags — never on literal agent ids — so a fork's custom agent set works by
tagging, not by code edits (issue #36).
"""

from __future__ import annotations

BEHAVIOR_TAGS: dict[str, str] = {
    "gtd": "Owns the GTD layer — task clarify, reviews, Todoist sync and captures.",
    "finance": "Owns money — receipts, subscriptions, budgets and market data.",
    "research": "Owns knowledge — research, RSS/article ingest and lookups.",
    "infra": "Owns infrastructure — homelab/k8s alerts and slow async operations.",
}
