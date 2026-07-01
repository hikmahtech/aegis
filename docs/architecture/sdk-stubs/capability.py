"""Reference stub — the plugin contract for the target ``aegis-sdk`` package.

Non-running reference. See ``docs/architecture/productization.md`` §5.1, §7, §8.

A capability declares itself with a single ``CAPABILITY: Capability`` module-level
object. The kernel discovers it via the ``aegis.capabilities`` entry point, validates
``requires_ports`` + ``requires_features`` against the configured adapters, and
auto-wires tools, flows, schedules, routes, events, read-models, UI pages, and
migrations. This is the ONE place a plugin is registered — replacing today's
CHAT_TOOLS / TOOL_EXECUTORS / AGENT_TOOL_SETS / worker __main__ list / activities.yaml
/ route includes / frontend page registry.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolSpec:
    """A single chat tool: JSON schema + async executor, co-located.

    ``permission`` reclaims the (currently dead) per-agent permission tiers in
    config/personalities.yaml and makes them real + enforced by the kernel:
      * "autonomous" — run without asking (read tools, safe queries);
      * "approval"   — kernel spawns an Interactions card and only runs on approve
                       (restart_service, sync_argocd_app, kimi fix-mode, scale);
      * "forbidden"  — never callable in this install (delete_data, drop_database).
    ``scopes`` lists which lenses surface the tool; empty means all lenses.
    """

    name: str
    schema: dict
    executor: Callable[..., Any]          # async (ctx: ToolContext, args: dict) -> str
    permission: str = "autonomous"        # "autonomous" | "approval" | "forbidden"
    scopes: Sequence[str] = ()


@dataclass(frozen=True)
class Schedule:
    slug: str
    flow: type
    cron: str


@dataclass(frozen=True)
class Subscription:
    """Bind an event name to an async handler ``(ctx, payload) -> None`` — how a plugin
    reacts to another plugin WITHOUT importing it (the decoupling spine)."""

    event: str
    handler: Callable[..., Any]


@dataclass(frozen=True)
class ReadModel:
    """A named, versioned projection this plugin PUBLISHES for others to read via
    ``ctx.read_models.get(name)``. The public contract that replaces direct
    cross-schema queries (productization.md §7)."""

    name: str            # "money.upcoming_renewals"
    version: int         # bump on breaking shape change
    builder: Callable[..., Any]  # async (ctx) -> list[dict]; refreshed on its events


@dataclass(frozen=True)
class UiPage:
    """A page this plugin contributes to the kernel-served Admin shell. The shell is a
    host; plugins ship their own panels (money page, infra page, references page)."""

    slug: str
    title: str
    route: str           # API route the page calls
    icon: str | None = None


@dataclass(frozen=True)
class Capability:
    name: str
    version: str                         # semver; checked against the SDK compat range
    sdk_range: str = ">=1,<2"            # kernel refuses to load on mismatch
    schema: str = ""                     # owned DB schema namespace, e.g. "money"

    requires_ports: Sequence[str] = ()        # ["MarketData", "LLMProvider", ...]
    requires_features: Sequence[str] = ()     # ["LLMProvider:embed", "KnowledgeStore:write_facts"]

    tools: Sequence[ToolSpec] = ()
    flows: Sequence[type] = ()                # Temporal workflow classes
    activities: Sequence[Callable[..., Any]] = ()
    schedules: Sequence[Schedule] = ()
    routes: Any | None = None                 # an APIRouter, mounted in api mode only

    emits: Sequence[str] = ()                 # event names this plugin publishes
    consumes: Sequence[Subscription] = ()
    read_models: Sequence[ReadModel] = ()     # projections this plugin publishes
    ui_pages: Sequence[UiPage] = ()

    migrations_dir: str | None = None
    settings_schema: dict | None = None       # namespaced under the capability name
    lens: str | None = None                   # default voice/scope label (was agent_id)
    voice_md: str | None = None

    # --- Lifecycle hooks (productization.md §6 lifecycle) -----------------------
    health: Callable[..., Any] | None = None      # async (ctx) -> {"ok": bool, ...}
    self_test: Callable[..., Any] | None = None   # async (ctx) -> bool; CI/post-deploy smoke
    entitlement: str | None = None                # license feature gate, e.g. "premium.exec"


@dataclass(frozen=True)
class Bundle:
    """A shippable product = a curated set of capabilities + the adapters that back
    their ports + a default lens. The unit of distribution for the standalone-product
    track (productization.md §9)."""

    name: str                                  # "on-call"
    title: str
    capabilities: Sequence[str]                # ["alerts", "exec", "infra"]
    adapters: dict[str, str] = field(default_factory=dict)  # {"ExecRunner": "ssh-node-a", ...}
    default_lens: str | None = None
    entitlements: Sequence[str] = ()           # what the license unlocks
