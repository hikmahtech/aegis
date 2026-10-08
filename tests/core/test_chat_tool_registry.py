"""Registry-integrity guards for the `services/tools/` decomposition (D7).

The executors moved out of `services/chat.py` are almost entirely covered by
incumbent tests, but three failure modes of the *move itself* are not:

1. a tool name silently disappearing from `TOOL_EXECUTORS` — which fails in
   production, not in a default-config test, because a DB
   `agents.metadata.tool_set` can reference a tool no seed agent declares;
2. an executor registering under the wrong name, so `defer_task` quietly
   runs `complete_task`;
3. the compat re-export in `chat.py` binding a *copy* rather than the same
   object.

The infra, coding-run and session-registry tools left with the infra and
development lanes (the DevOps and Development verticals).

The name list below is a committed snapshot, deliberately hand-written rather
than derived, so a dropped or renamed tool has to be acknowledged here.
"""

from __future__ import annotations

from aegis.services import chat
from aegis.services.chat import TOOL_EXECUTORS, ToolContext, _execute_tool
from aegis.services.tools import gtd as tools_gtd

# Every chat tool as of the services/tools split. Sorted.
EXPECTED_TOOL_NAMES = [
    "ask_knowledge",
    "capture_to_inbox",
    "complete_task",
    "configure_triage",
    "create_schedule",
    "defer_task",
    "desk_status",
    "find_reference",
    "get_finance_news",
    "get_market_overview",
    "get_quote",
    "github_issues",
    "handoff_task",
    "last_contact_with_person",
    "ledger_add_rule",
    "ledger_post",
    "ledger_query",
    "ledger_reclassify",
    "library_book",
    "library_read",
    "library_search",
    "library_suggest",
    "list_feeds",
    "list_interactions",
    "list_next_actions",
    "list_projects",
    "list_social_channels",
    "mark_waiting",
    "merge_problems",
    "note_link",
    "note_read",
    "note_search",
    "note_write",
    "paper_read",
    "paper_search",
    "pdf_to_text",
    "query_activities",
    "query_observations",
    "read_url",
    "remember_this",
    "research_topic",
    "search_knowledge",
    "social_timeline",
    "subscribe_feed",
    "system_status",
    "track_topic",
    "trigger_workflow",
    "unsubscribe_feed",
    "untrack_topic",
    "web_search",
    "whats_next",
    "youtube_transcript",
]

# name -> executor __qualname__, module path deliberately excluded so this
# survives a later move. `@aegis_tool` wraps with `functools.wraps`, so the
# qualname is the decorated function's own — a tool registered under the wrong
# name shows up here as a mismatched pair.
EXPECTED_EXECUTOR_IDENTITY = {
    "capture_to_inbox": "_exec_capture_to_inbox",
    "complete_task": "_exec_complete_task",
    "defer_task": "_exec_defer_task",
    "handoff_task": "_exec_handoff_task",
    "mark_waiting": "_exec_mark_waiting",
    "merge_problems": "_exec_merge_problems",
    "subscribe_feed": "_exec_follow_feed",
}


def _identity(executor) -> str:
    return executor.__qualname__


def test_no_tool_lost_in_the_services_tools_split():
    """A dropped/renamed executor fails here, not in production."""
    assert sorted(TOOL_EXECUTORS) == EXPECTED_TOOL_NAMES


def test_chat_tools_schema_names_match_the_registry():
    """Every advertised schema has an executor and vice versa."""
    assert sorted(t["function"]["name"] for t in chat.CHAT_TOOLS) == EXPECTED_TOOL_NAMES


def test_moved_executor_identities_are_unchanged():
    """Guards an executor silently registering under another tool's name."""
    actual = {n: _identity(TOOL_EXECUTORS[n]) for n in EXPECTED_EXECUTOR_IDENTITY}
    assert actual == EXPECTED_EXECUTOR_IDENTITY


def test_moved_executors_live_in_their_domain_modules():
    """The split actually happened, and chat.py re-exports the SAME objects."""
    assert TOOL_EXECUTORS["capture_to_inbox"] is tools_gtd._exec_capture_to_inbox
    assert chat._exec_capture_to_inbox is tools_gtd._exec_capture_to_inbox


async def test_moved_executor_reachable_through_real_dispatch():
    """`_execute_tool` → registry → moved module, end to end (no pool needed:
    the hub tool refuses a non-uuid before it reads anything)."""
    raw = await _execute_tool(None, "merge_problems", {"keep_id": "x", "merge_id": "y"}, ToolContext())

    assert raw.startswith("Refused:")
