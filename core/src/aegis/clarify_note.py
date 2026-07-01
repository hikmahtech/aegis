"""Shared comment-loop guard constants for ClarifyFlow's Todoist notes.

ClarifyFlow posts machine-generated notes back to Todoist tasks to record
its classification decisions. Those notes MUST be distinguishable from
user-authored comments because:

1. `apply_sync_diff` only bumps `todoist_tasks.last_note_at` for user
   notes. Bumping on AEGIS's own output would loop the clarifier forever.
2. The webhook receiver triggers an immediate ClarifyFlow re-run on
   `note:added`; it must skip its own notes for the same reason.
3. `find_unclassified_items` reads `latest_user_note` to give the LLM
   user supervision — it filters out machine notes via SQL `NOT LIKE`.

Centralised here so a producer-side rename can't drift from the three
consumer-side filters. Reference impl after PR #220 + the 2026-05-23
todoist audit.
"""

from __future__ import annotations

# Machine-generated comment prefix. Trailing space is part of the contract
# (the timestamp `@ HH:MM UTC` follows immediately).
#
# Producers: `worker/src/aegis_worker/activities/clarify.py::_format_apply_note`
# + `_format_review_note`.
#
# Consumers:
# - `worker/src/aegis_worker/activities/todoist.py::apply_sync_diff` filters
#   note bumps that would trigger re-classify.
# - `core/src/aegis/api/routes/webhooks.py::todoist_webhook` skips the
#   immediate ClarifyFlow kick for its own notes.
# - `worker/src/aegis_worker/activities/clarify.py::find_unclassified_items`
#   filters its `latest_user_note` SQL via `NOT LIKE CLARIFY_NOTE_PREFIX || '%'`.
CLARIFY_NOTE_PREFIX = "[ClarifyFlow @ "

# Convenience for SQL LIKE patterns — Postgres expects `%` wildcards.
CLARIFY_NOTE_SQL_LIKE = CLARIFY_NOTE_PREFIX + "%"

# Agent-reply prefix family — used by post_agent_reply_comment and
# post_agent_reply_error_comment in worker.activities.clarify. The
# webhook receiver's is_clarify_own check AND find_unclassified_items'
# latest_user_note SQL subquery both filter on this prefix so an
# agent's own reply does NOT re-trigger itself.
AGENT_REPLY_PREFIX = "[Agent reply @ "
AGENT_REPLY_SQL_LIKE = AGENT_REPLY_PREFIX + "%"
# An errored reply (post_agent_reply_error_comment) ends its bracketed marker
# with ` ERROR]`. find_unclassified_items uses this to apply a cooldown after a
# failed reply so a chronically-failing AgentChatReplyFlow doesn't re-spawn (and
# re-error) every clarify tick.
AGENT_REPLY_ERROR_SQL_LIKE = AGENT_REPLY_PREFIX + "% ERROR]%"
