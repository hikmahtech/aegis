# What to steal from OpenClaw & the 2026 agent landscape — improvements for AEGIS

*Research date: 2026-06-29. Sources: OpenClaw docs/teardowns, Letta/Mem0/Zep, ACE/TRACE/Reflexion papers, LangChain ambient-agents/Agent-Inbox, OpenAI/Claude/Pydantic AI SDKs, Temporal/Restate/DBOS, n8n/Khoj, Anthropic context-management & sandbox-runtime, agent-safety literature. Full source URLs inline.*

---

## TL;DR verdict

**Do not migrate off AEGIS.** The entire industry converged in 2025–2026 on exactly the thesis AEGIS is built on — *durable execution + scheduled proactivity + structured human-in-the-loop*. OpenClaw is a viral conversational agent with a 30-min in-process heartbeat; AEGIS's Temporal core is **strictly stronger** on the reliability axis, and the Diagrid analysis confirms LangGraph/CrewAI/ADK checkpoints are *weaker* than what you already run ([diagrid.io](https://www.diagrid.io/blog/checkpoints-are-not-durable-execution-why-langgraph-crewai-google-adk-and-others-fall-short-for-production-agent-workflows)).

So this report is about **upgrades on top of the Temporal core**, not replacement. The gaps where the field is genuinely ahead of AEGIS — ranked by how much they'd move the needle:

| # | Gap | One-line | Tier |
|---|-----|----------|------|
| 1 | **Memory that learns** | Your `MEMORY.md` is hand-authored and *read-only at runtime* — agents never improve at "you" | **S** |
| 2 | **Notification discipline** | No budget; flows each decide-to-ping independently → measured ~34% noisy/empty alert flows | **S** |
| 3 | **Agent Inbox + HITL taxonomy** | Cards are ad-hoc Accept/Reject, scattered across 5 channels; no edit-before-approve | **A** |
| 4 | **Real eval** | You have OTel tracing, ~zero eval; KS judge is broken; no regression safety | **A** |
| 5 | **Tool-context scaling** | 38 tools hard-injected; accuracy degrades 95%→71% under tool load | **A** |
| 6 | **Typed agent ergonomics** | Hand-rolled `think()`/JSON-parse/`LLMTruncationError` plumbing | **B** |
| 7 | **Safety hardening** | Shell + PR + money agent reading untrusted alerts/emails = the full "lethal trifecta", no sandbox-by-default | **B** |
| 8 | **PR review surface** | alert→coding-run PRs land raw, no plan/test summary | **B** |

What AEGIS already does **as well as or better than the field** (don't touch): durable scheduled flows, storm-collapse dedup (`build_alert_signature`), `silent` gating + Gate-0/Gate-2, fail-closed `timeout_policy=archive`, per-agent channels, OTel traces, IP-whitelist + Slack-Socket-Mode (no public URL), secrets in env not in skill files.

---

## Scope: AEGIS's three distinct LLM surfaces

Recommendations below target **surface 1 only** unless explicitly noted. Conflating these is easy and wrong:

1. **AEGIS's own reasoning (the target of this report).** `LLMClient.think()/chat()` (`core/src/aegis/llm/__init__.py`) is an `AsyncOpenAI` client pointed at the **homelab LiteLLM proxy**. Plain OpenAI-compatible `chat.completions` — `tools=[...]` + `tool_choice="auto"` for the agent chat personas, prompt+`max_tokens` for extraction. Models are tiers **fast=`gemma4:e2b`, balanced=`gpt-oss:20b`, smart=`claude-sonnet` (a LiteLLM alias)** — i.e. **mostly small local open models**. This powers clarify, intel scoring, alert reasoning, receipt extraction, briefings, and the sebas/raphael/maou/pandora Slack chat. **Consequences that shape every recommendation:** (a) no Anthropic-API features (tool-search beta, memory tool, context-editing, Agent Skills) are available on this path — they only exist when talking to Claude *via the Anthropic API*, which AEGIS doesn't; (b) small models are *weaker and flakier* at tool-selection and structured output (hence your existing `LLMTruncationError`, empty-content-at-tight-`max_tokens`, gpt-oss reasoning-budget dance); (c) you swap/tune models and thresholds constantly. So the levers are **own-built memory/eval/tool-scoping over the OpenAI-compatible API + LiteLLM's own proxy features**, never "adopt a frontier-model API capability."

2. **The external coding agents (orchestration target, mostly out of scope).** `remote_script.py::start_kimi_run` / `_engine_for` spawns the **claude CLI or kimi CLI** on a remote host (asif/meem) over SSH+tmux to do code work and open a PR. These have their *own* harness, context, skills (SKILL.md), and memory — AEGIS just launches them and reads their output. Their internals (Agent Skills, Claude Agent SDK hooks, sandbox-runtime) are *their* concern, not "how AEGIS uses LLM." Anything about them is flagged **[external coding layer]** and collected in the appendix.

3. **Claude Code (this dev session).** The harness *I* run in. Its tool-search/deferred-loading, Agent Skills, memory tool, permission modes, sandbox-runtime are **not AEGIS runtime** — my first draft wrongly imported some of these. Corrected below.

---

## The big strategic finding

The market caught up to AEGIS. Temporal itself pivoted into "durable agents" in 2026 (OpenAI Agents SDK integration, Workflow Streams, Standalone Activities — [temporal.io](https://temporal.io/blog/announcing-openai-agents-sdk-integration)). Inngest/Trigger.dev/Restate/DBOS are **peers, not upgrades**. LangGraph/CrewAI/ADK are weaker (state-checkpoint ≠ durable execution). OpenClaw's own studied lesson is *"the skills aren't the magic, the agent loop is"* ([agor.live](https://agor.live/blog/openclaw)) — and AEGIS's loop (Temporal) is the part that's hard, and you already have it.

**Your moat is the durable core + deep personal integration** (Swarm, alertmanager, Sentry, your repos, Todoist-GTD, LiteLLM). The needle-movers are all things you can bolt *on top*.

---

## TIER S — transformational

### 1. Give agents memory that actually learns

**The gap (verified in code):** `personalities/<agent>/MEMORY.md` is hand-authored and **read-only at runtime** — `grep` finds zero code paths that write it. cmemory `save_lesson` appends but never reconciles. So your agents never get better at *you*; you curate their memory by hand forever. This is the single biggest lever.

**What to steal (a layered upgrade, cheapest-first):**

- **Consolidation classifier, not append** — when a new lesson/fact arrives, an LLM step classifies it **ADD / UPDATE-if-more-informative / DELETE-if-contradicted / NOOP** against existing memory (Mem0; reports +26% over OpenAI memory on LOCOMO, 90% fewer tokens — [mem0.ai/research](https://mem0.ai/research)). Stops `MEMORY.md`/cmemory from growing monotonically and rotting.
- **Recency × importance × relevance retrieval**, with an importance score assigned *at write time* (Generative Agents, the de-facto default — [arXiv 2304.03442](https://arxiv.org/abs/2304.03442)). Cheap; prevents a long lesson store from drowning the prompt.
- **Reflector/Curator split, merge as small deltas** — separate the agent that *does* the task from a *Reflector* (extracts the lesson) and a *Curator* (merges it as a small delta item). **Never full-rewrite the memory file** — that causes "context collapse" and "brevity bias," the exact failure mode your auto-managed CLAUDE.md profile is prone to. ACE: +10.6% on AppWorld, no fine-tuning ([arXiv 2510.04618](https://arxiv.org/abs/2510.04618)).
- **Nightly "reflection/dreaming" flow** — a scheduled Temporal flow that reads the day's interactions + corrections, synthesizes a few high-quality durable lessons, and prunes/merges old ones (Generative Agents reflection; OpenAI "Dreaming" — [openai.com](https://openai.com/index/chatgpt-memory-dreaming/)). You already have the scheduler; this is "just" a new flow.
- **Compile hot corrections into enforced gates (TRACE, Jun 2026)** — a remembered rule gets silently violated next session. For high-value prefs ("no co-author line", commit style, worktree policy), compile them into a pre-completion *check that must pass*, not a hope the model re-read the rule ([arXiv 2606.13174](https://arxiv.org/abs/2606.13174)).
- **Valid-time / supersession for changing prefs (Zep/Graphiti)** — tag a preference "true as of <date>"; when it flips, invalidate rather than delete, so the agent never re-applies a retired preference but you keep history ([agenticwire](https://www.agenticwire.news/article/mem0-zep-letta-agent-memory)).
- **Pre-compaction "silent turn" (OpenClaw)** — before a chat session truncates, inject a turn that forces "write durable facts to memory now," so the agent never silently forgets mid-conversation ([docs.openclaw.ai/concepts/memory](https://docs.openclaw.ai/concepts/memory)).

**Cheapest entry point (do this first):** you're ~90% built already. Every time you **Reject/Edit an `interactions` card with a reason**, that reason is a labelled correction — auto-`save_lesson` it (tagged flow+agent), and have the relevant flow `search_lessons` at the start of its next run. Example: a PR-opening run rejected "wrong base branch" persists that and stops repeating it. The data already flows through your `interactions` table + you already have `cmemory` (`save_lesson`/`search_lessons`/`reject_lesson`) + per-agent `MEMORY.md` — this is the lowest-effort path to "gets better at MY preferences" and it seeds the consolidation/reflection machinery above.

**Where it lands:** close the loop from `interactions` first (above); then new `activities/memory.py` (consolidation classifier + retrieval scorer), a `MemoryReflectionFlow`, a per-agent writable memory store (Postgres table or the existing pgvector), and a watermark on corrections. **Keep human-auditable** (see Safety #7 — agent-writable memory is the OpenClaw memory-poisoning vector).

**Trust boundary:** AEGIS memory storage is covered (Postgres+pgvector, knowledge-service). This is purely the *learning loop* on top. Study Letta's self-editing tiered core/archival blocks as the reference model ([github.com/letta-ai/letta](https://github.com/letta-ai/letta)).

### 2. A notification budget + value/risk gate (kill the noise)

**The gap:** your N radar flows each decide-to-notify independently; you measured ~34% alert-flow timeouts + a tail of flows that poll and produce nothing. There is no global "is this worth interrupting Arshad?" gate.

**What to steal:**

- **Hard daily notification budget: 3–5 proactive pushes across ALL four agents combined**, treated as a cognitive limit, not a preference (users already get 46–63 push/day; ~23 min recovery per interruption; ~50% of users who mute an app eventually churn). Optimize **"acted-on rate weighted by importance," NOT "notifications sent"** — a dismissed-unopened ping is *worse than silence* ([tianpan.co Notification Budget](https://tianpan.co/blog/2026-05-13-background-agents-notification-budget-attention-economy)).
- **`HEARTBEAT_OK` silent-default gate (OpenClaw)** — one periodic "review everything pending; is anything worth a message? if not, emit `HEARTBEAT_OK` and the dispatcher swallows it." 47 of 48 beats stay silent. A unified proactivity gate above your per-flow decisions ([openclawplaybook](https://www.openclawplaybook.ai/guides/openclaw-heartbeat-md-guide/)).
- **Value-vs-attention scoring before any real-time push:** `score = P(Arshad acts) × action_value − attention_cost`; only above-bar candidates interrupt, the rest **defer to the daily digest**. Learn the threshold from his own act-vs-dismiss history — you already store interaction outcomes. Beats a global `significance_threshold` (PersoNo direction — [arXiv 2508.19622](https://arxiv.org/pdf/2508.19622)).
- **Correlate before you interrupt** — collapse many low-confidence signals into one high-confidence incident and notify once. You already do this for alerts (`build_alert_signature`); extend "synthesize, don't relay" to *all* proactive surfaces ([incident.io](https://incident.io/blog/alert-fatigue-solutions-for-dev-ops-teams-in-2025-what-works)).
- **Bounded-deferral / breakpoint timing** — hold non-urgent pushes; release at a predictable breakpoint (morning brief). Suppress when he just dismissed several recent notifications (recent-volume = low receptivity — [ScienceDirect](https://www.sciencedirect.com/science/article/abs/pii/S1574119217300640)).

**Where it lands:** a `notification_budget` table + a `should_notify(candidate)` gate activity that every flow calls before dispatching, instead of each flow dispatching directly; the daily/weekly review cards become the carrier for everything held back.

---

## TIER A — high impact

### 3. A unified Agent Inbox + standard HITL action taxonomy

**The gap:** your Slack cards are per-flow ad-hoc, Accept/Reject only, scattered across five `#aegis-*` channels. Research is unanimous that *consolidation beats scatter* for fighting fatigue.

**What to steal:**

- **One HITL action vocabulary on every card: Approve / Edit / Respond / Notify** — where **Edit = edit the proposed tool args before they execute**, not a free-text reply (LangChain Agent Inbox — [github.com/langchain-ai/agent-inbox](https://github.com/langchain-ai/agent-inbox)). You're missing Edit and Notify-only entirely.
- **Per-tool-call / per-action approval that renders the exact action + args** (n8n HITL tools — [docs.n8n.io](https://docs.n8n.io/advanced-ai/human-in-the-loop-tools/)) — approve the *specific action*, not the whole flow. More granular than Gate-0/Gate-2.
- **A consolidated "needs-you" surface** — an admin-panel page (you already have the SPA) aggregating all pending `interactions` rows with the 4-verb schema, plus a pinned "3 things waiting on you" Slack summary. Your highest-leverage UX steal ([langchain.com/blog/introducing-ambient-agents](https://www.langchain.com/blog/introducing-ambient-agents)).
- **Feed denials back as learning** — when Arshad rejects/edits, persist the reason and surface it to the agent next time (HumanLayer denial→context loop — [github.com/humanlayer/humanlayer](https://github.com/humanlayer/humanlayer)). Ties into Tier-S #1.

**Where it lands:** extend the `interactions` schema (action_type, proposed_args, edited_args), a new admin-panel `/admin/inbox` page, and the comms card builder. Your `timeout_policy=archive` fail-closed default already matches best practice — keep it.

### 4. Real evaluation (your biggest *capability* gap vs the field)

**The gap:** you have tracing, near-zero eval. The KS judge is broken (kimi truncation, contaminated 0.000 scores). No regression safety when you change a prompt/model/threshold.

**What to steal:**

- **Langfuse, self-hosted** (Docker/K8s, traces stay in your infra, OTel-native ingest, datasets + LLM-as-judge + online evals) — slots directly under your existing OTel exporter ([langfuse.com](https://langfuse.com/integrations/native/opentelemetry)). Arize Phoenix is the close second self-hostable option.
- **OTel GenAI semantic conventions** (`gen_ai.*` agent/tool/LLM spans) — you already emit OTel; adding `gen_ai.*` attributes to your `llm.call` span is low-cost and future-proof ([opentelemetry.io](https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-agent-spans/)).
- **Ragas / DeepEval** — battle-tested faithfulness/correctness scorers to replace the hand-rolled KS judge prompt that's currently broken.

- **You already own half the wiring: LiteLLM has a native Langfuse callback + OTel callback.** Point the proxy at self-hosted Langfuse and *every* AEGIS LLM call (all tiers, every flow) is traced+costed at the proxy with near-zero app changes — then add app-side spans for flow context. This is *more* apt for AEGIS than for the frontier-API shops the research describes, because all your calls already funnel through one proxy.

**Why this matters more for AEGIS than for anyone:** your cmemory history is a graveyard of blind model-swap tuning — gemma4 empty-content at tight `max_tokens`, gpt-oss hangs, `significance_threshold` 5→4 by feel, the contaminated 0.000 KS judge. You change small models and thresholds constantly and have **no regression harness**. Eval is the instrument that turns all of that from vibes into measured deltas.

**Where it lands:** self-hosted Langfuse in the Swarm stack + the **LiteLLM proxy Langfuse/OTel callback** (biggest bang); `gen_ai.*` attrs in `telemetry.py`/`LLMClient`; swap the KS eval judge for Ragas/DeepEval; a small golden-set regression run before any model/threshold change.

### 5. Keep the tool surface small — and own the scaling mechanism

**The gap & the correction:** the *principle* (don't dump every tool schema; degraded selection under tool load is real) holds — but the mechanism I first cited (Anthropic's `advanced-tool-use-2025-11-20` Tool Search Tool, the `defer_loading` MCP beta, the Opus-4.5 "accuracy went up" numbers) is **Claude-Code/Anthropic-API-only and does NOT apply to AEGIS.** AEGIS sends `tools=[...]` over OpenAI-compatible function-calling to **gpt-oss:20b / gemma4:e2b** via LiteLLM. Two real implications:

- **Small models degrade *faster* under tool load than frontier models** — so your existing **per-agent `AGENT_TOOL_SETS` scoping is the correct primary lever, and it matters more here than the research's frontier-model numbers suggest.** Keep each agent's tool list tight; that's already your best defense. Don't chase a token-savings figure measured on Opus — it won't transfer to gemma/gpt-oss.
- **If tool count per agent ever grows past what a small model selects reliably, build your *own* tool-RAG** — embed the 38 tool descriptions in pgvector (you already run it), retrieve top-k by the incoming message, and assemble only those into the `chat.completions` `tools=` array. This is the `langgraph-bigtool` *pattern* (semantic tool retrieval), implemented in `core/src/aegis/services/chat.py` — **not** a dependency or an API feature. Model-agnostic, you own it, works over LiteLLM.
- **Binary/env/config eligibility gating (from OpenClaw)** — don't advertise the `clickhouse`/`gh`/ssh-backed tool to an agent on a host lacking its prereq; auto-hide instead of erroring at call time. Cheap, model-independent.

**Where it lands:** `core/src/aegis/services/chat.py` tool-assembly path (tight per-agent sets now; pgvector tool-RAG only if a set outgrows the model); an eligibility predicate on connectors. *(Note: SKILL.md / Agent Skills progressive disclosure is real but belongs to the [external coding layer] — see appendix — not AEGIS's LiteLLM flows.)*

---

## TIER B — strong, scoped

### 6. Pydantic AI on top of Temporal (typed ergonomics)

The single most stack-aligned framework: Python, typed agents, structured-output-by-construction, and **`durable_exec` integrations for Temporal/DBOS** — typed ergonomics *on top of* your durability, no execution-layer conflict, and it talks to **any OpenAI-compatible endpoint (so: your LiteLLM proxy, any tier)** ([pydantic.dev](https://pydantic.dev/articles/pydantic-ai-dbos)). Worth a prototype to kill the manual `think()`/JSON-parse/`LLMTruncationError` plumbing. **Caveat for AEGIS's small models:** structured-output/tool reliability depends on the model — gpt-oss:20b/gemma4:e2b will need JSON-mode or schema-coaxing and may still need your truncation/retry guards; validate against the *actual* tiers before ripping out the hand-rolled parsing, and consider routing structured-extraction to the smart tier. Also borrow the **handoff-as-tool-call + input/output guardrail** *concepts* (OpenAI Agents SDK) to formalize sebas→maou→pandora routing vs `clarify.py` branching — as patterns over LiteLLM, not necessarily the SDK itself. *(The Claude Agent SDK's hooks + subagent isolation are relevant only to the **[external coding layer]** — the claude-CLI runs — not AEGIS's own reasoning; see appendix.)*

### 7. Safety hardening for an agent with real power

AEGIS's *own* LLM calls read untrusted content (Sentry/alertmanager payloads, receipt emails, intel-scan web text) and feed it to gpt-oss/gemma — the textbook **"lethal trifecta"** (untrusted input + private data + exfil channel). You're structurally safer than OpenClaw already (single-user, IP-whitelist, no public URL, secrets in env). These apply to **AEGIS's own reasoning path (surface 1)** and are model-agnostic:

- **Adopt the "Rule of Two" as the explicit human-gate invariant (do this first — cheapest, biggest blast-radius cut).** An agent should hold at most two of: {processes untrusted input, accesses sensitive systems, changes external state}. All three ⇒ *mandate a human card*. Your alert→shell→PR flows are the textbook all-three case — this turns your ad-hoc Gate-0/Gate-2 into one legible safety rule ([Meta Rule of Two](https://zylos.ai/research/2026-04-12-indirect-prompt-injection-defenses-agents-untrusted-content/)).
- **Spotlight all untrusted content before it enters a LiteLLM prompt (trivial, do now).** Wrap alert payloads, email bodies, web/intel-scan text in randomized delimiters (`<UNTRUSTED_af7b3k>…`) marked as *data, not instructions*, before they hit `investigate()`/`classify_email`. You already `html.escape()` for Telegram; this is the LLM-context equivalent and the cheapest injection defense ([OWASP LLM01](https://genai.owasp.org/llmrisk/llm01-prompt-injection/)). *(Note: small models follow injected instructions more readily than frontier models, so this matters more for AEGIS, not less.)*
- **Dual-LLM / CaMeL data-flow tagging** for untrusted-content reads (CaMeL hits 67% on AgentDojo) — a privileged planner call + a quarantined call that processes the untrusted body but holds no tools. Implementable as two LiteLLM calls; no special model needed.
- **Spend governor + kill switch — and you mostly already own the lever: LiteLLM.** The LiteLLM proxy has **native per-virtual-key budgets, spend tracking, rate limits, and fallbacks** — configure a hard ceiling + alert there rather than hand-rolling, plus an app-side pre-call budget check for runaway loops (you've already hit gpt-oss hang loops; the documented $47K runaway incident is the worst case).
- **Treat any behavior-steering markdown (SOUL/MEMORY/personality, `save_lesson` writes) as a trust boundary** — the moment Tier-S #1 makes memory agent-writable, you've created the memory-poisoning vector unless writes stay human-auditable.

**[external coding layer]** — sandboxing the claude/kimi CLI runs (sandbox-by-default à la Codex/Anthropic `sandbox-runtime`, network-off + domain allowlist, ephemeral split read/write PR tokens, branch protection) is real and worth doing, but it's hardening *that external tool*, not "how AEGIS uses LLM." Tracked in the appendix.

### 8. Pre-merge review card for alert→coding-run PRs *(AEGIS-side surface over an [external coding layer] output)*

Jules/Codex/Devin all surface a **diff + plan + test results** before merge; your PRs land raw. The *run* is external (claude/kimi CLI), but **AEGIS owns the dispatch** — have AEGIS attach the run's plan + test output as the PR body / a pre-merge Slack review card ([jules.google](https://jules.google/)). This is AEGIS's HITL surface, so it's in scope even though the code work isn't.

### 9–11. Smaller formalizations

- **NL-authored schedules (Khoj)** — "every Monday summarize X" → generates an `activities` row, instead of editing seed YAML + DB by hand ([khoj.dev](https://khoj.dev/)).
- **Named-token HITL primitive** (`wait.forToken`/awakeable) — a typed, addressable "parked on token X" object replacing ad-hoc `submit_response` signals ([docs.restate.dev](https://docs.restate.dev/ai/patterns/durable-agents)).
- **Per-entity serialized mailbox** (Restate virtual objects) — formalize your `build_alert_signature` + `WorkflowAlreadyStartedError` per-repo dedup into a first-class key; and **per-session-lane command serialization** (OpenClaw) to kill the concurrent-tool race class (the bug you hit in the active-work guard, PR #322).

---

## What to explicitly NOT do

- **Don't migrate to OpenClaw / LangGraph / CrewAI / ADK / Inngest / Trigger.dev / Restate / DBOS / Cloudflare.** Your Temporal core is stronger or equal; these are lateral moves at best, downgrades at worst.
- **Don't build a ClawHub-style unsigned skill registry.** 1-week-old-GitHub-account publishing → 341 malicious skills. If you ever add a registry, require signing + review.
- **Don't chase weight-editing self-improvement (SEAL, Gödel Agent).** Wrong tier for personal preference-learning — training infra + stability/safety risk for capability gains you don't need. The runtime memory + reflection + enforced-rules stack (Tier-S #1) is the proven, low-risk path.
- **Don't chase edge/multi-tenant (Cloudflare Agents).** Irrelevant to a self-hosted homelab.

---

## Recommended sequencing (first three moves)

1. **Memory consolidation + nightly reflection flow (Tier-S #1, partial).** Start with the Mem0-style ADD/UPDATE/DELETE/NOOP classifier on cmemory/`MEMORY.md` writes + a `MemoryReflectionFlow`. Biggest long-term payoff; agents start getting better at you. Keep writes human-auditable.
2. **Notification budget gate (Tier-S #2).** A `should_notify()` gate + daily-digest carrier. Immediate quality-of-life; directly kills the ~34% noise you measured. Small, high-ROI.
3. **Langfuse + OTel GenAI attrs (Tier-A #4).** Stand up self-hosted eval so every subsequent change (incl. #1 and #2) is measurable instead of vibes. Unblocks fixing the broken KS judge.

**Parallel cheap wins (near-zero effort, do alongside):** the **Rule of Two** human-gate invariant and **spotlighting** untrusted content (Tier-B #7) are a few hours each and cut your biggest safety blast-radius — don't queue them behind the bigger items.

Then: Agent Inbox UX (#3), deferred tool loading (#5), Pydantic AI prototype (#6), rest of safety hardening (#7).

---

## Appendix — the [external coding layer] (separate concern, not "how AEGIS uses LLM")

These ideas harden the **claude/kimi CLI runs** that `remote_script.py` launches over SSH+tmux. They're worth doing operationally, but they're about *that external tool*, not AEGIS's own LiteLLM reasoning — kept out of the main ranking per the scope correction.

- **Agent Skills (SKILL.md) progressive disclosure** — the external claude/kimi CLIs natively support `SKILL.md` (3-tier: name+description → full body → bundled scripts). If you want the *coding runs* to follow fragile runbooks reliably, package those as skills *in the repos the CLI operates on* — but this is configuring claude/kimi, not AEGIS. (For AEGIS's own flows, the equivalent is just keeping system prompts lean and loading runbook detail conditionally in your own prompt construction — see Tier-A #5.)
- **Claude Agent SDK hooks + subagent isolation** — deterministic pre/post-tool gates and isolated subagents for the coding runs; again, a property of the external harness.
- **Sandbox-by-default for the runs** — network-off + domain-allowlist egress proxy (Codex `workspace-write`, Anthropic `sandbox-runtime`), so an injected instruction in an alert body can't make the *coding agent* exfiltrate. Inverse of OpenClaw's opt-in sandbox.
- **PR-path credential hygiene** — ephemeral/split read-write tokens, branch protection + required review, first-time-contributor gates on the bot's PRs.
- **Plan→validate→execute artifact** — have the run emit a structured plan, validate with a deterministic script, then apply; gives AEGIS a dry-run surface to show in the review card (#8).

*Methodology: seven parallel research agents (OpenClaw teardown; agent-memory SOTA; durable-orchestration landscape; HITL + notification quality; skills/tool disclosure; agent self-improvement; safety + voice), synthesized against the live AEGIS codebase (26 flows, activities, connectors, personalities — confirmed `MEMORY.md` is read-only, on migration 022). Full per-thread reports with all source URLs are in the session transcript.*
