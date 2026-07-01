"""Build Temporal Web UI links for Todoist comments.

Pure functions (no IO, no settings) so they're trivially testable and safe to
call from activities. The flow supplies `workflow.info().workflow_id` +
`.run_id`; the activity supplies the configured UI base url + namespace.

The Temporal Web UI history page for a single execution lives at:

    {base}/namespaces/{namespace}/workflows/{workflow_id}/{run_id}/history

`workflow_run_footer` deliberately keeps the literal ``Workflow run:`` token:
clarify's eligibility SQL excludes machine-authored notes via
``content NOT LIKE '%Workflow run:%'`` (the comment-loop guard). Turning the id
into a Markdown link MUST NOT drop that token, or these comments would start
re-triggering AgentChatReplyFlow/ClarifyFlow.
"""

from __future__ import annotations

from urllib.parse import quote


def workflow_history_url(
    base_url: str,
    workflow_id: str,
    run_id: str,
    namespace: str = "default",
) -> str | None:
    """Return the Temporal UI history-page URL, or None if any input is missing.

    The workflow_id segment is percent-encoded (ids are usually URL-safe, but
    encode defensively); the run_id is a Temporal uuid and left untouched.
    """
    if not base_url or not workflow_id or not run_id:
        return None
    base = base_url.rstrip("/")
    wf = quote(workflow_id, safe="")
    return f"{base}/namespaces/{namespace}/workflows/{wf}/{run_id}/history"


def workflow_run_footer(
    base_url: str,
    workflow_id: str,
    run_id: str,
    namespace: str = "default",
) -> str:
    """Render the ``Workflow run:`` footer for a Todoist comment.

    Clickable ``Workflow run: [<id>](<url>)`` when a UI url is buildable,
    otherwise the plain ``Workflow run: <id>`` token. Empty string when there's
    no workflow_id to reference. Either non-empty form preserves the
    ``Workflow run:`` marker the clarify loop-guard relies on.
    """
    if not workflow_id:
        return ""
    url = workflow_history_url(base_url, workflow_id, run_id, namespace)
    if url:
        return f"Workflow run: [{workflow_id}]({url})"
    return f"Workflow run: {workflow_id}"
