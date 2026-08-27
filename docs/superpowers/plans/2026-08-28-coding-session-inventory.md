# Coding-Session Inventory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop AEGIS launching a coding run on a repo where a human already has a busy Claude Code session, and expose the session inventory as a read-only tool.

**Architecture:** A pure-function module parses `claude agents --json` output (which Claude Code documents for scripting) into normalised session records. `RemoteScriptConnector` gains one read-only method that runs that command per configured account over the existing SSH identity. Two launch activities consult a shared helper and return `skipped` instead of launching. Nothing is persisted; the inventory is composed on read.

**Tech Stack:** Python 3.11+, asyncpg, Temporal (temporalio), pytest + pytest-xdist, structlog, ruff.

**Spec:** `docs/superpowers/specs/2026-08-28-coding-session-inventory-design.md`

## Global Constraints

- **Nothing user-specific may be hardcoded.** No hostname, path, account label, or repo name in code. Everything comes from the `infra.coding` block. This repo is public.
- **Never surface `messagingSocketPath`, `pid`, or anything from the sibling `.key` files.** Use an explicit field allow-list so a future CLI version cannot introduce a leaking field by default.
- **Fail open, always.** Any inventory failure yields an empty collision set and the launch proceeds. That is today's behaviour, so the worst case is no worse than the status quo. Every fail-open path logs.
- **Feature defaults to OFF** (`coding.inventory.enabled = false`). A fork must opt in.
- **Do NOT run `ruff format`** on `core/src/aegis/services/chat.py` or `core/src/aegis/services/tools/infra.py`. Hand-write already-formatted edits. Verify a minimal diff with `git diff main -- <file> | grep -c '^@@'`.
- **Run tests one package at a time, in parallel, with a log:** `pytest tests/core/ -n auto --dist loadfile --timeout=300 2>&1 | tee logs/test-core.log`. A bare `pytest` deadlocks.
- **From a worktree, tests need `PYTHONPATH=core/src:worker/src:comms/src`** or pytest silently tests the main checkout's editable install.
- **Lint scoped per package**, as CI does: `ruff check core/src/ tests/core/` and the worker equivalent.
- **Every test must be falsifiable.** After writing a test, break the implementation, confirm the test fails, then revert. This lane has a history of tests that passed while production was broken.

---

### Task 1: Pure session parsing and normalisation

**Files:**
- Create: `core/src/aegis/connectors/coding_sessions.py`
- Test: `tests/core/test_coding_sessions.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `parse_agents_json(raw: str) -> list[dict]`, `normalise_repo(cwd: str, repo_base: str) -> tuple[str, str]`, `to_records(parsed: list[dict], account: str, repo_base: str) -> list[dict]`, `match_busy(sessions: list[dict], repo: str) -> list[dict]`. Tasks 3, 4 and 5 import these.

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_coding_sessions.py`:

```python
"""Pure parsing/normalisation for the coding-session inventory."""

import pytest

from aegis.connectors.coding_sessions import (
    match_busy,
    normalise_repo,
    parse_agents_json,
    to_records,
)

BASE = "/home/user/Workspace"

# Real shape of `claude agents --json`, trimmed. Note `state` (background
# sessions) vs `status` (interactive) and `id` vs `sessionId`.
SAMPLE = """[
  {"id": "451cebcb", "cwd": "/home/user/Workspace/acme/api", "kind": "background",
   "sessionId": "451cebcb-ab17-4454-99ca-ed14e84a1cf2", "name": "build thing",
   "state": "blocked"},
  {"pid": 368220, "cwd": "/home/user/Workspace/acme/api", "kind": "interactive",
   "sessionId": "493b827c-a432-4c37-a5ed-dde55c25bf1a", "name": "api-2d",
   "status": "busy", "messagingSocketPath": "/run/user/1000/cc-socks/368220.sock"}
]"""


def test_parse_skips_leading_noise():
    """One profile prints a config-restore warning before its JSON array."""
    noisy = (
        "Claude configuration file not found at: /home/user/.claude/.claude.json\n"
        "A backup file exists at: /home/user/.claude/backups/x\n\n" + SAMPLE
    )
    assert len(parse_agents_json(noisy)) == 2


def test_parse_ignores_non_dict_entries():
    assert parse_agents_json('["junk", {"cwd": "/x"}]') == [{"cwd": "/x"}]


def test_parse_empty_array():
    assert parse_agents_json("[]") == []


@pytest.mark.parametrize("raw", ["", "not json at all", "[{oops}]", "{}"])
def test_parse_rejects_unusable_output(raw):
    with pytest.raises(ValueError):
        parse_agents_json(raw)


@pytest.mark.parametrize(
    "cwd,expected",
    [
        (f"{BASE}/acme/api", ("acme/api", "human")),
        # AEGIS's own per-run worktree convention.
        (f"{BASE}/acme/api-aegis-wt/954df86d", ("acme/api", "aegis")),
        # Claude Code's own worktree convention.
        (f"{BASE}/acme/api/.claude/worktrees/issue-12", ("acme/api", "human")),
        # Outside repo_base can never match anything.
        ("/tmp/scratch", ("", "human")),
        (BASE, ("", "human")),
        ("", ("", "human")),
    ],
)
def test_normalise_repo(cwd, expected):
    assert normalise_repo(cwd, BASE) == expected


def test_records_never_leak_private_fields():
    """messagingSocketPath and pid must never reach a record."""
    records = to_records(parse_agents_json(SAMPLE), "personal", BASE)
    for record in records:
        assert "messagingSocketPath" not in record
        assert "pid" not in record
    blob = repr(records)
    assert "cc-socks" not in blob
    assert ".key" not in blob


def test_records_read_status_and_state():
    records = to_records(parse_agents_json(SAMPLE), "personal", BASE)
    by_name = {r["name"]: r for r in records}
    assert by_name["api-2d"]["status"] == "busy"
    assert by_name["build thing"]["status"] == "blocked"
    assert by_name["api-2d"]["account"] == "personal"
    assert by_name["api-2d"]["repo"] == "acme/api"


def test_match_busy_only_matches_busy_human_same_repo():
    sessions = [
        {"owner": "human", "status": "busy", "repo": "acme/api", "name": "hit"},
        {"owner": "human", "status": "idle", "repo": "acme/api", "name": "idle"},
        {"owner": "aegis", "status": "busy", "repo": "acme/api", "name": "own-run"},
        {"owner": "human", "status": "busy", "repo": "acme/web", "name": "other-repo"},
    ]
    assert [s["name"] for s in match_busy(sessions, "acme/api")] == ["hit"]


def test_match_busy_empty_repo_never_matches():
    sessions = [{"owner": "human", "status": "busy", "repo": "", "name": "x"}]
    assert match_busy(sessions, "") == []
    assert match_busy(sessions, "acme/api") == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
PYTHONPATH=core/src:worker/src:comms/src pytest tests/core/test_coding_sessions.py -v 2>&1 | tee logs/test-task1.log
```
Expected: FAIL — `ModuleNotFoundError: No module named 'aegis.connectors.coding_sessions'`.

- [ ] **Step 3: Write the implementation**

Create `core/src/aegis/connectors/coding_sessions.py`:

```python
"""Read-only inventory of coding-CLI sessions on the coding host.

Claude Code publishes a session registry per ``CLAUDE_CONFIG_DIR`` and exposes
it through ``claude agents --json`` — documented for scripting and needing no
TTY. This module turns that output into records AEGIS can reason about, and
answers the one question the launch path asks: is a human already busy in this
repo?

Everything here is pure, so the parsing rules are unit-testable without SSH.

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
import re

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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
PYTHONPATH=core/src:worker/src:comms/src pytest tests/core/test_coding_sessions.py -v 2>&1 | tee logs/test-task1.log
```
Expected: PASS, 15 tests.

- [ ] **Step 5: Prove the tests are falsifiable**

Temporarily change `match_busy` to drop the `owner` check, run the tests, and confirm `test_match_busy_only_matches_busy_human_same_repo` fails. Then temporarily add `"pid": item.get("pid")` to the `to_records` dict and confirm `test_records_never_leak_private_fields` fails. Revert both.

- [ ] **Step 6: Lint and commit**

```bash
ruff check core/src/ tests/core/
git add core/src/aegis/connectors/coding_sessions.py tests/core/test_coding_sessions.py
git commit -m "feat(coding): parse and normalise claude session registry output"
```

---

### Task 2: Validate the `inventory` config block

**Files:**
- Modify: `core/src/aegis/services/infra.py` — inside `validate_coding`, after the `tmux` block
- Test: `tests/core/test_infra_coding_validation.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `validate_coding` now returns an `"inventory"` key: `{"enabled": bool, "skip_when_busy": bool, "accounts": list[str]}`. Task 3 reads it off the infra row.

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_infra_coding_validation.py`:

```python
"""validate_coding's rules for the inventory block."""

import pytest

from aegis.services.infra import validate_coding

CLAUDE = {
    "binary_path": "/usr/local/bin/claude",
    "config_dirs": {"personal": "/home/u/.claude-personal", "work": "/home/u/.claude"},
    "default_account": "personal",
}


def _coding(**inventory):
    return {"enabled": True, "engines": {"claude": dict(CLAUDE)}, "inventory": inventory}


def test_inventory_defaults_when_absent():
    out = validate_coding({"enabled": True, "engines": {"claude": dict(CLAUDE)}})
    assert out["inventory"] == {"enabled": False, "skip_when_busy": True, "accounts": []}


def test_inventory_accepts_a_good_block():
    out = validate_coding(_coding(enabled=True, skip_when_busy=False, accounts=["personal"]))
    assert out["inventory"] == {
        "enabled": True,
        "skip_when_busy": False,
        "accounts": ["personal"],
    }


def test_inventory_must_be_an_object():
    with pytest.raises(ValueError, match="inventory"):
        validate_coding({"enabled": True, "inventory": ["nope"]})


@pytest.mark.parametrize("field", ["enabled", "skip_when_busy"])
def test_inventory_flags_must_be_boolean(field):
    with pytest.raises(ValueError, match=field):
        validate_coding(_coding(**{field: "yes"}))


def test_inventory_accounts_must_be_a_list_of_strings():
    with pytest.raises(ValueError, match="accounts"):
        validate_coding(_coding(accounts="personal"))
    with pytest.raises(ValueError, match="accounts"):
        validate_coding(_coding(accounts=[1]))


def test_inventory_account_must_exist_in_config_dirs():
    """A typo'd account would otherwise silently enumerate nothing."""
    with pytest.raises(ValueError, match="config_dirs"):
        validate_coding(_coding(accounts=["personl"]))
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
PYTHONPATH=core/src:worker/src:comms/src pytest tests/core/test_infra_coding_validation.py -v 2>&1 | tee logs/test-task2.log
```
Expected: FAIL — `KeyError: 'inventory'` on the first two, and no `ValueError` raised on the rest.

- [ ] **Step 3: Write the implementation**

In `core/src/aegis/services/infra.py`, inside `validate_coding`, immediately after the block that sets `out["tmux"] = {...}`, insert:

```python
    # Read-only session inventory (see docs/superpowers/specs/
    # 2026-08-28-coding-session-inventory-design.md). Default OFF: this changes
    # whether runs start at all, so a fork opts in deliberately.
    inv_in = _expect(coding.get("inventory", {}) or {}, dict, "inventory")
    for flag in ("enabled", "skip_when_busy"):
        if flag in inv_in and not isinstance(inv_in[flag], bool):
            raise ValueError(f"coding.inventory.{flag} must be a boolean")
    accounts_in = _expect(inv_in.get("accounts", []) or [], list, "inventory.accounts")
    accounts: list[str] = []
    claude_dirs = (engines.get("claude") or {}).get("config_dirs") or {}
    for account in accounts_in:
        if not isinstance(account, str):
            raise ValueError("coding.inventory.accounts must be a list of strings")
        if account not in claude_dirs:
            raise ValueError(
                f"coding.inventory.accounts entry {account!r} "
                "is not in engines.claude.config_dirs"
            )
        accounts.append(account)
    out["inventory"] = {
        "enabled": bool(inv_in.get("enabled", False)),
        "skip_when_busy": bool(inv_in.get("skip_when_busy", True)),
        "accounts": accounts,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
PYTHONPATH=core/src:worker/src:comms/src pytest tests/core/test_infra_coding_validation.py -v 2>&1 | tee logs/test-task2.log
```
Expected: PASS, 8 tests.

- [ ] **Step 5: Confirm nothing else broke**

```bash
PYTHONPATH=core/src:worker/src:comms/src pytest tests/core/test_infra_routes.py tests/core/test_remote_script_db_config.py -n auto --dist loadfile --timeout=300 2>&1 | tee logs/test-task2-regress.log
```
Expected: PASS.

- [ ] **Step 6: Prove the tests are falsifiable**

Temporarily change `account not in claude_dirs` to `False`, confirm `test_inventory_account_must_exist_in_config_dirs` fails, revert.

- [ ] **Step 7: Lint and commit**

```bash
ruff check core/src/ tests/core/
git add core/src/aegis/services/infra.py tests/core/test_infra_coding_validation.py
git commit -m "feat(infra): validate the coding.inventory config block"
```

---

### Task 3: `RemoteScriptConnector.list_coding_sessions`

**Files:**
- Modify: `core/src/aegis/connectors/remote_script.py` — `_env_config` defaults, `_apply_config`, `_config_from_row`, and a new public method
- Test: `tests/core/test_coding_session_inventory.py`

**Interfaces:**
- Consumes: `parse_agents_json`, `to_records` from Task 1; the `inventory` config key from Task 2.
- Produces: `async RemoteScriptConnector.list_coding_sessions() -> dict` returning `{"status": str, "sessions": list[dict], "errors": list[dict], "skip_when_busy": bool}`. `status` is one of `ok`, `disabled`, `unavailable`. Tasks 4 and 5 call it via the helper; Task 6 calls it directly.

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_coding_session_inventory.py`:

```python
"""RemoteScriptConnector.list_coding_sessions — enumeration and fail-open."""

import pytest

from aegis.connectors.remote_script import RemoteScriptConnector

BASE = "/home/u/Workspace"

ONE_BUSY = """[
  {"pid": 1, "cwd": "/home/u/Workspace/acme/api", "kind": "interactive",
   "sessionId": "s-1", "name": "api-2d", "status": "busy"}
]"""


def _connector(**overrides):
    """A connector wired to explicit config, with SSH stubbed by the caller."""
    conn = RemoteScriptConnector(host="codehost", user="u", key_file="/dev/null")
    conn._host = "codehost"
    conn._repo_base = BASE
    conn._claude_binary = "/usr/local/bin/claude"
    conn._claude_config_dirs = {"personal": "/home/u/.claude-personal"}
    conn._inventory_config = {"enabled": True, "skip_when_busy": True, "accounts": []}
    conn._inventory_config.update(overrides)
    return conn


def _stub_exec(conn, results):
    """Replace _exec and _refresh_config; record the commands issued."""
    issued = []

    async def fake_exec(host, remote_cmd, timeout, **kwargs):
        issued.append(remote_cmd)
        return results.pop(0)

    async def noop_refresh():
        return None

    conn._exec = fake_exec
    conn._refresh_config = noop_refresh
    return issued


def _ok(stdout):
    return {"status": "succeeded", "exit_code": 0, "stdout": stdout, "stderr": ""}


def _fail(stderr="ssh: connect failed"):
    return {"status": "failed", "exit_code": -1, "stdout": "", "stderr": stderr}


@pytest.mark.asyncio
async def test_disabled_makes_no_ssh_call():
    conn = _connector(enabled=False)
    issued = _stub_exec(conn, [])
    result = await conn.list_coding_sessions()
    assert result["status"] == "disabled"
    assert result["sessions"] == []
    assert issued == []


@pytest.mark.asyncio
async def test_enumerates_each_account_and_normalises():
    conn = _connector()
    conn._claude_config_dirs = {"personal": "/home/u/.cp", "work": "/home/u/.cw"}
    issued = _stub_exec(conn, [_ok(ONE_BUSY), _ok("[]")])
    result = await conn.list_coding_sessions()
    assert result["status"] == "ok"
    assert len(issued) == 2
    assert "CLAUDE_CONFIG_DIR=/home/u/.cp" in issued[0]
    assert "agents --json" in issued[0]
    assert result["sessions"][0]["repo"] == "acme/api"
    assert result["sessions"][0]["account"] == "personal"


@pytest.mark.asyncio
async def test_accounts_allow_list_limits_enumeration():
    conn = _connector(accounts=["work"])
    conn._claude_config_dirs = {"personal": "/home/u/.cp", "work": "/home/u/.cw"}
    issued = _stub_exec(conn, [_ok("[]")])
    await conn.list_coding_sessions()
    assert len(issued) == 1
    assert "/home/u/.cw" in issued[0]


@pytest.mark.asyncio
async def test_one_account_failing_does_not_lose_the_other():
    conn = _connector()
    conn._claude_config_dirs = {"personal": "/home/u/.cp", "work": "/home/u/.cw"}
    _stub_exec(conn, [_fail(), _ok(ONE_BUSY)])
    result = await conn.list_coding_sessions()
    assert result["status"] == "ok"
    assert len(result["sessions"]) == 1
    assert result["errors"][0]["account"] == "personal"


@pytest.mark.asyncio
async def test_all_accounts_failing_is_unavailable():
    conn = _connector()
    _stub_exec(conn, [_fail()])
    result = await conn.list_coding_sessions()
    assert result["status"] == "unavailable"
    assert result["sessions"] == []


@pytest.mark.asyncio
async def test_unparseable_output_is_an_error_not_an_exception():
    conn = _connector()
    _stub_exec(conn, [_ok("segfault, sorry")])
    result = await conn.list_coding_sessions()
    assert result["status"] == "unavailable"
    assert "no JSON array" in result["errors"][0]["error"]


@pytest.mark.asyncio
async def test_no_claude_binary_is_unavailable_not_a_crash():
    conn = _connector()
    conn._claude_binary = ""
    issued = _stub_exec(conn, [])
    result = await conn.list_coding_sessions()
    assert result["status"] == "unavailable"
    assert issued == []


@pytest.mark.asyncio
async def test_config_dir_is_shell_quoted():
    conn = _connector()
    conn._claude_config_dirs = {"odd": "/home/u/dir with space"}
    issued = _stub_exec(conn, [_ok("[]")])
    await conn.list_coding_sessions()
    assert "'/home/u/dir with space'" in issued[0]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
PYTHONPATH=core/src:worker/src:comms/src pytest tests/core/test_coding_session_inventory.py -v 2>&1 | tee logs/test-task3.log
```
Expected: FAIL — `AttributeError: 'RemoteScriptConnector' object has no attribute 'list_coding_sessions'`.

- [ ] **Step 3: Wire the config through**

In `core/src/aegis/connectors/remote_script.py`:

a) Add the import near the existing local imports at the top of the module:

```python
from aegis.connectors.coding_sessions import parse_agents_json, to_records
```

b) In `_apply_config`, after the `self._api_key = cfg["api_key"]` line, add:

```python
        self._inventory_config = cfg["inventory"]
```

c) In `_config_from_row`, add `inventory = coding.get("inventory") or {}` beside the other `coding.get(...)` unpacking at the top of the method, then add this entry to the returned dict, next to `"tmux_window_cap"`:

```python
            "inventory": {
                "enabled": bool(inventory.get("enabled", False)),
                "skip_when_busy": bool(inventory.get("skip_when_busy", True)),
                "accounts": [str(a) for a in (inventory.get("accounts") or [])],
            },
```

d) In the `_env_config` dict built in `__init__`, add the matching default so an
env-configured connector has the key too:

```python
            "inventory": {"enabled": False, "skip_when_busy": True, "accounts": []},
```

- [ ] **Step 4: Add the method**

Add to `RemoteScriptConnector`, directly after `coding_settings`:

```python
    async def list_coding_sessions(self) -> dict:
        """Read-only inventory of Claude Code sessions on the coding host.

        Runs `claude agents --json` once per configured account over the SSH
        identity this connector already uses — no new credential. The command
        is documented for scripting and needs no TTY.

        Returns ``{"status", "sessions", "errors", "skip_when_busy"}`` where
        status is ``disabled`` (feature off), ``unavailable`` (nothing could be
        enumerated) or ``ok`` (including partial success with errors).

        NEVER raises. The launch path treats anything other than ``ok`` as "no
        collision known" and proceeds — a broken inventory must not become an
        outage of the coding lane.
        """
        await self._refresh_config()
        cfg = getattr(self, "_inventory_config", None) or {}
        skip = bool(cfg.get("skip_when_busy", True))
        if not cfg.get("enabled"):
            return {"status": "disabled", "sessions": [], "errors": [], "skip_when_busy": skip}

        dirs = self._claude_config_dirs or {}
        if not self._claude_binary or not dirs:
            return {
                "status": "unavailable",
                "sessions": [],
                "errors": [{"account": "", "error": "claude binary or config_dirs not configured"}],
                "skip_when_busy": skip,
            }

        sessions: list[dict] = []
        errors: list[dict] = []
        for account in cfg.get("accounts") or list(dirs):
            config_dir = dirs.get(account)
            if not config_dir:
                errors.append({"account": account, "error": "no config_dir for account"})
                continue
            cmd = (
                f"CLAUDE_CONFIG_DIR={shlex.quote(config_dir)} "
                f"{shlex.quote(self._claude_binary)} agents --json"
            )
            result = await self._exec(self._host, cmd, timeout=20, batch_mode=True)
            if result["status"] != "succeeded":
                errors.append({"account": account, "error": str(result["stderr"])[:200]})
                continue
            try:
                parsed = parse_agents_json(result["stdout"])
            except ValueError as exc:
                errors.append({"account": account, "error": str(exc)[:200]})
                continue
            sessions.extend(to_records(parsed, account, self._repo_base))

        status = "ok" if sessions or not errors else "unavailable"
        return {
            "status": status,
            "sessions": sessions,
            "errors": errors,
            "skip_when_busy": skip,
        }
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
PYTHONPATH=core/src:worker/src:comms/src pytest tests/core/test_coding_session_inventory.py tests/core/test_remote_script_db_config.py -v 2>&1 | tee logs/test-task3.log
```
Expected: PASS.

- [ ] **Step 6: Prove the tests are falsifiable**

Temporarily replace `shlex.quote(config_dir)` with plain `config_dir`, confirm `test_config_dir_is_shell_quoted` fails, revert. Temporarily make the `result["status"] != "succeeded"` branch `raise`, confirm `test_one_account_failing_does_not_lose_the_other` fails, revert.

- [ ] **Step 7: Lint and commit**

```bash
ruff check core/src/ tests/core/
git add core/src/aegis/connectors/remote_script.py tests/core/test_coding_session_inventory.py
git commit -m "feat(coding): enumerate claude sessions on the coding host"
```

---

### Task 4: The guard, and the dispatch lane

**Files:**
- Modify: `core/src/aegis/connectors/coding_sessions.py` — add the async helper
- Modify: `worker/src/aegis_worker/activities/agent_run.py` — `launch_agent_run`
- Modify: `worker/src/aegis_worker/flows/agent_run.py` — handle `skipped`
- Test: `tests/core/test_coding_sessions.py` (helper), `tests/worker/test_agent_run_skip.py` (activity)

**Interfaces:**
- Consumes: `match_busy` from Task 1, `list_coding_sessions` from Task 3.
- Produces: `async busy_human_sessions(remote_script, repo: str) -> list[dict]`. `launch_agent_run` may now return `{"status": "skipped", "reason": "repo_busy", "repo": str, "sessions": list[dict]}`. Task 5 uses the same helper.

- [ ] **Step 1: Write the failing helper tests**

Append to `tests/core/test_coding_sessions.py`:

```python
import pytest

from aegis.connectors.coding_sessions import busy_human_sessions


class _FakeConnector:
    def __init__(self, result=None, boom=False):
        self._result = result or {}
        self._boom = boom

    async def list_coding_sessions(self):
        if self._boom:
            raise RuntimeError("ssh exploded")
        return self._result


_BUSY_RESULT = {
    "status": "ok",
    "skip_when_busy": True,
    "sessions": [{"owner": "human", "status": "busy", "repo": "acme/api", "name": "api-2d"}],
    "errors": [],
}


@pytest.mark.asyncio
async def test_helper_returns_the_collision():
    found = await busy_human_sessions(_FakeConnector(_BUSY_RESULT), "acme/api")
    assert [s["name"] for s in found] == ["api-2d"]


@pytest.mark.asyncio
async def test_helper_observe_only_mode_never_blocks():
    """skip_when_busy=False reports nothing to block on, by design."""
    result = dict(_BUSY_RESULT, skip_when_busy=False)
    assert await busy_human_sessions(_FakeConnector(result), "acme/api") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        {"status": "disabled", "sessions": [], "errors": [], "skip_when_busy": True},
        {"status": "unavailable", "sessions": [], "errors": [], "skip_when_busy": True},
    ],
)
async def test_helper_fails_open_on_non_ok(result):
    assert await busy_human_sessions(_FakeConnector(result), "acme/api") == []


@pytest.mark.asyncio
async def test_helper_fails_open_when_the_connector_raises():
    assert await busy_human_sessions(_FakeConnector(boom=True), "acme/api") == []


@pytest.mark.asyncio
async def test_helper_fails_open_with_no_connector():
    assert await busy_human_sessions(None, "acme/api") == []


def test_fake_connector_matches_the_real_signature():
    """Guards against the fake drifting from RemoteScriptConnector."""
    import inspect

    from aegis.connectors.remote_script import RemoteScriptConnector

    real = inspect.signature(RemoteScriptConnector.list_coding_sessions)
    fake = inspect.signature(_FakeConnector.list_coding_sessions)
    assert list(real.parameters) == list(fake.parameters)
```

- [ ] **Step 2: Run to verify failure**

```bash
PYTHONPATH=core/src:worker/src:comms/src pytest tests/core/test_coding_sessions.py -v 2>&1 | tee logs/test-task4.log
```
Expected: FAIL — `ImportError: cannot import name 'busy_human_sessions'`.

- [ ] **Step 3: Add the helper**

Append to `core/src/aegis/connectors/coding_sessions.py` (and add `import logging` plus `logger = logging.getLogger(__name__)` near the top):

```python
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
```

- [ ] **Step 4: Run helper tests, expect PASS**

```bash
PYTHONPATH=core/src:worker/src:comms/src pytest tests/core/test_coding_sessions.py -v 2>&1 | tee logs/test-task4.log
```

- [ ] **Step 5: Write the failing activity test**

Create `tests/worker/test_agent_run_skip.py`:

```python
"""launch_agent_run defers to a busy human session."""

import pytest
from temporalio.testing import ActivityEnvironment

from aegis_worker.activities.agent_run import AgentRunActivities

BUSY = [{"owner": "human", "status": "busy", "repo": "acme/api", "name": "api-2d"}]


class _Connector:
    def __init__(self, sessions):
        self._sessions = sessions
        self.started = False

    async def list_coding_sessions(self):
        return {
            "status": "ok",
            "sessions": self._sessions,
            "errors": [],
            "skip_when_busy": True,
        }

    async def coding_settings(self):
        return {"kimi_binary": "/bin/kimi", "repo_base": "/w"}

    async def start_kimi_run(self, **kwargs):
        self.started = True
        return {"status": "running", "run_id": "r1", "output_file": "/tmp/o", "host": "h"}


@pytest.mark.asyncio
async def test_skips_when_a_human_is_busy_in_the_repo():
    conn = _Connector(BUSY)
    acts = AgentRunActivities(remote_script=conn)
    result = await ActivityEnvironment().run(acts.launch_agent_run, "do it", "acme/api")
    assert result["status"] == "skipped"
    assert result["reason"] == "repo_busy"
    assert conn.started is False


@pytest.mark.asyncio
async def test_launches_when_the_session_is_idle():
    idle = [dict(BUSY[0], status="idle")]
    conn = _Connector(idle)
    acts = AgentRunActivities(remote_script=conn)
    result = await ActivityEnvironment().run(acts.launch_agent_run, "do it", "acme/api")
    assert result["status"] == "running"
    assert conn.started is True


@pytest.mark.asyncio
async def test_launches_when_the_busy_session_is_aegis_own_run():
    own = [dict(BUSY[0], owner="aegis")]
    conn = _Connector(own)
    acts = AgentRunActivities(remote_script=conn)
    result = await ActivityEnvironment().run(acts.launch_agent_run, "do it", "acme/api")
    assert result["status"] == "running"
```

- [ ] **Step 6: Run to verify failure**

```bash
PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/test_agent_run_skip.py -v 2>&1 | tee logs/test-task4b.log
```
Expected: FAIL — first test returns `running`, not `skipped`.

- [ ] **Step 7: Add the guard to the activity**

In `worker/src/aegis_worker/activities/agent_run.py`, add the import:

```python
from aegis.connectors.coding_sessions import busy_human_sessions
```

Then in `launch_agent_run`, immediately after `target_repo = (repo or "").strip() or SCRATCH_REPO` and before `settings = await self.remote_script.coding_settings()`:

```python
        # Defer to a human already mid-thought in this repo. Fails open, so an
        # unreachable or disabled inventory launches exactly as it does today.
        busy = await busy_human_sessions(self.remote_script, target_repo)
        if busy:
            logger.warning(
                "coding_run_skipped_repo_busy",
                repo=target_repo,
                sessions=[s.get("name") for s in busy],
            )
            return {
                "status": "skipped",
                "reason": "repo_busy",
                "repo": target_repo,
                "sessions": busy,
            }
```

If the module has no `logger`, add `import structlog` and `logger = structlog.get_logger()` at module level, matching the sibling activity modules.

- [ ] **Step 8: Run tests, expect PASS**

```bash
PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/test_agent_run_skip.py tests/worker/test_agent_run_flow.py -v 2>&1 | tee logs/test-task4b.log
```

- [ ] **Step 9: Handle `skipped` in the flow**

In `worker/src/aegis_worker/flows/agent_run.py`, immediately after the `launch_agent_run` activity result is obtained and before the existing failure branch, add:

```python
        if launched.get("status") == "skipped":
            names = ", ".join(str(s.get("name") or "?") for s in launched.get("sessions") or [])
            await self._deliver(
                input,
                f"Skipped `{launched.get('repo', '')}` — you have a session working "
                f"there already ({names}).",
            )
            return self._result(status="skipped", reason="repo_busy")
```

Match the existing call signatures of `_deliver` and `_result` in that file; adjust argument names if they differ.

- [ ] **Step 10: Prove falsifiable, lint, commit**

Temporarily delete the `if busy:` block, confirm `test_skips_when_a_human_is_busy_in_the_repo` fails, revert.

```bash
ruff check core/src/ tests/core/ && ruff check worker/src/ tests/worker/
git add core/src/aegis/connectors/coding_sessions.py tests/core/test_coding_sessions.py \
        worker/src/aegis_worker/activities/agent_run.py worker/src/aegis_worker/flows/agent_run.py \
        tests/worker/test_agent_run_skip.py
git commit -m "feat(agent-run): skip a dispatch when a human is busy in the repo"
```

---

### Task 5: The Todoist task lane

**Files:**
- Modify: `worker/src/aegis_worker/activities/agent_task.py` — `run_task_investigation`
- Modify: `worker/src/aegis_worker/flows/agent_task.py` — `_investigate_coding_task`, and the `_run_coding` docstring
- Test: `tests/worker/test_agent_task_skip.py`

**Interfaces:**
- Consumes: `busy_human_sessions` from Task 4.
- Produces: `run_task_investigation` may return `{"status": "skipped", "reason": "repo_busy", "transcript": "", "run_id": ""}`. `_investigate_coding_task` returns a dict with no `"plan"` key on that path, which `_run_coding` already treats as an early exit.

- [ ] **Step 1: Write the failing test**

Create `tests/worker/test_agent_task_skip.py`:

```python
"""run_task_investigation defers to a busy human session, without parking."""

import pytest
from temporalio.testing import ActivityEnvironment

from aegis_worker.activities.agent_task import AgentTaskActivities

BUSY = [{"owner": "human", "status": "busy", "repo": "acme/api", "name": "api-2d"}]


class _Connector:
    def __init__(self, sessions):
        self._sessions = sessions
        self.started = False

    async def list_coding_sessions(self):
        return {
            "status": "ok",
            "sessions": self._sessions,
            "errors": [],
            "skip_when_busy": True,
        }

    async def coding_settings(self):
        return {"kimi_binary": "/bin/kimi", "repo_base": "/w"}

    async def start_kimi_run(self, **kwargs):
        self.started = True
        return {"status": "running", "run_id": "r1", "output_file": "/tmp/o", "host": "h"}


async def _investigate(conn):
    acts = AgentTaskActivities(remote_script=conn)
    return await ActivityEnvironment().run(
        acts.run_task_investigation, "t1", "Fix it", "", "acme/api", "acme/api"
    )


@pytest.mark.asyncio
async def test_skips_when_a_human_is_busy():
    conn = _Connector(BUSY)
    result = await _investigate(conn)
    assert result["status"] == "skipped"
    assert result["reason"] == "repo_busy"
    assert conn.started is False


@pytest.mark.asyncio
async def test_runs_when_nobody_is_busy():
    conn = _Connector([])
    result = await _investigate(conn)
    assert result["status"] == "running"
    assert conn.started is True
```

- [ ] **Step 2: Run to verify failure**

```bash
PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/test_agent_task_skip.py -v 2>&1 | tee logs/test-task5.log
```
Expected: FAIL — returns `running` rather than `skipped`.

- [ ] **Step 3: Add the guard**

In `worker/src/aegis_worker/activities/agent_task.py` add the import:

```python
from aegis.connectors.coding_sessions import busy_human_sessions
```

In `run_task_investigation`, immediately after the existing
`if self.remote_script is None or not repo_path:` early return and before `prompt = (`:

```python
        # Entry point of the coding lane: defer to a human already busy in this
        # repo. The caller must NOT park on this path — see the flow.
        busy = await busy_human_sessions(self.remote_script, repo_path)
        if busy:
            activity.logger.warning(
                "coding_run_skipped_repo_busy repo=%s sessions=%s",
                repo_path,
                [s.get("name") for s in busy],
            )
            return {
                "status": "skipped",
                "reason": "repo_busy",
                "transcript": "",
                "run_id": "",
            }
```

- [ ] **Step 4: Run tests, expect PASS**

```bash
PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/test_agent_task_skip.py -v 2>&1 | tee logs/test-task5.log
```

- [ ] **Step 5: Handle it in the flow without parking**

In `worker/src/aegis_worker/flows/agent_task.py`, inside `_investigate_coding_task`, insert this **before** the existing `if investigation.get("status") == "failed":` branch:

```python
        # A transient collision must NOT park: park_task stamps @waiting, which
        # removes the task from find_actionable_tasks' pool until something
        # unparks it. A busy afternoon would otherwise retire the task for good.
        # Returning without "plan" makes _run_coding exit; the cooldown on this
        # workflow's terminal run row defers the retry.
        if investigation.get("status") == "skipped":
            return {
                "status": "skipped",
                "reason": "repo_busy",
                "task_id": task_id,
            }
```

Then update the `_run_coding` docstring, replacing "Every exit path parks" with:

```
        Every exit path parks EXCEPT a repo-busy skip, which must leave the task
        untouched so it stays in the eligible pool.
```

and the inline comment `# early exit already parked (no repo / failed / empty)` with:

```python
            return setup  # early exit: parked, or a repo-busy skip that must not park
```

- [ ] **Step 6: Prove falsifiable**

Temporarily delete the `if busy:` block in the activity, confirm `test_skips_when_a_human_is_busy` fails, revert. Temporarily change the flow's skip branch to call `self._park_coding(...)`, run the worker flow tests, and confirm nothing silently passes — then revert.

- [ ] **Step 7: Lint and commit**

```bash
ruff check worker/src/ tests/worker/
git add worker/src/aegis_worker/activities/agent_task.py worker/src/aegis_worker/flows/agent_task.py \
        tests/worker/test_agent_task_skip.py
git commit -m "feat(agent-task): skip a coding task when a human is busy in the repo"
```

---

### Task 6: The `list_coding_sessions` chat tool

**Files:**
- Modify: `core/src/aegis/services/chat.py` — add to `CHAT_TOOLS` and `TOOL_EXECUTORS`
- Test: `tests/core/test_chat_coding_sessions_tool.py`

**Interfaces:**
- Consumes: `list_coding_sessions` from Task 3.
- Produces: chat tool `list_coding_sessions`, executor `_exec_list_coding_sessions(ctx, args) -> str`.

**DO NOT run `ruff format` on `chat.py`.** Write already-formatted edits by hand.

- [ ] **Step 1: Write the failing test**

Create `tests/core/test_chat_coding_sessions_tool.py`:

```python
"""The read-only list_coding_sessions chat tool."""

import pytest

from aegis.services.chat import CHAT_TOOLS, TOOL_EXECUTORS

TOOL = "list_coding_sessions"


def test_tool_is_registered_and_dispatchable():
    """A schema without an executor is advertised but never runs, and vice versa."""
    assert any(t["name"] == TOOL for t in CHAT_TOOLS)
    assert TOOL in TOOL_EXECUTORS


@pytest.mark.asyncio
async def test_reports_sessions():
    from aegis.services.chat import _exec_list_coding_sessions

    class _Ctx:
        class remote_script:
            @staticmethod
            async def list_coding_sessions():
                return {
                    "status": "ok",
                    "sessions": [
                        {
                            "account": "personal",
                            "name": "api-2d",
                            "repo": "acme/api",
                            "status": "busy",
                            "owner": "human",
                            "kind": "interactive",
                            "session_id": "s-1",
                            "cwd": "/w/acme/api",
                        }
                    ],
                    "errors": [],
                    "skip_when_busy": True,
                }

    out = await _exec_list_coding_sessions(_Ctx(), {})
    assert "api-2d" in out
    assert "acme/api" in out
    assert "cc-socks" not in out


@pytest.mark.asyncio
async def test_disabled_says_so_rather_than_looking_empty():
    from aegis.services.chat import _exec_list_coding_sessions

    class _Ctx:
        class remote_script:
            @staticmethod
            async def list_coding_sessions():
                return {"status": "disabled", "sessions": [], "errors": [], "skip_when_busy": True}

    out = await _exec_list_coding_sessions(_Ctx(), {})
    assert "disabled" in out.lower()
```

- [ ] **Step 2: Run to verify failure**

```bash
PYTHONPATH=core/src:worker/src:comms/src pytest tests/core/test_chat_coding_sessions_tool.py -v 2>&1 | tee logs/test-task6.log
```
Expected: FAIL — tool not in `CHAT_TOOLS`.

- [ ] **Step 3: Add the schema**

In `core/src/aegis/services/chat.py`, add to the `CHAT_TOOLS` list, matching the surrounding entries' hand-laid-out style exactly:

```python
    {
        "name": "list_coding_sessions",
        "description": (
            "List coding-CLI sessions currently open on the coding host, across every "
            "configured account. Read-only. Shows which repo each session is in and "
            "whether it is busy, and marks AEGIS's own runs as owner=aegis. Use it to "
            "answer 'what is running on the coding host?' or to check before asking for "
            "a coding run."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
```

- [ ] **Step 4: Add the executor and register it**

Add the executor beside the other `_exec_*` functions in `chat.py`:

```python
async def _exec_list_coding_sessions(ctx, args: dict) -> str:
    """Read-only inventory of coding sessions on the coding host."""
    connector = getattr(ctx, "remote_script", None)
    if connector is None:
        return "The coding host is not configured."
    result = await connector.list_coding_sessions()
    status = result.get("status")
    if status == "disabled":
        return "Session inventory is disabled for this coding host (coding.inventory.enabled)."
    sessions = result.get("sessions") or []
    errors = result.get("errors") or []
    if not sessions and status != "ok":
        detail = "; ".join(f"{e.get('account', '?')}: {e.get('error', '')}" for e in errors)
        return f"Could not read the session inventory. {detail}".strip()
    if not sessions:
        return "No coding sessions are open on the coding host."
    lines = [
        f"- {s.get('name') or s.get('session_id')} "
        f"[{s.get('account')}] {s.get('repo') or s.get('cwd')} "
        f"— {s.get('status')} ({s.get('owner')})"
        for s in sessions
    ]
    if errors:
        lines.append(f"({len(errors)} account(s) could not be read)")
    return "\n".join(lines)
```

Then add the registry entry to `TOOL_EXECUTORS`, in the same hand-laid-out style:

```python
    "list_coding_sessions": _exec_list_coding_sessions,
```

- [ ] **Step 5: Run tests and verify the diff stayed small**

```bash
PYTHONPATH=core/src:worker/src:comms/src pytest tests/core/test_chat_coding_sessions_tool.py -v 2>&1 | tee logs/test-task6.log
git diff main -- core/src/aegis/services/chat.py | grep -c '^@@'
```
Expected: tests PASS; the hunk count should be 3 or fewer. A large number means `ruff format` ran — revert and redo by hand.

- [ ] **Step 6: Lint and commit**

```bash
ruff check core/src/ tests/core/
git add core/src/aegis/services/chat.py tests/core/test_chat_coding_sessions_tool.py
git commit -m "feat(chat): add read-only list_coding_sessions tool"
```

---

### Task 7: Admin UI fields and documentation

**Files:**
- Modify: `admin-panel/frontend/src/pages/Infra.tsx` — `CodingFormData`, `codingFromRow`, `codingToPayload`, `codingTouched`, and the form
- Modify: `docs/infrastructure.md` — document the `inventory` block

**Interfaces:**
- Consumes: the `inventory` shape from Task 2.
- Produces: nothing other tasks depend on.

- [ ] **Step 1: Read the existing coding form**

```bash
grep -n "CodingFormData\|codingFromRow\|codingToPayload\|codingTouched" admin-panel/frontend/src/pages/Infra.tsx
```
Read `admin-panel/frontend/src/pages/Infra.tsx:16-142` to match the established pattern before editing. Follow it exactly rather than inventing a new one.

- [ ] **Step 2: Add the three fields**

Extend `CodingFormData` with `inventoryEnabled: boolean`, `inventorySkipWhenBusy: boolean`, and `inventoryAccounts: string` (comma-separated in the form, split to an array in the payload). Round-trip them through `codingFromRow`, `codingToPayload` and `codingTouched` exactly as the neighbouring tmux fields are handled. Add two checkboxes and one text input to the coding section, labelled:

- "Check for open sessions before launching a run"
- "Skip the launch when a session is busy (off = log only)"
- "Accounts to check (blank = all)"

- [ ] **Step 3: Build the frontend**

```bash
cd admin-panel/frontend && npm run build 2>&1 | tail -20; cd ../..
```
Expected: build succeeds with no type errors.

- [ ] **Step 4: Document the block**

Add a subsection to `docs/infrastructure.md` under the coding-host documentation:

````markdown
### Session inventory

Before starting a coding run, AEGIS can check whether one of your own Claude
Code sessions is already busy in the same repo, and skip rather than compete
with you. It reads `claude agents --json` — the documented, TTY-free listing —
once per configured account, over the SSH identity the coding host already uses.
Nothing is stored: the inventory is read fresh each time.

```json
"inventory": {
  "enabled": false,
  "skip_when_busy": true,
  "accounts": []
}
```

- `enabled` — off by default. Turn it on deliberately: it changes whether runs start.
- `skip_when_busy` — set false to log collisions without acting on them, to see
  what it would do before letting it decide.
- `accounts` — restrict to some of `engines.claude.config_dirs`; blank means all.

A busy session only blocks a run when it is human-owned. AEGIS's own runs live in
`<repo>-aegis-wt/<run_id>` worktrees and are recognised as its own, so runs never
block each other. Any failure to read the inventory fails open and the run starts.

Skipped runs appear in `workflow_runs` with `result_summary.reason = "repo_busy"`.
A task skipped this way is retried after the sweep's `cooldown_hours`, not on the
next tick, because the workflow completed. Lower `cooldown_hours` if you want
faster retries.
````

- [ ] **Step 5: Commit**

```bash
git add admin-panel/frontend/src/pages/Infra.tsx docs/infrastructure.md
git commit -m "feat(admin): expose the coding session-inventory settings"
```

---

### Task 8: Full suite, and the follow-up issue

- [ ] **Step 1: Run all three packages exactly as CI does**

```bash
mkdir -p logs
PYTHONPATH=core/src:worker/src:comms/src pytest tests/core/ -n auto --dist loadfile --timeout=300 2>&1 | tee logs/test-core.log
PYTHONPATH=core/src:worker/src:comms/src pytest tests/worker/ -n auto --dist loadfile --timeout=300 2>&1 | tee logs/test-worker.log
PYTHONPATH=core/src:worker/src:comms/src pytest tests/comms/ -n auto --dist loadfile --timeout=300 2>&1 | tee logs/test-comms.log
```
Expected: all green. Postgres must be up (`docker compose up -d postgres`, port 25432).

- [ ] **Step 2: Lint per package**

```bash
ruff check core/src/ tests/core/
ruff check worker/src/ tests/worker/
ruff check comms/src/ tests/comms/
```

- [ ] **Step 3: Confirm the CI path filter covers the new inputs**

Check `.github/workflows/` — the core and worker test workflows are `paths:`-filtered. Confirm `core/src/**`, `worker/src/**` and `tests/**` are already listed. A missing path silently disarms the tests rather than skipping them visibly.

- [ ] **Step 4: File the out-of-scope issue**

```bash
gh issue create --label enhancement \
  --title "PROJECT_REPO_MAP hardcodes one operator's Todoist projects and repos" \
  --body "\`PROJECT_REPO_MAP\` in \`worker/src/aegis_worker/activities/agent_task.py:107-112\` maps Todoist project names to GitHub repositories as Python constants, so a public repository ships one operator's project and repo names and no fork can change them without editing code.

Move it to DB configuration in the manner of \`content_routes\` (\`core/src/aegis/services/content_routes.py\`) or \`gtd_rules\`: a \`settings\` row, an admin editor, and the Python dict demoted to a seed-time default.

Found while implementing the coding-session inventory (\`docs/superpowers/specs/2026-08-28-coding-session-inventory-design.md\`); deliberately kept out of that change."
```

- [ ] **Step 5: Open the PR**

```bash
git push -u origin worktree-coding-session-inventory
gh pr create --title "feat(coding): skip a run when a human is already busy in the repo" \
  --body "$(cat <<'EOF'
Implements PR1 of `docs/superpowers/specs/2026-08-28-coding-session-inventory-design.md`.

AEGIS launched coding runs blind: nothing checked whether someone was already working in the same repo on the coding host. This reads the session registry Claude Code already publishes and defers when a human is busy there.

## What it does

- `claude agents --json` per configured account over the existing SSH identity — no new credential.
- Normalises both worktree conventions so a worktree compares equal to its repo.
- Tags AEGIS's own runs `owner=aegis` so runs never block each other.
- `launch_agent_run` and `run_task_investigation` return `skipped` instead of launching.
- New read-only `list_coding_sessions` chat tool.

## Safety

- Defaults OFF (`coding.inventory.enabled`), so a fork gets no behaviour change and no extra SSH calls.
- Fails open everywhere: a broken inventory launches exactly as today.
- Output fields are an explicit allow-list. `messagingSocketPath`, `pid` and the sibling `.key` files are never read or surfaced — they are an undocumented private interface and auth material.
- Nothing user-specific is hardcoded; all config comes from the `infra.coding` block.
- A skip never parks the task, so a transient collision cannot retire it.

## Known trade-off

A skipped Todoist task is retried after the sweep's `cooldown_hours` rather than on the next tick, because the child workflow completes. The sweep cannot filter earlier: it does not know a task's repo until `resolve_task_repo` runs inside the flow.
EOF
)"
```

---

## Self-Review

**Spec coverage.** Configuration → Task 2 and Task 7. Enumeration → Task 3. Session record and dropped fields → Task 1. Parsing rules → Task 1. The guard and both call sites → Tasks 4 and 5. No-park rule → Task 5. Visibility (log line, `workflow_runs` row, dispatch chat message) → Tasks 4 and 5. Read-only visibility → Task 6. Fail open → Tasks 1, 3, 4. No persistence → nothing to build. Testing → every task, plus Task 8. Out-of-scope issue → Task 8.

**Placeholders.** None. Every code step carries the actual code. Task 7 describes UI edits against a pattern the implementer is told to read first, because inventing a shape for that file without reading it would be worse than following it.

**Type consistency.** `parse_agents_json`, `normalise_repo`, `to_records`, `match_busy` and `busy_human_sessions` keep the same names and signatures across Tasks 1, 3, 4, 5 and 6. The inventory dict keys `status`, `sessions`, `errors`, `skip_when_busy` are identical in Task 3's implementation, Task 4's and Task 5's fakes, and Task 6's executor. The skip dict keys `status`, `reason`, `repo`/`transcript`, `sessions` match between activity and flow.
