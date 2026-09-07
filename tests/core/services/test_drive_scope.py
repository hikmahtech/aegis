"""The Drive granted-scope check (statement spec §5.5).

A token minted before a scope existed simply lacks it. The check degrades the
way `MeetingNotesFlow` does — `doc_status=no_drive_scope`, reported by name —
because the alternative is an opaque 403 or, worse, a silent "the folder was
empty".
"""

import json

from aegis.services.drive import (
    DRIVE_FILE_SCOPE,
    DRIVE_READONLY_SCOPE,
    NO_SCOPE,
    SCOPE_OK,
    scope_status,
    token_scopes,
)

GMAIL = "https://www.googleapis.com/auth/gmail.modify"


def _token(tmp_path, *scopes):
    path = tmp_path / "arshad-hikmah.json"
    path.write_text(json.dumps({"token": "x", "scopes": list(scopes)}))
    return path


def test_a_token_with_drive_readonly_is_ok(tmp_path):
    path = _token(tmp_path, GMAIL, DRIVE_READONLY_SCOPE, DRIVE_FILE_SCOPE)
    assert scope_status(path) == SCOPE_OK
    assert token_scopes(path) == [GMAIL, DRIVE_READONLY_SCOPE, DRIVE_FILE_SCOPE]


def test_drive_file_alone_cannot_see_a_file_the_owner_dropped_in(tmp_path):
    # `drive.file` sees only what AEGIS itself created, so it is NOT a
    # substitute for readonly on the intake folder.
    assert scope_status(_token(tmp_path, GMAIL, DRIVE_FILE_SCOPE)) == NO_SCOPE


def test_a_token_without_the_scope_reports_no_drive_scope(tmp_path):
    assert scope_status(_token(tmp_path, GMAIL)) == NO_SCOPE


def test_the_scope_asked_for_is_the_scope_checked(tmp_path):
    path = _token(tmp_path, GMAIL, DRIVE_READONLY_SCOPE)
    assert scope_status(path, DRIVE_FILE_SCOPE) == NO_SCOPE
    assert scope_status(path, DRIVE_READONLY_SCOPE) == SCOPE_OK


def test_a_missing_or_unreadable_token_reports_the_scope_missing_not_an_error(tmp_path):
    assert scope_status(tmp_path / "absent.json") == NO_SCOPE
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    assert scope_status(broken) == NO_SCOPE
    assert token_scopes(broken) == []


def test_a_token_with_no_scopes_key_reports_the_scope_missing(tmp_path):
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"token": "x"}))
    assert scope_status(path) == NO_SCOPE
