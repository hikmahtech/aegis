"""Raphael's notes in the Obsidian vault (#514).

Pure tests always run; the round trips need git and use local bare repos, as
the books tests do. No personal note content: every fixture is made up here.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest
from aegis.services import notes
from aegis.services import notes_write as nw

from tests.notes_vault import (
    CIPHER,
    DAILY_TEMPLATE,
    git,
    make_vault,
    needs_git,
    remote_file,
    remote_head,
)

# --------------------------------------------------------------------- names


def test_journal_paths_match_the_vaults_own_notes():
    # The vault's real last notes (2023-10-25): `25 Oct 23` and `W43 Oct 23`.
    assert notes.daily_note_path(date(2023, 10, 25)) == "journal/25 Oct 23.md"
    assert notes.weekly_note_path(date(2023, 10, 25)) == "journal/W43 Oct 23.md"
    assert notes.daily_note_path(date(2026, 9, 12)) == "journal/12 Sep 26.md"
    # An ISO week's Monday lands in the Sunday-started week holding it.
    assert notes.weekly_note_path(date(2026, 9, 7)) == "journal/W37 Sep 26.md"
    assert notes.monthly_note_path(date(2026, 9, 1)) == "journal/2026/09. Sep.md"


def test_week_one_is_the_week_that_holds_new_year():
    # Sun 27 Dec 2026 – Sat 2 Jan 2027 holds 1 January, so it is week 1 of 2027,
    # formatted from its Sunday — exactly what moment's `[W]ww MMM YY` gives.
    assert notes.locale_week(date(2026, 12, 29)) == 1
    assert notes.weekly_note_path(date(2026, 12, 29)) == "journal/W01 Dec 26.md"
    assert notes.locale_week(date(2026, 12, 26)) == 52


def test_monthly_notes_do_not_collide_across_years():
    assert notes.monthly_note_path(date(2025, 9, 1)) != notes.monthly_note_path(date(2026, 9, 1))


def test_is_journal_path():
    assert notes.is_journal_path("journal/12 Sep 26.md")
    assert notes.is_journal_path("journal/W37 Sep 26.md")
    assert notes.is_journal_path("journal/2026/09. Sep.md")
    assert not notes.is_journal_path("journal/2014/11. Nov/07 Nov 14.md")
    assert not notes.is_journal_path("knowledge/dev/python.md")


def test_moment_format_tokens_and_literals():
    when = datetime(2026, 9, 12, 7, 5)
    assert notes.moment_format("DD-MM-YYYY", when) == "12-09-2026"
    assert notes.moment_format("MMM DD, YYYY", when) == "Sep 12, 2026"
    assert notes.moment_format("HH:mm", when) == "07:05"
    assert notes.moment_format("[W]ww", when) == "W37"
    assert notes.moment_format("dddd", when) == "Saturday"


def test_render_template_fills_placeholders_and_drops_templater_tags():
    text = DAILY_TEMPLATE + "<% tp.file.cursor() %>\n{{title}}\n"
    out = notes.render_template(text, title="12 Sep 26", when=datetime(2026, 9, 12, 21, 30))
    assert 'aliases: ["12-09-2026", "Sep 12, 2026"]' in out
    assert 'date: "2026-09-12"' in out
    assert 'time: "21:30"' in out
    assert "# Sep 12, 2026" in out
    assert "12 Sep 26" in out
    assert "<%" not in out and "{{" not in out


# ---------------------------------------------------------------- encryption


def test_strip_encrypted_removes_every_block():
    text = f"before\n{CIPHER}\nmiddle %%🔐 AAAA 🔐%% after"
    out = notes.strip_encrypted(text)
    assert "c2VjcmV0" not in out and "AAAA" not in out
    assert out.count(notes.ENCRYPTED_PLACEHOLDER) == 2
    assert out.startswith("before\n") and out.endswith(" after")


def test_an_unterminated_block_drops_everything_after_it():
    out = notes.strip_encrypted("keep this %%🔐 dGFpbCBsZWFr and more")
    assert out == "keep this " + notes.ENCRYPTED_PLACEHOLDER


def test_text_without_blocks_is_unchanged():
    assert notes.strip_encrypted("plain %% comment %% text") == "plain %% comment %% text"


# --------------------------------------------------------------------- paths


@pytest.mark.parametrize("rel", ["raphael/topics/rag.md", "raphael/questions/why-x-abc.md"])
def test_raphael_paths_are_writable(rel):
    assert notes.check_path(rel) == rel


def test_journal_paths_only_for_the_journal_writer():
    with pytest.raises(notes.NotesPathError):
        notes.check_path("journal/12 Sep 26.md")
    assert notes.check_path("journal/12 Sep 26.md", journal=True)


@pytest.mark.parametrize(
    "rel",
    [
        "../etc/passwd.md",
        "/abs/raphael/x.md",
        "raphael/../journal/12 Sep 26.md",
        "raphael/./x.md",
        "raphael/.hidden.md",
        ".obsidian/app.md",
        "knowledge/dev/python.md",
        "raphael/x.txt",
        "raphael",
        "raphael\\x.md",
        "",
    ],
)
def test_other_paths_are_refused(rel):
    with pytest.raises(notes.NotesPathError):
        notes.check_path(rel, journal=True)


# ------------------------------------------------------------------ sections


def test_append_keeps_existing_text_and_a_marker_makes_it_a_noop():
    ap = notes.Append(rel="raphael/x.md", key="write:abc", body="new", heading="2026-09-12")
    existing = "# X\nthe user's line"
    out = notes.append_text(existing, ap)
    assert out.startswith(existing)
    assert notes.marker("write:abc") in out and out.rstrip().endswith("new")
    assert notes.append_text(out, ap) is None


def test_a_body_cannot_forge_another_writes_marker():
    ap = notes.Append(rel="raphael/x.md", key="write:abc", body="%% aegis:write:zzz %%", heading="h")
    out = notes.append_text("", ap)
    assert notes.marker("write:zzz") not in out


def test_a_link_is_one_line_with_its_marker():
    ap = nw.build_append("link", {"path": "raphael/x.md", "target": "Designing Data-Intensive Apps"})
    out = notes.append_text("# X\n", ap)
    assert out.endswith(f"- [[Designing Data-Intensive Apps]] {notes.marker(ap.key)}\n")


# --------------------------------------------------------------- notes_write


def test_normalise_adds_the_folder_and_extension_and_a_dated_heading():
    payload, problem = nw.normalise(
        "write", {"path": "topics/rag", "text": "RAG notes"}, now=datetime(2026, 9, 12)
    )
    assert problem is None
    assert payload == {
        "path": "raphael/topics/rag.md",
        "text": "RAG notes",
        "heading": "2026-09-12",
        "title": "",
    }


@pytest.mark.parametrize(
    ("op", "payload"),
    [
        ("write", {"path": "../journal/12 Sep 26", "text": "x"}),
        ("write", {"path": "raphael/x", "text": "  "}),
        ("link", {"path": "raphael/x", "target": ""}),
        ("link", {"path": "raphael/x", "target": "a]]b"}),
        ("delete", {"path": "raphael/x"}),
    ],
)
def test_normalise_refuses(op, payload):
    _, problem = nw.normalise(op, payload)
    assert problem


def test_workflow_id_and_marker_key_on_the_same_content():
    payload, _ = nw.normalise("write", {"path": "x", "text": "t", "heading": "h"})
    wid = nw.write_workflow_id("write", payload)
    assert wid == nw.write_workflow_id("write", dict(payload))
    assert wid.endswith(nw.build_append("write", payload).key.split(":", 1)[1])


def test_a_url_link_is_a_markdown_link():
    ap = nw.build_append("link", {"path": "raphael/x.md", "target": "https://a.example/p", "label": "P"})
    assert ap.body == "- [P](https://a.example/p)"


def test_question_append_is_keyed_on_question_and_answer():
    a = notes.question_append("What is RAG?", "answer one", datetime(2026, 9, 12))
    b = notes.question_append("what is rag", "answer two", datetime(2026, 9, 13))
    assert a.rel == b.rel and a.rel.startswith("raphael/questions/what-is-rag-")
    assert a.key != b.key


# ---------------------------------------------------------------- git trips


_git = git
_remote_file = remote_file
_remote_head = remote_head


@pytest.fixture
def vault(tmp_path):
    """A bare 'GitHub' repo seeded like a small vault, a 'device' clone that
    stands in for obsidian-git, and a config pointing AEGIS at the remote."""
    return make_vault(tmp_path)


def _journal_append(day: date, body: str = "Raphael's day.") -> notes.Append:
    return notes.journal_append("daily", day, day.isoformat(), body, datetime(2026, 9, 12, 21, 0))


@needs_git
def test_a_new_journal_note_is_rendered_from_the_template_and_pushed(vault):
    res = notes.write_sync(vault["cfg"], [_journal_append(date(2026, 9, 13))], "journal")
    assert res["status"] == "written"
    text = _remote_file(vault, "journal/13 Sep 26.md")
    assert text.startswith("---\n") and "# Sep 13, 2026" in text
    assert "## Raphael" in text and "Raphael's day." in text
    assert notes.marker("daylog:2026-09-13") in text
    assert "{{" not in text


@needs_git
def test_a_note_the_user_wrote_is_appended_to_never_rewritten(vault):
    before = _remote_file(vault, "journal/12 Sep 26.md")
    notes.write_sync(vault["cfg"], [_journal_append(date(2026, 9, 12))], "journal")
    after = _remote_file(vault, "journal/12 Sep 26.md")
    assert after.startswith(before)
    assert after[len(before):].strip().startswith("## Raphael")


@needs_git
def test_the_same_write_twice_changes_nothing(vault):
    notes.write_sync(vault["cfg"], [_journal_append(date(2026, 9, 12))], "journal")
    head = _remote_head(vault)
    again = notes.write_sync(vault["cfg"], [_journal_append(date(2026, 9, 12), "other")], "journal")
    assert again["status"] == "exists"
    assert _remote_head(vault) == head


@needs_git
def test_a_path_outside_raphael_is_refused_before_git(vault):
    ap = notes.Append(rel="knowledge/dev/secrets.md", key="write:x", body="no", heading="h")
    head = _remote_head(vault)
    with pytest.raises(notes.NotesPathError):
        notes.write_sync(vault["cfg"], [ap], "nope")
    assert _remote_head(vault) == head


@needs_git
def test_a_push_rejected_by_a_device_commit_is_retried_after_a_fresh_pull(vault, monkeypatch):
    cfg = vault["cfg"]
    # Clone once, so the checkout exists before the device moves on.
    notes.read_many_sync(cfg, ["journal/12 Sep 26.md"])
    # obsidian-git on the phone appends to the same note and pushes.
    device_note = vault["device"] / "journal/12 Sep 26.md"
    device_note.write_text(device_note.read_text("utf-8") + "- written on the phone\n", "utf-8")
    _git("commit", "-q", "-am", "vault backup", cwd=vault["device"])
    _git("push", "-q", cwd=vault["device"])

    real_pull = notes._pull
    calls = {"n": 0}

    def stale_first(c):
        calls["n"] += 1
        if calls["n"] > 1:
            real_pull(c)

    monkeypatch.setattr(notes, "_pull", stale_first)
    res = notes.write_sync(cfg, [_journal_append(date(2026, 9, 12))], "journal")
    assert res["status"] == "written" and res["attempts"] == 2
    text = _remote_file(vault, "journal/12 Sep 26.md")
    assert "- written on the phone" in text
    assert text.index("- written on the phone") < text.index("## Raphael")


@needs_git
def test_two_failed_pushes_report_and_leave_the_checkout_at_upstream(vault, monkeypatch):
    cfg = vault["cfg"]

    def rejected(c):
        raise notes.NotesConflict("git push was rejected: simulated")

    monkeypatch.setattr(notes, "_push", rejected)
    head = _remote_head(vault)
    with pytest.raises(notes.NotesConflict):
        notes.write_sync(cfg, [_journal_append(date(2026, 9, 14))], "journal")
    assert _remote_head(vault) == head
    assert _git("rev-parse", "HEAD", cwd=cfg.path).strip() == head
    assert not (cfg.path / "journal/14 Sep 26.md").exists()


def test_an_unconfigured_vault_refuses_to_write(tmp_path):
    cfg = notes.NotesConfig(path=tmp_path / "c", repo_url="git@example.com:x.git", deploy_key=None)
    assert not cfg.configured
    with pytest.raises(notes.NotesDisabled):
        notes.write_sync(cfg, [_journal_append(date(2026, 9, 12))], "journal")


@needs_git
def test_reads_strip_encrypted_blocks(vault):
    out = notes.read_many_sync(vault["cfg"], ["knowledge/dev/secrets.md"])
    text = out["knowledge/dev/secrets.md"]
    assert "c2VjcmV0" not in text
    assert "open text" in text and notes.ENCRYPTED_PLACEHOLDER in text


@needs_git
async def test_read_note_refuses_leaving_the_vault(vault):
    assert "error" in await notes.read_note(vault["cfg"], "../../etc/passwd.md")
    ok = await notes.read_note(vault["cfg"], "journal/12 Sep 26.md")
    assert ok["text"].startswith("# Sep 12, 2026")


@needs_git
def test_vault_changes_are_full_first_then_incremental(vault):
    cfg = vault["cfg"]
    first = notes.vault_changes_sync(cfg, None)
    assert first.full
    assert "journal/12 Sep 26.md" in first.changed
    assert "knowledge/dev/secrets.md" in first.changed
    assert not any(p.startswith((".obsidian/", "_templates/")) for p in first.changed)

    dev = vault["device"]
    (dev / "knowledge/new.md").write_text("# New\n", "utf-8")
    (dev / "journal/12 Sep 26.md").write_text("# changed\n", "utf-8")
    _git("mv", "knowledge/dev/secrets.md", "knowledge/dev/renamed.md", cwd=dev)
    _git("add", "-A", cwd=dev)
    _git("commit", "-q", "-m", "vault backup", cwd=dev)
    _git("push", "-q", cwd=dev)

    later = notes.vault_changes_sync(cfg, first.head)
    assert not later.full
    assert set(later.changed) == {"knowledge/new.md", "journal/12 Sep 26.md", "knowledge/dev/renamed.md"}
    assert later.deleted == ["knowledge/dev/secrets.md"]
    assert notes.vault_changes_sync(cfg, later.head).changed == []


@needs_git
async def test_perform_write_through_the_one_implementation(vault):
    payload, problem = nw.normalise("write", {"path": "topics/rag", "text": "RAG is retrieval."})
    assert problem is None
    first = await nw.perform_write("write", payload, vault["cfg"])
    second = await nw.perform_write("write", payload, vault["cfg"])
    assert first == {"ok": True, "message": "wrote to raphael/topics/rag.md"}
    assert second["ok"] and second["message"].startswith("already in")
    text = _remote_file(vault, "raphael/topics/rag.md")
    assert text.startswith("# rag\n") and text.count("RAG is retrieval.") == 1


async def test_perform_write_when_not_configured(tmp_path):
    cfg = notes.NotesConfig(path=tmp_path / "c")
    payload, _ = nw.normalise("write", {"path": "x", "text": "t"})
    out = await nw.perform_write("write", payload, cfg)
    assert out["ok"] is False and "not configured" in out["message"]


def test_install_deploy_key_writes_a_private_file(tmp_path):
    from types import SimpleNamespace

    settings = SimpleNamespace(gmail_token_dir=str(tmp_path), notes_deploy_key="-----BEGIN\nx\n-----END")
    path = notes.install_deploy_key(settings)
    assert path == tmp_path / "notes_deploy_key"
    assert oct(path.stat().st_mode)[-3:] == "600"
    assert notes.config_from_settings(
        SimpleNamespace(gmail_token_dir=str(tmp_path), notes_repo_url="git@x:y.git")
    ).configured
