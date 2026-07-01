"""Pydantic models for Money Hygiene (Maou) — receipt extraction shape."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class ReceiptExtraction(BaseModel):
    """Per-receipt structured output from the Haiku batch classifier.

    Used by `LLMClient.extract_receipts_batch` and consumed by
    `MoneyActivities.upsert_charges`.
    """

    is_receipt: bool
    vendor_name: str = ""
    sender_label: str = ""
    category: Literal["domain", "saas", "insurance", "lease", "media", "infra", "other"] = "other"
    amount: float | None = None
    currency: str | None = None
    cadence: Literal["monthly", "quarterly", "yearly", "unknown"] = "unknown"
    next_due_at: str | None = None
    confidence: float = 0.0
