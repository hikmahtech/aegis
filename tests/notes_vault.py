"""A throwaway Obsidian vault for the notes tests (#514).

A bare "GitHub" repo seeded with made-up notes, a "device" clone standing in
for obsidian-git on the phone, and a `NotesConfig` pointing AEGIS at the remote.
Nothing here is the user's content — every note is invented for the tests.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from aegis.services import notes

HAS_GIT = shutil.which("git") is not None
needs_git = pytest.mark.skipif(not HAS_GIT, reason="git not installed")

CIPHER = "%%🔐 c2VjcmV0IGRpYXJ5IGVudHJ5 🔐%%"
DAILY_TEMPLATE = (
    "---\n"
    'aliases: ["{{date:DD-MM-YYYY}}", "{{date:MMM DD, YYYY}}"]\n'
    'date: "{{date}}"\n'
    'time: "{{time}}"\n'
    'tags: ["journal", "daily"]\n'
    "---\n"
    "# {{date:MMM DD, YYYY}}\n"
    "## Journal\n"
    "- \n"
)
SEED_FILES = {
    "_templates/{{tp_title_today}}.md": DAILY_TEMPLATE,
    "journal/12 Sep 26.md": "# Sep 12, 2026\n## Journal\n- the user wrote this\n",
    "knowledge/dev/secrets.md": f"# Secrets\nopen text\n{CIPHER}\nmore open text\n",
    ".obsidian/app.json": "{}\n",
}


def _env() -> dict[str, str]:
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "Device",
        "GIT_AUTHOR_EMAIL": "device@example.com",
        "GIT_COMMITTER_NAME": "Device",
        "GIT_COMMITTER_EMAIL": "device@example.com",
    }


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=_env(), capture_output=True, text=True, check=True
    ).stdout


def make_vault(tmp_path: Path, files: dict[str, str] | None = None) -> dict:
    remote = tmp_path / "remote.git"
    git("init", "-q", "--bare", "-b", "main", str(remote), cwd=tmp_path)
    seed = tmp_path / "seed"
    seed.mkdir()
    git("init", "-q", "-b", "main", cwd=seed)
    for rel, text in (files if files is not None else SEED_FILES).items():
        (seed / rel).parent.mkdir(parents=True, exist_ok=True)
        (seed / rel).write_text(text, "utf-8")
    git("add", "-A", cwd=seed)
    git("commit", "-q", "-m", "seed", cwd=seed)
    git("remote", "add", "origin", str(remote), cwd=seed)
    git("push", "-q", "-u", "origin", "main", cwd=seed)
    device = tmp_path / "device"
    git("clone", "-q", str(remote), str(device), cwd=tmp_path)
    key = tmp_path / notes.DEPLOY_KEY_NAME
    key.write_text("not a real key\n", "utf-8")
    checkout = tmp_path / "checkout"
    cfg = notes.NotesConfig(path=checkout, repo_url=str(remote), deploy_key=key)
    settings = SimpleNamespace(
        gmail_token_dir=str(tmp_path), notes_repo_url=str(remote), notes_path=str(checkout)
    )
    return {
        "cfg": cfg,
        "settings": settings,
        "remote": remote,
        "device": device,
        "tmp": tmp_path,
    }


def remote_file(v: dict, rel: str) -> str:
    probe = v["tmp"] / f"probe-{os.urandom(3).hex()}"
    git("clone", "-q", str(v["remote"]), str(probe), cwd=v["tmp"])
    path = probe / rel
    return path.read_text("utf-8") if path.exists() else ""


def remote_head(v: dict) -> str:
    return git("rev-parse", "main", cwd=v["remote"]).strip()


def device_commit(v: dict, changes: dict[str, str | None], message: str = "vault backup") -> None:
    """obsidian-git on the phone: write (or delete, with None) and push."""
    dev = v["device"]
    git("pull", "-q", cwd=dev)
    for rel, text in changes.items():
        path = dev / rel
        if text is None:
            git("rm", "-q", rel, cwd=dev)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, "utf-8")
    git("add", "-A", cwd=dev)
    git("commit", "-q", "-m", message, cwd=dev)
    git("push", "-q", cwd=dev)
