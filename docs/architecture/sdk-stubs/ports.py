"""Reference stub — provider ports for the target ``aegis-sdk`` package.

Non-running reference. See ``docs/architecture/productization.md`` §5.3, §6, §13.

Each port is a Protocol with a default OSS adapter and optional premium/external
adapters. Capabilities depend on these interfaces, never on concrete adapters — this
is what lets an install swap the backend (e.g. KnowledgeStore: pgvector default vs an
external API vs an MCP server) purely in config, and is the basis of the OSS-core /
paid-edge split.

FEATURE NEGOTIATION
-------------------
Ports may expose OPTIONAL capabilities. An adapter advertises what it supports via a
class-level ``FEATURES: frozenset[str]``; a capability declares what it needs via
``Capability.requires_features``; the kernel refuses to boot a capability whose
required features aren't satisfied by the configured adapters (a clear error instead
of a runtime ``AttributeError``). This generalises the old "you must have an embedding
model" prerequisite into a first-class, checkable contract — and is what keeps AEGIS
"open to other APIs" honest: a thin external adapter can support ``search`` without
``embed``, and the kernel will simply refuse to enable the plugins that need embed.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class LLMProvider(Protocol):
    FEATURES: frozenset[str]  # {"think", "chat", "tools", "embed"}
    async def think(self, prompt: str, *, model: str, tools: list | None = None) -> Any: ...
    async def chat(self, messages: list, *, model: str, tools: list | None = None) -> Any: ...
    async def embed(self, texts: list[str], *, model: str) -> list[list[float]]: ...  # feature: "embed"


@runtime_checkable
class KnowledgeStore(Protocol):
    FEATURES: frozenset[str]  # {"search", "ask", "ingest", "facts", "write_facts", "contradictions"}
    async def search(
        self, q: str, *, limit: int = 10, source_type: str | None = None, tags: list | None = None
    ) -> list[dict]: ...
    async def ask(self, question: str, *, max_sources: int = 5) -> Any: ...  # feature: "ask"
    async def ingest(
        self, *, url: str, title: str, source_type: str, raw_text: str, tags: list | None = None
    ) -> Any: ...
    async def facts(
        self, *, subject: str | None = None, predicate: str | None = None, object: str | None = None
    ) -> list[dict]: ...  # feature: "facts"
    # write_facts: the claims-ingest path (calendar->claims, runbook updates, auto fact
    # extraction, intel/alert verdict->KG). 9+ live call sites today; a read-only store
    # advertises FEATURES without "write_facts" and the kernel disables plugins needing it.
    async def add_facts(
        self, *, source_url: str, source_type: str, extractor: str, triples: list[dict]
    ) -> Any: ...  # feature: "write_facts"
    async def contradictions(self, *, min_confidence: float = 0.0) -> list[dict]: ...  # feature: "contradictions"


@runtime_checkable
class DeliveryChannel(Protocol):
    FEATURES: frozenset[str]  # {"text", "document", "card", "topics"}
    async def send(self, *, surface: SurfaceRef, text: str, reply_markup: Any = None) -> Any: ...
    async def send_document(self, *, surface: SurfaceRef, files: list, caption: str | None = None) -> Any: ...
    async def send_card(self, *, surface: SurfaceRef, interaction_id: str, prompt: str, options: list) -> Any: ...


@runtime_checkable
class TaskStore(Protocol):
    """The GTD backend. Default: local postgres-gtd. Alt: todoist / linear / jira.

    Project ops are part of the port because ``create_project`` / ``list_projects`` are
    live tools and clarify's project-seed path needs them. ``capture`` carries labels +
    source_tag because the clarify watermark + comment-channel eligibility depend on them.
    """

    FEATURES: frozenset[str]  # {"capture", "projects", "comments", "labels", "outbox"}
    async def capture(self, *, text: str, labels: list, source_tag: str, idempotency_key: str) -> Any: ...
    async def list_open(self, *, filter: dict) -> list[dict]: ...
    async def update(self, *, task_id: str, **changes: Any) -> None: ...
    async def comment(self, *, task_id: str, text: str) -> None: ...
    async def create_project(self, *, name: str, outcome: str | None = None) -> Any: ...  # feature: "projects"
    async def list_projects(self) -> list[dict]: ...  # feature: "projects"


@runtime_checkable
class ExecRunner(Protocol):
    """Remote execution (the node-a node). Default: local subprocess.
    Premium: ssh-node-a running kimi / claude-code / scripts; k8s-job; GH Actions.

    ``target`` is "aegis-self" | a registered repo slug | an infra host.
    ``agent`` is "kimi" | "claude-code". ``mode`` is "investigate" | "fix".
    Sandboxing (per-run git worktree, repo-relevance gate) is the adapter's concern.
    """

    FEATURES: frozenset[str]  # {"script", "agent", "fix_mode", "worktree_sandbox"}
    async def run_script(self, *, script_id: str, args: dict, host: str | None = None) -> Any: ...
    async def run_agent(
        self, *, target: str, prompt: str, agent: str = "kimi", mode: str = "investigate"
    ) -> Any: ...  # feature: "agent"
    async def fetch_output(self, handle: Any) -> Any: ...


@runtime_checkable
class Interactions(Protocol):
    """Human-in-the-loop, exposed to capabilities as a PORT (the primitive lives in the
    kernel, which capabilities may not import). Backs Gate-1/Gate-2 (alerts), low-conf
    clarify cards (gtd), and review acknowledgements (digests). ``await_response`` is
    valid only inside a durable flow; ``timeout_policy`` is "archive" | "hold"."""

    async def request(
        self, *, kind: str, prompt: str, options: list, surface: SurfaceRef,
        timeout_policy: str = "archive", metadata: dict | None = None,
    ) -> str: ...  # returns interaction_id
    async def await_response(self, interaction_id: str, *, timeout_s: int | None = None) -> dict: ...


@runtime_checkable
class TranscriptionProvider(Protocol):
    """Audio/video → text. Default: ElevenLabs Scribe (hosted vendor). Used by the
    knowledge ingest pipeline (voice notes, YouTube) and Slack audio uploads."""

    FEATURES: frozenset[str]  # {"audio", "youtube"}
    async def transcribe(self, *, source: str, kind: str = "audio") -> str: ...


@runtime_checkable
class McpGateway(Protocol):
    """Generic 'call any tool on any configured MCP server'. A kernel-managed extension
    surface (today's mcp_manager + /api/mcp), exposed to capabilities as a port so a
    plugin can lean on external MCP tools without bespoke connectors."""

    async def list_tools(self, server: str) -> list[dict]: ...
    async def call_tool(self, *, server: str, tool: str, args: dict) -> Any: ...


@runtime_checkable
class SearchProvider(Protocol):
    async def search(self, q: str, *, categories: list | None = None, limit: int = 10) -> list[dict]: ...


@runtime_checkable
class MarketData(Protocol):
    async def query(self, sql: str, params: dict | None = None) -> list[dict]: ...


class SurfaceRef(Protocol):
    """An addressable inbound/outbound surface (Telegram topic, web thread, comment
    channel, API). Symmetric: the same ref the kernel resolves an INBOUND message from
    is what a capability replies to — so DeliveryChannel never hardcodes 'telegram'."""

    kind: str   # "telegram" | "web" | "todoist_comment" | "api"
    lens: str   # voice/scope label (was: agent_id)
    ref: str    # channel-specific address (topic id, thread id, task id)
