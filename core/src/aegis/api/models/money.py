"""Pydantic models for Money Hygiene (Maou) — receipt extraction shape."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, field_validator


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


_KEY_RE = re.compile(r"[^a-z0-9]+")


def payee_key(payee: str) -> str:
    """Lowercase, punctuation to single spaces, trimmed. The matching key for
    rules, curiosity and dues dedupe (spec §2)."""
    return _KEY_RE.sub(" ", (payee or "").lower()).strip()


class MoneyEvent(BaseModel):
    """One money email, parsed (spec §2). The journal is written from this."""

    kind: Literal["transaction", "due", "failed", "info", "ignore"]
    direction: Literal["in", "out"] | None = None
    amount: Decimal | None = None
    currency: str | None = None
    payee: str = ""
    payee_key: str = ""
    channel: Literal[
        "upi", "imps", "neft", "card", "autopay", "remittance",
        "receipt", "bill", "statement", "manual", "other",
    ] = "other"
    instrument: str | None = None
    occurred_on: date | None = None
    due_on: date | None = None
    entity: Literal["personal", "hikmah", "none"] = "personal"
    account: str | None = None
    category: str | None = None
    ref: str | None = None
    is_recurring: bool | None = None
    parser: str = "llm"
    confidence: float = 1.0
    source_class: Literal["bank", "receipt", "other"] = "other"

    @field_validator("amount", mode="after")
    @classmethod
    def _two_places(cls, v: Decimal | None) -> Decimal | None:
        """Amounts are major units at 2 places everywhere — quantize once, here,
        so the journal and every renderer agree."""
        return v.quantize(Decimal("0.01")) if v is not None else v
