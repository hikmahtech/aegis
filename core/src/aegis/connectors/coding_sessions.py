"""Read-only inventory of coding-CLI sessions on the coding host.

Claude Code publishes a session registry per ``CLAUDE_CONFIG_DIR`` and exposes
it through ``claude agents --json`` — documented for scripting and needing no
TTY. This module turns that output into records AEGIS can reason about, and
answers the questions the launch path asks: is a human already busy in this
repo, is this task's own session still alive, and is one of the operator's
sessions already working on this very task?

Everything here except :func:`busy_human_sessions` is pure, so the parsing rules
are unit-testable without SSH.

Two rules are load-bearing rather than cosmetic:

* AEGIS's own headless runs register in the SAME registry (they appear with
  ``entrypoint="sdk-cli"``), so a session inside a ``-aegis-wt/`` worktree is
  tagged ``owner="aegis"`` and never counts as a collision. Without that, the
  first run in a repo would block every later one.
* The CLI's session objects also carry ``messagingSocketPath``, and a sibling
  ``.key`` file holds that socket's auth material. Both are an undocumented
  private interface. This module keeps an explicit ALLOW-LIST of output fields
  rather than passing the CLI's object through, so a future CLI version cannot
  introduce a field that leaks by default.
"""

from __future__ import annotations

import json
import logging
import re

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


def to_records(parsed: list[dict], account: str, repo_base: str) -> list[dict]:
    """Normalised records. Output fields are an allow-list — see module docstring.

    Interactive sessions report ``status``/``sessionId``; background ones report
    ``state``/``id``. Both are read so the inventory sees the whole fleet.
    """
    records: list[dict] = []
    for item in parsed:
        cwd = str(item.get("cwd") or "")
        repo, owner = normalise_repo(cwd, repo_base)
        records.append(
            {
                "account": account,
                "session_id": str(item.get("sessionId") or item.get("id") or ""),
                "name": str(item.get("name") or ""),
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


def find_session(sessions: list[dict], session_id: str) -> dict | None:
    """The record with this `session_id`, whatever its owner or status.

    Used to ask "is the task's OWN session live?" — an idle session, or one a
    person has resumed, is still the same conversation, so no status or owner
    filter belongs here.
    """
    target = (session_id or "").strip()
    if not target:
        return None
    return next((s for s in sessions if s.get("session_id") == target), None)


def human_sessions_in_repo(sessions: list[dict], repo: str) -> list[dict]:
    """Human-owned sessions in `repo`, busy or not — `match_busy`'s any-status sibling.

    Idle counts here because the question is different: not "is a person mid-
    thought?" but "does a person already have this task open?". A session parked
    idle on this task is exactly the collision we are looking for.
    """
    target = (repo or "").strip().strip("/")
    if not target:
        return []
    return [s for s in sessions if s.get("owner") == "human" and s.get("repo") == target]


def build_same_task_prompt(title: str, description: str, sessions: list[dict]) -> str:
    """Ask a model whether any of `sessions` is already working on THIS task.

    Git context (branch, last commit, dirty files) is what separates "same repo"
    from "same task", so every field is rendered even when absent — a blank
    would read as "no changes" rather than "not known".
    """
    blocks = []
    for index, session in enumerate(sessions, start=1):
        blocks.append(
            f"{index}. name: {_render(session, 'name')}\n"
            f"   cwd: {_render(session, 'cwd')}\n"
            f"   branch: {_render(session, 'branch')}\n"
            f"   last commit: {_render(session, 'log')}\n"
            f"   uncommitted files: {_render(session, 'status_short')}"
        )
    listing = "\n".join(blocks) or "(none)"
    return (
        "A task is about to be handed to an automated coding agent. Decide whether "
        "a person is already working on that SAME task in one of the sessions below.\n\n"
        f"TASK TITLE: {title}\n"
        f"TASK DESCRIPTION: {description or '(none)'}\n\n"
        f"OPEN SESSIONS:\n{listing}\n\n"
        "Being in the same repo is NOT enough — every session listed is already in "
        "this task's repo. Answer yes only if a session's branch, last commit, "
        "changed files or name show it is working on this task in particular.\n\n"
        'Reply with ONE JSON object and nothing else: {"same_task": bool, '
        '"session_name": str, "reason": str}. Put the matching session\'s name in '
        '"session_name", or "" when "same_task" is false.'
    )


def _render(session: dict, key: str) -> str:
    """A prompt field value; missing or blank reads as `unknown`, never as blank."""
    return str(session.get(key) or "").strip() or "unknown"


def parse_same_task_verdict(text: str) -> dict:
    """The first JSON object in `text`, normalised. Fails CLOSED to `same_task=False`.

    Models wrap JSON in prose or code fences, so the object is located rather
    than assumed to be the whole reply. An unreadable answer must not read as a
    collision: false means "launch", which is the pre-existing behaviour.
    """
    body = text or ""
    decoder = json.JSONDecoder()
    start = body.find("{")
    while start >= 0:
        try:
            obj, _ = decoder.raw_decode(body, start)
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            return {
                "same_task": bool(obj.get("same_task")),
                "session_name": str(obj.get("session_name") or ""),
                "reason": str(obj.get("reason") or ""),
            }
        start = body.find("{", start + 1)
    return {"same_task": False, "session_name": "", "reason": "unparseable"}
