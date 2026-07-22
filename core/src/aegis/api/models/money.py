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
    # True = subscription/utility that will bill again; False = one-off
    # purchase (e.g. a single Amazon order) that should never be minted as
    # a recurring_charge (#113). None = model didn't answer / pre-fix
    # extraction — upsert_charges treats that conservatively as recurring,
    # preserving prior behaviour for ambiguous cases.
    is_recurring: bool | None = None
