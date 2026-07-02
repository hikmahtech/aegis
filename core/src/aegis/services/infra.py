"""Infrastructure registry + provisioning service.

`infra` rows describe machines AEGIS can reach over SSH (or a local docker
context) and, optionally, files/commands to push to bring the host into a
known-good state ("provisioning"). This module is intentionally thin — it
takes an asyncpg pool and plain dicts in/out so it's easy to call from both
the admin CRUD routes (api/routes/infra_admin.py) and the system-status
probe (api/routes/system_status.py).

ponytail: provisioning is a straight-line "write files, run one command"
flow, not a general config-management engine. If this ever needs retries,
templating, or multi-step orchestration, reach for a real tool (Ansible)
instead of growing this file.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import re
import shlex
import tempfile
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import asyncpg
import structlog

from aegis.connectors._ssh import build_ssh_args
from aegis.connectors._subprocess import kill_and_wait
from aegis.crypto import decrypt_secret, encrypt_secret

logger = structlog.get_logger()

_SELECT_COLS = (
    "id, slug, name, kind, host, ssh_user, ssh_port, ssh_key_ref, docker_context, "
    "hosts_aegis, setup_files, setup_command, status, last_provisioned_at, "
    "last_error, metadata, credentials, created_at, updated_at"
)

# Fields an operator may set directly via create/update. `status`,
# `last_provisioned_at`, and `last_error` are deliberately excluded — those
# are only ever written by provision_infra() so the UI can't fake a "ready"
# state without actually provisioning.
_EDITABLE_FIELDS = (
    "name",
    "kind",
    "host",
    "ssh_user",
    "ssh_port",
    "ssh_key_ref",
    "docker_context",
    "hosts_aegis",
    "setup_files",
    "setup_command",
    "metadata",
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")

_SSH_TIMEOUT = 30
_PROVISION_STDOUT_CAP = 16 * 1024
_PROVISION_STDERR_CAP = 4 * 1024


def _slugify(name: str) -> str:
    slug = _SLUG_RE.sub("-", name.strip().lower()).strip("-")
    return slug or "infra"


def public_infra(row: dict) -> dict:
    """Strip `credentials` from a row, replacing it with has_* booleans.

    Every dict that leaves this module through an admin-facing route goes
    through here — key material never reaches the API, only its presence.
    """
    out = dict(row)
    creds = out.pop("credentials", None) or {}
    out["has_ssh_key"] = bool((creds.get("ssh_private_key_enc") or {}).get("value"))
    out["has_kubeconfig"] = bool((creds.get("kubeconfig_enc") or {}).get("value"))
    return out


def _merged_credentials(existing: dict | None, data: dict[str, Any], secret_key: str) -> dict:
    """Fold write-only `ssh_private_key` / `kubeconfig` inputs into the stored
    credentials dict. Blank/absent keeps the existing value (slack_config
    convention: a blank admin-UI field never wipes a saved secret)."""
    creds = dict(existing or {})
    if data.get("ssh_private_key"):
        creds["ssh_private_key_enc"] = encrypt_secret(data["ssh_private_key"], secret_key)
    if data.get("kubeconfig"):
        creds["kubeconfig_enc"] = encrypt_secret(data["kubeconfig"], secret_key)
    return creds


@contextlib.contextmanager
def ssh_key_file(infra: dict, secret_key: str) -> Iterator[str | None]:
    """Yield a private-key path for `infra`: the DB-stored key (decrypted,
    materialized to a mode-0600 temp file that is removed on exit) when set,
    else `ssh_key_ref`, else None. DB key wins when both exist."""
    key = decrypt_secret((infra.get("credentials") or {}).get("ssh_private_key_enc"), secret_key)
    if not key:
        yield infra.get("ssh_key_ref")
        return
    fd, path = tempfile.mkstemp(prefix="aegis-infra-key-")  # mkstemp => mode 0600
    try:
        # OpenSSH rejects a PEM key without a trailing newline; textarea pastes
        # often lose it.
        os.write(fd, (key.rstrip("\n") + "\n").encode())
        os.close(fd)
        yield path
    finally:
        os.unlink(path)


async def _unique_slug(pool: asyncpg.Pool, base: str) -> str:
    slug = base
    n = 2
    while await pool.fetchval("SELECT 1 FROM infra WHERE slug = $1", slug):
        slug = f"{base}-{n}"
        n += 1
    return slug


async def list_infra(pool: asyncpg.Pool) -> list[dict]:
    rows = await pool.fetch(f"SELECT {_SELECT_COLS} FROM infra ORDER BY name")
    return [public_infra(dict(r)) for r in rows]


async def get_infra(
    pool: asyncpg.Pool, infra_id: UUID | str, *, include_credentials: bool = False
) -> dict | None:
    """Fetch one row. Only server-side callers (provisioning, probes) may pass
    ``include_credentials=True`` — never a route that returns the result."""
    row = await pool.fetchrow(f"SELECT {_SELECT_COLS} FROM infra WHERE id = $1", infra_id)
    if not row:
        return None
    return dict(row) if include_credentials else public_infra(dict(row))


async def get_aegis_host(pool: asyncpg.Pool) -> dict | None:
    """Return the infra row flagged hosts_aegis=true, if any."""
    row = await pool.fetchrow(
        f"SELECT {_SELECT_COLS} FROM infra WHERE hosts_aegis = true ORDER BY updated_at DESC LIMIT 1"
    )
    return dict(row) if row else None


async def create_infra(pool: asyncpg.Pool, data: dict[str, Any], secret_key: str = "") -> dict:
    name = (data.get("name") or "").strip()
    if not name:
        raise ValueError("name is required")
    slug = (data.get("slug") or "").strip()
    slug = await _unique_slug(pool, _slugify(slug or name))

    row = await pool.fetchrow(
        f"INSERT INTO infra "
        "(slug, name, kind, host, ssh_user, ssh_port, ssh_key_ref, docker_context, "
        " hosts_aegis, setup_files, setup_command, metadata, credentials) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13) "
        f"RETURNING {_SELECT_COLS}",
        slug,
        name,
        data.get("kind") or "ssh_host",
        data.get("host"),
        data.get("ssh_user"),
        data.get("ssh_port") or 22,
        data.get("ssh_key_ref"),
        data.get("docker_context"),
        bool(data.get("hosts_aegis", False)),
        data.get("setup_files") or [],
        data.get("setup_command"),
        data.get("metadata") or {},
        _merged_credentials(None, data, secret_key),
    )
    return public_infra(dict(row))


async def update_infra(
    pool: asyncpg.Pool, infra_id: UUID | str, data: dict[str, Any], secret_key: str = ""
) -> dict | None:
    existing = await get_infra(pool, infra_id, include_credentials=True)
    if not existing:
        return None

    fields = {k: v for k, v in data.items() if k in _EDITABLE_FIELDS and v is not None}
    if data.get("ssh_private_key") or data.get("kubeconfig"):
        fields["credentials"] = _merged_credentials(existing.get("credentials"), data, secret_key)
    if not fields:
        return public_infra(existing)

    set_clauses = []
    values: list[Any] = [infra_id]
    for i, (key, value) in enumerate(fields.items(), start=2):
        set_clauses.append(f"{key} = ${i}")
        values.append(value)
    set_sql = ", ".join(set_clauses)

    row = await pool.fetchrow(
        f"UPDATE infra SET {set_sql}, updated_at = now() WHERE id = $1 RETURNING {_SELECT_COLS}",
        *values,
    )
    return public_infra(dict(row)) if row else None


async def delete_infra(pool: asyncpg.Pool, infra_id: UUID | str) -> bool:
    result = await pool.execute("DELETE FROM infra WHERE id = $1", infra_id)
    return result != "DELETE 0"


async def _run_ssh(
    ssh_args: list[str], timeout: int = _SSH_TIMEOUT, stdin: bytes | None = None
) -> dict:
    """Run an ssh command, optionally piping `stdin`, and capture the result."""
    proc = await asyncio.create_subprocess_exec(
        *ssh_args,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(input=stdin), timeout=timeout)
        return {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": stdout.decode("utf-8", "replace")[-_PROVISION_STDOUT_CAP:],
            "stderr": stderr.decode("utf-8", "replace")[-_PROVISION_STDERR_CAP:],
        }
    except TimeoutError:
        return {"ok": False, "exit_code": -1, "stdout": "", "stderr": "ssh timed out"}
    except Exception as exc:  # pragma: no cover - defensive
        return {"ok": False, "exit_code": -1, "stdout": "", "stderr": str(exc)}
    finally:
        await kill_and_wait(proc)


def _write_file_remote_cmd(path: str, mode: str | None) -> str:
    """Remote command that base64-decodes stdin into `path`.

    Base64 (rather than a heredoc/quoted literal) sidesteps all shell-quoting
    hazards in arbitrary file content — the content never touches the shell,
    only the path does.
    """
    quoted_path = shlex.quote(path)
    quoted_dir = shlex.quote(path.rsplit("/", 1)[0] or ".")
    cmd = f"mkdir -p {quoted_dir} && base64 -d > {quoted_path}"
    if mode:
        cmd += f" && chmod {shlex.quote(str(mode))} {quoted_path}"
    return cmd


async def provision_infra(pool: asyncpg.Pool, infra_id: UUID | str, secret_key: str = "") -> dict:
    """Push setup_files + run setup_command on the target host over SSH.

    Returns the updated infra row plus a `log` list of per-step results.
    Never raises — all failures are captured into status='error'/last_error
    so the caller (route) can just serialize the return value.
    """
    infra = await get_infra(pool, infra_id, include_credentials=True)
    if not infra:
        return {"error": "infra not found"}

    has_stored_key = bool(
        ((infra.get("credentials") or {}).get("ssh_private_key_enc") or {}).get("value")
    )
    if (
        not infra.get("host")
        or not infra.get("ssh_user")
        or not (infra.get("ssh_key_ref") or has_stored_key)
    ):
        error = "host, ssh_user, and an SSH key (stored or ssh_key_ref) are required to provision"
        row = await pool.fetchrow(
            "UPDATE infra SET status = 'error', last_error = $2, updated_at = now() "
            f"WHERE id = $1 RETURNING {_SELECT_COLS}",
            infra_id,
            error,
        )
        return {
            **public_infra(dict(row)),
            "log": [{"step": "preflight", "ok": False, "error": error}],
        }

    await pool.execute(
        "UPDATE infra SET status = 'provisioning', updated_at = now() WHERE id = $1", infra_id
    )

    host = infra["host"]
    user = infra["ssh_user"]
    port = infra.get("ssh_port") or 22

    log: list[dict] = []
    setup_files = infra.get("setup_files") or []
    error: str | None = None

    with ssh_key_file(infra, secret_key) as key_file:
        if not key_file:
            error = "stored SSH key could not be decrypted (wrong AEGIS_SECRET_KEY?)"
            log.append({"step": "preflight", "ok": False, "error": error})
            setup_files = []

        def ssh_args(remote_cmd: str) -> list[str]:
            args = build_ssh_args(host, user, key_file, remote_cmd, connect_timeout=10)
            # build_ssh_args doesn't take a port; splice `-p <port>` in before the
            # destination (second-to-last element).
            if port and port != 22:
                args = args[:-2] + ["-p", str(port)] + args[-2:]
            return args

        for entry in setup_files:
            path = entry.get("path") if isinstance(entry, dict) else None
            content = entry.get("content", "") if isinstance(entry, dict) else None
            mode = entry.get("mode") if isinstance(entry, dict) else None
            if not path:
                log.append({"step": "write_file", "ok": False, "error": "missing path"})
                error = error or "a setup_files entry is missing 'path'"
                continue
            encoded = base64.b64encode((content or "").encode()).decode()
            result = await _run_ssh(
                ssh_args(_write_file_remote_cmd(path, mode)), stdin=encoded.encode()
            )
            log.append({"step": f"write_file:{path}", "ok": result["ok"], **result})
            if not result["ok"]:
                error = error or f"failed writing {path}: {result['stderr'][:200]}"
                break

        if error is None and infra.get("setup_command"):
            result = await _run_ssh(ssh_args(infra["setup_command"]), timeout=300)
            log.append({"step": "setup_command", "ok": result["ok"], **result})
            if not result["ok"]:
                error = f"setup_command failed: {result['stderr'][:200]}"

    if error is None:
        row = await pool.fetchrow(
            "UPDATE infra SET status = 'ready', last_provisioned_at = now(), "
            f"last_error = NULL, updated_at = now() WHERE id = $1 RETURNING {_SELECT_COLS}",
            infra_id,
        )
        logger.info("infra_provisioned", infra_id=str(infra_id), slug=infra["slug"])
    else:
        row = await pool.fetchrow(
            "UPDATE infra SET status = 'error', last_error = $2, updated_at = now() "
            f"WHERE id = $1 RETURNING {_SELECT_COLS}",
            infra_id,
            error,
        )
        logger.warning("infra_provision_failed", infra_id=str(infra_id), error=error)

    return {**public_infra(dict(row)), "log": log}
