"""The journal prompt's answer, end to end (vault record spec §3): it lands in
the day's note word for word, and nowhere else AEGIS keeps things.

    JournalPromptFlow (real, under the run recorder)
      → journal_gap_check (real, against a throwaway vault)
      → ABANDONED InteractionFlow child (real, private)
        → interactions row (real)
          → POST /api/interactions/{id}/resolve (the real route and its learning loop)
            → Temporal signal to the live child
              → file_journal_answer (real: the vault write, then the blank)

Only the clock and the card's delivery are stubs. The answer must then be in
the note, and in none of: a log record, a run's result, `workflow_runs`,
`interactions` and `agent_memory`. What Temporal's own history keeps is out of
AEGIS's hands and stated in the docs.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from uuid import uuid4

import pytest
import pytest_asyncio
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.api.routes.interactions import get_workflow_client
from aegis.config import Settings
from aegis_worker.activities.interactions import InteractionActivities
from aegis_worker.activities.notes import NotesActivities
from aegis_worker.activities.runs_v3 import RunRecorderActivities
from aegis_worker.flows.interaction import InteractionFlow
from aegis_worker.flows.journal_prompt import JournalPromptConfig, JournalPromptFlow
from aegis_worker.interceptors import WorkflowRunRecorderInterceptor
from httpx import ASGITransport, AsyncClient
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from tests.notes_vault import SEED_FILES, make_vault, needs_git, remote_file

AGENT = "sebas"
DAY = "2026-09-21"  # the day before the stub clock's 2026-09-22
CHILD = f"journal-prompt-{DAY}"
PATH = "journal/2026/09. Sep/21 Sep 26.md"
# Two lines, each a phrase nothing else in AEGIS would ever write.
LINES = ("zebra walnut origami at dawn", "quartz lantern meadow by the river")
ANSWER = "\n".join(LINES)

SETTINGS = Settings(
    database_url="postgresql://test:test@localhost:5432/test",
    litellm_url="https://litellm.example.com/v1",
    temporal_ui_url="https://temporal.example.com",
    n8n_ui_url="https://n8n.example.com",
    admin_username="admin",
    admin_password="admin",
    n8n_webhook_secret="test-secret",
)
AUTH = {"Authorization": "Basic " + base64.b64encode(b"admin:admin").decode()}


@pytest_asyncio.fixture(loop_scope="function")
async def clean_db(db_pool):
    async def wipe():
        await db_pool.execute("DELETE FROM interactions WHERE origin = 'journal_prompt'")
        await db_pool.execute(
            "DELETE FROM workflow_runs WHERE workflow_type IN ('JournalPromptFlow', "
            "'InteractionFlow')"
        )
        await db_pool.execute("DELETE FROM settings WHERE key = 'vault_layout'")

    await db_pool.execute(
        "INSERT INTO agents (id, name, role, system_prompt_path, active) "
        "VALUES ('sebas', 'Sebas', 'assistant', 'personalities/sebas', TRUE) "
        "ON CONFLICT (id) DO NOTHING"
    )
    await wipe()
    yield db_pool
    await wipe()


def _clock():
    @activity.defn(name="daylog_local_day")
    async def daylog_local_day(now_iso: str) -> dict:
        return {"timezone": "UTC", "date": "2026-09-22"}

    return daylog_local_day


def _card():
    @activity.defn(name="send_interaction_card")
    async def send_interaction_card(
        interaction_id, agent_id, kind, prompt, options, allow_hint=False
    ):
        return {"ok": True, "delivery_ref": {"adapter": "web"}}

    return send_interaction_card


async def _wait_for(factory, timeout: float = 15.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        value = await factory()
        if value or asyncio.get_running_loop().time() > deadline:
            return value
        await asyncio.sleep(0.05)


def _leaks(text: str) -> list[str]:
    return [line for line in LINES if line in (text or "")]


@needs_git
@pytest.mark.asyncio
async def test_the_answer_reaches_the_note_and_nothing_else(tmp_path, clean_db, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    vault = make_vault(tmp_path, SEED_FILES)
    notes_acts = NotesActivities(settings=vault["settings"], db_pool=clean_db)
    inter = InteractionActivities(clean_db)
    recorder = RunRecorderActivities(db_pool=clean_db)
    app = create_app(run_lifespan=False)
    app.state.db_pool = clean_db
    app.dependency_overrides[get_settings] = lambda: SETTINGS

    async with await WorkflowEnvironment.start_time_skipping() as env:
        # The card's 22-hour timer would fire at once under time skipping.
        with env.auto_time_skipping_disabled():
            app.dependency_overrides[get_workflow_client] = lambda: env.client
            async with Worker(
                env.client,
                task_queue="jp-privacy",
                workflows=[JournalPromptFlow, InteractionFlow],
                activities=[
                    _clock(),
                    _card(),
                    notes_acts.journal_gap_check,
                    notes_acts.file_journal_answer,
                    inter.insert_interaction,
                    inter.resolve_interaction,
                    inter.apply_interaction_timeout,
                    inter.update_interaction_delivery_ref,
                    recorder.record_workflow_run,
                ],
                interceptors=[WorkflowRunRecorderInterceptor()],
            ):
                parent_id = f"journal-prompt-run-{uuid4().hex[:8]}"
                parent = await env.client.execute_workflow(
                    JournalPromptFlow.run,
                    JournalPromptConfig(agent_id=AGENT),
                    id=parent_id,
                    task_queue="jp-privacy",
                )
                assert parent["status"] == "sent", parent
                card = await _wait_for(
                    lambda: clean_db.fetchval(
                        "SELECT id FROM interactions WHERE flow_run_id = $1 "
                        "AND delivery_ref IS NOT NULL",
                        CHILD,
                    )
                )
                assert card is not None, "the flow never opened a card"
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as client:
                    resp = await client.post(
                        f"/api/interactions/{card}/resolve",
                        json={"response": {"value": ANSWER}},
                        headers=AUTH,
                    )
                assert resp.status_code == 200, resp.status_code
                child = await env.client.get_workflow_handle(CHILD).result()

    out = capsys.readouterr()
    note = remote_file(vault, PATH)
    row = await clean_db.fetchrow(
        "SELECT response, row_to_json(i)::text AS whole FROM interactions i WHERE id = $1", card
    )
    runs = await clean_db.fetch(
        "SELECT workflow_id, row_to_json(w)::text AS whole FROM workflow_runs w "
        "WHERE workflow_id = ANY($1::text[])",
        [parent_id, CHILD],
    )
    memory = await clean_db.fetch(
        "SELECT content FROM agent_memory WHERE content LIKE '%' || $1 || '%'", LINES[0]
    )

    # The premises: the words went into the note, one bullet per line, and
    # every place checked below was written to.
    assert f"\t- {LINES[0]}\n\t- {LINES[1]}\n" in note
    assert child["status"] == "resolved"
    assert sorted(r["workflow_id"] for r in runs) == sorted([parent_id, CHILD])
    assert caplog.records, "the logs were captured"

    assert row["response"] == {"value": "", "filed": PATH}
    assert _leaks(row["whole"]) == [], "interactions still holds the answer"
    for r in runs:
        assert _leaks(r["whole"]) == [], f"workflow_runs holds the answer ({r['workflow_id']})"
    assert _leaks(repr(parent)) == [] and _leaks(repr(child)) == [], "a run's result holds it"
    assert memory == [], "the learning loop banked the answer"
    assert _leaks(caplog.text + out.out + out.err) == [], "a log line carries the answer"
