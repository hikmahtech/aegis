"""RemoteScript connector — execute scripts on remote machines via SSH.

Used by the kimi_code action handler to run Kimi CLI on remote hosts,
and by the script action handler for general remote execution.
"""

from __future__ import annotations

import asyncio
import re
import shlex
from typing import Any

import structlog

from aegis.connectors._ssh import build_ssh_args
from aegis.connectors._subprocess import kill_and_wait

logger = structlog.get_logger()

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
    """Execute scripts on remote machines via SSH."""

    def __init__(
        self,
        host: str,
        user: str,
        key_file: str,
        script_dir: str = "/opt/aegis",
        repo_base: str = "",
        known_hosts: str | None = None,
        kimi_host: str = "",
        tmux_session: str = "remote",
        tmux_window_cap: int = 10,
        claude_orgs: str = "",
        claude_binary: str = "",
    ):
        self._host = host
        self._user = user
        self._key_file = key_file
        self._script_dir = script_dir.rstrip("/")
        self._repo_base = repo_base.rstrip("/")
        self._known_hosts = known_hosts
        self._kimi_host = kimi_host
        self._tmux_session = tmux_session
        self._tmux_window_cap = tmux_window_cap
        # GitHub orgs whose repos run with the claude CLI pinned to the base
        # host (its claude login belongs to the org). Comma-separated, matched
        # case-insensitively against the org part of github_repo.
        self._claude_orgs = {org.strip().lower() for org in claude_orgs.split(",") if org.strip()}
        self._claude_binary = claude_binary

    def _ssh_args_host(self, host: str, remote_cmd: str) -> list[str]:
        """Build SSH args for an explicit host (safe — no shell injection)."""
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

    async def _probe_host(self, host: str) -> bool:
        """Return True if `host` answers a fast, non-interactive SSH `true`."""
        args = build_ssh_args(
            host,
            self._user,
            self._key_file,
            "true",
            known_hosts=self._known_hosts,
            connect_timeout=3,
            batch_mode=True,
        )
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=8)
            return proc.returncode == 0
        except Exception:
            return False
        finally:
            await kill_and_wait(proc)

    def _engine_for(self, github_repo: str) -> str:
        """Pick the coding-CLI engine for a repo: "claude" when the repo's
        GitHub org is in claude_orgs (case-insensitive), else "kimi"."""
        org = github_repo.split("/", 1)[0].strip().lower() if github_repo else ""
        return "claude" if org and org in self._claude_orgs else "kimi"

    async def _resolve_kimi_host(self) -> tuple[str, bool]:
        """Resolve the effective host for a kimi run.

        Returns (host, use_tmux): the preferred `kimi_host` when set AND
        reachable (tmux mode), otherwise the default host (today's nohup mode).
        Fail-closed — an unreachable/unset kimi_host degrades to the base host.
        """
        if self._kimi_host and await self._probe_host(self._kimi_host):
            return self._kimi_host, True
        return self._host, False

    async def _run_capture(self, ssh_args: list[str], timeout: int) -> dict:
        """Run an SSH command and capture its output into the standard envelope."""
        proc = await asyncio.create_subprocess_exec(
            *ssh_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
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
        ssh_args = self._ssh_args(f"bash {shlex.quote(script_path)} {arg_str}")
        return await self._run_capture(ssh_args, timeout)

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
        ssh_args = self._ssh_args_host(host or self.workspace_scan_host(), scan_cmd)
        result = await self._run_capture(ssh_args, timeout=120)
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
        if not self._repo_base:
            return {"status": "failed", "path": rel, "error": "repo_base not configured"}
        repo_path = f"{self._repo_base}/{rel}"
        target_host = host or self._host

        check = await self._run_capture(
            self._ssh_args_host(target_host, f"test -d {shlex.quote(repo_path)}"), timeout=15
        )
        if check["status"] == "succeeded":
            return {"status": "present", "path": repo_path}

        if not clone_url:
            return {"status": "failed", "path": repo_path, "error": "no origin_url to clone from"}
        parent = repo_path.rsplit("/", 1)[0]
        clone = await self._run_capture(
            self._ssh_args_host(
                target_host,
                f"mkdir -p {shlex.quote(parent)} && "
                f"git clone {shlex.quote(clone_url)} {shlex.quote(repo_path)}",
            ),
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

        `engine_override` ("claude") forces the claude CLI regardless of org —
        the kimi→claude fallback path uses this for a non-org repo, passing
        `claude_config_dir` (CLAUDE_CONFIG_DIR) so the run uses the personal
        login rather than the default ~/.claude (which belongs to an org).

        Reachable-host runs are wrapped in a tmux window for live attach.
        Output is always captured to `output_file` (via `tee` in tmux mode) so
        stream-json parsing is unchanged.

        Returns {"run_id", "repo", "repo_path", "output_file", "status": "running",
        "worktree_path", "host", "in_tmux", "engine"} on launch success, or
        {"run_id", "status": "failed", "error": ...} on error.
        """
        import uuid

        run_id = str(uuid.uuid4())[:8]
        repo_path = f"{self._repo_base}/{repo}" if self._repo_base else repo
        engine = engine_override or self._engine_for(github_repo)
        if engine == "claude":
            if not self._claude_binary:
                error = f"claude engine selected for {github_repo} but claude_binary not configured"
                logger.warning("claude_binary_missing", github_repo=github_repo)
                return {"run_id": run_id, "status": "failed", "error": error}
            host, use_tmux = self._host, True
            binary = self._claude_binary
            config_dir = claude_config_dir
        else:
            host, use_tmux = await self._resolve_kimi_host()
            binary = kimi_binary
            config_dir = ""

        def _ssh(cmd: str) -> list[str]:
            return self._ssh_args_host(host, cmd)

        # Phase 1: ensure repo directory is present and up-to-date
        proc = await asyncio.create_subprocess_exec(
            *_ssh(f"test -d {shlex.quote(repo_path)}"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=10)
            dir_exists = proc.returncode == 0
        except Exception as exc:
            logger.warning("kimi_repo_check_failed", error=str(exc))
            return {"run_id": run_id, "status": "failed", "error": str(exc)}
        finally:
            await kill_and_wait(proc)

        if not dir_exists:
            error = (
                f"Repo checkout missing on {host}: {repo_path} — "
                "provision it via WorkspaceRepoSyncFlow (no JIT clone)"
            )
            logger.warning("kimi_repo_missing", repo=repo, repo_path=repo_path, host=host)
            return {"run_id": run_id, "status": "failed", "error": error}

        proc = await asyncio.create_subprocess_exec(
            *_ssh(f"git -C {shlex.quote(repo_path)} pull --ff-only --quiet 2>/dev/null || true"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=30)
        except Exception:
            pass
        finally:
            await kill_and_wait(proc)

        # Phase 2: create an isolated per-run worktree (sibling of the shared clone).
        worktree_path = f"{repo_path}-aegis-wt/{run_id}"
        worktree_parent = f"{repo_path}-aegis-wt"
        wt_cmd = (
            f"mkdir -p {shlex.quote(worktree_parent)} && "
            f"git -C {shlex.quote(repo_path)} worktree add --detach {shlex.quote(worktree_path)}"
        )
        proc = await asyncio.create_subprocess_exec(
            *_ssh(wt_cmd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr_wt = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode != 0:
                logger.warning(
                    "kimi_worktree_add_failed", repo=repo, error=stderr_wt.decode()[:300]
                )
                work_path = repo_path
                worktree_path = ""
            else:
                work_path = worktree_path
        except Exception as exc:
            logger.warning("kimi_worktree_add_failed", repo=repo, error=str(exc))
            work_path = repo_path
            worktree_path = ""
        finally:
            await kill_and_wait(proc)

        # Phase 3: write prompt to temp file on remote via stdin
        prompt_file = f"/tmp/aegis-prompt-{run_id}.txt"
        output_file = f"/tmp/aegis-kimi-run-{run_id}.jsonl"

        proc = await asyncio.create_subprocess_exec(
            *_ssh(f"cat > {shlex.quote(prompt_file)}"),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(proc.communicate(input=prompt[:5000].encode()), timeout=15)
        except Exception as exc:
            logger.warning("kimi_prompt_write_failed", error=str(exc))
            return {"run_id": run_id, "status": "failed", "error": str(exc)}
        finally:
            await kill_and_wait(proc)

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
            proc = await asyncio.create_subprocess_exec(
                *_ssh(nohup_cmd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                await asyncio.wait_for(proc.communicate(), timeout=15)
            except Exception as exc:
                logger.warning("kimi_start_failed", error=str(exc))
                return {"run_id": run_id, "status": "failed", "error": str(exc)}
            finally:
                await kill_and_wait(proc)

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

        def _ssh(cmd: str) -> list[str]:
            return self._ssh_args_host(host, cmd)

        sess = self._tmux_session
        # Round trip 1: ensure the session exists, then list its windows.
        ensure_list = (
            f"tmux has-session -t {shlex.quote(sess)} 2>/dev/null || "
            f"tmux new-session -d -s {shlex.quote(sess)}; "
            f"tmux list-windows -t {shlex.quote(sess)} "
            f"-F '#{{window_id}}:#{{window_name}}:#{{pane_dead}}' 2>/dev/null"
        )
        proc = await asyncio.create_subprocess_exec(
            *_ssh(ensure_list),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
            list_output = stdout.decode()
        except Exception as exc:
            logger.warning("kimi_tmux_list_failed", host=host, error=str(exc))
            return False
        finally:
            await kill_and_wait(proc)

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
        proc = await asyncio.create_subprocess_exec(
            *_ssh(launch),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
            if proc.returncode != 0:
                logger.warning("kimi_tmux_launch_failed", host=host, error=stderr.decode()[:300])
                return False
            return True
        except Exception as exc:
            logger.warning("kimi_tmux_launch_failed", host=host, error=str(exc))
            return False
        finally:
            await kill_and_wait(proc)

    async def fetch_kimi_run_output(self, output_file: str, host: str = "") -> str | None:
        """Fetch the raw stream-json output of a kimi run from the remote host.

        output_file is the path returned by start_kimi_run.  The caller parses
        the stream-json lines to extract session_id (first event) and the final
        assistant message.

        Returns the file content or None if empty / not yet written.
        """
        cat_args = self._ssh_args_host(
            host or self._host, f"cat {shlex.quote(output_file)} 2>/dev/null"
        )
        proc = await asyncio.create_subprocess_exec(
            *cat_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
            content = stdout.decode()
            return content if content.strip() else None
        except Exception as exc:
            logger.warning("fetch_kimi_run_output_failed", output_file=output_file, error=str(exc))
            return None
        finally:
            await kill_and_wait(proc)

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
        rm_args = self._ssh_args_host(host or self._host, rm_cmd)
        proc = await asyncio.create_subprocess_exec(
            *rm_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=30)
        except Exception as exc:
            logger.warning(
                "kimi_worktree_remove_failed",
                worktree_path=worktree_path,
                error=str(exc),
            )
        finally:
            await kill_and_wait(proc)
