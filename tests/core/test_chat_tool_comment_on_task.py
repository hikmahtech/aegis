"""_exec_comment_on_task: a user-voice Todoist note, posted verbatim.

The tool is how an operator (or a chat agent) drives a task's coding session:
only a *user-authored* note starts the next turn, and `is_user_note` decides
that by looking for AEGIS's own markers — the clarify/agent-reply prefixes and
the `Workflow run:` footer every turn posts. So anything this tool were to add
to the text, however cosmetic, would silently turn the comment into AEGIS's own
and the turn would never fire. These tests assert against `is_user_note`
itself rather than restating its rules, so the two cannot drift apart.

The same property is why the tool refuses a task with no coding session. On
such a task the footer-less note starts nothing; it just sits there reading as
fresh user input, which is the shape that had ClarifyFlow grading itself on
AEGIS's own labels (#353). So the session is the tool's scope, not a nicety.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from aegis.connectors.todoist import TodoistConnector
from aegis.services.chat import ToolContext, _exec_comment_on_task
from aegis.services.task_sessions import create_session, is_user_note

COMMENT = "Use the retry helper in http.py, not a bare loop.\n\nWorkflow run of the mill."

# The task under test holds a coding session; the other deliberately does not.
_TASK = "T_CMT"
_NO_SESSION = "T_CMT_NONE"


@pytest_asyncio.fixture(autouse=True, loop_scope="function")
async def sessions(db_pool):
    """`_TASK` has a coding session for the whole file; `_NO_SESSION` never does."""
    for task in (_TASK, _NO_SESSION):
        await db_pool.execute("DELETE FROM task_sessions WHERE task_id = $1", task)
    await create_session(db_pool, task_id=_TASK, agent_id="pandoras-actor")
    yield
    for task in (_TASK, _NO_SESSION):
        await db_pool.execute("DELETE FROM task_sessions WHERE task_id = $1", task)


def _fake_connector(sent: list[list[dict]]):
    """A connector that records every batch. Subclasses the real one so the
    static build_* command builders are the production ones."""

    class FakeConnector(TodoistConnector):
        def __init__(self, *a, **kw):
            pass

        async def commands(self, batch):
            sent.append(batch)
            return {"ok": True, "data": {"sync_status": {}, "temp_id_mapping": {}}}

    return FakeConnector


async def _call(db_pool, sent: list[list[dict]], **args) -> str:
    fake_settings = MagicMock()
    fake_settings.todoist_api_key = "fake"
    with patch("aegis.connectors.todoist.TodoistConnector", _fake_connector(sent)), patch(
        "aegis.config.Settings", return_value=fake_settings
    ):
        return await _exec_comment_on_task(db_pool, args, ToolContext(agent_id="sebas"))


@pytest.mark.asyncio
async def test_posts_one_note_add_with_the_text_verbatim(db_pool) -> None:
    sent: list[list[dict]] = []

    result = await _call(db_pool, sent, task_id="T_CMT", text=COMMENT)

    assert result == "Commented on T_CMT"
    assert len(sent) == 1
    batch = sent[0]
    assert [c["type"] for c in batch] == ["note_add"]
    args = batch[0]["args"]
    assert args["item_id"] == "T_CMT"
    assert args["content"] == COMMENT  # verbatim: no prefix, no footer


@pytest.mark.asyncio
async def test_whitespace_and_newlines_survive_byte_for_byte(db_pool) -> None:
    """Verbatim means the caller's exact string, whitespace included.

    A Markdown code block, an indented diff, a deliberate blank first line: all
    of them are content, and a `.strip()` on the way out would quietly rewrite
    the comment the operator typed. The only stripping this tool does is on a
    throwaway copy, to decide whether the comment is empty.
    """
    sent: list[list[dict]] = []
    padded = "\n  leading spaces\n\n\tinner tab\n\ntrailing newlines\n\n"

    await _call(db_pool, sent, task_id="T_CMT", text=padded)

    assert sent[0][0]["args"]["content"] == padded


@pytest.mark.asyncio
async def test_the_posted_text_reads_as_a_user_note(db_pool) -> None:
    """The turn trigger, asserted through the predicate that decides it."""
    sent: list[list[dict]] = []

    await _call(db_pool, sent, task_id="T_CMT", text=COMMENT)

    posted = sent[0][0]["args"]["content"]
    assert "Workflow run:" not in posted
    assert is_user_note(posted)


@pytest.mark.asyncio
async def test_refuses_empty_text_and_empty_task_id(db_pool) -> None:
    sent: list[list[dict]] = []

    assert (await _call(db_pool, sent, task_id="T_CMT", text="   ")).startswith("Refused:")
    assert (await _call(db_pool, sent, task_id="", text=COMMENT)).startswith("Refused:")
    assert sent == [], "a refused call must not reach Todoist"


@pytest.mark.asyncio
async def test_refuses_text_over_the_todoist_comment_cap(db_pool) -> None:
    """Refuse rather than truncate: a silently clipped comment is worse than a
    rejected one, because the tool's whole contract is that it posts verbatim."""
    sent: list[list[dict]] = []

    result = await _call(db_pool, sent, task_id="T_CMT", text="x" * 15_001)

    assert result.startswith("Refused:")
    assert sent == []


@pytest.mark.asyncio
async def test_refuses_a_task_with_no_coding_session(db_pool) -> None:
    """The tool drives task sessions and nothing else.

    On a session-less task the note starts no turn — it is a footer-less
    comment that ClarifyFlow then reads back as the user's own words, which is
    the #353 self-grading shape. Refusing is what keeps the tool's one contract
    ("this starts the next turn") true of every call it accepts.

    Falsifiable: delete the `get_session` guard in `_exec_comment_on_task` and
    this test fails on both assertions.
    """
    sent: list[list[dict]] = []

    result = await _call(db_pool, sent, task_id=_NO_SESSION, text=COMMENT)

    assert result == (
        f"Refused: task {_NO_SESSION} has no coding session — "
        "comment_on_task only drives task sessions"
    )
    assert sent == [], "a refused call must not reach Todoist"


@pytest.mark.asyncio
async def test_a_task_that_gains_a_session_is_accepted(db_pool) -> None:
    """The other half of the guard: the refusal is about the session, not about
    the task id. The same id that was refused above posts once a session
    exists, so the check cannot be satisfied by a stale allowlist."""
    sent: list[list[dict]] = []
    assert (await _call(db_pool, sent, task_id=_NO_SESSION, text=COMMENT)).startswith("Refused:")

    await create_session(db_pool, task_id=_NO_SESSION, agent_id="pandoras-actor")

    assert await _call(db_pool, sent, task_id=_NO_SESSION, text=COMMENT) == (
        f"Commented on {_NO_SESSION}"
    )
    assert len(sent) == 1
    assert sent[0][0]["args"]["content"] == COMMENT
