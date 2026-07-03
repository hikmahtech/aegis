"""Reference stub — execution contexts for the target ``aegis-sdk`` package.

Non-running reference. See ``docs/architecture/productization.md`` §5.2.

There are TWO contexts, because AEGIS does work in two places:

  * ``ToolContext``     — a synchronous chat tool or an event handler.
  * ``ActivityContext`` — a Temporal activity (where most ingest/investigation work
                          actually runs). Today the worker bootstrap hand-injects
                          connectors into activity classes; the SDK makes that a
                          first-class, typed contract so a migrated capability's
                          activities get the same ports/DI without importing the kernel.

Both expose the SAME resolved ports, the plugin's own namespaced settings, the event
bus, and the read-model store. A plugin reaches nothing it cannot get from a context.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class PortRegistry(Protocol):
    """Resolved adapters keyed by port name. Concrete impls chosen by config; a
    capability only ever sees the Protocol, never the adapter class."""

    llm: Any            # LLMProvider
    knowledge: Any      # KnowledgeStore
    delivery: Any       # DeliveryChannel
    tasks: Any          # TaskStore
    exec: Any           # ExecRunner
    interactions: Any   # Interactions
    transcription: Any  # TranscriptionProvider
    mcp: Any            # McpGateway
    search: Any         # SearchProvider
    market: Any         # MarketData


class EventBus(Protocol):
    async def emit(self, event: str, payload: dict) -> None: ...


class ReadModels(Protocol):
    """Read-only access to projections OTHER plugins publish (see productization.md §7).

    This is the SANCTIONED cross-plugin read path that replaces 'digests queries the
    money.* tables directly'. A consumer reads a named, versioned projection; it never
    touches another plugin's private schema. Delete the publishing plugin and the
    projection simply goes empty — the law holds."""

    async def get(self, name: str, *, since: Any = None) -> list[dict]: ...


class SettingsView(Protocol):
    """A read/write view scoped to one capability's settings namespace, including its
    capability-level kill switch (``enabled``)."""

    async def get(self, key: str, default: Any = None) -> Any: ...
    async def set(self, key: str, value: Any) -> None: ...


@dataclass
class ToolContext:
    db: Any                       # asyncpg pool, kernel-managed (capability's schema)
    settings: SettingsView        # namespaced to this capability
    ports: PortRegistry
    events: EventBus
    read_models: ReadModels
    lens: str | None = None       # active lens (drives voice); was agent_id
    task_id: str | None = None    # set only on the comment-channel surface
    surface: str = "api"          # "slack" | "todoist_comment" | "api" | "flow"
    # the trust tier the CALLER is allowed to exercise (productization.md §9); the
    # kernel pre-filters tools, this lets an executor double-check before a mutation.
    granted_tier: str = "autonomous"  # "autonomous" | "approval" | "forbidden"


@dataclass
class ActivityContext:
    """Injected into a capability's Temporal activities by the kernel worker host."""

    db: Any
    settings: SettingsView
    ports: PortRegistry
    events: EventBus
    read_models: ReadModels
