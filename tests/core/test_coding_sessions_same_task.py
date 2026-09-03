"""Same-task collision helpers: lookup, repo filter, prompt and verdict parsing.

The verdict parser fails CLOSED — an unreadable answer must never be read as
"yes, a human is already on this task", because that silently parks the lane.
"""

from __future__ import annotations

from aegis.connectors.coding_sessions import (
    build_same_task_prompt,
    find_session,
    human_sessions_in_repo,
    parse_same_task_verdict,
)

S = [
    {"session_id": "a", "owner": "human", "repo": "acme/app", "status": "idle", "name": "fix eps"},
    {"session_id": "b", "owner": "aegis", "repo": "acme/app", "status": "busy", "name": "task 9"},
    {"session_id": "c", "owner": "human", "repo": "acme/web", "status": "busy", "name": "web"},
]


def test_find_session_matches_any_owner_or_status():
    assert find_session(S, "b")["name"] == "task 9"
    assert find_session(S, "zzz") is None


def test_human_sessions_in_repo_includes_idle_and_excludes_aegis():
    """Unlike `match_busy`, an idle human session still counts — it may hold the task."""
    assert [s["session_id"] for s in human_sessions_in_repo(S, "acme/app")] == ["a"]
    assert human_sessions_in_repo(S, "") == []


def test_parse_verdict_extracts_json_and_fails_closed_to_false():
    v = parse_same_task_verdict(
        'Sure.\n{"same_task": true, "session_name": "fix eps", "reason": "same branch"}'
    )
    assert v == {"same_task": True, "session_name": "fix eps", "reason": "same branch"}
    assert parse_same_task_verdict("no json here")["same_task"] is False
    assert parse_same_task_verdict('{"same_task": "yes"}')["same_task"] is True


def test_parse_verdict_reads_string_booleans_literally():
    """`bool("false")` is True, which would invent a collision out of a "no"."""
    assert parse_same_task_verdict('{"same_task": "false"}')["same_task"] is False
    assert parse_same_task_verdict('{"same_task": "no"}')["same_task"] is False
    assert parse_same_task_verdict('{"same_task": " TRUE "}')["same_task"] is True
    assert parse_same_task_verdict('{"same_task": "1"}')["same_task"] is True


def test_parse_verdict_fills_missing_keys_and_reports_unparseable():
    assert parse_same_task_verdict("no json here") == {
        "same_task": False,
        "session_name": "",
        "reason": "unparseable",
    }
    assert parse_same_task_verdict('{"same_task": true}') == {
        "same_task": True,
        "session_name": "",
        "reason": "",
    }
    # A fenced object with a trailing prose tail is still the first {...}.
    assert parse_same_task_verdict('```json\n{"same_task": false}\n```\nDone.') == {
        "same_task": False,
        "session_name": "",
        "reason": "",
    }


def test_prompt_names_every_session_and_asks_for_json():
    p = build_same_task_prompt(
        "Fix EPS",
        "dupes",
        [
            {
                "name": "fix eps",
                "cwd": "/r/app",
                "branch": "fix/eps",
                "log": "abc fix",
                "status_short": " M a.py",
            }
        ],
    )
    assert "fix eps" in p and "fix/eps" in p and '"same_task"' in p
    assert "Fix EPS" in p and "dupes" in p and "/r/app" in p and "abc fix" in p


def test_prompt_tolerates_missing_git_fields_and_distinguishes_repo_from_task():
    p = build_same_task_prompt("Fix EPS", "", [{"name": "bare", "cwd": "/r/app"}])
    assert "bare" in p and "unknown" in p
    # Working in the same repo is not the same task — the prompt must say so.
    assert "same repo" in p.lower()
