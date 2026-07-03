# SDK stubs (reference)

**Non-running reference stubs** for a target `aegis-sdk` package — a possible
kernel + SDK + capability-plugin redesign. They define the plugin contract and
the provider ports that capabilities code against — implementation-free on purpose
(the real package is carved out in migration step 1).

| File | Defines |
|---|---|
| [`capability.py`](capability.py) | the plugin manifest — `Capability`, `ToolSpec` (with permission tiers), `Schedule`, `Subscription`, `ReadModel`, `UiPage`, `Bundle` |
| [`context.py`](context.py) | `ToolContext` **and** `ActivityContext` (DI for Temporal activities), `ReadModels`, `PortRegistry` |
| [`ports.py`](ports.py) | swappable provider interfaces + **feature negotiation** (`LLMProvider`, `KnowledgeStore`, `DeliveryChannel`, `TaskStore`, `ExecRunner`, `Interactions`, `TranscriptionProvider`, `McpGateway`, `SearchProvider`, `MarketData`, `SurfaceRef`) |
| [`events.py`](events.py) | the `EventBus` + the event catalog as code + the saga/choreography convention |
| [`lifecycle.py`](lifecycle.py) | capability state machine, health/self-test, entitlements |
| [`example_capability.py`](example_capability.py) | a **complete reference capability** exercising every surface — the `aegis new capability` template |

Start with `example_capability.py` — it's the executable spec; the rest are the types
it leans on.
