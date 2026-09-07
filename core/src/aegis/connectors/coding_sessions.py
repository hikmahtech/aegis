"""Read-only inventory of coding-CLI sessions on the coding host.

Claude Code publishes a session registry per ``CLAUDE_CONFIG_DIR`` and exposes
it through ``claude agents --json`` — documented for scripting and needing no
TTY. This module turns that output into records AEGIS can reason about, and
answers the question the launch path asks: is a human already busy in this
repo? (Task ownership is a `work_sessions` lookup, not an inventory question —
see `services/work_sessions.py`.)

Everything here except :func:`busy_human_sessions` is pure, so the parsing rules
are unit-testable without SSH.

Three rules are load-bearing rather than cosmetic:

* AEGIS's own headless runs register in the SAME registry (they appear with
  ``entrypoint="sdk-cli"``), so a session inside a ``-aegis-wt/`` worktree is
  tagged ``owner="aegis"`` and never counts as a collision. Without that, the
  first run in a repo would block every later one.
* The CLI's session objects also carry ``messagingSocketPath``, and a sibling
  ``.key`` file holds that socket's auth material. Both are an undocumented
  private interface. This module keeps an explicit ALLOW-LIST of output fields
  rather than passing the CLI's object through, so a future CLI version cannot
  introduce a field that leaks by default.
* Stale records are DROPPED at parse time, because the CLI prunes an
  interactive session when its process exits but never prunes a ``claude --bg``
  job record — a finished or blocked job stays listed until someone runs
  ``claude rm``. So a background job in a terminal or blocked ``state`` goes,
  and so does any session whose ``startedAt`` is older than a week. A job
  waiting on the human is not a person mid-thought, and without this a
  five-week-old dead job counts as "the operator has this repo open" for ever
  (issue #369). An unreadable or absent ``startedAt`` is kept: an unknown age
  must not be guessed at.
"""

from __future__ import annotations

import json
import logging
import re
import time

logger = logging.getLogger(__name__)

# `claude agents --json` pretty-prints, so the array opens a line. Matching at
# line start survives a warning banner that happens to contain a bracket.
_ARRAY_START = re.compile(r"^\[", re.MULTILINE)

# AEGIS's per-run worktree: `{repo_path}-aegis-wt/{run_id}` (see
# RemoteScriptConnector.start_kimi_run). Claude Code's own worktrees nest under
# the repo. Both must fold back to the repo so they compare equal to it.
_AEGIS_WT_MARKER = "-aegis-wt/"
_CLAUDE_WT_MARKER = "/.claude/worktrees/"

_BUSY = "busy"

# `claude --bg` job states meaning the job is finished or parked on the human.
# The CLI keeps listing such a job until `claude rm`, so AEGIS drops it instead.
_STALE_BACKGROUND_STATES = frozenset({"blocked", "done", "failed", "stopped", "killed", "exited"})
_BACKGROUND = "background"

# Nothing started more than a week ago is a live collision, whatever is listed.
_MAX_SESSION_AGE_MS = 7 * 24 * 60 * 60 * 1000


def parse_agents_json(raw: str) -> list[dict]:
    """Session objects from `claude agents --json`. Raises ValueError if unusable.

    Tolerates leading non-JSON: a profile whose `.claude.json` is missing prints
    a restore warning before the array. Non-dict entries are dropped rather than
    raising, so one malformed row cannot blind the whole inventory.
    """
    text = (raw or "").strip()
    match = _ARRAY_START.search(text)
    start = match.start() if match else text.find("[")
    if start < 0:
        raise ValueError("no JSON array in `claude agents --json` output")
    try:
        parsed = json.loads(text[start:])
    except json.JSONDecodeError as exc:
        raise ValueError(f"unparseable `claude agents --json` output: {exc}") from exc
    if not isinstance(parsed, list):
        raise ValueError("`claude agents --json` did not return an array")
    return [item for item in parsed if isinstance(item, dict)]


def normalise_repo(cwd: str, repo_base: str) -> tuple[str, str]:
    """``(repo, owner)`` for a session working directory.

    ``repo`` is the path relative to ``repo_base`` with either worktree
    convention folded back to the repo itself, or ``""`` when the session is
    outside ``repo_base`` — in which case it can never match a launch target.
    """
    base = (repo_base or "").rstrip("/")
    path = (cwd or "").rstrip("/")
    if not base or not path or not path.startswith(base + "/"):
        return "", "human"
    rel = path[len(base) + 1 :]
    if _AEGIS_WT_MARKER in rel:
        return rel.split(_AEGIS_WT_MARKER, 1)[0], "aegis"
    if _CLAUDE_WT_MARKER in rel:
        return rel.split(_CLAUDE_WT_MARKER, 1)[0], "human"
    return rel, "human"


def _started_at_ms(value: object) -> int | None:
    """``startedAt`` as epoch milliseconds, or ``None`` when it cannot be read.

    The CLI has emitted this as an int and as a string of digits. Anything else
    — absent, blank, an ISO timestamp — is an UNKNOWN age, and unknown is never
    treated as old.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def to_records(
    parsed: list[dict], account: str, repo_base: str, *, now_ms: int | None = None
) -> list[dict]:
    """Normalised records, minus the stale ones. Output fields are an allow-list.

    Interactive sessions report ``status``/``sessionId``; background ones report
    ``state``/``id``. Both are read so the inventory sees the whole fleet.

    Two records are dropped, both explained in the module docstring: a
    background job in a terminal or blocked state, and any session started more
    than ``_MAX_SESSION_AGE_MS`` ago. ``now_ms`` exists so tests can pin the
    clock; it defaults to now.
    """
    clock = int(time.time() * 1000) if now_ms is None else now_ms
    records: list[dict] = []
    for item in parsed:
        name = str(item.get("name") or "")
        kind = str(item.get("kind") or "").strip().lower()
        state = str(item.get("state") or "").strip().lower()
        if kind == _BACKGROUND and state in _STALE_BACKGROUND_STATES:
            logger.debug("coding_session_dropped: name=%s reason=background_state=%s", name, state)
            continue
        started = _started_at_ms(item.get("startedAt"))
        if started is not None and clock - started > _MAX_SESSION_AGE_MS:
            logger.debug("coding_session_dropped: name=%s reason=age_ms=%s", name, clock - started)
            continue
        cwd = str(item.get("cwd") or "")
        repo, owner = normalise_repo(cwd, repo_base)
        records.append(
            {
                "account": account,
                "session_id": str(item.get("sessionId") or item.get("id") or ""),
                "name": name,
                "cwd": cwd,
                "repo": repo,
                "status": str(item.get("status") or item.get("state") or ""),
                "kind": str(item.get("kind") or ""),
                "owner": owner,
            }
        )
    return records


def match_busy(sessions: list[dict], repo: str) -> list[dict]:
    """Human-owned sessions actively busy in `repo`.

    Idle sessions are excluded deliberately: the collision that matters is a
    person mid-thought, not a shell parked in a directory.
    """
    target = (repo or "").strip().strip("/")
    if not target:
        return []
    return [
        s
        for s in sessions
        if s.get("owner") == "human" and s.get("status") == _BUSY and s.get("repo") == target
    ]


async def busy_human_sessions(remote_script: object, repo: str) -> list[dict]:
    """Human sessions busy in `repo`, or `[]` when there is nothing to block on.

    ALWAYS fails open. A connector that is missing, disabled, unreachable or
    returning nonsense yields `[]` and the caller launches — which is exactly
    today's behaviour, so the worst case is no worse than the status quo.

    `skip_when_busy=False` is observe-only: the collision is logged but not
    reported, so an operator can watch the inventory before letting it decide.
    """
    if remote_script is None or not repo:
        return []
    try:
        inventory = await remote_script.list_coding_sessions()
    except Exception as exc:  # noqa: BLE001 — the coding lane must not break on this
        logger.warning("coding_inventory_failed: %s", str(exc)[:200])
        return []
    if (inventory or {}).get("status") != "ok":
        return []
    busy = match_busy(inventory.get("sessions") or [], repo)
    if busy and not inventory.get("skip_when_busy", True):
        logger.info(
            "coding_inventory_observe_only: repo=%s sessions=%s",
            repo,
            [s.get("name") for s in busy],
        )
        return []
    return busy
