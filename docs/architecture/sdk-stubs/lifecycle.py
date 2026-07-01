"""Reference stub — capability lifecycle, health, and entitlements (``aegis-sdk``).

Non-running reference. See ``docs/architecture/productization.md`` §6.

Productizing a plugin framework means each plugin must be operable as an independent
unit: it can be enabled/disabled, it reports its own health, it can self-test after a
deploy, and — for the paid edge — it can be license-gated. The kernel owns the
lifecycle; plugins implement the hooks declared on their ``Capability``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class State(str, Enum):
    DISCOVERED = "discovered"     # entry point found, manifest read
    VALIDATED = "validated"       # ports + features + sdk_range satisfied
    DISABLED = "disabled"         # kill switch off, or entitlement not held
    ACTIVE = "active"             # wired into chat/worker/api/events
    DEGRADED = "degraded"         # health check failing; tools may be hidden


@dataclass
class Health:
    ok: bool
    detail: str = ""
    checked_at: str = ""


class Entitlements:
    """Pluggable license check. OSS default grants everything; a hosted/premium build
    swaps an adapter that validates a signed license, so a ``Bundle`` only activates the
    capabilities its entitlements unlock. This is the kernel hook the observability
    metering also feeds (usage-based billing without the plugin knowing)."""

    async def holds(self, feature: str) -> bool: ...  # e.g. "premium.exec"


class Lifecycle:
    """Kernel-side, documented here so plugin authors know the guarantees:

      1. DISCOVERED -> VALIDATED: requires_ports / requires_features / sdk_range checked.
         A failure here is a clear boot error naming the missing port/feature — never a
         late AttributeError.
      2. VALIDATED -> DISABLED if settings.enabled is False OR entitlement not held.
      3. -> ACTIVE: tools registered (filtered by permission tier + lens scope), flows
         + activities registered with the worker, schedules reconciled, routes mounted,
         subscriptions bound, read-models registered, ui_pages exposed.
      4. periodic health() -> DEGRADED hides the plugin's tools but leaves its schedules
         running, so a flaky external dependency degrades gracefully instead of
         breaking chat.
      5. self_test() runs in CI and post-deploy as a smoke check.
    """
