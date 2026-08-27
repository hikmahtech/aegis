"""Per-agent, short-TTL tokens for a coding run's MCP mount (issue #288).

Before this, one shared API key authenticated every ``/api/mcp-server/{agent_id}``
endpoint. That key is written into a config file on the coding host, where every
run can read it, and a run reads untrusted content by design and (ungated) has a
full shell. Two consequences the issue documents:

* a run could swap the ``{agent_id}`` path segment and drive ANOTHER agent's tool
  surface — a sebas run reaching pandora's ``restart_service``;
* a run could print the key into its transcript, which ``AgentRunFlow`` then
  delivers to a chat channel, leaking a credential that never expires.

A mount token fixes both by *binding* the credential rather than by hiding it.
It carries the agent id and the gated flag, so presenting it for a different
agent — or at the ungated endpoint when the run was launched gated — fails the
signature check. And it expires, so a leaked transcript ages out on its own.

Deliberately stateless: an HMAC over the payload with ``AEGIS_SECRET_KEY``, which
both Core and the worker already hold. No table, no migration, no lookup on the
hot auth path, and nothing to clean up when a run dies with the power (this
homelab's normal failure mode).

This is NOT a general-purpose auth system. It authenticates one thing: "a run
launched for agent X, in mode Y, before time Z".
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

# A run is capped at 240 minutes (`chat.py::_run_timeout_minutes`); this is that
# plus slack, used when the caller does not know its own deadline. The point of
# the TTL is that a leaked token dies on its own, so hours — not the never of a
# static API key — is what matters, and being generous costs nothing.
DEFAULT_TTL_SECONDS = 6 * 3600

_SEP = "."
_PREFIX = "aegismcp1"


def _payload(agent_id: str, expires_at: int, gated: bool) -> str:
    # `gated` is part of the signed payload so a run cannot downgrade itself by
    # pointing at the ungated endpoint with the token it was given.
    return f"{_PREFIX}:{agent_id}:{expires_at}:{'g' if gated else 'u'}"


def _sign(payload: str, secret: str) -> str:
    digest = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def mint_mount_token(
    agent_id: str,
    secret: str,
    *,
    gated: bool = False,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    now: int | None = None,
) -> str:
    """A token authorising `agent_id`'s MCP endpoint until it expires.

    Returns "" when there is no secret or no agent — the caller then has no
    credential to write, which is a visible mount failure rather than a silent
    downgrade to something weaker.
    """
    agent_id = (agent_id or "").strip()
    if not agent_id or not secret:
        return ""
    ttl = max(60, int(ttl_seconds or DEFAULT_TTL_SECONDS))
    expires_at = int(now if now is not None else time.time()) + ttl
    payload = _payload(agent_id, expires_at, gated)
    encoded = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"{encoded}{_SEP}{_sign(payload, secret)}"


def read_mount_token(token: str, secret: str, *, now: int | None = None) -> tuple[str, bool] | None:
    """``(agent_id, gated)`` when the signature and expiry hold, else ``None``.

    This answers "is this a credential we issued?" WITHOUT deciding what it may
    reach — the route layer compares the returned agent and mode against the URL
    it was presented at. Keeping the two apart is what lets the router-level
    dependency stay ignorant of the route shape while the binding is still
    enforced exactly once, where the path is known.

    Never raises: any malformed input is simply not a token.
    """
    if not token or not secret:
        return None
    encoded, _, signature = token.partition(_SEP)
    if not encoded or not signature:
        return None
    try:
        padding = "=" * (-len(encoded) % 4)
        payload = base64.urlsafe_b64decode(encoded + padding).decode()
    except Exception:  # noqa: BLE001 — any decode failure is just "not a token"
        return None
    # Signature first, and constant-time, so a forged token cannot be told from
    # an expired one by timing.
    if not hmac.compare_digest(signature, _sign(payload, secret)):
        return None
    parts = payload.split(":")
    if len(parts) != 4 or parts[0] != _PREFIX:
        return None
    _, token_agent, expires_raw, mode = parts
    if mode not in ("g", "u"):
        return None
    try:
        expires_at = int(expires_raw)
    except ValueError:
        return None
    if expires_at <= int(now if now is not None else time.time()):
        return None
    return token_agent, mode == "g"


def verify_mount_token(
    token: str,
    agent_id: str,
    secret: str,
    *,
    gated: bool = False,
    now: int | None = None,
) -> bool:
    """True when `token` authorises exactly this agent, in this mode, right now."""
    if not agent_id:
        return False
    read = read_mount_token(token, secret, now=now)
    return read is not None and read == (agent_id.strip(), gated)
