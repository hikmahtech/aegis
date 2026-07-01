"""Shared helpers for the connectors that shell out via asyncio subprocesses.

Centralises the "kill the child and wait for the OS to reap it" pattern so
every call site behaves the same on TimeoutError / CancelledError. Without
the reap step the local ssh / docker / openssl process dies, but its PID
sits in the worker container as a zombie until either tini collects it
(grandchildren only) or the container restarts — see lesson ad86df08 for
the historical 6.7k-zombie incident.
"""

from __future__ import annotations

import asyncio


async def kill_and_wait(
    proc: asyncio.subprocess.Process, wait_timeout: float = 2.0
) -> None:
    """SIGKILL ``proc`` and wait for the kernel to reap it.

    Safe to call when the process has already exited. Swallows
    ``CancelledError`` and ``TimeoutError`` so callers can use it inside a
    ``finally`` clause without masking the original error.
    """
    if proc.returncode is not None:
        return
    try:
        proc.kill()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=wait_timeout)
    except (TimeoutError, asyncio.CancelledError, Exception):
        # Reaping is best-effort — never let it mask the original error.
        pass
