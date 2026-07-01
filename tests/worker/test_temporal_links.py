"""Pure helpers that build Temporal Web UI links for Todoist comments."""

from __future__ import annotations

from aegis_worker.shared.temporal_links import (
    workflow_history_url,
    workflow_run_footer,
)


def test_history_url_full_shape():
    url = workflow_history_url(
        "https://temporal.example.com",
        "chat-investigate-abc-123",
        "run-uuid-456",
        "default",
    )
    assert url == (
        "https://temporal.example.com/namespaces/default/workflows/"
        "chat-investigate-abc-123/run-uuid-456/history"
    )


def test_history_url_strips_trailing_slash_on_base():
    url = workflow_history_url("https://temporal.example.com/", "wf", "run", "default")
    assert url == ("https://temporal.example.com/namespaces/default/workflows/wf/run/history")


def test_history_url_url_encodes_workflow_id():
    """Workflow ids can in theory carry path-hostile chars; the id segment is
    percent-encoded so the link doesn't break."""
    url = workflow_history_url("https://t.example", "a/b c", "run", "default")
    assert "a%2Fb%20c" in url
    # run_id (a Temporal uuid) is safe and left as-is.
    assert url.endswith("/run/history")


def test_history_url_none_when_inputs_missing():
    assert workflow_history_url("", "wf", "run") is None
    assert workflow_history_url("https://t", "", "run") is None
    assert workflow_history_url("https://t", "wf", "") is None


def test_footer_is_clickable_link_when_url_buildable():
    footer = workflow_run_footer("https://temporal.example.com", "wf-1", "run-1", "default")
    # Preserves the literal "Workflow run:" marker (clarify loop-guard SQL).
    assert footer.startswith("Workflow run: ")
    assert "[wf-1]" in footer
    assert (
        "(https://temporal.example.com/namespaces/default/workflows/wf-1/run-1/history)" in footer
    )


def test_footer_falls_back_to_plain_when_no_url():
    """No UI url configured (or no run_id) → plain marker, still excluded by
    clarify's NOT LIKE '%Workflow run:%' guard."""
    footer = workflow_run_footer("", "wf-1", "run-1")
    assert footer == "Workflow run: wf-1"
    assert "Workflow run:" in workflow_run_footer("https://t", "wf-1", "")


def test_footer_empty_when_no_workflow_id():
    assert workflow_run_footer("https://t", "", "run") == ""
