"""RemoteScript connector — execute scripts on remote machines via SSH.

Used by the kimi_code action handler to run Kimi CLI on remote hosts,
and by the script action handler for general remote execution.

Configuration is DB-first: when constructed with a `db_pool`, the connector
resolves its SSH identity (host/user/port/key) and coding-agent settings from
the infra registry row whose `coding->>'enabled'` is true (admin Infra page),
refreshing every ~30s so admin edits apply without a restart. The stored SSH
key is decrypted and materialized to a mode-0600 temp file per SSH invocation
and unlinked immediately after — no key file lives on any volume. When no
enabled row exists, the env-derived constructor arguments apply unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shlex
import tempfile
import time
from collections.abc import Iterator
from typing import Any

import structlog

from aegis.connectors._ssh import build_ssh_args
from aegis.connectors._subprocess import kill_and_wait
from aegis.connectors.coding_sessions import parse_agents_json, to_records
from aegis.crypto import decrypt_secret
from aegis.services.mcp_tokens import DEFAULT_TTL_SECONDS as DEFAULT_MOUNT_TTL_SECONDS
from aegis.services.mcp_tokens import mint_mount_token

logger = structlog.get_logger()

# How long a DB-resolved config is trusted before the infra registry is
# re-read. Short enough that admin edits apply without a restart, long
# enough that a burst of SSH calls doesn't hammer the DB.
_DB_CONFIG_TTL_SECONDS = 30.0

# script_id may contain alphanumerics, underscore, dot, hyphen, and a single
# forward slash for one level of subdirectory (e.g. "infra/infra_list_nodes").
# ".." is rejected outright to prevent path traversal.
_SCRIPT_ID_RE = re.compile(r"^[a-zA-Z0-9_\-./]+$")

# tmux window names for agent runs are "<engine>-<repo>-<run_id>"; the planner
# counts only these toward the cap so the session's default shell window is
# ignored.
_AGENT_WINDOW_PREFIXES = ("kimi-", "claude-")

# The MCP server key written into a run's client config (`_mcp_run_config`).
_MCP_SERVER_NAME = "aegis"

# The synthetic approval tool AEGIS's own MCP server serves for gated runs
# (`api/routes/mcp_server.py::APPROVAL_TOOL_NAME`). Addressed through the
# `mcp__<server>__<tool>` naming the CLI resolves `--permission-prompt-tool`
# against, so BOTH halves have to match core — a rename on either side turns
# every gated tool use into an unresolvable prompt. Asserted in
# `tests/core/test_kimi_connector.py`.
PERMISSION_PROMPT_TOOL = f"mcp__{_MCP_SERVER_NAME}__approve_tool_use"

# claude's per-MCP-tool-call timeout, in MILLISECONDS. A gated run's approval
# call blocks on a human being, so the stock timeout would abort the prompt long
# before anyone could answer. 10 minutes, deliberately longer than core's 9-min
# approval cap (`AGENT_RUN_APPROVAL_TIMEOUT_S`): the slow case must come back as
# a clean DENY from AEGIS, not as a transport failure inside the run.
MCP_TOOL_TIMEOUT_MS = 600_000


def _sanitize_window_repo(repo: str) -> str:
    """Basename of `repo` with all non [A-Za-z0-9_-] chars replaced by '-'."""
    base = repo.rstrip("/").split("/")[-1]
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "-", base)
    return cleaned or "repo"


def _sanitize_agent_id(agent_id: str) -> str:
    """`agent_id` reduced to [A-Za-z0-9_-] for use in a remote filesystem path.

    The MCP config path is deliberately left UNQUOTED in the remote commands
    (so `$HOME` expands on the coding host), which is only safe because every
    character of the variable part comes through here first.
    """
    return re.sub(r"[^a-zA-Z0-9_-]", "-", agent_id.strip()) or "agent"


def _mcp_config_path(agent_id: str, gated: bool = False) -> str:
    """Where a run's MCP client config lives on the coding host.

    Outside the run's worktree on purpose: a file inside it could be committed
    (and pushed) by the very agent it authenticates.

    A gated run gets its OWN file. The two configs differ in the URL they point
    at (gated ⇒ the enforcing endpoint), so sharing one path would let a gated
    launch overwrite the ungated config an in-flight run is still reading — or,
    worse, leave a gated run pointed at the ungated endpoint.
    """
    suffix = "-gated" if gated else ""
    return f"$HOME/.aegis/mcp-{_sanitize_agent_id(agent_id)}{suffix}.json"


def _mcp_run_config(agent_id: str, external_url: str, api_key: str, gated: bool = False) -> str:
    """The claude-CLI MCP client config mounting AEGIS's own tool server.

    `external_url` is core's base URL **as reachable from the coding host**
    (a LAN address, not the browser-facing one). One server, `aegis`, pointed
    at this agent's endpoint — so the run's tools are exactly that agent's
    `metadata.tool_set`, per `api/routes/mcp_server.py`.

    `gated` selects the `/gated` variant of that endpoint, where AEGIS itself
    requires an operator approval before executing a mutating tool. That URL is
    the ONLY thing that gates AEGIS's own tools: the claude CLI trusts tools
    from an explicitly-passed `--mcp-config` in `-p` mode and never routes them
    to `--permission-prompt-tool` (measured live, 2.1.231 — issue #294).
    """
    base = external_url.rstrip("/")
    path = f"{base}/api/mcp-server/{agent_id}" + ("/gated" if gated else "")
    return json.dumps(
        {
            "mcpServers": {
                _MCP_SERVER_NAME: {
                    "type": "http",
                    "url": path,
                    "headers": {"X-API-Key": api_key},
                }
            }
        }
    )


def _skills_copy_fragment(skills_src: str, worktree_path: str) -> str:
    """Shell fragment appended to the worktree-creation command that seeds
    `<worktree>/.claude/skills` from AEGIS's own checkout.

    Returns "" when there is nothing to copy. The fragment is a `&&`-chained
    group whose LAST step is `|| true`, so:
      - it runs only when `git worktree add` succeeded (never dirtying the
        shared clone, which — unlike a per-run worktree — is not thrown away);
      - it always exits 0, so the caller's rc still reflects the worktree add.
    A missing source directory is therefore a silent skip, never a failed launch.
    """
    if not skills_src or not worktree_path:
        return ""
    dest = f"{worktree_path}/.claude/skills"
    return (
        f" && {{ [ -d {shlex.quote(skills_src)} ] && "
        f"mkdir -p {shlex.quote(dest)} && "
        f"cp -r {shlex.quote(skills_src)}/. {shlex.quote(dest)}/ || true; }}"
    )


def _agent_launch_flags(
    engine: str,
    binary: str,
    work_path: str,
    prompt_file: str,
    config_dir: str = "",
    mcp_config: str = "",
    gated: bool = False,
    session_id: str = "",
    resume: bool = False,
    name: str = "",
) -> str:
    """Build the agent CLI invocation (without output redirection) for `engine`.

    kimi:   --output-format stream-json -p "$(cat <prompt_file>)"
            (CLI 0.31.x dropped --print/--input-format/--work-dir outright — issue
            #271. There is no --work-dir either; the launcher cd's into work_path
            instead, same as claude. No permission flag is passed: 0.31.x's prompt
            mode (-p) rejects both --auto and --yolo with "Cannot combine
            --prompt with --auto/--yolo" — prompt-mode runs are unconditionally
            forced to full-auto permission internally regardless, so there is
            nothing to opt into.)
    claude: --print --output-format stream-json --verbose --dangerously-skip-permissions
            (stream-json in print mode requires --verbose; permissions are skipped
            because runs are non-interactive — nobody can answer a prompt. claude
            has no --work-dir; the launcher cd's into work_path instead.)
            `config_dir`, when set, becomes CLAUDE_CONFIG_DIR so the run uses a
            non-default login (personal account for non-org fallback runs).
            `mcp_config`, when set, adds `--mcp-config <path> --strict-mcp-config`
            so the run mounts AEGIS's own tools — and ONLY those (see below).
            `gated` REPLACES `--dangerously-skip-permissions` with
            `--permission-prompt-tool mcp__aegis__approve_tool_use` and exports
            `MCP_TOOL_TIMEOUT`, so every action the CLI would otherwise
            auto-allow becomes an approval card a human answers. The two are
            mutually exclusive by construction: leaving skip-permissions in
            would auto-allow everything and the prompt tool would never fire,
            which is a gate that silently does nothing. These flags are kept as
            belt-and-braces for the tools the CLI DOES route through them
            (Bash, Edit); AEGIS's own MCP tools are gated server-side instead,
            by the `/gated` endpoint `_mcp_run_config` points the run at (#294).

    `session_id` pins the run to ONE long-lived claude session, which is how a
    Todoist task keeps its context across turns instead of re-explaining itself
    every time. It is caller-chosen (a deterministic uuid5 of the task), so the
    caller knows the id before the run exists and can find the session again.
    The first turn CREATES it with `--session-id <uuid>` (plus `-n <name>`, a
    human label for the session picker); every later turn REPLAYS it with
    `--resume <uuid>`. The two are mutually exclusive by construction:
    `--session-id` on an existing session errors out, and `--resume` needs no
    name because the session already has one. Emitting both would be a run that
    either dies at argument parsing or silently starts a fresh, amnesiac
    session — the exact failure this primitive exists to prevent.

    `mcp_config`, `gated`, `session_id`, `resume` and `name` are claude-only:
    the kimi CLI has no equivalent flags, so a kimi run gets no mounted AEGIS
    tools, cannot be gated (`start_kimi_run` refuses a gated kimi launch
    outright rather than downgrading it to an ungated one) and carries no
    session across turns.

    claude reads the prompt from stdin; kimi takes it as a `-p` argument via
    `$(cat ...)` command substitution instead. Both emit one JSON event per
    line, so the flow's output polling and STATUS-footer parsing are
    engine-agnostic.
    """
    if engine == "claude":
        env_parts = []
        if config_dir:
            env_parts.append(f"CLAUDE_CONFIG_DIR={shlex.quote(config_dir)}")
        if gated:
            # Without this the CLI abandons the approval call after its default
            # per-tool timeout and the gate becomes a hang.
            env_parts.append(f"MCP_TOOL_TIMEOUT={MCP_TOOL_TIMEOUT_MS}")
        env = "".join(f"{part} " for part in env_parts)
        # NOT shlex-quoted: the path is composed by `_mcp_config_path` from a
        # sanitized agent id and starts with `$HOME`, which must stay expandable
        # by the remote shell. `--strict-mcp-config` makes that file the ONLY
        # server list, so a `.mcp.json` checked into the target repo cannot
        # inject extra servers into an unattended run.
        mcp = f" --mcp-config {mcp_config} --strict-mcp-config" if mcp_config else ""
        permission = (
            f" --permission-prompt-tool {PERMISSION_PROMPT_TOOL}"
            if gated
            else " --dangerously-skip-permissions"
        )
        session = ""
        if session_id:
            # One branch or the other, never both — see the session note above.
            if resume:
                session = f" --resume {shlex.quote(session_id)}"
            else:
                session = f" --session-id {shlex.quote(session_id)}"
                if name:
                    session += f" -n {shlex.quote(name)}"
        return (
            f"{env}{shlex.quote(binary)} --print --output-format stream-json "
            f"--verbose{session}{permission}{mcp} < {shlex.quote(prompt_file)}"
        )
    return (
        f"{shlex.quote(binary)} --output-format stream-json "
        f'-p "$(cat {shlex.quote(prompt_file)})"'
    )


def _plan_tmux_launch(list_windows_output: str, cap: int) -> tuple[list[str], bool]:
    """Decide which finished agent windows to prune and whether to use tmux.

    Input is `tmux list-windows -F '#{window_id}:#{window_name}:#{pane_dead}'`.
    Returns (prune_window_ids, use_tmux):
      - under cap                       → ([], True)
      - at/over cap, enough dead to fit → (oldest-dead ids, True)
      - at/over cap, NOT enough dead    → ([], False)   # never kills a live agent

    Window ids are tmux "@N" handles; oldest = lowest N.
    """
    windows: list[tuple[str, str]] = []  # (window_id, pane_dead)
    for line in list_windows_output.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        win_id, rest = line.split(":", 1)
        name, _, dead = rest.rpartition(":")
        if not name.startswith(_AGENT_WINDOW_PREFIXES):
            continue
        windows.append((win_id, dead))

    if len(windows) < cap:
        return [], True

    def _idnum(wid: str) -> int:
        try:
            return int(wid.lstrip("@"))
        except ValueError:
            return 0

    dead_oldest_first = sorted((w for w in windows if w[1] == "1"), key=lambda w: _idnum(w[0]))
    need = len(windows) - cap + 1
    if len(dead_oldest_first) >= need:
        return [w[0] for w in dead_oldest_first[:need]], True
    return [], False


# Size caps for captured stdout/stderr. stdout needs to accommodate full JSON
# responses from infra scripts (list_pods on a large cluster is ~15KB compact).
# Using tail-slicing ([-N:]) instead of head keeps error messages at the bottom
# of long task-handler output visible.
_STDOUT_CAP = 32 * 1024
_STDERR_CAP = 4 * 1024
# Coding-CLI stream-json logs are mostly tool results; keep enough of the tail
# that the agent's own assistant turns survive the slice.
_KIMI_OUTPUT_CAP = 512 * 1024


def _parse_workspace_scan(output: str) -> list[dict]:
    """Parse `list_workspace_repos` scan output ("relpath|origin_url" lines).

    Drops blank lines, repos nested inside another scanned repo (submodules /
    vendored checkouts), and per-run worktree dirs (`*-aegis-wt`). Origin URL
    may be empty (local-only repo) — kept, the caller decides.
    """
    entries: list[tuple[str, str]] = []
    for line in output.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        rel, _, url = line.partition("|")
        rel = rel.strip().strip("/")
        if not rel or "-aegis-wt" in rel or rel.startswith(".claude"):
            continue
        entries.append((rel, url.strip()))

    paths = {rel for rel, _ in entries}
    out: list[dict] = []
    for rel, url in sorted(entries):
        parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
        if parent and parent in paths:
            continue  # nested inside another repo
        out.append({"path": rel, "origin_url": url})
    return out


def _normalize_args(args: list[Any] | dict[str, Any] | None) -> list[Any]:
    """Normalize args to a list for shell quoting.

    Accepts list (positional args), dict (values in insertion order), or None.
    """
    if args is None:
        return []
    if isinstance(args, dict):
        return list(args.values())
    if isinstance(args, (list, tuple)):
        return list(args)
    raise TypeError(f"args must be list, dict, or None; got {type(args).__name__}")


class RemoteScriptConnector:
    """Execute scripts on remote machines via SSH (DB-first config)."""

    def __init__(
        self,
        host: str = "",
        user: str = "deploy",
        key_file: str = "",
        script_dir: str = "/opt/aegis",
        repo_base: str = "",
        known_hosts: str | None = None,
        kimi_host: str = "",
        tmux_session: str = "remote",
        tmux_window_cap: int = 10,
        claude_orgs: str = "",
        claude_binary: str = "",
        kimi_binary: str = "",
        self_repo_path: str = "",
        runbooks_dir: str = "",
        mcp_server_url: str = "",
        api_key: str = "",
        db_pool: Any = None,
        secret_key: str = "",
    ):
        self._script_dir = script_dir.rstrip("/")
        self._known_hosts = known_hosts
        self._db_pool = db_pool
        self._secret_key = secret_key
        self._config_expiry = 0.0
        self._config_source = "env"
        # Env-derived fallback config, applied at construction and whenever no
        # infra row has coding.enabled. The legacy comma-separated claude_orgs
        # maps onto the routing table ({org: engine=claude, default account}).
        self._env_config: dict[str, Any] = {
            "host": host,
            "user": user,
            "port": 22,
            "key_file": key_file,
            "key_material": "",
            "repo_base": repo_base.rstrip("/"),
            "kimi_host": kimi_host,
            "tmux_session": tmux_session,
            "tmux_window_cap": tmux_window_cap,
            "routing_orgs": {
                org.strip().lower(): {"engine": "claude", "account": ""}
                for org in claude_orgs.split(",")
                if org.strip()
            },
            "default_engine": "kimi",
            "claude_binary": claude_binary,
            "kimi_binary": kimi_binary,
            "claude_config_dirs": {},
            "claude_default_account": "",
            "self_repo_path": self_repo_path,
            "runbooks_dir": runbooks_dir,
            # Core's base URL as reachable FROM the coding host, plus the key a
            # mounted run authenticates with. Both empty ⇒ no MCP mount at all.
            "mcp_server_url": mcp_server_url,
            "api_key": api_key,
            # Session inventory is DB-only (infra.coding.inventory) and OFF for
            # an env-configured connector: it changes whether runs start.
            "inventory": {"enabled": False, "skip_when_busy": True, "accounts": []},
        }
        self._apply_config(self._env_config)

    # ── configuration resolution (infra registry first, env fallback) ───────

    def _apply_config(self, cfg: dict[str, Any]) -> None:
        self._host = cfg["host"]
        self._user = cfg["user"]
        self._port = cfg["port"]
        self._key_file = cfg["key_file"]
        self._key_material = cfg["key_material"]
        self._repo_base = cfg["repo_base"]
        self._kimi_host = cfg["kimi_host"]
        self._tmux_session = cfg["tmux_session"]
        self._tmux_window_cap = cfg["tmux_window_cap"]
        self._routing_orgs = cfg["routing_orgs"]
        self._default_engine = cfg["default_engine"]
        self._claude_binary = cfg["claude_binary"]
        self._kimi_binary = cfg["kimi_binary"]
        self._claude_config_dirs = cfg["claude_config_dirs"]
        self._claude_default_account = cfg["claude_default_account"]
        self._self_repo_path = cfg["self_repo_path"]
        self._runbooks_dir = cfg["runbooks_dir"]
        self._mcp_server_url = cfg["mcp_server_url"]
        self._api_key = cfg["api_key"]
        self._inventory_config = cfg["inventory"]

    def _config_from_row(self, row: dict, kimi_host: str) -> dict[str, Any]:
        """Map an infra row (+ resolved kimi host) onto the active config.

        The enabled row is authoritative for the SSH identity and the coding
        block; self_repo_path/runbooks_dir/mcp_server_url keep their env values
        when the block omits them (they have image-local defaults a DB row
        rarely needs to override).
        """
        coding = row.get("coding") or {}
        engines = coding.get("engines") or {}
        claude = engines.get("claude") or {}
        kimi = engines.get("kimi") or {}
        routing = coding.get("routing") or {}
        tmux = coding.get("tmux") or {}
        inventory = coding.get("inventory") or {}
        env = self._env_config
        key_material = (
            decrypt_secret(
                (row.get("credentials") or {}).get("ssh_private_key_enc"), self._secret_key
            )
            or ""
        )
        return {
            "host": row.get("host") or "",
            "user": row.get("ssh_user") or env["user"],
            "port": int(row.get("ssh_port") or 22),
            "key_file": row.get("ssh_key_ref") or env["key_file"],
            "key_material": key_material,
            "repo_base": (coding.get("repo_base") or "").rstrip("/"),
            "kimi_host": kimi_host,
            "tmux_session": (tmux.get("session") or "remote"),
            "tmux_window_cap": int(tmux.get("window_cap") or 10),
            "inventory": {
                "enabled": bool(inventory.get("enabled", False)),
                "skip_when_busy": bool(inventory.get("skip_when_busy", True)),
                "accounts": [str(a) for a in (inventory.get("accounts") or [])],
            },
            "routing_orgs": {
                str(org).lower(): dict(route)
                for org, route in (routing.get("orgs") or {}).items()
                if isinstance(route, dict)
            },
            "default_engine": (routing.get("default_engine") or "kimi").lower(),
            "claude_binary": claude.get("binary_path") or "",
            "kimi_binary": kimi.get("binary_path") or "",
            "claude_config_dirs": {
                str(k): str(v) for k, v in (claude.get("config_dirs") or {}).items()
            },
            "claude_default_account": claude.get("default_account") or "",
            "self_repo_path": coding.get("self_repo_path") or env["self_repo_path"],
            "runbooks_dir": coding.get("runbooks_dir") or env["runbooks_dir"],
            "mcp_server_url": coding.get("mcp_server_url") or env["mcp_server_url"],
            # Never sourced from the coding block: the API key is a secret, and
            # `coding` is the ONE non-secret jsonb on the infra row (returned
            # verbatim by the public read). It stays env/constructor-only.
            "api_key": env["api_key"],
        }

    async def _refresh_config(self) -> None:
        """Re-resolve the active config from the infra registry (TTL-cached).

        DB errors keep the current config — a flaky DB must not flip a
        working DB-configured connector back to stale env values mid-flight.
        """
        if self._db_pool is None:
            return
        now = time.monotonic()
        if now < self._config_expiry:
            return
        from aegis.services import infra as infra_service

        try:
            row = await infra_service.get_coding_host(self._db_pool)
            kimi_host = ""
            if row:
                slug = ((row.get("coding") or {}).get("kimi_host_slug") or "").strip()
                if slug:
                    kimi_row = await infra_service.get_infra_by_slug(self._db_pool, slug)
                    if kimi_row and kimi_row.get("host"):
                        kimi_host = kimi_row["host"]
                    else:
                        logger.warning("remote_script_kimi_host_slug_unresolved", slug=slug)
        except Exception as exc:  # noqa: BLE001 — keep last-known-good config
            logger.warning("remote_script_db_config_lookup_failed", error=str(exc))
            self._config_expiry = now + _DB_CONFIG_TTL_SECONDS
            return

        self._config_expiry = now + _DB_CONFIG_TTL_SECONDS
        if not row:
            source = "env"
            self._apply_config(self._env_config)
        else:
            source = f"db:{row['slug']}"
            self._apply_config(self._config_from_row(row, kimi_host))
        if source != self._config_source:
            logger.info("remote_script_config_source_changed", source=source)
        self._config_source = source

    async def ensure_config(self) -> None:
        """Public hook: refresh the DB-first config (no-op without a db_pool).

        Callers that read connector attributes outside an SSH call (e.g. the
        base host for host-selection logic) should await this first.
        """
        await self._refresh_config()

    async def coding_settings(self) -> dict:
        """Effective (DB-first) coding-agent settings for external callers."""
        await self._refresh_config()
        return {
            "host": self._host,
            "repo_base": self._repo_base,
            "kimi_binary": self._kimi_binary,
            "claude_binary": self._claude_binary,
            "self_repo_path": self._self_repo_path,
            "runbooks_dir": self._runbooks_dir,
            "source": self._config_source,
        }

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
                "errors": [
                    {"account": "", "error": "claude binary or config_dirs not configured"}
                ],
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

    # ── SSH plumbing ─────────────────────────────────────────────────────────

    @contextlib.contextmanager
    def _key_path(self) -> Iterator[str]:
        """Yield the private-key path for one SSH invocation.

        A DB-stored key is materialized to a mode-0600 temp file and unlinked
        on exit (mirrors services/infra.py::ssh_key_file); otherwise the
        configured key path (ssh_key_ref / env key file) is used as-is.
        """
        if self._key_material:
            fd, path = tempfile.mkstemp(prefix="aegis-remote-script-key-")  # mkstemp => 0600
            try:
                # OpenSSH rejects a PEM key without a trailing newline.
                os.write(fd, (self._key_material.rstrip("\n") + "\n").encode())
                os.close(fd)
                yield path
            finally:
                os.unlink(path)
        else:
            yield self._key_file

    def _ssh_args_host(self, host: str, remote_cmd: str) -> list[str]:
        """Build SSH args for an explicit host (safe — no shell injection).

        Uses the configured key *path* only — DB-stored key material is
        handled by `_exec`, which materializes it around the subprocess call.
        Prefer `run_on_host` outside this class.
        """
        return build_ssh_args(
            host,
            self._user,
            self._key_file,
            remote_cmd,
            known_hosts=self._known_hosts,
        )

    def _ssh_args(self, remote_cmd: str) -> list[str]:
        """Build SSH args for the connector's default host."""
        return self._ssh_args_host(self._host, remote_cmd)

    async def _exec(
        self,
        host: str,
        remote_cmd: str,
        timeout: int,
        stdin: bytes | None = None,
        connect_timeout: int = 10,
        batch_mode: bool = False,
        stdout_cap: int = _STDOUT_CAP,
    ) -> dict:
        """Run `remote_cmd` on `host` with the active key (materialized per
        call when DB-stored), returning the standard result envelope."""
        if not host:
            return {
                "status": "failed",
                "exit_code": -1,
                "stdout": "",
                "stderr": "remote script host not configured "
                "(no infra entry with coding.enabled and no AEGIS_REMOTE_SCRIPT_HOST)",
            }
        with self._key_path() as key_file:
            args = build_ssh_args(
                host,
                self._user,
                key_file,
                remote_cmd,
                known_hosts=self._known_hosts,
                connect_timeout=connect_timeout,
                batch_mode=batch_mode,
            )
            if self._port and self._port != 22:
                # build_ssh_args doesn't take a port; splice `-p` in before the
                # destination (second-to-last element).
                args = args[:-2] + ["-p", str(self._port)] + args[-2:]
            return await self._run_capture(args, timeout, stdin=stdin, stdout_cap=stdout_cap)

    async def run_on_host(
        self, host: str, remote_cmd: str, timeout: int = 30, stdin: bytes | None = None
    ) -> dict:
        """Public: run a command on `host` (or the base host when empty) using
        the connector's DB-first auth. Returns the standard envelope."""
        await self._refresh_config()
        return await self._exec(host or self._host, remote_cmd, timeout, stdin=stdin)

    async def _probe_host(self, host: str) -> bool:
        """Return True if `host` answers a fast, non-interactive SSH `true`."""
        result = await self._exec(host, "true", timeout=8, connect_timeout=3, batch_mode=True)
        return result["status"] == "succeeded"

    # ── engine routing ───────────────────────────────────────────────────────

    def _claude_config_dir_for(self, account: str) -> str:
        """CLAUDE_CONFIG_DIR for a named account (default account when empty);
        '' means the host's default ~/.claude login."""
        label = account or self._claude_default_account
        return self._claude_config_dirs.get(label, "") if label else ""

    def _route_for(self, github_repo: str) -> tuple[str, str]:
        """Resolve (engine, claude_config_dir) for a repo from the org routing
        table; unrouted orgs fall through to the default engine."""
        org = github_repo.split("/", 1)[0].strip().lower() if github_repo else ""
        route = self._routing_orgs.get(org) if org else None
        if route:
            engine = (route.get("engine") or "claude").lower()
            if engine == "claude":
                return "claude", self._claude_config_dir_for(route.get("account") or "")
            return engine, ""
        if self._default_engine == "claude":
            return "claude", self._claude_config_dir_for("")
        return "kimi", ""

    def _engine_for(self, github_repo: str) -> str:
        """Pick the coding-CLI engine for a repo (routing-table lookup)."""
        return self._route_for(github_repo)[0]

    async def _resolve_kimi_host(self) -> tuple[str, bool]:
        """Resolve the effective host for a kimi run.

        Returns (host, use_tmux): the preferred `kimi_host` when set AND
        reachable (tmux mode), otherwise the default host (today's nohup mode).
        Fail-closed — an unreachable/unset kimi_host degrades to the base host.
        """
        if self._kimi_host and await self._probe_host(self._kimi_host):
            return self._kimi_host, True
        return self._host, False

    async def _run_capture(
        self,
        ssh_args: list[str],
        timeout: int,
        stdin: bytes | None = None,
        stdout_cap: int = _STDOUT_CAP,
    ) -> dict:
        """Run an SSH command and capture its output into the standard envelope."""
        proc = await asyncio.create_subprocess_exec(
            *ssh_args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=stdin), timeout=timeout
            )
            status = "succeeded" if proc.returncode == 0 else "failed"
            return {
                "status": status,
                "exit_code": proc.returncode,
                "stdout": stdout.decode()[-stdout_cap:],
                "stderr": stderr.decode()[-_STDERR_CAP:],
            }
        except TimeoutError:
            return {"status": "timed_out", "exit_code": -1, "stdout": "", "stderr": "timeout"}
        except Exception as exc:
            return {"status": "failed", "exit_code": -1, "stdout": "", "stderr": str(exc)}
        finally:
            await kill_and_wait(proc)

    async def run_script(
        self,
        script_id: str,
        args: list[Any] | dict[str, Any] | None = None,
        timeout: int = 300,
    ) -> dict:
        """Run a named script on the remote host.

        script_id is resolved to {script_dir}/scripts/{script_id}.sh on the
        remote host. Subdirectories are allowed (e.g. "infra/infra_list_nodes"
        resolves to /opt/aegis/scripts/infra/infra_list_nodes.sh), but "..",
        leading slashes, and other shell metacharacters are rejected.
        """
        if ".." in script_id or not _SCRIPT_ID_RE.match(script_id):
            return {
                "status": "failed",
                "exit_code": -1,
                "stdout": "",
                "stderr": f"invalid script_id: {script_id!r}",
            }
        safe_id = script_id.lstrip("/")
        script_path = f"{self._script_dir}/scripts/{safe_id}.sh"
        arg_str = " ".join(shlex.quote(str(v)) for v in _normalize_args(args))
        await self._refresh_config()
        return await self._exec(self._host, f"bash {shlex.quote(script_path)} {arg_str}", timeout)

    def workspace_scan_host(self) -> str:
        """The canonical workspace host: kimi_host when configured, else base.

        node-b (the kimi host in prod) holds the hand-maintained workspace
        hierarchy; node-a (base) is the mirror. No reachability probe here — a
        scan against the wrong host could mass-delete resources, so an
        unreachable canonical host must FAIL, not fall back.
        """
        return self._kimi_host or self._host

    async def list_workspace_repos(self, host: str = "") -> list[dict]:
        """Scan `repo_base` on `host` for git checkouts (depth ≤ 2 below base).

        Returns [{"path": "<relpath under repo_base>", "origin_url": "..."}].
        Raises RuntimeError on SSH/scan failure — callers must not interpret
        a failed scan as an empty workspace.
        """
        await self._refresh_config()
        if not self._repo_base:
            raise RuntimeError("repo_base not configured — cannot scan workspace")
        base = shlex.quote(self._repo_base)
        scan_cmd = (
            f"cd {base} && find . -mindepth 2 -maxdepth 3 -name .git "
            f"\\( -type d -o -type f \\) 2>/dev/null | while read -r g; do "
            f'd="${{g#./}}"; d="${{d%/.git}}"; '
            f'u=$(git -C "$d" config --get remote.origin.url 2>/dev/null); '
            f'echo "$d|$u"; done'
        )
        result = await self._exec(host or self.workspace_scan_host(), scan_cmd, timeout=120)
        if result["status"] != "succeeded":
            raise RuntimeError(
                f"workspace scan failed on {host or self.workspace_scan_host()}: "
                f"{result.get('stderr', '')[:300]}"
            )
        return _parse_workspace_scan(result["stdout"])

    async def ensure_repo(self, rel_path: str, clone_url: str, host: str = "") -> dict:
        """Ensure `repo_base/rel_path` exists on `host`, cloning if missing.

        This is the ONE deliberate clone path (WorkspaceRepoSyncFlow mirroring
        the canonical hierarchy onto the base host) — per-run JIT cloning was
        removed from start_kimi_run.

        Returns {"status": "present" | "cloned" | "failed", "path", "error"?}.
        """
        rel = rel_path.strip().strip("/")
        if not rel or ".." in rel:
            return {"status": "failed", "path": rel_path, "error": "invalid rel_path"}
        await self._refresh_config()
        if not self._repo_base:
            return {"status": "failed", "path": rel, "error": "repo_base not configured"}
        repo_path = f"{self._repo_base}/{rel}"
        target_host = host or self._host

        check = await self._exec(target_host, f"test -d {shlex.quote(repo_path)}", timeout=15)
        if check["status"] == "succeeded":
            return {"status": "present", "path": repo_path}

        if not clone_url:
            return {"status": "failed", "path": repo_path, "error": "no origin_url to clone from"}
        parent = repo_path.rsplit("/", 1)[0]
        clone = await self._exec(
            target_host,
            f"mkdir -p {shlex.quote(parent)} && "
            f"git clone {shlex.quote(clone_url)} {shlex.quote(repo_path)}",
            timeout=300,
        )
        if clone["status"] != "succeeded":
            error = clone.get("stderr", "")[:300] or clone["status"]
            logger.warning("workspace_mirror_clone_failed", path=repo_path, error=error)
            return {"status": "failed", "path": repo_path, "error": error}
        logger.info("workspace_mirror_cloned", path=repo_path, host=target_host)
        return {"status": "cloned", "path": repo_path}

    async def start_kimi_run(
        self,
        repo: str,
        prompt: str,
        kimi_binary: str,
        timeout: int = 1800,
        github_repo: str = "",
        engine_override: str = "",
        claude_config_dir: str = "",
        claude_account: str = "",
        agent_id: str = "",
        gated: bool = False,
        token_ttl_seconds: int = 0,
        session_id: str = "",
        resume: bool = False,
        name: str = "",
        worktree_path: str = "",
    ) -> dict:
        """Start a coding-CLI run (kimi or claude) on the effective host.

        `token_ttl_seconds` bounds the life of the MCP mount token (issue #288);
        0 uses the generous default. Pass the run's own deadline when the caller
        knows it, so the credential cannot outlive the run by much.

        `repo` is the workspace-relative path of a FIXED checkout under
        `repo_base` (may contain subdirectories, e.g. "acme/bcp").
        Checkouts are provisioned by WorkspaceRepoSyncFlow, never JIT-cloned
        here — a missing directory is a hard failure so a wrong path surfaces
        instead of silently minting a stray clone.

        Engine routing: when `github_repo`'s org is in `claude_orgs`, the run
        uses the claude CLI pinned to the base host — that host's claude login
        belongs to the org, so org-repo work stays on the org's account, and
        there is deliberately NO failover to the kimi host. All other repos use
        kimi: the preferred `kimi_host` when set and reachable, otherwise the
        base host with a detached `nohup` launch.

        `engine_override` ("claude" | "kimi") forces the engine regardless of
        org routing — used both by the kimi→claude fallback (passing
        `claude_config_dir` for the personal login) and by resource-scoped
        routing (a resource pins its own engine). `claude_account` is a
        resource-scoped CLAUDE_CONFIG_DIR account *label* (resolved against the
        coding block's config_dirs); it wins over org routing but not over an
        explicit `claude_config_dir`. (Kimi ignores both — no profile.)

        `agent_id` is the DISPATCHING AEGIS agent. It is what makes a run
        AEGIS-aware: a claude-engine launch gets that agent's own tool surface
        mounted over MCP (`_mount_mcp_config`, gated on `mcp_server_url` + an
        API key) on top of the SKILL.md runbooks copied into the worktree. Both
        are best-effort — a missing skills dir or a failed config write degrades
        to a plain run, never to a failed launch. Kimi runs get neither: the CLI
        has no `--mcp-config`, and skills are a claude-side convention.

        `gated` turns the run into a human-in-the-loop one. It works on two
        levels, and only the second is trustworthy:
          * the CLI is asked to route non-allowed actions to AEGIS's approval
            tool (`--permission-prompt-tool`) — which it honours for BUILT-IN
            tools (Bash, Edit) but demonstrably not for the MCP tools it was
            handed via `--mcp-config` (#294);
          * the run's mount points at `/api/mcp-server/{agent_id}/gated`, where
            AEGIS refuses to execute a mutating tool until a human approves it.
            No CLI policy can bypass that, because the enforcement runs in
            core's own process.
        It has two HARD preconditions, both of which return a normal `failed`
        result rather than quietly launching an ungated run — the whole point is
        that nothing mutating happens without a human, so "gated was requested
        and not applied" must never be a silent outcome:
          1. engine must be `claude` (kimi has no equivalent flag), and
          2. the MCP mount must have succeeded — it is both where the approval
             tool lives and what points the run at the enforcing endpoint, so
             an unmounted gated run is an ungated run.

        `session_id`/`resume`/`name` pin a claude run to one long-lived session
        (see `_agent_launch_flags`): the task lane's turns are separate runs of
        the same session, not separate agents.

        `worktree_path`, when set, means the CALLER owns that directory: this
        method neither creates it (no `test -d`/`git pull`/`worktree add`) nor
        removes it on any failure path. A per-task worktree outlives the run
        that used it — the next turn resumes into the same tree with the same
        branch and the same uncommitted work — so the per-run ownership rule
        below ("this method owns what it created") must not reach it. Default
        "" keeps the per-run behaviour: provision a throwaway worktree here and
        clean it up when nothing was launched.

        Reachable-host runs are wrapped in a tmux window for live attach.
        Output is always captured to `output_file` (via `tee` in tmux mode) so
        stream-json parsing is unchanged.

        Returns {"run_id", "repo", "repo_path", "output_file", "status": "running",
        "worktree_path", "host", "in_tmux", "engine"} on launch success, or
        {"run_id", "status": "failed", "error": ...} on error.
        """
        import uuid

        run_id = str(uuid.uuid4())[:8]
        await self._refresh_config()
        repo_path = f"{self._repo_base}/{repo}" if self._repo_base else repo
        route_engine, route_config_dir = self._route_for(github_repo)
        engine = engine_override or route_engine
        if gated and engine != "claude":
            error = (
                f"gated runs require the claude engine, but this run routed to '{engine}' — "
                "the kimi CLI has no --permission-prompt-tool equivalent. Pass "
                "engine='claude' (an ungated fallback would defeat the gate)."
            )
            logger.warning("gated_run_engine_unsupported", engine=engine, github_repo=github_repo)
            return {"run_id": run_id, "status": "failed", "error": error, "engine": engine}
        if engine == "claude":
            if not self._claude_binary:
                error = f"claude engine selected for {github_repo} but claude_binary not configured"
                logger.warning("claude_binary_missing", github_repo=github_repo)
                return {"run_id": run_id, "status": "failed", "error": error, "engine": "claude"}
            host, use_tmux = self._host, True
            binary = self._claude_binary
            # Precedence for CLAUDE_CONFIG_DIR (empty ⇒ host's default ~/.claude):
            #   1. explicit claude_config_dir param (the kimi→claude fallback path)
            #   2. per-resource claude_account label (resource-scoped routing)
            #   3. the org-routed account's dir (route table)
            #   4. the default account (forced-claude on a non-claude-routed org)
            account_dir = self._claude_config_dir_for(claude_account) if claude_account else ""
            if claude_config_dir:
                config_dir = claude_config_dir
            elif account_dir:
                config_dir = account_dir
            elif route_engine == "claude":
                config_dir = route_config_dir
            else:
                config_dir = self._claude_config_dir_for("")
        else:
            host, use_tmux = await self._resolve_kimi_host()
            # DB-configured kimi binary wins over the caller's env-derived path.
            binary = self._kimi_binary or kimi_binary
            config_dir = ""

        # A caller-supplied worktree is the caller's to create, refresh and
        # delete; this flag is what keeps every cleanup path below off it.
        owns_worktree = not worktree_path
        skills_src = ""
        work_path = worktree_path

        if owns_worktree:
            # Phase 1: ensure repo directory is present and up-to-date
            check = await self._exec(host, f"test -d {shlex.quote(repo_path)}", timeout=10)
            if check["exit_code"] == -1:  # ssh error/timeout — not a missing dir
                error = check["stderr"] or "repo check failed"
                logger.warning("kimi_repo_check_failed", error=error)
                return {"run_id": run_id, "status": "failed", "error": error, "engine": engine}
            dir_exists = check["status"] == "succeeded"

            if not dir_exists:
                error = (
                    f"Repo checkout missing on {host}: {repo_path} — "
                    "provision it via WorkspaceRepoSyncFlow (no JIT clone)"
                )
                logger.warning("kimi_repo_missing", repo=repo, repo_path=repo_path, host=host)
                return {"run_id": run_id, "status": "failed", "error": error, "engine": engine}

            await self._exec(
                host,
                f"git -C {shlex.quote(repo_path)} pull --ff-only --quiet 2>/dev/null || true",
                timeout=30,
            )

            # Phase 2: create an isolated per-run worktree (sibling of the shared
            # clone), and — for claude — seed it with AEGIS's SKILL.md runbooks in
            # the same round trip. The fragment is rc-neutral and self-skipping, so
            # neither an unconfigured self_repo_path nor a missing skills dir can
            # change what this command reports.
            worktree_path = f"{repo_path}-aegis-wt/{run_id}"
            worktree_parent = f"{repo_path}-aegis-wt"
            skills_src = self._skills_source_dir() if engine == "claude" else ""
            wt_cmd = (
                f"mkdir -p {shlex.quote(worktree_parent)} && "
                f"git -C {shlex.quote(repo_path)} worktree add --detach "
                f"{shlex.quote(worktree_path)}"
                f"{_skills_copy_fragment(skills_src, worktree_path)}"
            )
            wt = await self._exec(host, wt_cmd, timeout=30)
            if wt["status"] != "succeeded":
                logger.warning("kimi_worktree_add_failed", repo=repo, error=wt["stderr"][:300])
                work_path = repo_path
                worktree_path = ""
            else:
                work_path = worktree_path

        # Phase 3: write prompt to temp file on remote via stdin
        prompt_file = f"/tmp/aegis-prompt-{run_id}.txt"
        output_file = f"/tmp/aegis-kimi-run-{run_id}.jsonl"

        wrote = await self._exec(
            host,
            f"cat > {shlex.quote(prompt_file)}",
            timeout=15,
            stdin=prompt[:5000].encode(),
        )
        if wrote["exit_code"] != 0:
            logger.warning(
                "kimi_prompt_write_failed", error=wrote["stderr"], exit_code=wrote["exit_code"]
            )
            # Nothing was launched, so nothing can be holding the worktree: this
            # method owns what it created up to the point a process exists — and
            # a caller-owned worktree is never "what it created".
            if owns_worktree:
                await self.remove_worktree(worktree_path, host=host)
            return {
                "run_id": run_id,
                "status": "failed",
                "error": wrote["stderr"],
                "engine": engine,
            }

        # Phase 3b: mount AEGIS's own tools over MCP (claude only). Best-effort
        # by design — a run without its tools is degraded, a run that never
        # launched is dead.
        mcp_config = await self._mount_mcp_config(
            host, engine, agent_id, gated, ttl_seconds=token_ttl_seconds
        )
        if gated and not mcp_config:
            # Best-effort stops being acceptable here: the permission prompt
            # lives ON that server. An unmounted gated run would ask a server
            # it cannot see for permission on every action.
            error = (
                "gated run aborted: AEGIS's MCP tools were not mounted, so the CLI cannot "
                "reach the approval tool. Check mcp_server_external_url, the API key and "
                "that the run carries an agent_id."
            )
            logger.warning("gated_run_mcp_mount_missing", agent_id=agent_id, run_id=run_id)
            if owns_worktree:
                await self.remove_worktree(worktree_path, host=host)
            return {"run_id": run_id, "status": "failed", "error": error, "engine": engine}

        # Phase 4: launch the agent. tmux mode → live-attachable window with
        # tee-capture; otherwise today's detached nohup. `nohup` alone detaches;
        # we never add `< /dev/null` — for claude, a second stdin redirect
        # would (last-wins) blank the prompt (claude still reads it from
        # stdin). kimi no longer reads the prompt from stdin at all (it's
        # substituted into `-p` via `$(cat ...)`), but the same rule holds
        # regardless: this launch command never redirects stdin itself.
        agent_flags = _agent_launch_flags(
            engine,
            binary,
            work_path,
            prompt_file,
            config_dir,
            mcp_config,
            gated,
            session_id=session_id,
            resume=resume,
            name=name,
        )
        # `nohup env …`, never `nohup VAR=… …`: nohup execs its first argument
        # as a PROGRAM, so an environment-assignment prefix (CLAUDE_CONFIG_DIR,
        # MCP_TOOL_TIMEOUT) makes it look for a binary literally named
        # "VAR=value" and die with "nohup: failed to run command". `env` is the
        # POSIX tool whose whole job is turning those assignments back into
        # environment. Harmless when there are none — `env <binary> …` is just
        # `<binary> …`. The tmux path needs no such wrapper: tmux runs its
        # command through a shell, which understands the prefix natively.
        nohup_cmd = (
            f"cd {shlex.quote(work_path)} && "
            f"(nohup env {agent_flags} > {shlex.quote(output_file)} 2>&1 &)"
        )

        launched_in_tmux = False
        if use_tmux:
            launched_in_tmux = await self._launch_in_tmux(
                host=host,
                repo=repo,
                run_id=run_id,
                work_path=work_path,
                prompt_file=prompt_file,
                output_file=output_file,
                engine=engine,
                binary=binary,
                config_dir=config_dir,
                mcp_config=mcp_config,
                gated=gated,
                session_id=session_id,
                resume=resume,
                name=name,
            )

        if not launched_in_tmux:
            launch = await self._exec(host, nohup_cmd, timeout=15)
            if launch["exit_code"] == -1:  # ssh error/timeout, not a remote rc
                logger.warning("kimi_start_failed", error=launch["stderr"])
                # Same reasoning as the engine field below: a TIMED-OUT launch may
                # already have forked the agent remotely, and pulling the worktree
                # out from under a live process is worse than leaking it. A clean
                # ssh/connect failure never started anything, so it cleans up.
                if owns_worktree and launch["status"] != "timed_out":
                    await self.remove_worktree(worktree_path, host=host)
                return {
                    "run_id": run_id,
                    "status": "failed",
                    "error": launch["stderr"],
                    # A timed-out launch (vs. a clean ssh/connect failure) may have
                    # already forked the `(nohup ... &)` remotely before the 15s
                    # timeout hit — the kimi agent could be alive, so a claude
                    # fallback would race it on the same deterministic fix branch.
                    "engine": "" if launch["status"] == "timed_out" else engine,
                }

        logger.info(
            "kimi_run_started",
            run_id=run_id,
            repo=repo,
            host=host,
            in_tmux=launched_in_tmux,
            engine=engine,
            # Booleans, never the config itself — the file holds an API key.
            # `mcp_mounted` is verified (the write returned rc 0);
            # `skills_requested` is not — the copy is a self-skipping fragment
            # whose rc is deliberately swallowed, so claiming "mounted" here
            # would assert something this process never observed.
            mcp_mounted=bool(mcp_config),
            skills_requested=bool(skills_src) and worktree_path != "",
        )
        return {
            "run_id": run_id,
            "repo": repo,
            "repo_path": repo_path,
            "output_file": output_file,
            "status": "running",
            "worktree_path": worktree_path,
            "host": host,
            "in_tmux": launched_in_tmux,
            "engine": engine,
        }

    def _skills_source_dir(self) -> str:
        """Absolute path of `config/skills` inside AEGIS's own checkout on the
        coding host, or "" when `self_repo_path` is unset.

        `self_repo_path` is workspace-relative (the same value `start_kimi_run`
        takes as `repo`), so it is resolved against `repo_base` exactly the way
        a run's own checkout is. The checkout is kept fresh by
        WorkspaceRepoSyncFlow, which is what makes the skills current without a
        redeploy.
        """
        path = (self._self_repo_path or "").strip().rstrip("/")
        if not path:
            return ""
        if not path.startswith("/") and self._repo_base:
            path = f"{self._repo_base}/{path}"
        return f"{path}/config/skills"

    async def _resolve_mount_api_key(self) -> str:
        """The key a mounted run authenticates with — constructor first, DB next.

        `verify_auth` accepts BOTH the env `AEGIS_API_KEY` (this connector's
        constructor value) and the admin-generated key stored encrypted in the
        `settings` table, and the admin UI's *Generate* button is the only one
        of the two a deployment gets by clicking. A mount that consulted the
        env var alone would report `mcp_mount_skipped reason=api_key_unset` on
        a perfectly well-configured instance, so the DB key is resolved here
        when the constructor value is empty.

        Never raises: `resolve_api_key` swallows its own read/decrypt failures
        and returns "", which the caller degrades on.
        """
        if self._api_key:
            return self._api_key
        if self._db_pool is None or not self._secret_key:
            return ""
        # Imported inside the method: `services/api_key` is core-side and this
        # connector is constructed by the worker too — a module-level import
        # would widen the worker's import graph for a call it may never make.
        from types import SimpleNamespace

        from aegis.services.api_key import resolve_api_key

        # `resolve_api_key` reads exactly one attribute off `settings`
        # (`secret_key`, to decrypt), and the connector already holds it.
        return await resolve_api_key(self._db_pool, SimpleNamespace(secret_key=self._secret_key))

    async def _mount_mcp_config(
        self,
        host: str,
        engine: str,
        agent_id: str,
        gated: bool = False,
        ttl_seconds: int = 0,
    ) -> str:
        """Write the run's MCP client config on `host`; return its path.

        "" means no mount, and that is a normal outcome: kimi has no
        `--mcp-config`, an unrouted run has no `agent_id`, and a deployment
        that never set `mcp_server_external_url` is opting out. A *failed*
        write is also "" — the run launches without its tools rather than not
        at all.

        `gated` writes a SEPARATE file pointed at the enforcing endpoint, so a
        gated launch never disturbs the ungated config (and vice versa).

        The credential is a per-agent, short-TTL MOUNT TOKEN (issue #288), not
        the shared API key. It is signed for this agent and this gated mode, so
        a run that reads its own config file still cannot swap the `{agent_id}`
        path segment to reach another agent's tools, nor present an ungated
        token at the gated endpoint. It also expires, so a token printed into a
        transcript and delivered to chat ages out instead of being a permanent
        credential. `AEGIS_SECRET_KEY` signs it; Core verifies with the same
        secret, so there is no lookup and nothing to revoke on a dead run.

        Falls back to the shared API key ONLY when no secret is configured —
        otherwise a deployment without `AEGIS_SECRET_KEY` would lose its tools
        entirely. That fallback is logged, because it is the weaker posture.

        The credential is piped through the SSH channel's STDIN and never
        appears in a command line: argv is world-readable via `ps` on the
        coding host and lands in shell audit logs. `umask 077` makes the
        directory and file owner-only, and the path is deliberately OUTSIDE the
        run's worktree so the agent cannot commit its own credential. The
        content is never logged.
        """
        if engine != "claude" or not agent_id or not self._mcp_server_url:
            return ""
        credential = mint_mount_token(
            agent_id,
            self._secret_key or "",
            gated=gated,
            ttl_seconds=ttl_seconds or DEFAULT_MOUNT_TTL_SECONDS,
        )
        if not credential:
            credential = await self._resolve_mount_api_key()
            if credential:
                logger.warning(
                    "mcp_mount_token_unavailable_using_shared_key",
                    agent_id=agent_id,
                    reason="secret_key_unset",
                )
        if not credential:
            # Visible degradation: the URL was configured, so somebody meant to
            # mount this and a silent skip would look like a broken feature.
            logger.warning("mcp_mount_skipped", agent_id=agent_id, reason="no_credential")
            return ""
        path = _mcp_config_path(agent_id, gated)
        # Written to a temp file and `mv`d into place. `mv` within one
        # filesystem is atomic, so a second launch for the same agent cannot be
        # read half-written by the first run's CLI — a plain `cat > path`
        # truncates the file the other process is about to open. `$$` (the
        # remote shell's pid) keeps two concurrent temps apart.
        wrote = await self._exec(
            host,
            f"umask 077 && mkdir -p $HOME/.aegis && cat > {path}.$$.tmp "
            f"&& mv {path}.$$.tmp {path}",
            timeout=15,
            stdin=_mcp_run_config(agent_id, self._mcp_server_url, credential, gated).encode(),
        )
        if wrote["exit_code"] != 0:
            logger.warning(
                "mcp_config_write_failed",
                agent_id=agent_id,
                exit_code=wrote["exit_code"],
                error=wrote["stderr"][:200],
            )
            return ""
        logger.info("mcp_config_written", agent_id=agent_id, path=path, gated=gated)
        return path

    async def _launch_in_tmux(
        self,
        *,
        host: str,
        repo: str,
        run_id: str,
        work_path: str,
        prompt_file: str,
        output_file: str,
        engine: str,
        binary: str,
        config_dir: str = "",
        mcp_config: str = "",
        gated: bool = False,
        session_id: str = "",
        resume: bool = False,
        name: str = "",
    ) -> bool:
        """Launch the agent in a tmux window on `host`. Returns True if a window
        was created, False if it should fall back to detached nohup (all windows
        live, or any tmux step errored).

        `session_id`/`resume`/`name` are the claude session flags, passed
        straight through to `_agent_launch_flags`. `name` labels the claude
        SESSION, not the tmux window (`winname` below) — the tmux path must
        compose the same command as the nohup fallback, or a run would carry its
        task session only when tmux happened to be available."""
        sess = self._tmux_session
        # Round trip 1: ensure the session exists, then list its windows.
        ensure_list = (
            f"tmux has-session -t {shlex.quote(sess)} 2>/dev/null || "
            f"tmux new-session -d -s {shlex.quote(sess)}; "
            f"tmux list-windows -t {shlex.quote(sess)} "
            f"-F '#{{window_id}}:#{{window_name}}:#{{pane_dead}}' 2>/dev/null"
        )
        listed = await self._exec(host, ensure_list, timeout=15)
        if listed["exit_code"] == -1:  # ssh error/timeout
            logger.warning("kimi_tmux_list_failed", host=host, error=listed["stderr"])
            return False
        list_output = listed["stdout"]

        prune_ids, use_tmux = _plan_tmux_launch(list_output, self._tmux_window_cap)
        if not use_tmux:
            logger.warning("kimi_tmux_cap_all_running", host=host, session=sess)
            return False

        winname = f"{engine}-{_sanitize_window_repo(repo)}-{run_id}"
        agent_flags = _agent_launch_flags(
            engine,
            binary,
            work_path,
            prompt_file,
            config_dir,
            mcp_config,
            gated,
            session_id=session_id,
            resume=resume,
            name=name,
        )
        inner = (
            f"cd {shlex.quote(work_path)} && {agent_flags} "
            f"2>&1 | tee {shlex.quote(output_file)}"
        )
        prune = "".join(f"tmux kill-window -t {shlex.quote(pid)}; " for pid in prune_ids)
        # The ssh exit status MUST reflect `new-window` (the load-bearing step), NOT
        # the trailing `set-window-option`. Otherwise a cosmetic remain-on-exit
        # failure returns False and triggers a SECOND nohup launch on the same
        # worktree (both writing output_file → corrupted stream-json). Capture
        # new-window's rc, run set-window-option best-effort, exit with that rc.
        # remain-on-exit only keeps a finished pane scrollable; the sub-second race
        # before it is set can at worst lose scrollback for a run that finished that
        # fast — never a correctness issue (tee already captured the output).
        launch = (
            f"{prune}"
            f"tmux new-window -t {shlex.quote(sess)} -n {shlex.quote(winname)} {shlex.quote(inner)}; "
            f"rc=$?; "
            f"tmux set-window-option -t {shlex.quote(sess + ':' + winname)} remain-on-exit on 2>/dev/null; "
            f"exit $rc"
        )
        launched = await self._exec(host, launch, timeout=15)
        if launched["status"] != "succeeded":
            logger.warning("kimi_tmux_launch_failed", host=host, error=launched["stderr"][:300])
            return False
        return True

    async def fetch_kimi_run_output(self, output_file: str, host: str = "") -> str | None:
        """Fetch the raw stream-json output of a kimi run from the remote host.

        output_file is the path returned by start_kimi_run.  The caller parses
        the stream-json lines to extract session_id (claude and pre-0.31 kimi
        carry it in the first event; kimi CLI 0.31.x only in the last, a
        `session.resume_hint` meta event written once the run completes) and
        the final assistant message.

        Returns the file content or None if empty / not yet written.
        """
        await self._refresh_config()
        result = await self._exec(
            host or self._host,
            f"cat {shlex.quote(output_file)} 2>/dev/null",
            timeout=15,
            # The generic 32KB tail cap decapitates a tool-heavy run: tool
            # results dwarf the agent's own turns, so the last 32KB can hold
            # zero complete assistant events and the transcript comes back
            # empty. Callers extract assistant text and re-cap it themselves.
            stdout_cap=_KIMI_OUTPUT_CAP,
        )
        if len(result.get("stdout", "")) >= _KIMI_OUTPUT_CAP:
            # Capped ⇒ the slice starts mid-line. Drop that fragment; leaving
            # it lets JSON debris ("…\"timeout\": 30}\"}}]}") pass downstream
            # as if it were the agent's own prose.
            _, _, rest = result["stdout"].partition("\n")
            result["stdout"] = rest
            logger.warning("kimi_output_truncated", output_file=output_file)
        if result["exit_code"] == -1:
            logger.warning(
                "fetch_kimi_run_output_failed", output_file=output_file, error=result["stderr"]
            )
            return None
        content = result["stdout"]
        return content if content.strip() else None

    async def kimi_run_alive(self, output_file: str, host: str = "") -> bool:
        """Return whether the agent process for a run is still running.

        Probes via `fuser` on `output_file` rather than matching the launched
        process's own command line: shell command substitution (kimi's
        `-p "$(cat prompt_file)"`) and stdin redirection (claude's
        `< prompt_file`) both consume the prompt-file path *before* exec, so
        it never survives into the running process's argv once launched
        detached via `nohup ... &` (verified empirically — the only process
        that ever has that literal path in its own cmdline is the transient
        launcher shell, which exits within milliseconds of backgrounding the
        job). A marker matched against argv would therefore report a
        healthy nohup-launched run as dead almost immediately — and nohup is
        the common path here (prod runs with no `kimi_host` configured).

        `output_file`, by contrast, stays open for the run's entire duration
        regardless of launch mode: in nohup mode the agent process itself
        holds the `> output_file` redirection open until it exits; in tmux
        mode `tee output_file` holds it and exits once the agent's end of
        the pipe closes. Either way, a live run holds the file open and a
        dead run doesn't — and a launch that died before ever creating the
        file also correctly reads as "not held open".

        rc 0 (something holds it open) → True (alive)
        rc 1 (file exists but nothing holds it, or the file was never
            created — e.g. the process died before writing anything) →
            False (dead)
        ssh error, missing `fuser`, or any other rc → True (fail-open: a
            flaky probe must never be mistaken for a dead run and kill a
            healthy investigation)
        """
        await self._refresh_config()
        target_host = host or self._host
        result = await self._exec(
            target_host,
            f"fuser {shlex.quote(output_file)} >/dev/null 2>&1",
            timeout=10,
        )
        if result["exit_code"] not in (0, 1):
            # e.g. rc 127 (fuser missing on host) — fail-open (below) is
            # unchanged, but a permanently-inconclusive probe would silently
            # never fail-fast again; surface it instead of staying silent.
            logger.warning(
                "kimi_run_alive_probe_inconclusive",
                exit_code=result["exit_code"],
                output_file=output_file,
                host=target_host,
            )
        return result["exit_code"] != 1

    async def stop_coding_run(self, run_id: str, host: str = "") -> dict:
        """Kill the tmux window of a coding run. Returns a result envelope.

        The operator-facing counterpart to `start_kimi_run`. Until now stopping a
        run meant knowing the window name and running `tmux kill-window` by hand;
        the flow observes the death on its next poll, reports `failed`, and
        cleans the worktree, so the machinery already existed — only the handle
        was missing.

        Matches on the run id SUFFIX of the window name
        (`{engine}-{repo}-{run_id}`, `_launch_in_tmux`) rather than
        reconstructing the whole name, because the repo segment is sanitised on
        the way in and is not recoverable from the run id alone.

        `stopped: False` with `reason="not_found"` is a normal outcome, not an
        error: the run may have finished already, or been launched detached with
        no window at all (what happens past the tmux window cap).

        Never raises. A run id that is not plain hex is refused outright — the
        value is spliced into a remote shell command.
        """
        run_id = (run_id or "").strip()
        if not run_id or not re.fullmatch(r"[A-Za-z0-9_-]{4,64}", run_id):
            return {"stopped": False, "reason": "invalid_run_id", "window": ""}
        await self._refresh_config()
        target_host = host or self._host
        sess = self._tmux_session
        listed = await self._exec(
            target_host,
            f"tmux list-windows -t {shlex.quote(sess)} -F '#{{window_id}}:#{{window_name}}' 2>/dev/null",
            timeout=15,
        )
        if listed["status"] != "succeeded":
            logger.warning(
                "coding_run_stop_list_failed", run_id=run_id, error=listed["stderr"][:200]
            )
            return {"stopped": False, "reason": "tmux_unreachable", "window": ""}

        window_id = ""
        window_name = ""
        for line in listed["stdout"].splitlines():
            wid, _, name = line.strip().partition(":")
            if not wid or not name.startswith(_AGENT_WINDOW_PREFIXES):
                continue
            if name.endswith(f"-{run_id}"):
                window_id, window_name = wid, name
                break
        if not window_id:
            return {"stopped": False, "reason": "not_found", "window": ""}

        killed = await self._exec(
            target_host,
            f"tmux kill-window -t {shlex.quote(window_id)}",
            timeout=15,
        )
        if killed["status"] != "succeeded":
            logger.warning(
                "coding_run_stop_failed",
                run_id=run_id,
                window=window_name,
                error=killed["stderr"][:200],
            )
            return {"stopped": False, "reason": "kill_failed", "window": window_name}
        logger.info("coding_run_stopped", run_id=run_id, window=window_name, host=target_host)
        return {"stopped": True, "reason": "", "window": window_name}

    async def ensure_task_worktree(
        self, repo: str, worktree_path: str, branch: str, host: str = ""
    ) -> dict:
        """Create (once) the persistent worktree a Todoist task works in.

        The task lane's counterpart to `start_kimi_run`'s per-run worktree: this
        one is keyed on the task, lives on its own branch and SURVIVES between
        turns, so turn 2 finds turn 1's uncommitted work. Idempotent by design —
        it is called before every turn and must be a cheap no-op after the first.

        Ordering matters. The shared clone is pulled FIRST, so a first-turn
        worktree branches off current `main` rather than off whatever the clone
        was left at; an existing worktree then short-circuits (`exit 0`) before
        anything can touch the branch the task is mid-way through. The pull is
        `|| true` for the same reason it is in `start_kimi_run`: a detached
        network or a diverged clone must not stop work that only needs the local
        objects.

        `worktree add -b <branch>` is tried first and falls back to
        `worktree add <wt> <branch>` — the branch already exists whenever a
        previous worktree for this task was removed but its branch kept, which is
        what a git-level cleanup leaves behind. Skills are seeded in the same
        round trip, by the same rc-neutral fragment the per-run path uses.

        Returns {"status": "ready"|"failed", "error": str}.
        """
        await self._refresh_config()
        target = host or self._host
        repo_path = f"{self._repo_base}/{repo}" if self._repo_base else repo

        check = await self._exec(target, f"test -d {shlex.quote(repo_path)}", timeout=10)
        if check["exit_code"] == -1:  # ssh error/timeout — not a missing dir
            error = check["stderr"] or "repo check failed"
            logger.warning("task_worktree_repo_check_failed", repo=repo, error=error)
            return {"status": "failed", "error": error}
        if check["status"] != "succeeded":
            error = (
                f"Repo checkout missing on {target}: {repo_path} — "
                "provision it via WorkspaceRepoSyncFlow (no JIT clone)"
            )
            logger.warning("task_worktree_repo_missing", repo=repo, repo_path=repo_path)
            return {"status": "failed", "error": error}

        parent = worktree_path.rsplit("/", 1)[0]
        skills_src = self._skills_source_dir()
        cmd = (
            f"git -C {shlex.quote(repo_path)} pull --ff-only --quiet 2>/dev/null || true; "
            f"[ -d {shlex.quote(worktree_path)} ] && exit 0; "
            f"mkdir -p {shlex.quote(parent)} && "
            f"(git -C {shlex.quote(repo_path)} worktree add -b {shlex.quote(branch)} "
            f"{shlex.quote(worktree_path)} 2>/dev/null || "
            f"git -C {shlex.quote(repo_path)} worktree add {shlex.quote(worktree_path)} "
            f"{shlex.quote(branch)})"
            f"{_skills_copy_fragment(skills_src, worktree_path)}"
        )
        result = await self._exec(target, cmd, timeout=60)
        if result["status"] != "succeeded":
            error = result["stderr"][:300] or "worktree add failed"
            logger.warning(
                "task_worktree_add_failed",
                repo=repo,
                worktree_path=worktree_path,
                branch=branch,
                error=error,
            )
            return {"status": "failed", "error": error}
        return {"status": "ready", "error": ""}

    async def kill_run(self, output_file: str, host: str = "") -> bool:
        """Kill the agent process still writing `output_file`. Returns True when
        the remote command ran (it says nothing about whether anything died).

        The output file is the only handle a caller reliably has on a task turn:
        a nohup launch has no window name, and even in tmux the pane may already
        be gone. `fuser -k` kills whatever holds the file open, which is exactly
        the run. `; true` makes the rc report the SSH round trip rather than
        fuser's "nobody had it open" (rc 1) — a turn that already exited is a
        successful kill, not a failure to report upward.
        """
        if not output_file:
            return False
        await self._refresh_config()
        result = await self._exec(
            host or self._host,
            f"fuser -k {shlex.quote(output_file)} >/dev/null 2>&1; true",
            timeout=15,
        )
        if result["status"] != "succeeded":
            logger.warning(
                "task_run_kill_failed", output_file=output_file, error=result["stderr"][:200]
            )
            return False
        return True

    async def remove_worktree(self, worktree_path: str, host: str = "") -> None:
        """Best-effort cleanup of a per-run git worktree created by start_kimi_run.

        Never raises — failures are logged as warnings only.  If worktree_path is
        empty the method returns immediately.

        The shared repo path is derived by splitting on "-aegis-wt/" so no
        additional bookkeeping is required by the caller.
        """
        if not worktree_path:
            return

        # Derive the shared clone path from the worktree path convention:
        #   {repo_path}-aegis-wt/{run_id}  →  repo_path = part before "-aegis-wt/"
        parts = worktree_path.split("-aegis-wt/")
        repo_path = parts[0]

        # `prune` is not redundant with `remove`: a `worktree remove` that fails
        # (locked, or a dirty tree git won't force past) still leaves the `rm -rf`
        # to delete the directory, and that combination leaves a REGISTRATION
        # behind — which is what makes `git worktree list` in the shared clone
        # useless for telling live runs from dead ones.
        rm_cmd = (
            f"git -C {shlex.quote(repo_path)} worktree remove --force "
            f"{shlex.quote(worktree_path)} 2>/dev/null; "
            f"rm -rf {shlex.quote(worktree_path)}; "
            f"git -C {shlex.quote(repo_path)} worktree prune"
        )
        await self._refresh_config()
        result = await self._exec(host or self._host, rm_cmd, timeout=30)
        if result["exit_code"] == -1:
            logger.warning(
                "kimi_worktree_remove_failed",
                worktree_path=worktree_path,
                error=result["stderr"],
            )
