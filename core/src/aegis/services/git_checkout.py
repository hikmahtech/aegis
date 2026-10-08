"""The git checkout layer: a deploy-keyed clone, a flock, a scoped revert.

Shared by the books (`books.py`) and the Obsidian vault (`notes.py`). Moved out
of `books.py` so the vault keeps working once the books lane leaves v1. A
config here is any object with `path`, `repo_url` and `deploy_key` attributes
(`BooksConfig`, `NotesConfig`).
"""

from __future__ import annotations

import base64
import fcntl
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

# The flock file. It lives inside the checkout; writers drop it from every
# pathspec by name (`git_paths`).
LOCK_NAME = ".aegis.lock"
# How long a clone may take. Every activity that can trigger the first write
# must allow MORE than this, or it times out mid-clone and burns every retry
# attempt on the same clone (`_POST_TIMEOUT` in the money flows).
CLONE_TIMEOUT_S = 180


class CheckoutError(Exception):
    """A checkout operation failed; the working copy is left clean."""


class CheckoutDisabled(CheckoutError):  # noqa: N818 — a state, not an error suffix
    """No repo url and no checkout: the checkout is not configured."""


def write_deploy_key(raw: str, path: Path, label: str) -> Path | None:
    """Write one deploy key (PEM, or base64 of PEM) to `path` with mode 0600.

    Shared by the books key and the vault key (`notes.install_deploy_key`,
    #514), so both land on disk the same way. Never logs the value."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if "\n" not in raw:
        try:
            raw = base64.b64decode(raw, validate=True).decode("utf-8").strip()
        except Exception as exc:  # noqa: BLE001
            raise CheckoutError(f"{label} is neither PEM text nor base64 PEM") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    # O_CREAT's mode applies only when the file is NEW, so this closes the window
    # where a fresh key file exists world-readable; the chmod then covers the
    # case where the path already existed with looser permissions.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(raw + "\n")
    path.chmod(0o600)
    return path


def parse_csv_set(raw: str) -> frozenset[str]:
    """`" a, b ,,c"` → `{"a", "b", "c"}`. Blank/None ⇒ empty."""
    return frozenset(s.strip() for s in (raw or "").split(",") if s.strip())


def parse_kv(raw: str) -> dict[str, str]:
    """`"personal=6h2f, acme = 6h2g"` → `{"personal": "6h2f", "acme": "6h2g"}`.

    Lenient by design: a malformed pair is dropped, never raised — these are
    admin-typed strings and a typo must not take a boot path down.
    """
    out: dict[str, str] = {}
    for part in (raw or "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip() and v.strip():
                out[k.strip()] = v.strip()
    return out


def ssh_env(cfg: Any) -> dict[str, str]:
    """The process environment, plus the deploy key when the config has one."""
    env = dict(os.environ)
    if cfg.deploy_key:
        env["GIT_SSH_COMMAND"] = (
            f"ssh -i {cfg.deploy_key} -o StrictHostKeyChecking=accept-new -o IdentitiesOnly=yes"
        )
    return env


def spawn(
    cmd: list[str], *, cwd: str, timeout: int, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    """`subprocess.run`, with the two non-`CheckoutError` escapes closed: a
    missing binary or working copy (`OSError`) and a hung pull/push
    (`TimeoutExpired`). Callers see one exception type, so a degraded host never
    escapes as a bare `FileNotFoundError` through an async activity."""
    try:
        return subprocess.run(
            cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise CheckoutError(f"{cmd[0]} timed out after {timeout}s") from exc
    except OSError as exc:
        raise CheckoutError(f"{cmd[0]} could not run: {exc}") from exc


def _run(args: list[str], cfg: Any, *, timeout: int = 60) -> subprocess.CompletedProcess:
    return spawn(args, cwd=str(cfg.path), timeout=timeout, env=ssh_env(cfg))


def ensure_checkout_sync(cfg: Any, *, what: str = "repo_url") -> None:
    """Clone if the working copy is missing. Raises CheckoutDisabled with no
    repo url and no checkout; `what` names the missing setting in its message.

    Called with the flock HELD (see `books._write_sync`), so core and worker
    cannot both clone on the first-ever write. That is also why the clone stages
    in a sibling directory: the lock lives INSIDE `cfg.path`, and a clone
    refuses a destination that is not empty — measured, exit 128, "already
    exists and is not an empty directory". Nothing is moved into place until the
    clone has succeeded.
    """
    if (cfg.path / ".git").exists():
        return
    if not cfg.repo_url:
        raise CheckoutDisabled(f"{what} is not configured and no checkout exists")
    cfg.path.mkdir(parents=True, exist_ok=True)
    staging = cfg.path.parent / f".{cfg.path.name}.cloning"
    shutil.rmtree(staging, ignore_errors=True)
    proc = spawn(
        ["git", "clone", "-q", cfg.repo_url, str(staging)],
        cwd=str(cfg.path.parent), timeout=CLONE_TIMEOUT_S, env=ssh_env(cfg),
    )
    if proc.returncode != 0:
        shutil.rmtree(staging, ignore_errors=True)
        raise CheckoutError(f"git clone failed: {proc.stderr.strip()[:500]}")
    for item in staging.iterdir():
        item.rename(cfg.path / item.name)
    staging.rmdir()


def git_paths(cfg: Any, paths: list[str], *, on_disk_only: bool = False) -> list[str]:
    """The pathspec git can actually act on.

    The lock file is dropped by NAME rather than with a `:!` exclusion pathspec:
    combining an exclusion with a positive pathspec makes `git add` stage
    nothing at all for a new file, silently and with exit 0 (measured on git
    2.x), which is how a written report would never reach a commit. And a
    pathspec matching neither the working tree nor the index makes `git add`
    and `git commit` fail outright, so those are dropped too — a journal file
    the write never had to create cannot have changed.
    """
    wanted = [p for p in paths if p != LOCK_NAME]
    if not wanted or on_disk_only:
        return wanted
    listed = _run(["git", "ls-files", "-z", "--", *wanted], cfg).stdout
    tracked = set(listed.split("\0"))
    return [p for p in wanted if (cfg.path / p).exists() or p in tracked]


def revert_sync(cfg: Any, paths: list[str]) -> None:
    """Undo ONLY the paths this write touched. A repo-wide revert would destroy
    a human's unrelated uncommitted edits — including the hand edit that made
    the write fail in the first place."""
    targets = git_paths(cfg, paths, on_disk_only=True)
    if not targets:
        return
    # Unstage first. A write can fail AFTER `git add` (the commit itself), and
    # `git checkout — <path>` restores from the INDEX, so a staged bad version
    # would be "restored" straight back into the working copy. Reset also makes
    # a newly-added file untracked again, so the `clean` below can remove it.
    _run(["git", "reset", "-q", "HEAD", "--", *targets], cfg)
    # One checkout per path: git aborts the WHOLE command when any pathspec
    # names an untracked file, reverting nothing, so a new year's journal in
    # the list would silently protect every other path from being restored.
    for rel in targets:
        _run(["git", "checkout", "-q", "--", rel], cfg)
    # The lock file is outside `targets`, so `clean` cannot delete it out from
    # under a holder — which would hand the next writer a different inode and
    # therefore no mutual exclusion at all.
    _run(["git", "clean", "-qfd", "--", *targets], cfg)


class FileLock:
    """flock on <checkout>/.aegis.lock — core and worker share the directory."""

    def __init__(self, cfg: Any) -> None:
        self._path = cfg.path / LOCK_NAME

    def __enter__(self):
        # The directory may not exist yet: the clone happens INSIDE this lock,
        # so the lock file has to be creatable before there is a checkout.
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = open(self._path, "w")  # noqa: SIM115 — held for the with-block
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self._fd, fcntl.LOCK_UN)
        self._fd.close()
