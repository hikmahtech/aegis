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
from aegis.crypto import decrypt_secret

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


def _sanitize_window_repo(repo: str) -> str:
    """Basename of `repo` with all non [A-Za-z0-9_-] chars replaced by '-'."""
    base = repo.rstrip("/").split("/")[-1]
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "-", base)
    return cleaned or "repo"


def _agent_launch_flags(
    engine: str, binary: str, work_path: str, prompt_file: str, config_dir: str = ""
) -> str:
    """Build the agent CLI invocation (without output redirection) for `engine`.

    kimi:   --print --input-format text --output-format stream-json --work-dir <p>
    claude: --print --output-format stream-json --verbose --dangerously-skip-permissions
            (stream-json in print mode requires --verbose; permissions are skipped
            because runs are non-interactive — nobody can answer a prompt. claude
            has no --work-dir; the launcher cd's into work_path instead.)
            `config_dir`, when set, becomes CLAUDE_CONFIG_DIR so the run uses a
            non-default login (personal account for non-org fallback runs).

    Both read the prompt from stdin and emit one JSON event per line, so the
    flow's output polling and STATUS-footer parsing are engine-agnostic.
    """
    if engine == "claude":
        env = f"CLAUDE_CONFIG_DIR={shlex.quote(config_dir)} " if config_dir else ""
        return (
            f"{env}{shlex.quote(binary)} --print --output-format stream-json "
            f"--verbose --dangerously-skip-permissions < {shlex.quote(prompt_file)}"
        )
    return (
        f"{shlex.quote(binary)} --print --input-format text "
        f"--output-format stream-json --work-dir {shlex.quote(work_path)} "
        f"< {shlex.quote(prompt_file)}"
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

    def _config_from_row(self, row: dict, kimi_host: str) -> dict[str, Any]:
        """Map an infra row (+ resolved kimi host) onto the active config.

        The enabled row is authoritative for the SSH identity and the coding
        block; self_repo_path/runbooks_dir keep their env values when the
        block omits them (they have image-local defaults a DB row rarely
        needs to override).
        """
        coding = row.get("coding") or {}
        engines = coding.get("engines") or {}
        claude = engines.get("claude") or {}
        kimi = engines.get("kimi") or {}
        routing = coding.get("routing") or {}
        tmux = coding.get("tmux") or {}
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
            return await self._run_capture(args, timeout, stdin=stdin)

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
        self, ssh_args: list[str], timeout: int, stdin: bytes | None = None
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
                "stdout": stdout.decode()[-_STDOUT_CAP:],
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
    ) -> dict:
        """Start a coding-CLI run (kimi or claude) on the effective host.

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
        if engine == "claude":
            if not self._claude_binary:
                error = f"claude engine selected for {github_repo} but claude_binary not configured"
                logger.warning("claude_binary_missing", github_repo=github_repo)
                return {"run_id": run_id, "status": "failed", "error": error}
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

        # Phase 1: ensure repo directory is present and up-to-date
        check = await self._exec(host, f"test -d {shlex.quote(repo_path)}", timeout=10)
        if check["exit_code"] == -1:  # ssh error/timeout — not a missing dir
            error = check["stderr"] or "repo check failed"
            logger.warning("kimi_repo_check_failed", error=error)
            return {"run_id": run_id, "status": "failed", "error": error}
        dir_exists = check["status"] == "succeeded"

        if not dir_exists:
            error = (
                f"Repo checkout missing on {host}: {repo_path} — "
                "provision it via WorkspaceRepoSyncFlow (no JIT clone)"
            )
            logger.warning("kimi_repo_missing", repo=repo, repo_path=repo_path, host=host)
            return {"run_id": run_id, "status": "failed", "error": error}

        await self._exec(
            host,
            f"git -C {shlex.quote(repo_path)} pull --ff-only --quiet 2>/dev/null || true",
            timeout=30,
        )

        # Phase 2: create an isolated per-run worktree (sibling of the shared clone).
        worktree_path = f"{repo_path}-aegis-wt/{run_id}"
        worktree_parent = f"{repo_path}-aegis-wt"
        wt_cmd = (
            f"mkdir -p {shlex.quote(worktree_parent)} && "
            f"git -C {shlex.quote(repo_path)} worktree add --detach {shlex.quote(worktree_path)}"
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
        if wrote["exit_code"] == -1:
            logger.warning("kimi_prompt_write_failed", error=wrote["stderr"])
            return {"run_id": run_id, "status": "failed", "error": wrote["stderr"]}

        # Phase 4: launch the agent. tmux mode → live-attachable window with
        # tee-capture; otherwise today's detached nohup. `nohup` alone detaches;
        # a second stdin redirect would (last-wins) blank the prompt, so we
        # never add `< /dev/null`.
        agent_flags = _agent_launch_flags(engine, binary, work_path, prompt_file, config_dir)
        nohup_cmd = (
            f"cd {shlex.quote(work_path)} && "
            f"(nohup {agent_flags} > {shlex.quote(output_file)} 2>&1 &)"
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
            )

        if not launched_in_tmux:
            launch = await self._exec(host, nohup_cmd, timeout=15)
            if launch["exit_code"] == -1:  # ssh error/timeout, not a remote rc
                logger.warning("kimi_start_failed", error=launch["stderr"])
                return {"run_id": run_id, "status": "failed", "error": launch["stderr"]}

        logger.info(
            "kimi_run_started",
            run_id=run_id,
            repo=repo,
            host=host,
            in_tmux=launched_in_tmux,
            engine=engine,
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
    ) -> bool:
        """Launch the agent in a tmux window on `host`. Returns True if a window
        was created, False if it should fall back to detached nohup (all windows
        live, or any tmux step errored)."""
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
        inner = (
            f"cd {shlex.quote(work_path)} && "
            f"{_agent_launch_flags(engine, binary, work_path, prompt_file, config_dir)} "
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
        the stream-json lines to extract session_id (first event) and the final
        assistant message.

        Returns the file content or None if empty / not yet written.
        """
        await self._refresh_config()
        result = await self._exec(
            host or self._host, f"cat {shlex.quote(output_file)} 2>/dev/null", timeout=15
        )
        if result["exit_code"] == -1:
            logger.warning(
                "fetch_kimi_run_output_failed", output_file=output_file, error=result["stderr"]
            )
            return None
        content = result["stdout"]
        return content if content.strip() else None

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

        rm_cmd = (
            f"git -C {shlex.quote(repo_path)} worktree remove --force "
            f"{shlex.quote(worktree_path)} 2>/dev/null; "
            f"rm -rf {shlex.quote(worktree_path)}"
        )
        await self._refresh_config()
        result = await self._exec(host or self._host, rm_cmd, timeout=30)
        if result["exit_code"] == -1:
            logger.warning(
                "kimi_worktree_remove_failed",
                worktree_path=worktree_path,
                error=result["stderr"],
            )
