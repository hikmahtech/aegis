"""Channel-neutral base types for the comms seam.

`DeliveryRef` is the channel-neutral handle to a sent message (for edit/delete);
`SendResult` carries the outcome + back-compat mirror of the legacy top-level
dict keys; `CardSpec` is the neutral interaction-card spec. Slack is the only
channel today (`SlackAdapter`); these types stay channel-neutral so a future
channel can re-use them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class DeliveryRef:
    """Channel-neutral handle to a sent message (for edit/delete)."""

    adapter: str  # e.g. "slack" | "web"
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"adapter": self.adapter, **self.data}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DeliveryRef:
        d = dict(d or {})
        adapter = d.pop("adapter", "")
        return cls(adapter=adapter, data=d)


@dataclass
class SendResult:
    ok: bool
    ref: DeliveryRef | None = None
    used_html: bool = True
    error: str | None = None

    def to_response(self) -> dict[str, Any]:
        out: dict[str, Any] = {"ok": self.ok, "used_html": self.used_html}
        if self.ref:
            out["delivery_ref"] = self.ref.to_dict()
            # Back-compat mirror so existing dispatch-logging keeps working.
            out.update(self.ref.data)
        if self.error:
            out["error"] = self.error
        return out


@dataclass
class CardSpec:
    interaction_id: str
    agent_id: str
    kind: str  # approval|choice|ack|input|draft_review
    prompt: str
    options: dict[str, Any] | None
    allow_hint: bool = False
