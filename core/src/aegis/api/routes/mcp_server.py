"""MCP **server** — AEGIS's own chat tools, served over streamable HTTP.

The mirror image of :mod:`aegis.mcp_manager` (the client). One endpoint,
``POST /api/mcp-server/{agent_id}``, speaks JSON-RPC 2.0 so an external agent
harness — ``claude`` / ``kimi`` CLI headless runs, Claude Desktop — can mount
AEGIS's GTD / knowledge / infra / money tools natively instead of shelling back
into the chat API. ``routes/mcp.py`` is the *client* admin surface and is
untouched by this module.

Serving tools to a third-party harness is a door into this AEGIS, so it is shut
by default and narrow when open:

* **Off by default.** ``settings.mcp_server_enabled`` (``AEGIS_MCP_SERVER_ENABLED``)
  gates every method, matching the client's default-deny posture. Off ⇒ 403.
* **Repo-standard auth.** The router carries the same ``verify_auth`` dependency
  as every other authenticated route — API key or Basic, no new scheme.
* **Per-agent tool gating.** The URL names an agent and the served *chat* tools
  are exactly that agent's ``metadata.tool_set`` (via ``_get_agent_tools``), so a
  mounted server can never reach past what that agent may already do in chat.
  ``_UNSERVED_TOOLS`` is removed on top of that — the MCP passthrough and every
  tool that spawns another agent run (see the constant). The one addition is
  ``approve_tool_use`` (below), which grants nothing.
* **Stateless.** No ``Mcp-Session-Id`` is ever issued and one sent by a client is
  ignored, so there is no server-side session to hijack, expire or leak. ``GET``
  is 405 (no server-initiated streams) and ``DELETE`` is a 204 no-op.

Responses are always ``application/json``; SSE is spec-legal but never used.
JSON-RPC *application* errors travel as a 200 with an ``error`` envelope on
purpose — a non-2xx status is a transport failure to a compliant client
(``mcp_manager._post`` raises before it ever decodes the body), which would hide
the message the caller needs.

One tool served here is **not** a chat tool: ``approve_tool_use``. It is the
target of a gated run's ``--permission-prompt-tool`` flag — the claude CLI calls
it instead of prompting a terminal nobody is sitting at, and blocks on the
answer. The handler raises an ``interactions`` card (the repo's universal HITL
primitive) and returns the operator's verdict. It is deliberately absent from
``CHAT_TOOLS``/``TOOL_EXECUTORS``: it is a transport-level permission gate, not
something an agent may call in chat.

**The gated endpoint** ``POST /api/mcp-server/{agent_id}/gated`` is the same
server with enforcement moved INSIDE it (issue #294). Live E2E on 2026-08-13
showed the claude CLI (2.1.231) executing ``mcp__aegis__capture_to_inbox`` in a
gated run with zero approval cards: tools from an explicitly-passed
``--mcp-config`` are trusted in ``-p`` mode and never reach
``--permission-prompt-tool``. A permission model that lives in the CLI's policy
is therefore not a permission model at all for AEGIS's own tools, so the gated
mount points here instead: every tool NOT in ``_READ_ONLY_TOOLS`` needs an
operator approval before this process will execute it, whatever the CLI thinks.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from jsonschema.exceptions import ValidationError as JSONSchemaValidationError

from aegis.api.auth import verify_auth
from aegis.api.deps import get_settings
from aegis.config import Settings
from aegis.mcp_manager import _PROTOCOL_VERSION as MCP_PROTOCOL_VERSION
from aegis.services.agents import get_agent
from aegis.services.chat import (
    _MCP_TOOL_NAME,
    _TOOL_TIMEOUT_OVERRIDES,
    _execute_tool,
    _get_agent_tools,
    _schema_hint,
    _truncate_result,
    _truncate_text,
    _validate_tool_args,
)
from aegis.services.tools.base import ToolContext

logger = structlog.get_logger()

_SERVER_NAME = "aegis"

# JSON-RPC 2.0 reserved error codes.
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602

# Tool results are consumed by an *engine* (a claude/kimi CLI run with a
# 200k-token context), not by the small local model driving the chat loop, so
# the 4 KB chat cap (settings.tool_result_max_bytes) would throw away output
# that is genuinely useful here. Still bounded — an unbounded result is a way
# to blow up the caller's context with one call.
_TOOL_RESULT_MAX_BYTES = 65_536

# Cap on the text of an executor failure echoed back to the caller.
_ERROR_CHARS = 300

# Chat tools that are NEVER served here, however the agent's `tool_set` reads.
#
# `call_mcp_tool` is the MCP *client* passthrough: serving it would let a
# mounted client drive AEGIS's own client at a third-party server on its behalf
# (confused deputy).
#
# The other three each START ANOTHER CLI RUN — and a run's mount carries the
# same tool set, so a mounted run calling one of them spawns a run that can
# spawn a run. There is no depth counter anywhere in that loop: the only brake
# is the coding host's tmux window cap (10), past which launches fall through
# to detached `nohup` and stop being bounded at all. Each level is a billed
# agent session, so this is removed at the source rather than rate-limited.
#
# Both halves of the door are closed by ONE derivation: `_served_tools` is what
# `tools/list` advertises AND what `tools/call` authorizes against, so a name
# missing from that list is unreachable, not merely unadvertised.
_UNSERVED_TOOLS = frozenset(
    {
        _MCP_TOOL_NAME,
        "dispatch_agent_run",
        "aegis_self_diagnose",
        "investigate_resource",
    }
)

# ── the permission gate (gated agent runs) ─────────────────────────────────
#
# `connectors/remote_script.py` launches a gated run with
# `--permission-prompt-tool mcp__aegis__approve_tool_use`. Both halves of that
# string are load-bearing: `aegis` is the server key in the run's MCP config,
# and this is the tool name. `tests/core/test_kimi_connector.py` asserts the two
# modules still agree.
APPROVAL_TOOL_NAME = "approve_tool_use"

# How long we hold the CLI's permission call open waiting for a human.
#
# It MUST stay below the `MCP_TOOL_TIMEOUT` the gated launch sets (600_000 ms =
# 10 min). If the CLI's timeout fired first, a slow operator would surface as a
# transport failure inside the run instead of a clean deny, and the card would
# be answered into the void. 9 min leaves a minute of headroom.
AGENT_RUN_APPROVAL_TIMEOUT_S = 540

# Bytes of the tool input shown on the card. A `Write` carries a whole file and
# a card is one chat message — but the operator still has to see enough to
# judge, so this is a plain head-cut, never a structural shrink that could drop
# the very key (`command`, `file_path`) the decision turns on.
_APPROVAL_PREVIEW_BYTES = 800

# Response values that mean "allow". Everything else — including a malformed or
# missing value — is a deny, because this is a permission gate.
_APPROVE_VALUES = frozenset({"approve", "approved", "allow"})

# Card buttons. `kind="choice"` rather than `kind="approval"` on purpose: the
# Slack renderer emits the option KEY as the response value for a choice, and so
# does the admin panel, so both surfaces answer `approve`/`deny`. The built-in
# `approval` kind disagrees across the two (`approve`/`reject` in Slack,
# `approved`/`rejected` in the admin panel) — harmless for a deny, but an
# approval that arrives under an unexpected name would be silently downgraded.
_APPROVAL_OPTIONS = {"approve": "✅ Approve", "deny": "⛔ Deny"}

_APPROVAL_TOOL_DESCRIPTOR = {
    "name": APPROVAL_TOOL_NAME,
    "description": (
        "Permission gate for a gated AEGIS agent run. The claude CLI calls this "
        "automatically via --permission-prompt-tool when it wants to use a tool that "
        "is not auto-allowed; it asks a human in the operator's chat channel and "
        "returns their verdict. Do NOT call it yourself — it grants nothing and only "
        "interrupts a person."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "tool_name": {
                "type": "string",
                "description": "The tool the run wants to use, e.g. 'Bash' or 'Write'.",
            },
            "input": {
                "type": "object",
                "description": "The arguments the run wants to call that tool with.",
            },
            "tool_use_id": {
                "type": "string",
                "description": "Optional correlation id for the pending tool use.",
            },
        },
        "required": ["tool_name", "input"],
    },
}


# ── server-side gating for the /gated endpoint (issue #294) ────────────────
#
# v1 of the read-only/mutating classification #289 asks for. It is a CURATED
# frozenset rather than metadata on `CHAT_TOOLS` because the registry has no
# such field yet, and it is an ALLOW-list on purpose: a tool that is not named
# here needs an approval, so a tool added tomorrow — or renamed — is gated by
# construction instead of quietly auto-allowed.
#
# Every entry below was read at its executor (`TOOL_EXECUTORS[name]`) and does
# nothing but SELECT / fetch / synthesise. Deliberately excluded despite being
# "reads":
#   * `youtube_transcript` / `pdf_to_text` — both end in `_deliver_documents`,
#     which POSTS a file into the operator's channel. Harmless, but not a read.
#   * every infra / cloud / vercel tool — `list_nodes` and friends run a shell
#     script on the script host through the same `_INFRA_SPECS` pipeline as
#     `restart_service`, so "read-only" there is a property of the remote
#     script, not of this process. v1 keeps that whole domain gated.
# `tests/core/test_mcp_server_gated.py` re-derives the write-marker check from
# the executors' own source, so an executor that GAINS a write fails CI.
_READ_ONLY_TOOLS = frozenset(
    {
        "ask_knowledge",
        "find_reference",
        "get_finance_news",
        "get_market_overview",
        "get_quote",
        "last_contact_with_person",
        "list_interactions",
        "list_next_actions",
        "list_projects",
        "list_social_channels",
        "query_activities",
        "query_observations",
        "research_topic",
        "search_knowledge",
        "social_timeline",
        "system_status",
        "whats_next",
    }
)

# `interactions.origin` for both gates — the CLI-facing `approve_tool_use` and
# this one. `memory._UNLEARNABLE_ORIGINS` keys on it: these prompts quote a
# run's untrusted tool input and must never become agent memory.
_GATE_ORIGIN = "agent_run_gate"

# How long an approval stays usable. The card's own `timeout_seconds` matches,
# so an unanswered card archives exactly when its approval would have expired.
_GATE_TTL_SECONDS = 900

# Re-read cadence while waiting on a card raised by an EARLIER request.
_GATE_POLL_INTERVAL_S = 2.0

# What the model is told when the operator has not answered yet. It is
# deliberately an instruction, not a status: the CLI caps an MCP tool call at
# ~60s, so the only way an approval can ever result in an execution is if the
# model comes BACK with byte-identical arguments after the human answers.
_GATE_RETRY_TEXT = (
    "Pending operator approval — a card was sent to the operator. Retry this exact "
    "tool call with identical arguments in about 60 seconds; it will execute once "
    "approved. Do not change the arguments (that requires a new approval)."
)


@dataclass(frozen=True)
class _GateDecision:
    """`allow=True` ⇒ execute. Anything else is a message for the model and NO
    execution — there is no third outcome."""

    allow: bool
    message: str = ""


def _args_fingerprint(args: dict) -> str:
    """Canonical hash of the call's arguments.

    Key-order-independent (`sort_keys`) and whitespace-independent so the
    retry the model makes hashes to the same value as the call the human saw —
    while any actual change of an argument does not, which is the point: an
    approval authorises exactly the arguments on the card.
    """
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _gate_key(agent_id: str, tool_name: str, args: dict) -> str:
    return f"{agent_id}:{tool_name}:{_args_fingerprint(args)}"


def _render_tool_input(tool_input: dict) -> str:
    try:
        return json.dumps(tool_input, indent=2, default=str, ensure_ascii=False)
    except (TypeError, ValueError):  # pragma: no cover — default=str covers ~everything
        return str(tool_input)


def _decision_allow(tool_input: dict) -> str:
    """The CLI expects the verdict JSON-stringified in the tool result text.

    `updatedInput` echoes the request VERBATIM: this gate decides whether a call
    happens, never what it does. Rewriting it here would let an approval the
    human read apply to a different call than the one they saw.
    """
    return json.dumps({"behavior": "allow", "updatedInput": tool_input})


def _decision_deny(message: str) -> str:
    return json.dumps({"behavior": "deny", "message": message})


def _approval_prompt(agent_id: str, tool_name: str, tool_input: dict) -> str:
    """The card text: whose run, which tool, and a bounded look at the input."""
    rendered = _render_tool_input(tool_input)
    return (
        f"🔒 Gated run for *{agent_id}* wants to use `{tool_name}`.\n\n"
        f"```\n{_truncate_text(rendered, _APPROVAL_PREVIEW_BYTES)}\n```\n\n"
        "Approve and the run proceeds with exactly this input. Deny and the tool is "
        "blocked — the run keeps going and is told why."
    )


async def _handle_approve_tool_use(
    request: Request, agent_id: str, request_id: Any, args: dict
) -> JSONResponse:
    """Raise an approval card and block on it, then answer the CLI.

    **Fails closed, on every path.** No Temporal client, a malformed request, a
    workflow that will not start, a timeout, an unexpected exception — all of
    them deny. The only thing that produces an `allow` is a human resolving the
    card with an approve value. A permission gate that opens when it breaks is
    not a gate, so there is deliberately no `except` here that ends in an allow.

    The verdict is always a normal (`isError: false`) tool result: the CLI reads
    the decision out of the result text, and an error result would be a broken
    permission check rather than a deny.
    """
    # Argument parsing lives INSIDE the fail-closed try, not before it: an
    # exception escaping here would leave FastAPI to return a 500, which the
    # CLI reads as a transport failure rather than as a decision — the one
    # outcome a permission gate must never produce. Malformed input for any
    # OTHER tool is still a normal JSON-RPC error; only this one owes the
    # caller a verdict on every path.
    try:
        tool_name = args.get("tool_name")
        tool_input = args.get("input")
        tool_use_id = str(args.get("tool_use_id") or "")[:64]
        malformed = (
            not isinstance(tool_name, str)
            or not tool_name.strip()
            or not isinstance(tool_input, dict)
        )
        if not malformed:
            tool_name = tool_name.strip()
            # Length only. The input is the thing we are refusing to trust: it
            # can carry a file's contents, a token pasted into a command,
            # someone's private text.
            input_bytes = len(json.dumps(tool_input, default=str).encode())
    except Exception as exc:  # noqa: BLE001 — a broken gate must deny, never raise
        logger.warning(
            "mcp_approval_decision",
            agent_id=agent_id,
            decision="deny",
            reason="unreadable_request",
            error=type(exc).__name__,
        )
        return _tool_result(
            request_id,
            _decision_deny("Denied — the permission request could not be read."),
            is_error=False,
        )

    if malformed:
        logger.warning(
            "mcp_approval_decision",
            agent_id=agent_id,
            tool=str(tool_name)[:64],
            decision="deny",
            reason="malformed_request",
        )
        return _tool_result(
            request_id,
            _decision_deny("Malformed permission request — denied."),
            is_error=False,
        )

    # Read the module global at call time so a test can shrink the wait without
    # sleeping out the real 9-minute cap.
    timeout_s = AGENT_RUN_APPROVAL_TIMEOUT_S

    client = getattr(request.app.state, "temporal_client", None)
    if client is None:
        logger.warning(
            "mcp_approval_decision",
            agent_id=agent_id,
            tool=tool_name,
            decision="deny",
            reason="no_temporal_client",
            input_bytes=input_bytes,
        )
        return _tool_result(
            request_id,
            _decision_deny("Denied — AEGIS could not reach anyone to approve this."),
            is_error=False,
        )

    try:
        handle = await client.start_workflow(
            "InteractionFlow",
            {
                "agent_id": agent_id,
                "kind": "choice",
                "origin": "agent_run_gate",
                "prompt": _approval_prompt(agent_id, tool_name, tool_input),
                "options": _APPROVAL_OPTIONS,
                "timeout_seconds": int(timeout_s),
                # `archive` (not `hold`): the flow must give up on its own, or a
                # never-answered card leaves a workflow pending forever for a
                # run that stopped waiting minutes ago.
                "timeout_policy": "archive",
            },
            id=f"agent-run-approval-{uuid4().hex[:12]}",
            task_queue="aegis-main",
        )
        result = await asyncio.wait_for(handle.result(), timeout=timeout_s)
    except TimeoutError:
        logger.warning(
            "mcp_approval_decision",
            agent_id=agent_id,
            tool=tool_name,
            decision="deny",
            reason="timeout",
            input_bytes=input_bytes,
            tool_use_id=tool_use_id,
        )
        return _tool_result(
            request_id,
            _decision_deny(
                f"Denied — the operator did not respond within {int(timeout_s)}s."
            ),
            is_error=False,
        )
    except Exception as exc:  # noqa: BLE001 — a broken gate must deny, never allow
        logger.warning(
            "mcp_approval_decision",
            agent_id=agent_id,
            tool=tool_name,
            decision="deny",
            reason="error",
            error=type(exc).__name__,
            input_bytes=input_bytes,
            tool_use_id=tool_use_id,
        )
        return _tool_result(
            request_id,
            _decision_deny(
                f"Denied — the approval request failed ({type(exc).__name__}). "
                "Nothing was approved."
            ),
            is_error=False,
        )

    payload = result if isinstance(result, dict) else {}
    interaction_id = str(payload.get("interaction_id") or "")
    status = str(payload.get("status") or "")
    response = payload.get("response") if isinstance(payload.get("response"), dict) else {}
    value = str((response or {}).get("value") or "").strip().lower()
    note = str((response or {}).get("note") or "").strip()

    if status == "resolved" and value in _APPROVE_VALUES:
        logger.info(
            "mcp_approval_decision",
            agent_id=agent_id,
            tool=tool_name,
            decision="allow",
            interaction_id=interaction_id,
            input_bytes=input_bytes,
            tool_use_id=tool_use_id,
        )
        return _tool_result(request_id, _decision_allow(tool_input), is_error=False)

    if status == "resolved":
        message = "Denied by operator"
        if note:
            message = f"{message}: {note[:_ERROR_CHARS]}"
    else:
        # `archived` — the flow hit its own deadline first.
        message = "Denied — the operator did not respond in time."
    logger.info(
        "mcp_approval_decision",
        agent_id=agent_id,
        tool=tool_name,
        decision="deny",
        reason=status or "unresolved",
        interaction_id=interaction_id,
        input_bytes=input_bytes,
        tool_use_id=tool_use_id,
    )
    return _tool_result(request_id, _decision_deny(message), is_error=False)


# ── the server-side gate (gated endpoint) ──────────────────────────────────


def _gate_prompt(agent_id: str, tool_name: str, args: dict) -> str:
    """The card text for a gated AEGIS tool call.

    It has to state the RETRY contract, because unlike the CLI-facing
    `approve_tool_use` gate nothing is blocked waiting on this answer: the
    request that raised the card has already come back to the model telling it
    to retry. What the operator approves is the *next* attempt.
    """
    return (
        f"🔒 Gated run for *{agent_id}* wants to run the AEGIS tool `{tool_name}`.\n\n"
        f"```\n{_truncate_text(_render_tool_input(args), _APPROVAL_PREVIEW_BYTES)}\n```\n\n"
        "Approve and the agent will retry and execute it with exactly this input "
        "(valid 15 minutes, single use). Deny and it is blocked — the run keeps "
        "going and is told why."
    )


async def _read_gate_row(pool: Any, gate_key: str) -> dict | None:
    """The newest UNCONSUMED gate card for this (agent, tool, args), within TTL.

    Consumed rows are filtered out here rather than reported as "already used":
    a second identical call is a second mutation and needs its own approval, so
    it falls through to the no-match branch and raises a fresh card.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, status, response FROM interactions "
            "WHERE origin = $1 AND metadata->>'gate_key' = $2 "
            "AND metadata->>'gate_consumed_at' IS NULL "
            "AND created_at > now() - make_interval(secs => $3::float8) "
            "ORDER BY created_at DESC LIMIT 1",
            _GATE_ORIGIN,
            gate_key,
            float(_GATE_TTL_SECONDS),
        )
    return dict(row) if row is not None else None


async def _claim_gate_approval(pool: Any, interaction_id: str) -> bool:
    """Mark an approval consumed, atomically. True ⇒ this caller owns it.

    Single-use is enforced by the UPDATE's own predicate, not by a read-then-
    write: two concurrent retries of the same approved call race here, and
    exactly one of them can win. There is no sanctioned service for this write
    (the `interactions` table is owned by the worker's InteractionActivities,
    which has no notion of a gate), so it is a deliberately narrow UPDATE that
    touches one metadata key and nothing else.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE interactions "
            "SET metadata = metadata || jsonb_build_object('gate_consumed_at', now()) "
            "WHERE id = $1::uuid AND status = 'resolved' "
            "AND metadata->>'gate_consumed_at' IS NULL "
            "RETURNING id",
            interaction_id,
        )
    return row is not None


async def _await_gate_row(pool: Any, interaction_id: str, wait_s: float) -> dict | None:
    """Re-read one interaction until it leaves `pending` or the budget runs out."""
    deadline = time.monotonic() + wait_s
    while True:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, status, response FROM interactions WHERE id = $1::uuid",
                interaction_id,
            )
        if row is None or str(row["status"]) != "pending":
            return dict(row) if row is not None else None
        if time.monotonic() >= deadline:
            return dict(row)
        await asyncio.sleep(_GATE_POLL_INTERVAL_S)


async def _raise_gate_card(
    request: Request, agent_id: str, tool_name: str, args: dict, gate_key: str, wait_s: float
) -> dict | None:
    """Start the approval card and wait `wait_s` for an answer.

    Returns the resolved interaction, or None when the operator has not
    answered yet — in which case the card stays LIVE (its own timeout is the
    gate TTL) so the model's retry finds it. Raises on a broken Temporal path;
    the caller turns that into a deny.
    """
    client = getattr(request.app.state, "temporal_client", None)
    if client is None:
        raise RuntimeError("no temporal client — cannot ask anyone for approval")
    handle = await client.start_workflow(
        "InteractionFlow",
        {
            "agent_id": agent_id,
            "kind": "choice",
            "origin": _GATE_ORIGIN,
            "prompt": _gate_prompt(agent_id, tool_name, args),
            "options": _APPROVAL_OPTIONS,
            "timeout_seconds": _GATE_TTL_SECONDS,
            # `archive`, so an unanswered card dies with its own approval
            # window instead of leaving a workflow pending forever.
            "timeout_policy": "archive",
            # The key the retry looks itself up by. `gate_tool`/`gate_agent`
            # are forensics only — the match is on `gate_key` alone.
            "metadata": {
                "gate_key": gate_key,
                "gate_tool": tool_name,
                "gate_agent": agent_id,
            },
        },
        id=f"agent-run-gate-{uuid4().hex[:12]}",
        task_queue="aegis-main",
    )
    try:
        result = await asyncio.wait_for(handle.result(), timeout=wait_s)
    except TimeoutError:
        return None
    payload = result if isinstance(result, dict) else {}
    return {
        "id": str(payload.get("interaction_id") or ""),
        "status": str(payload.get("status") or ""),
        "response": payload.get("response"),
    }


def _gate_verdict(row: dict | None) -> tuple[str, str]:
    """(verdict, operator note) for one gate row.

    `approved` requires BOTH a resolved row and an explicit approve value —
    every other shape (archived, unknown status, a missing or malformed
    response) resolves to something that does not execute.
    """
    if row is None:
        return "pending", ""
    status = str(row.get("status") or "")
    response = row.get("response") if isinstance(row.get("response"), dict) else {}
    value = str((response or {}).get("value") or "").strip().lower()
    note = str((response or {}).get("note") or "").strip()
    if status == "resolved":
        return ("approved" if value in _APPROVE_VALUES else "denied"), note
    if status == "pending":
        return "pending", ""
    return "expired", ""


async def _gate_tool_call(
    request: Request, agent_id: str, settings: Settings, tool_name: str, args: dict
) -> _GateDecision:
    """Decide whether ONE mutating tool call on the gated endpoint may execute.

    The protocol is approve-then-retry, because the claude CLI abandons an MCP
    tool call after ~60s and cannot be made to wait for a human:

      1. no card for these exact arguments  → raise one, wait `wait_s`, and if
         nobody has answered tell the model to retry;
      2. a card exists and is still pending → wait `wait_s` on it, same answer;
      3. approved                           → claim it (single use) and execute;
      4. denied                             → say so, permanently, with the note;
      5. archived (nobody answered in 15m)  → say the approval expired.

    **Fails closed on every path.** No pool, no Temporal, a malformed row, an
    unexpected exception — all of them return `allow=False`. The only thing
    that returns `allow=True` is a claim on a human-approved card, so a bug in
    here can cost an execution that should have happened but can never cause
    one that should not.
    """
    wait_s = float(max(1, int(getattr(settings, "mcp_gate_wait_seconds", 40) or 40)))
    gate_key = _gate_key(agent_id, tool_name, args)
    try:
        pool = getattr(request.app.state, "db_pool", None)
        if pool is None:
            raise RuntimeError("no db pool — cannot read or record approvals")
        row = await _read_gate_row(pool, gate_key)
        if row is None:
            row = await _raise_gate_card(request, agent_id, tool_name, args, gate_key, wait_s)
        elif str(row["status"]) == "pending":
            row = await _await_gate_row(pool, str(row["id"]), wait_s)
        verdict, note = _gate_verdict(row)

        if verdict == "approved":
            interaction_id = str((row or {}).get("id") or "")
            if not interaction_id or not await _claim_gate_approval(pool, interaction_id):
                logger.warning(
                    "mcp_gate_decision",
                    agent_id=agent_id,
                    tool=tool_name,
                    decision="block",
                    reason="approval_already_used",
                    interaction_id=interaction_id,
                )
                return _GateDecision(
                    False,
                    "That approval has already been used — an approval covers exactly one "
                    "execution. Retry to request a fresh one.",
                )
            logger.info(
                "mcp_gate_decision",
                agent_id=agent_id,
                tool=tool_name,
                decision="allow",
                interaction_id=interaction_id,
            )
            return _GateDecision(True)

        logger.info(
            "mcp_gate_decision",
            agent_id=agent_id,
            tool=tool_name,
            decision="block",
            reason=verdict,
            interaction_id=str((row or {}).get("id") or ""),
        )
        if verdict == "denied":
            message = f"Denied by operator: {note[:_ERROR_CHARS]}" if note else "Denied by operator"
            return _GateDecision(
                False,
                f"{message}. This call is blocked — do NOT retry it with these arguments.",
            )
        if verdict == "expired":
            return _GateDecision(
                False,
                "The approval for this call expired before it was used — the whole request "
                "must be re-attempted from the start.",
            )
        return _GateDecision(False, _GATE_RETRY_TEXT)
    except Exception as exc:  # noqa: BLE001 — a broken gate must block, never execute
        logger.warning(
            "mcp_gate_decision",
            agent_id=agent_id,
            tool=tool_name,
            decision="block",
            reason="error",
            error=type(exc).__name__,
        )
        return _GateDecision(
            False,
            f"Approval could not be obtained ({type(exc).__name__}) — nothing was executed. "
            "Retry shortly.",
        )


def _require_enabled(settings: Settings = Depends(get_settings)) -> None:
    """Refuse every method while the server side is switched off — or while it
    would be served with no authentication at all.

    ``verify_auth`` returns True unconditionally under ``auth_disabled``, which
    is the documented posture for a deployment fronted by an authenticating
    proxy (Cloudflare Access, an OAuth2 proxy). That posture does NOT extend to
    this endpoint: the URL a run mounts is by design a LAN/overlay address
    reachable *from the coding host*, i.e. one that bypasses the proxy the
    posture depends on. Enabling both would serve every agent's tool set —
    `restart_service`, `run_infra_script`, the money and GTD writes — to
    anything that can open a TCP connection to Core.

    So the two settings are refused together unless the operator has explicitly
    accepted it. `mcp_server_allow_unauthenticated` is the acceptance, and it
    exists as a separate switch precisely so that turning the MCP server on is
    never *implicitly* a decision about authentication.
    """
    if not getattr(settings, "mcp_server_enabled", False):
        raise HTTPException(
            status_code=403,
            detail=(
                "AEGIS MCP server is disabled — set AEGIS_MCP_SERVER_ENABLED=true "
                "and restart Core to serve AEGIS's chat tools to external MCP clients."
            ),
        )
    if getattr(settings, "auth_disabled", False) and not getattr(
        settings, "mcp_server_allow_unauthenticated", False
    ):
        raise HTTPException(
            status_code=403,
            detail=(
                "AEGIS MCP server refuses to serve unauthenticated tool calls: "
                "AEGIS_AUTH_DISABLED=true makes verify_auth a no-op, and this "
                "endpoint is mounted at a LAN/overlay URL that bypasses the "
                "proxy AEGIS_AUTH_DISABLED assumes in front of Core. As it "
                "stands, anything that can reach Core could run every agent's "
                "tools (restart_service, run_infra_script, money and GTD "
                "writes) with no credential. Fix it by unsetting "
                "AEGIS_AUTH_DISABLED and setting AEGIS_API_KEY (or generating "
                "an API key in the admin UI) — or, if Core really is "
                "unreachable except from trusted hosts, accept the risk "
                "explicitly with AEGIS_MCP_SERVER_ALLOW_UNAUTHENTICATED=true."
            ),
        )


router = APIRouter(
    prefix="/api/mcp-server",
    dependencies=[Depends(verify_auth), Depends(_require_enabled)],
)


def _rpc_result(request_id: Any, result: dict) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})


def _rpc_error(request_id: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
    )


def _tool_result(request_id: Any, text: str, *, is_error: bool) -> JSONResponse:
    """An MCP ``tools/call`` result. A *tool* failure is not a protocol error —
    it comes back as ``isError: true`` so the calling model can read the reason
    and self-correct rather than seeing the transport blow up."""
    return _rpc_result(
        request_id, {"content": [{"type": "text", "text": text}], "isError": is_error}
    )


async def _load_agent(request: Request, agent_id: str) -> dict:
    """The agent row, or 404. Mirrors how the chat surfaces resolve an agent."""
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="Database not available")
    agent = await get_agent(pool, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    return agent


def _served_tools(agent_id: str, metadata: dict | None) -> list[dict]:
    """The agent's chat tools, minus ``_UNSERVED_TOOLS``.

    ``_get_agent_tools`` is the single source of truth for what an agent may
    call (DB ``metadata.tool_set`` → seed defaults → the minimal fallback set),
    so this surface can never be broader than the same agent's chat surface.
    This function is then the single derivation of the *served* set: the
    endpoint calls it once per request and hands the result to both
    ``tools/list`` and ``tools/call``, so an excluded tool is neither listed
    nor callable.
    """
    return [
        tool
        for tool in _get_agent_tools(agent_id, metadata=metadata)
        if tool["function"]["name"] not in _UNSERVED_TOOLS
    ]


def _tool_descriptor(tool: dict) -> dict:
    """One CHAT_TOOLS entry as an MCP tool descriptor."""
    fn = tool.get("function", {})
    schema = fn.get("parameters")
    return {
        "name": fn.get("name", ""),
        "description": fn.get("description", ""),
        # MCP names it inputSchema; the OpenAI-shaped registry names it
        # parameters. Same JSONSchema either way.
        "inputSchema": schema if isinstance(schema, dict) else {"type": "object", "properties": {}},
    }


def _tool_context(request: Request, agent_id: str, settings: Settings) -> ToolContext:
    """Build the executor context exactly as the chat path does.

    ``task_id``/``chat_context`` are None: an MCP call has no Todoist anchor and
    no conversation around it. Every executor that reads ``chat_context`` does so
    as ``(ctx.chat_context or {})``, so None is the supported "no chat" value.
    ``mcp_manager`` is wired for parity with chat, but ``call_mcp_tool`` — its
    only consumer — is filtered out of the served set above.
    """
    state = request.app.state
    return ToolContext(
        agent_id=agent_id,
        task_id=None,
        knowledge_connector=getattr(state, "knowledge_connector", None),
        finance_connector=getattr(state, "finance_connector", None),
        chat_context=None,
        settings=settings,
        temporal_client=getattr(state, "temporal_client", None),
        search_connector=getattr(state, "search_connector", None),
        llm_client=getattr(state, "llm", None),
        remote_script_connector=getattr(state, "remote_script_connector", None),
        vercel_connector=getattr(state, "vercel_connector", None),
        mcp_manager=getattr(state, "mcp_manager", None),
        model_light=getattr(settings, "model_fast", "gemma4:e2b"),
    )


async def _handle_tools_call(
    request: Request,
    agent_id: str,
    settings: Settings,
    request_id: Any,
    params: Any,
    tools: list[dict],
    gated: bool = False,
) -> JSONResponse:
    """Validate, authorize and run one tool call.

    `gated` is the /gated endpoint's enforcement: a tool outside
    `_READ_ONLY_TOOLS` needs an operator approval before it executes here.
    """
    if not isinstance(params, dict):
        return _rpc_error(request_id, _INVALID_PARAMS, "'params' must be an object")
    name = params.get("name")
    if not isinstance(name, str) or not name:
        return _rpc_error(request_id, _INVALID_PARAMS, "'params.name' must be a tool name")
    args = params.get("arguments")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return _rpc_error(request_id, _INVALID_PARAMS, "'params.arguments' must be an object")

    # The permission gate is served to every agent and has no CHAT_TOOLS entry,
    # so it is dispatched before the tool-set check rather than through it.
    if name == APPROVAL_TOOL_NAME:
        return await _handle_approve_tool_use(request, agent_id, request_id, args)

    if name not in {t["function"]["name"] for t in tools}:
        logger.info("mcp_server_tool_call", agent_id=agent_id, tool=name, status="not_granted")
        return _rpc_error(
            request_id,
            _INVALID_PARAMS,
            f"Unknown tool '{name}' — not in agent '{agent_id}' tool set",
        )

    # Log keys only: an argument value can carry personal text (a task title, a
    # note body), and this line lands in the shared log stream.
    arg_keys = sorted(args.keys())

    try:
        _validate_tool_args(name, args)
    except JSONSchemaValidationError as exc:
        hint = _schema_hint(name)
        message = f"{exc.message} — {hint}" if hint else exc.message
        logger.info(
            "mcp_server_tool_call",
            agent_id=agent_id,
            tool=name,
            status="invalid_args",
            arg_keys=arg_keys,
        )
        # Deliberately a tool result, not a JSON-RPC error: the schema hint is
        # what lets the calling model fix its own arguments and retry.
        return _tool_result(request_id, message, is_error=True)

    # The gate sits between validation and execution on purpose: a call that
    # would have been rejected for bad arguments must not raise a card, and the
    # arguments the operator sees are the ones that would run.
    if gated and name not in _READ_ONLY_TOOLS:
        decision = await _gate_tool_call(request, agent_id, settings, name, args)
        if not decision.allow:
            logger.info(
                "mcp_server_tool_call",
                agent_id=agent_id,
                tool=name,
                status="gate_blocked",
                arg_keys=arg_keys,
            )
            return _tool_result(request_id, decision.message, is_error=True)

    pool = request.app.state.db_pool
    ctx = _tool_context(request, agent_id, settings)
    timeout = _TOOL_TIMEOUT_OVERRIDES.get(
        name, getattr(settings, "tool_timeout_seconds", 30) or 30
    )
    try:
        result = await asyncio.wait_for(_execute_tool(pool, name, args, ctx), timeout=timeout)
    except TimeoutError:
        logger.warning(
            "mcp_server_tool_call",
            agent_id=agent_id,
            tool=name,
            status="timeout",
            arg_keys=arg_keys,
        )
        return _tool_result(
            request_id, f"Tool '{name}' timed out after {timeout}s", is_error=True
        )
    except Exception as exc:  # noqa: BLE001 — one bad tool must not 500 the endpoint
        logger.warning(
            "mcp_server_tool_call",
            agent_id=agent_id,
            tool=name,
            status="error",
            error=type(exc).__name__,
            arg_keys=arg_keys,
        )
        return _tool_result(
            request_id,
            f"Tool '{name}' failed: {type(exc).__name__}: {str(exc)[:_ERROR_CHARS]}",
            is_error=True,
        )

    text = _truncate_result(str(result), max_bytes=_TOOL_RESULT_MAX_BYTES)
    logger.info(
        "mcp_server_tool_call",
        agent_id=agent_id,
        tool=name,
        status="success",
        arg_keys=arg_keys,
        result_bytes=len(text.encode()),
    )
    return _tool_result(request_id, text, is_error=False)


async def _serve(agent_id: str, request: Request, settings: Settings, *, gated: bool) -> Response:
    """One JSON-RPC message. `gated` decides whether a mutating tool needs an
    operator approval first; nothing else differs between the two endpoints."""
    agent = await _load_agent(request, agent_id)
    tools = _served_tools(agent_id, dict(agent.get("metadata") or {}))

    raw = await request.body()
    try:
        message = json.loads(raw)
    except ValueError:
        return _rpc_error(None, _PARSE_ERROR, "Parse error: body is not valid JSON")

    if isinstance(message, list):
        return _rpc_error(
            None,
            _INVALID_REQUEST,
            "Batch requests are not supported — send one JSON-RPC message per POST",
        )
    if not isinstance(message, dict):
        return _rpc_error(None, _INVALID_REQUEST, "A JSON-RPC message must be an object")

    method = message.get("method")
    # No `id` ⇒ a notification. JSON-RPC forbids a response to one, and the MCP
    # streamable-HTTP transport wants 202 + empty body.
    if "id" not in message:
        logger.debug("mcp_server_notification", agent_id=agent_id, method=str(method)[:64])
        return Response(status_code=202)

    request_id = message.get("id")
    if not isinstance(method, str) or not method:
        return _rpc_error(request_id, _INVALID_REQUEST, "'method' must be a string")

    if method == "initialize":
        # The client's requested protocolVersion is accepted whatever it is; we
        # answer with the revision we implement and let it negotiate down.
        logger.info("mcp_server_initialize", agent_id=agent_id, tools=len(tools))
        return _rpc_result(
            request_id,
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": _SERVER_NAME, "version": request.app.version},
            },
        )

    if method == "ping":
        return _rpc_result(request_id, {})

    if method == "tools/list":
        # The permission gate rides along for every agent: `--permission-prompt-tool`
        # only resolves a tool the server actually advertises, and a gated run
        # whose gate is invisible cannot take a single non-allowed action.
        return _rpc_result(
            request_id,
            {"tools": [_tool_descriptor(t) for t in tools] + [_APPROVAL_TOOL_DESCRIPTOR]},
        )

    if method == "tools/call":
        return await _handle_tools_call(
            request, agent_id, settings, request_id, message.get("params"), tools, gated
        )

    return _rpc_error(request_id, _METHOD_NOT_FOUND, f"Unknown method '{method}'")


@router.post("/{agent_id}")
async def mcp_server_endpoint(
    agent_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),
) -> Response:
    """Streamable-HTTP MCP endpoint serving `agent_id`'s chat tools.

    One JSON-RPC message per POST. Stateless: no `Mcp-Session-Id` is issued and
    one sent by the client is ignored.
    """
    return await _serve(agent_id, request, settings, gated=False)


@router.post("/{agent_id}/gated")
async def mcp_server_gated_endpoint(
    agent_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),
) -> Response:
    """Same server, with the approval gate enforced HERE.

    What a gated agent run mounts (`RemoteScriptConnector._mcp_run_config`).
    Identical tool list and identical semantics for read-only tools; anything
    else executes only after an operator approves it, because the CLI's own
    permission routing was measured not to gate MCP tools at all (#294).
    """
    return await _serve(agent_id, request, settings, gated=True)


@router.get("/{agent_id}")
async def mcp_server_no_stream(agent_id: str) -> Response:
    """No server-initiated SSE stream — the spec's way to say so is 405."""
    raise HTTPException(
        status_code=405,
        detail="This MCP endpoint is stateless and POST-only; it opens no server-initiated stream.",
        headers={"Allow": "POST, DELETE"},
    )


@router.get("/{agent_id}/gated")
async def mcp_server_gated_no_stream(agent_id: str) -> Response:
    """Parity with the ungated endpoint — a client probes the URL it mounts."""
    raise HTTPException(
        status_code=405,
        detail="This MCP endpoint is stateless and POST-only; it opens no server-initiated stream.",
        headers={"Allow": "POST, DELETE"},
    )


@router.delete("/{agent_id}", status_code=204)
async def mcp_server_end_session(agent_id: str) -> Response:
    """Session termination. No session is ever issued, so this is a no-op."""
    return Response(status_code=204)


@router.delete("/{agent_id}/gated", status_code=204)
async def mcp_server_gated_end_session(agent_id: str) -> Response:
    """Session termination on the gated URL — same no-op."""
    return Response(status_code=204)
