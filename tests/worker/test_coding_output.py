"""Reading a coding-CLI run's stream-json output (`activities/coding_output.py`).

Moved from the alert investigation's tests when the infra lane left v1; the
agent-run lane reads its runs with these helpers.
"""

import json  # noqa: F401 — some cases build their fixtures with it


def test_extract_transcript_returns_empty_for_toolonly_stream_json():
    """Stream-json with no assistant event yields "" — never raw JSON.

    `_exec` caps SSH stdout to the tail of the file, so a tool-heavy run can
    lose every assistant turn. The old raw fallback handed those bytes to the
    assessor LLM, producing every prod verdict's confidence=0.0 with
    `{"role":"tool"…` inside root_cause.
    """
    from aegis_worker.activities.coding_output import _extract_kimi_transcript

    tool_only = (
        '{"role":"tool","content":[{"type":"text","text":"command not found"}]}\n'
        '{"role":"user","content":[{"type":"text","text":"continue"}]}\n'
    )
    assert _extract_kimi_transcript(tool_only) == ""


def test_extract_transcript_keeps_assistant_text_and_drops_tool_noise():
    from aegis_worker.activities.coding_output import _extract_kimi_transcript

    raw = "\n".join(
        [
            json.dumps({"session_id": "s1"}),
            json.dumps(
                {"role": "tool", "content": [{"type": "text", "text": "NOISE nfs mount table"}]}
            ),
            json.dumps(
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Root cause: stale NFS handle."}],
                }
            ),
        ]
    )
    out = _extract_kimi_transcript(raw)
    assert out == "Root cause: stale NFS handle."
    assert "NOISE" not in out


def test_extract_transcript_kimi_031_flat_string_shape():
    """kimi CLI 0.31.x's assistant `content` is a plain string (issue #271),
    not a list of typed blocks — the pre-0.31 shape `_iter_kimi_assistant_text`
    was written for. Regression: before the fix, `isinstance(content, list)`
    silently skipped every 0.31.x assistant message, so the transcript (and
    thus the assessor prompt) came back empty for every kimi run."""
    from aegis_worker.activities.coding_output import _extract_kimi_transcript

    raw = (
        '{"role":"tool","tool_call_id":"t1","content":"NOISE nfs mount table"}\n'
        '{"role":"assistant","content":"Root cause: stale NFS handle."}\n'
    )
    out = _extract_kimi_transcript(raw)
    assert out == "Root cause: stale NFS handle."
    assert "NOISE" not in out


def test_extract_transcript_still_passes_plain_text_through():
    from aegis_worker.activities.coding_output import _extract_kimi_transcript

    assert _extract_kimi_transcript("STATUS: scoped\n") == "STATUS: scoped"
