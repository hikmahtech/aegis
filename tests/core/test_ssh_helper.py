"""Tests for shared SSH argument builder."""

from aegis.connectors._ssh import build_ssh_args


def test_build_ssh_args_returns_list():
    args = build_ssh_args("node-a", "user", "~/.ssh/id_ed25519", "echo hi")
    assert args[0] == "ssh"
    assert "-i" in args
    assert "user@node-a" in args
    assert args[-1] == "echo hi"


def test_build_ssh_args_includes_options():
    args = build_ssh_args("node-a", "user", "~/.ssh/id_ed25519", "ls")
    assert "-o" in args
    # TOFU: accept unknown host once, reject on host-key change (MITM detection)
    assert any("StrictHostKeyChecking=accept-new" in a for a in args)
    assert not any("StrictHostKeyChecking=no" in a for a in args)
    assert any("ConnectTimeout=10" in a for a in args)


def test_build_ssh_args_key_file_injected():
    args = build_ssh_args("host", "user", "/path/to/key", "cmd")
    key_idx = args.index("-i")
    assert args[key_idx + 1] == "/path/to/key"


def test_build_ssh_args_known_hosts_optional():
    """When known_hosts is not set, no UserKnownHostsFile option is emitted."""
    args = build_ssh_args("host", "user", "/k", "cmd")
    assert not any("UserKnownHostsFile" in a for a in args)


def test_build_ssh_args_known_hosts_set():
    """When known_hosts is provided, it's passed via -o UserKnownHostsFile=..."""
    args = build_ssh_args("host", "user", "/k", "cmd", known_hosts="/etc/aegis/ssh/known_hosts")
    assert any(a == "UserKnownHostsFile=/etc/aegis/ssh/known_hosts" for a in args)
