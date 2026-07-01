"""Shared SSH argument builder."""

from __future__ import annotations


def build_ssh_args(
    host: str,
    user: str,
    key_file: str,
    remote_cmd: str,
    known_hosts: str | None = None,
    connect_timeout: int = 10,
    batch_mode: bool = False,
) -> list[str]:
    """Build SSH argument list for subprocess execution.

    Uses ``StrictHostKeyChecking=accept-new`` for trust-on-first-use: the first
    time we see a host its key is recorded, but a *changed* host key is rejected
    (MITM detection). Pass ``known_hosts`` to pin the known_hosts file to a
    persistent, well-known location (recommended for production).

    ``connect_timeout`` sets ``ConnectTimeout`` (seconds). ``batch_mode`` adds
    ``BatchMode=yes`` so a probe fails fast instead of blocking on a prompt.
    """
    args = [
        "ssh",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"ConnectTimeout={connect_timeout}",
    ]
    if batch_mode:
        args += ["-o", "BatchMode=yes"]
    if known_hosts:
        args += ["-o", f"UserKnownHostsFile={known_hosts}"]
    args += [
        "-i",
        key_file,
        f"{user}@{host}",
        remote_cmd,
    ]
    return args
