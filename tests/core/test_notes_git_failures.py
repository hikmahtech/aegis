"""A vault write tells a conflict from a failure (#514, from the audit).

`NotesConflict` means the remote moved on under the write — a rejected
non-fast-forward push or a rebase that conflicted — and is worth the
drop-pull-reapply retry. Anything else (no network, a refused key, a missing
repository) fails the same way twice, so it is a plain `NotesError` at once,
with a short reason that carries no URL, path or credential. Every git failure
used to be a conflict: retried, then reported as "the vault changed under the
write twice" with git's own stderr in it."""

from __future__ import annotations

import subprocess
from datetime import date, datetime

import pytest
from aegis.services import notes

from tests.notes_vault import device_commit, make_vault, needs_git, remote_file, remote_head


@pytest.fixture
def vault(tmp_path):
    return make_vault(tmp_path)


def _append(day: date) -> notes.Append:
    return notes.journal_append("daily", day, day.isoformat(), "Raphael's day.", datetime(2026, 9, 13, 21, 0))


@needs_git
def test_a_push_rejected_as_non_fast_forward_is_a_conflict_and_is_retried(vault, monkeypatch):
    real_commit = notes._commit
    calls = {"n": 0}

    def commit_then_the_phone_pushes(cfg, summary, paths):
        real_commit(cfg, summary, paths)
        calls["n"] += 1
        if calls["n"] == 1:
            device_commit(
                vault,
                {"journal/12 Sep 26.md": "# Sep 12, 2026\n## Journal\n- the user wrote this\n"
                 "- and this, on the phone\n"},
            )

    monkeypatch.setattr(notes, "_commit", commit_then_the_phone_pushes)
    res = notes.write_sync(vault["cfg"], [_append(date(2026, 9, 14))], "journal")
    assert res["status"] == "written" and res["attempts"] == 2
    assert "on the phone" in remote_file(vault, "journal/12 Sep 26.md")
    assert notes.marker("daylog:2026-09-14") in remote_file(
        vault, "journal/2026/09. Sep/14 Sep 26.md"
    )


@needs_git
def test_a_remote_that_is_gone_is_an_error_not_a_conflict(vault):
    cfg = vault["cfg"]
    notes.read_many_sync(cfg, ["journal/12 Sep 26.md"])  # the checkout exists
    vault["remote"].rename(vault["tmp"] / "gone.git")
    with pytest.raises(notes.NotesError) as err:
        notes.write_sync(cfg, [_append(date(2026, 9, 15))], "journal")
    assert not isinstance(err.value, notes.NotesConflict)
    message = str(err.value)
    assert str(vault["tmp"]) not in message, "the reason names no path"
    assert len(message) < 120
    assert not (cfg.path / notes.daily_note_path(date(2026, 9, 15))).exists()


@needs_git
def test_a_push_refused_for_the_key_is_an_error_with_a_short_reason(vault, monkeypatch):
    real_run = notes._run

    def refused_push(args, cfg, **kw):
        if args[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(
                args,
                128,
                "",
                "git@github.com: Permission denied (publickey).\r\n"
                "fatal: Could not read from remote repository.\n",
            )
        return real_run(args, cfg, **kw)

    monkeypatch.setattr(notes, "_run", refused_push)
    head = remote_head(vault)
    with pytest.raises(notes.NotesError) as err:
        notes.write_sync(vault["cfg"], [_append(date(2026, 9, 16))], "journal")
    assert not isinstance(err.value, notes.NotesConflict)
    assert "deploy key" in str(err.value)
    assert "github.com" not in str(err.value)
    assert remote_head(vault) == head


def test_a_credential_in_a_url_never_reaches_an_error_message():
    stderr = "fatal: unable to access 'https://x-access-token:ghp_s3cret@github.com/a/b.git/': 403"
    scrubbed = notes._scrub(stderr)
    assert "ghp_s3cret" not in scrubbed and "x-access-token" not in scrubbed
    assert "github.com/a/b.git" in scrubbed
