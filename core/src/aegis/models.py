"""AEGIS v2 domain models."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class AlertSeverity(StrEnum):
    CRITICAL = "critical"
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class AlertPayload(BaseModel):
    """Normalized alert from any source (Sentry, Grafana, AlertManager, generic)."""

    source: str
    title: str
    severity: AlertSeverity
    service: str = ""
    environment: str = ""
    fingerprint: str = ""
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    resolved: bool = False
