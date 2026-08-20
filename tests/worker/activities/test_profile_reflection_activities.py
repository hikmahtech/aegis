"""A2 — ProfileActivities: evidence, proposal, budget gate, and the applier.

Everything here runs against a real Postgres. The applier tests in particular
must never be satisfied by a mock: `apply_profile_reflection` writing the wrong
document, or writing one at all when the human said no, is only observable in
`agent_personalities` / `agent_profile_revisions`.

Row prefix `zza2-` keeps this file's data disjoint from every other test file's
under `-n auto --dist loadfile`.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
import pytest_asyncio
from aegis_worker.activities.profile import ProfileActivities
from temporalio.testing import ActivityEnvironment

from tests.llm_stub import StubbedLLMClient

AGENT = "zza2-profile"
OTHER = "zza2-other"
PURPOSE = "profile_reflection"

# Long enough that a short replacement trips A1's >50%-shrink guard, and
# distinct enough that "the doc did not change" is unambiguous.
CURRENT_DOC = "Owner is based in Pune.\nPrefers concise answers.\n" + ("x" * 400)
PROPOSED_DOC = CURRENT_DOC + "\nRuns a homelab swarm called meem."
EDITED_DOC = CURRENT_DOC + "\nRuns a homelab swarm on three nodes."


async def clear_global_evidence(conn):
    """The evidence sources `gather_profile_evidence` reads GLOBALLY (issue #220).

    Three of its five sources are agent-scoped, and a `WHERE agent_id = 'zza2-…'`
    teardown covers them. The other two are not, and no per-agent teardown
    anywhere ever could be: `finance.recurring_charge` / `finance.receipt_email`
    have no `agent_id` column at all, and `_evidence_calendar` reads EVERY
    `calendar_events_%` row in `settings`.

    So any test in the suite that leaves an active charge, a recent receipt or a
    calendar KV row behind turns this file's "quiet week" assertions into
    `assert 18 == 0` — but only on the shardings where `--dist loadfile` happens
    to co-locate the leaker, so it presents as an unrelated PR breaking an
    unrelated test. #218 chased three such fixtures at source; this clears the
    sources themselves, so the assertion holds regardless of what a future test
    leaves behind.

    Children before parents: `renewal_alert` and `receipt_email` both carry an
    FK to `recurring_charge`.
    """
    await conn.execute("DELETE FROM settings WHERE key LIKE 'calendar_events_%'")
    await conn.execute("DELETE FROM finance.renewal_alert")
    await conn.execute("DELETE FROM finance.receipt_email")
    await conn.execute("DELETE FROM finance.recurring_charge")


async def _wipe(conn):
    for agent in (AGENT, OTHER):
        await conn.execute("DELETE FROM agent_profile_revisions WHERE agent_id = $1", agent)
        await conn.execute("DELETE FROM agent_personalities WHERE agent_id = $1", agent)
        await conn.execute("DELETE FROM agent_memory WHERE agent_id = $1", agent)
        await conn.execute("DELETE FROM chat_history WHERE agent_id = $1", agent)
        await conn.execute("DELETE FROM interactions WHERE agent_id = $1", agent)
        await conn.execute("DELETE FROM notification_log WHERE agent_id = $1", agent)
        await conn.execute("DELETE FROM llm_calls WHERE agent_id = $1", agent)
    await clear_global_evidence(conn)


@pytest_asyncio.fixture(loop_scope="function")
async def clean_db(db_pool):
    async with db_pool.acquire() as conn:
        for agent in (AGENT, OTHER):
            await conn.execute(
                "INSERT INTO agents (id, name, role, system_prompt_path, active) "
                "VALUES ($1, $1, 'assistant', 'personalities/x', TRUE) "
                "ON CONFLICT (id) DO NOTHING",
                agent,
            )
        await _wipe(conn)
    yield db_pool
    async with db_pool.acquire() as conn:
        await _wipe(conn)
        await conn.execute("DELETE FROM agents WHERE id = ANY($1)", [AGENT, OTHER])


class FakeLLM:
    """Minimal stand-in for LLMClient — only `think` is ever called."""

    def __init__(self, *, response: str = "", raises: BaseException | None = None):
        self._response = response
        self._raises = raises
        self.calls: list[dict] = []

    async def think(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return {
            "response": self._response,
            "model": "fake-balanced",
            "prompt_tokens": 11,
            "completion_tokens": 22,
        }


def _acts(pool, **kwargs) -> ProfileActivities:
    return ProfileActivities(db_pool=pool, **kwargs)


async def _set_doc(pool, agent: str, content: str, kind: str = "user"):
    await pool.execute(
        "INSERT INTO agent_personalities (agent_id, kind, content) VALUES ($1,$2,$3) "
        "ON CONFLICT (agent_id, kind) DO UPDATE SET content = EXCLUDED.content",
        agent,
        kind,
        content,
    )
    from aegis.services.personalities import invalidate

    invalidate(agent)


async def _doc(pool, agent: str, kind: str = "user") -> str:
    return (
        await pool.fetchval(
            "SELECT content FROM agent_personalities WHERE agent_id = $1 AND kind = $2",
            agent,
            kind,
        )
        or ""
    )


async def _revisions(pool, agent: str) -> list:
    return await pool.fetch(
        "SELECT source, interaction_id, before_content, after_content "
        "FROM agent_profile_revisions WHERE agent_id = $1 ORDER BY id",
        agent,
    )


# ------------------------------------------------------------------- evidence


async def _seed_all_sources(pool):
    await pool.execute(
        "INSERT INTO chat_history (thread_id, agent_id, role, content) "
        "VALUES ('zza2-t', $1, 'user', 'I moved the swarm leader to node daal')",
        AGENT,
    )
    await pool.execute(
        "INSERT INTO agent_memory (agent_id, content, importance, source) "
        "VALUES ($1, 'Owner prefers single-line commit messages', 0.8, 'correction')",
        AGENT,
    )
    await pool.execute(
        "INSERT INTO interactions (flow_run_id, agent_id, kind, origin, prompt, status, "
        "response, resolved_at) VALUES ($1, $2, 'approval', 'zza2', 'Ship it?', 'resolved', "
        "$3, now())",
        f"zza2-{uuid4()}",
        AGENT,
        {"value": "rejected", "reason": "never deploy on a Friday"},
    )
    await pool.execute(
        "INSERT INTO finance.recurring_charge (account, sender_label, vendor_name, category, "
        "amount_cents, currency, cadence, status) "
        "VALUES ('zza2-acct', 'zza2', 'Zephyrly', 'software', 100, 'INR', 'monthly', 'active')"
    )
    await pool.execute(
        "INSERT INTO finance.receipt_email (message_id, account, sender, subject, received_at) "
        "VALUES ($1, 'zza2-acct', 'billing@zephyrly.test', 'Your receipt', now())",
        f"zza2-{uuid4()}",
    )
    await pool.execute(
        "INSERT INTO settings (key, value) VALUES ('calendar_events_zza2', $1) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        [{"summary": "Weekly infra review", "start": "2026-08-02T09:00:00Z"}],
    )


@pytest.mark.asyncio
async def test_gather_evidence_reads_every_source(clean_db):
    await _seed_all_sources(clean_db)
    out = await ActivityEnvironment().run(_acts(clean_db).gather_profile_evidence, AGENT, 7)

    assert out["failed"] == []
    assert out["lookback_days"] == 7
    for source in ("chat", "memories", "corrections", "finance", "calendar"):
        assert out["counts"][source] >= 1, f"{source} produced no evidence"
    assert out["total"] == sum(out["counts"].values())
    # The words themselves must survive — a count-only bundle teaches the LLM
    # nothing.
    assert any("node daal" in c for c in out["chat"])
    assert any("single-line commit" in m for m in out["memories"])
    assert any("never deploy on a Friday" in c for c in out["corrections"])
    assert any("Zephyrly" in f for f in out["finance"])
    assert any("Weekly infra review" in c for c in out["calendar"])


@pytest.mark.asyncio
async def test_gather_evidence_is_scoped_to_the_agent(clean_db):
    await clean_db.execute(
        "INSERT INTO agent_memory (agent_id, content, importance, source) "
        "VALUES ($1, 'someone else lesson', 0.9, 'correction')",
        OTHER,
    )
    out = await ActivityEnvironment().run(_acts(clean_db).gather_profile_evidence, AGENT, 7)
    assert all("someone else" not in m for m in out["memories"])


@pytest.mark.asyncio
async def test_gather_evidence_survives_one_dead_source(clean_db, monkeypatch):
    """A broken source costs its own slice and is named in `failed` — it does
    not take the bundle (or the run) with it."""
    await _seed_all_sources(clean_db)
    acts = _acts(clean_db)

    async def boom(agent_id, days):
        raise RuntimeError("finance schema is on fire")

    monkeypatch.setattr(acts, "_evidence_finance", boom)
    out = await ActivityEnvironment().run(acts.gather_profile_evidence, AGENT, 7)

    assert out["failed"] == ["finance"]
    assert out["counts"]["finance"] == 0
    assert out["counts"]["chat"] >= 1
    assert out["counts"]["memories"] >= 1
    assert out["total"] >= 4


@pytest.mark.asyncio
async def test_gather_evidence_on_a_quiet_week_is_total_zero(clean_db):
    out = await ActivityEnvironment().run(_acts(clean_db).gather_profile_evidence, AGENT, 7)
    assert out["total"] == 0
    assert out["failed"] == []


async def _seed_foreign_leak(pool):
    """What a leaking fixture in some OTHER test file leaves behind.

    Deliberately prefixed `zzleak-`, not `zza2-`: the point is rows this file's
    own row-prefix teardown has no reason to know about.
    """
    await pool.execute(
        "INSERT INTO finance.recurring_charge (account, sender_label, vendor_name, category, "
        "amount_cents, currency, cadence, status) "
        "VALUES ('zzleak-acct', 'zzleak', 'Leakwire', 'software', 900, 'INR', 'monthly', 'active')"
    )
    await pool.execute(
        "INSERT INTO finance.receipt_email (message_id, account, sender, subject, received_at) "
        "VALUES ($1, 'zzleak-acct', 'billing@leakwire.test', 'Your receipt', now())",
        f"zzleak-{uuid4()}",
    )
    await pool.execute(
        "INSERT INTO settings (key, value) VALUES ('calendar_events_zzleak', $1) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        [{"summary": "Someone else's standup", "start": "2026-08-02T09:00:00Z"}],
    )


@pytest.mark.asyncio
async def test_a_foreign_leak_cannot_survive_this_files_setup(db_pool):
    """Issue #220: the quiet-week assertion must not depend on the whole suite
    being tidy.

    Two of the five evidence sources are global, so a stray active charge, a
    recent receipt email or a `calendar_events_*` row left by ANY other test
    file counts as this agent's evidence. Reproduced on pristine `main` as
    `assert 18 == 0` here, and `assert 'carded' == 'skipped'` in the flow test.

    Uses `db_pool` rather than `clean_db` on purpose: the fixture teardown is
    not what has to work — the fixture SETUP is, because a foreign leaker has
    no teardown of ours to run.
    """
    try:
        await _seed_foreign_leak(db_pool)
        acts = _acts(db_pool)

        # The leak is real and IS read as this agent's evidence — without this
        # the assertion below would be "empty because nothing was seeded".
        dirty = await ActivityEnvironment().run(acts.gather_profile_evidence, AGENT, 7)
        assert dirty["counts"]["finance"] >= 2, dirty["counts"]
        assert dirty["counts"]["calendar"] >= 1, dirty["counts"]
        assert any("Leakwire" in f for f in dirty["finance"]), dirty["finance"]

        # Exactly what `clean_db` runs before every test in this file.
        async with db_pool.acquire() as conn:
            await _wipe(conn)

        out = await ActivityEnvironment().run(acts.gather_profile_evidence, AGENT, 7)
        assert out["total"] == 0, out["counts"]
        assert out["failed"] == []
    finally:
        # Explicit, prefix-scoped — so this test cleans up after itself even
        # when the very helper it is exercising is broken.
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM finance.receipt_email WHERE account = 'zzleak-acct'")
            await conn.execute("DELETE FROM finance.recurring_charge WHERE account = 'zzleak-acct'")
            await conn.execute("DELETE FROM settings WHERE key = 'calendar_events_zzleak'")


@pytest.mark.asyncio
async def test_gather_evidence_omits_a_memory_a4_soft_retired(clean_db):
    """A4 composition: retirement must remove a belief from A2's evidence.

    A4 (migration 020) retires a memory by stamping `superseded_at` and leaving
    the row in place — it is the applier's only DELETE. If `_evidence_memories`
    does not filter on `superseded_at IS NULL`, a lesson the consolidator
    withdrew as redundant/contradicted keeps being quoted at the model that
    drafts the persona doc, so retirement makes the belief MORE durable.

    Retirement here goes through the real `apply_consolidation` (which issues
    `_SQL_RETIRE`), not a hand-written UPDATE, so the test breaks if A4 ever
    changes how a row is marked retired.

    Two live rows, one retired — a bundle that is empty because nothing was
    seeded would prove nothing, so the surviving row is asserted BY CONTENT.
    """
    from aegis.services.memory import apply_consolidation

    live = "zzret-live: owner squashes before merging"
    retired = "zzret-retired: owner always rebases onto main"
    ids = {}
    for content in (live, retired):
        ids[content] = await clean_db.fetchval(
            "INSERT INTO agent_memory (agent_id, content, importance, source) "
            "VALUES ($1, $2, 0.8, 'correction') RETURNING id",
            AGENT,
            content,
        )

    outcome = await apply_consolidation(
        clean_db,
        AGENT,
        [{"op": "DELETE", "id": ids[retired], "apply": True}],
        run_id=f"zza2-{uuid4()}",
        dry_run=False,
    )
    assert outcome["applied"] == 1, "nothing was retired — the test proves nothing"
    assert (
        await clean_db.fetchval(
            "SELECT superseded_at IS NOT NULL FROM agent_memory WHERE id = $1", ids[retired]
        )
        is True
    ), "the row was hard-deleted, not soft-retired"

    out = await ActivityEnvironment().run(_acts(clean_db).gather_profile_evidence, AGENT, 7)

    assert any(live in m for m in out["memories"]), "the LIVE row vanished too"
    assert not any(retired in m for m in out["memories"]), out["memories"]
    assert out["counts"]["memories"] == 1

    # ...and it stays out of everything derived from the bundle. The prompt is
    # where a leaked belief actually does its damage.
    llm = FakeLLM(response=_llm_json())
    await ActivityEnvironment().run(
        _acts(clean_db, llm_client=llm).propose_profile_patch, AGENT, out, CURRENT_DOC
    )
    prompt = llm.calls[0]["prompt"]
    assert live in prompt, "the live evidence never reached the prompt — wrong code path"
    assert retired not in prompt


@pytest.mark.asyncio
async def test_gather_evidence_respects_the_lookback_window(clean_db):
    await clean_db.execute(
        "INSERT INTO agent_memory (agent_id, content, importance, source, created_at) "
        "VALUES ($1, 'ancient history', 0.9, 'correction', now() - interval '30 days')",
        AGENT,
    )
    out = await ActivityEnvironment().run(_acts(clean_db).gather_profile_evidence, AGENT, 7)
    assert out["counts"]["memories"] == 0


# ------------------------------------------------------------------- proposal


def _llm_json(doc: str = PROPOSED_DOC, rationale: str = "adds the homelab fact") -> str:
    return json.dumps(
        {"proposed_doc": doc, "rationale": rationale, "changed_lines": ["+ Runs a homelab"]}
    )


_EVIDENCE = {"memories": ["Owner runs a homelab"], "lookback_days": 7}


async def _llm_rows(pool, agent: str = AGENT) -> list:
    return await pool.fetch(
        "SELECT model, purpose, status, error, input_tokens, output_tokens "
        "FROM llm_calls WHERE agent_id = $1 ORDER BY created_at",
        agent,
    )


@pytest.mark.asyncio
async def test_propose_returns_the_doc_and_records_the_successful_call(clean_db):
    """A success row exists here or nowhere — which is exactly how issue #106's
    call sites came to look like zero traffic.

    Real `LLMClient` with a stubbed HTTP layer: since #106 the row is written by
    `LLMClient._record_call`, so a `FakeLLM` with its own `think()` would bypass
    the code this test exists to protect.
    """
    llm = StubbedLLMClient(db_pool=clean_db, content=_llm_json())
    out = await ActivityEnvironment().run(
        _acts(clean_db, llm_client=llm).propose_profile_patch, AGENT, _EVIDENCE, CURRENT_DOC
    )

    assert out["proposed_doc"] == PROPOSED_DOC
    assert out["rationale"] == "adds the homelab fact"
    assert out["changed_lines"] == ["+ Runs a homelab"]
    assert out["unchanged"] is False
    assert out["revision_of"]

    rows = await _llm_rows(clean_db)
    # Exactly one — two means the activity records on top of the choke point.
    assert len(rows) == 1, f"expected one {PURPOSE} row, got {len(rows)}"
    assert rows[0]["purpose"] == PURPOSE
    assert rows[0]["status"] == "success"
    assert rows[0]["input_tokens"] == 11
    assert rows[0]["output_tokens"] == 22


@pytest.mark.asyncio
async def test_propose_records_a_truncated_call(clean_db):
    """`LLMTruncationError` is raised after a real, billed upstream call, so the
    row comes off the choke point's truncation branch — a truncating model must
    not read as zero traffic. Two rows since #321: the empty truncation is
    re-rolled once at a bigger budget and both attempts are billed."""
    llm = StubbedLLMClient(db_pool=clean_db, content="", finish_reason="length")
    out = await ActivityEnvironment().run(
        _acts(clean_db, llm_client=llm).propose_profile_patch, AGENT, _EVIDENCE, CURRENT_DOC
    )

    assert out == {}
    rows = await _llm_rows(clean_db)
    assert len(rows) == 2, f"expected one {PURPOSE} row per billed call, got {len(rows)}"
    assert {r["purpose"] for r in rows} == {PURPOSE}
    assert {r["status"] for r in rows} == {"error"}
    assert all("truncated" in (r["error"] or "") for r in rows)


@pytest.mark.asyncio
async def test_propose_degrades_to_empty_on_an_llm_error(clean_db):
    llm = FakeLLM(raises=RuntimeError("connection reset"))
    out = await ActivityEnvironment().run(
        _acts(clean_db, llm_client=llm).propose_profile_patch, AGENT, _EVIDENCE, CURRENT_DOC
    )
    assert out == {}
    # think() owns the failure row on this path (db_pool + purpose were passed);
    # the fake never gets that far, so what matters is that we wrote no spurious
    # success row on top.
    assert [r["status"] for r in await _llm_rows(clean_db)] == []


@pytest.mark.asyncio
async def test_propose_degrades_to_empty_on_unparseable_json(clean_db):
    llm = FakeLLM(response="I'm afraid I can't do that, Dave.")
    out = await ActivityEnvironment().run(
        _acts(clean_db, llm_client=llm).propose_profile_patch, AGENT, _EVIDENCE, CURRENT_DOC
    )
    assert out == {}


@pytest.mark.asyncio
async def test_propose_degrades_to_empty_on_a_blank_document(clean_db):
    llm = FakeLLM(response=json.dumps({"proposed_doc": "   ", "rationale": "nothing"}))
    out = await ActivityEnvironment().run(
        _acts(clean_db, llm_client=llm).propose_profile_patch, AGENT, _EVIDENCE, CURRENT_DOC
    )
    assert out == {}


@pytest.mark.asyncio
async def test_propose_flags_an_unchanged_document(clean_db):
    llm = FakeLLM(response=_llm_json(doc=CURRENT_DOC, rationale="nothing to add"))
    out = await ActivityEnvironment().run(
        _acts(clean_db, llm_client=llm).propose_profile_patch, AGENT, _EVIDENCE, CURRENT_DOC
    )
    assert out["unchanged"] is True


@pytest.mark.asyncio
async def test_propose_without_an_llm_client_is_a_no_op(clean_db, caplog):
    """A worker built without an LLM must say so, not look like a flaky model.

    Without the explicit `llm_client is None` guard this path still returns {} —
    the AttributeError falls into the generic handler — but it would be logged
    as `profile_propose_failed`, i.e. a misconfigured deployment indistinguishable
    from a bad week at the proxy. The distinction IS the behaviour under test.
    (`activity.logger` is a stdlib logger under `temporalio.activity`, so caplog
    sees it; structlog would not have been captured here.)
    """
    with caplog.at_level("WARNING", logger="temporalio.activity"):
        out = await ActivityEnvironment().run(
            _acts(clean_db).propose_profile_patch, AGENT, _EVIDENCE, CURRENT_DOC
        )
    assert out == {}
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "profile_propose_no_llm" in messages
    assert "profile_propose_failed" not in messages
    assert await _llm_rows(clean_db) == []


@pytest.mark.asyncio
async def test_propose_with_no_evidence_never_calls_the_llm(clean_db):
    llm = FakeLLM(response=_llm_json())
    out = await ActivityEnvironment().run(
        _acts(clean_db, llm_client=llm).propose_profile_patch, AGENT, {"counts": {}}, CURRENT_DOC
    )
    assert out == {}
    assert llm.calls == []


# -------------------------------------------------------------------- applier


async def _seed_card(pool, agent: str = AGENT) -> str:
    row = await pool.fetchrow(
        "INSERT INTO interactions (flow_run_id, agent_id, kind, origin, prompt, status) "
        "VALUES ($1, $2, 'draft_review', 'profile_reflection', 'draft', 'resolved') RETURNING id",
        f"zza2-{uuid4()}",
        agent,
    )
    return str(row["id"])


@pytest.mark.asyncio
async def test_apply_writes_the_edited_doc_not_the_proposed_one(clean_db):
    """The human's edits are the point of the panel — a version that ignored
    `edited_doc` and wrote `proposed_doc` would still 'work'."""
    await _set_doc(clean_db, AGENT, CURRENT_DOC)
    iid = await _seed_card(clean_db)

    out = await ActivityEnvironment().run(
        _acts(clean_db).apply_profile_reflection,
        iid,
        {"action": "approve", "edited_doc": EDITED_DOC},
        {"agent_id": AGENT, "kind": "user", "proposed_doc": PROPOSED_DOC},
    )

    assert out["applied"] is True
    assert out["doc_source"] == "edited_doc"
    assert await _doc(clean_db, AGENT) == EDITED_DOC

    revs = await _revisions(clean_db, AGENT)
    assert len(revs) == 1
    assert revs[0]["source"] == "profile_reflection"
    assert str(revs[0]["interaction_id"]) == iid
    assert revs[0]["before_content"] == CURRENT_DOC
    assert revs[0]["after_content"] == EDITED_DOC


@pytest.mark.asyncio
async def test_apply_falls_back_to_the_proposed_doc(clean_db):
    await _set_doc(clean_db, AGENT, CURRENT_DOC)
    iid = await _seed_card(clean_db)

    out = await ActivityEnvironment().run(
        _acts(clean_db).apply_profile_reflection,
        iid,
        {"action": "approve"},
        {"agent_id": AGENT, "kind": "user", "proposed_doc": PROPOSED_DOC},
    )

    assert out["applied"] is True
    assert out["doc_source"] == "proposed_doc"
    assert await _doc(clean_db, AGENT) == PROPOSED_DOC


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        {"action": "reject", "reason": "that fact is wrong"},
        {"action": "", "edited_doc": EDITED_DOC},
        {"edited_doc": EDITED_DOC},
        {"value": "approved", "edited_doc": EDITED_DOC},
        {},
        None,
    ],
    ids=["reject", "blank-action", "no-action", "old-value-shape", "empty", "none"],
)
async def test_apply_is_a_no_op_for_every_non_approve_response(clean_db, response):
    await _set_doc(clean_db, AGENT, CURRENT_DOC)
    iid = await _seed_card(clean_db)

    out = await ActivityEnvironment().run(
        _acts(clean_db).apply_profile_reflection,
        iid,
        response,
        {"agent_id": AGENT, "kind": "user", "proposed_doc": PROPOSED_DOC},
    )

    assert out["applied"] is False
    assert await _doc(clean_db, AGENT) == CURRENT_DOC
    assert await _revisions(clean_db, AGENT) == []


@pytest.mark.asyncio
async def test_apply_refuses_a_doc_that_trips_a1s_shrink_guard(clean_db):
    """A1's >50%-loss guard stays in force: `allow_shrink` is never passed, so
    an approved stub is refused rather than applied."""
    await _set_doc(clean_db, AGENT, CURRENT_DOC)
    iid = await _seed_card(clean_db)

    out = await ActivityEnvironment().run(
        _acts(clean_db).apply_profile_reflection,
        iid,
        {"action": "approve", "edited_doc": "ok"},
        {"agent_id": AGENT, "kind": "user", "proposed_doc": PROPOSED_DOC},
    )

    assert out["applied"] is False
    assert out["status"] == "refused"
    assert "shrink" in out["reason"]
    assert await _doc(clean_db, AGENT) == CURRENT_DOC
    assert await _revisions(clean_db, AGENT) == []


@pytest.mark.asyncio
async def test_apply_without_an_agent_in_metadata_writes_nothing(clean_db):
    await _set_doc(clean_db, AGENT, CURRENT_DOC)
    iid = await _seed_card(clean_db)
    out = await ActivityEnvironment().run(
        _acts(clean_db).apply_profile_reflection,
        iid,
        {"action": "approve", "edited_doc": EDITED_DOC},
        {"kind": "user", "proposed_doc": PROPOSED_DOC},
    )
    assert out == {"applied": False, "status": "no_agent"}
    assert await _doc(clean_db, AGENT) == CURRENT_DOC


@pytest.mark.asyncio
async def test_apply_with_an_empty_doc_everywhere_writes_nothing(clean_db):
    await _set_doc(clean_db, AGENT, CURRENT_DOC)
    iid = await _seed_card(clean_db)
    out = await ActivityEnvironment().run(
        _acts(clean_db).apply_profile_reflection,
        iid,
        {"action": "approve", "edited_doc": "   "},
        {"agent_id": AGENT, "kind": "user", "proposed_doc": ""},
    )
    assert out == {"applied": False, "status": "empty_doc"}
    assert await _revisions(clean_db, AGENT) == []


@pytest.mark.asyncio
async def test_apply_never_touches_the_soul_doc_by_default(clean_db):
    """`kind` defaults to `user`; the identity docs are not an automated
    writer's business."""
    await _set_doc(clean_db, AGENT, "I am Sebas.", kind="soul")
    iid = await _seed_card(clean_db)
    await ActivityEnvironment().run(
        _acts(clean_db).apply_profile_reflection,
        iid,
        {"action": "approve", "edited_doc": EDITED_DOC},
        {"agent_id": AGENT, "proposed_doc": PROPOSED_DOC},
    )
    assert await _doc(clean_db, AGENT, kind="soul") == "I am Sebas."
    assert await _doc(clean_db, AGENT) == EDITED_DOC


# --------------------------------------------------------------- budget gate


async def _charge(pool, log_event: str = PURPOSE, sent: bool = True, agent: str = AGENT):
    await pool.execute(
        "INSERT INTO notification_log (agent_id, log_event, sent) VALUES ($1,$2,$3)",
        agent,
        log_event,
        sent,
    )


async def _pending_card(pool, agent: str = AGENT, origin: str = PURPOSE):
    await pool.execute(
        "INSERT INTO interactions (flow_run_id, agent_id, kind, origin, prompt, status) "
        "VALUES ($1, $2, 'draft_review', $3, 'draft', 'pending')",
        f"zza2-{uuid4()}",
        agent,
        origin,
    )


@pytest.mark.asyncio
async def test_budget_gate_allows_a_clean_week(clean_db):
    gate = await ActivityEnvironment().run(_acts(clean_db).check_profile_budget, AGENT, 1)
    assert gate == {
        "allow": True,
        "reason": "ok",
        "sent_today": 0,
        "pending": 0,
        "global_today": 0,
    }


@pytest.mark.asyncio
async def test_budget_gate_blocks_a_second_card_the_same_day(clean_db):
    """Proven with NO pending card and the shared budget disabled, so the
    per-flow cap is unambiguously the guard that fired."""
    await _charge(clean_db)
    gate = await ActivityEnvironment().run(_acts(clean_db).check_profile_budget, AGENT, 1)
    assert gate["allow"] is False
    assert gate["reason"] == "budget"
    assert gate["sent_today"] == 1
    assert gate["pending"] == 0


@pytest.mark.asyncio
async def test_budget_gate_ignores_yesterdays_card(clean_db):
    await clean_db.execute(
        "INSERT INTO notification_log (agent_id, log_event, sent, created_at) "
        "VALUES ($1, $2, TRUE, now() - interval '1 day')",
        AGENT,
        PURPOSE,
    )
    gate = await ActivityEnvironment().run(_acts(clean_db).check_profile_budget, AGENT, 1)
    assert gate["reason"] == "ok"


@pytest.mark.asyncio
async def test_budget_gate_honours_the_shared_notification_budget(clean_db):
    """The SHARED budget, proven independently: the day's traffic is a `drift`
    push, so this flow's own per-day cap is at zero and cannot be the guard
    that fired."""
    await _charge(clean_db, log_event="drift")
    gate = await ActivityEnvironment().run(
        _acts(clean_db, budget_enabled=True, daily_budget=1).check_profile_budget, AGENT, 1
    )
    assert gate["allow"] is False
    assert gate["reason"] == "global_budget"
    assert gate["sent_today"] == 0
    assert gate["pending"] == 0


@pytest.mark.asyncio
async def test_budget_gate_blocks_while_a_draft_is_still_open(clean_db):
    """Proven with an empty notification_log and the shared budget disabled, so
    neither budget check can be masking this one."""
    await _pending_card(clean_db)
    gate = await ActivityEnvironment().run(_acts(clean_db).check_profile_budget, AGENT, 1)
    assert gate["allow"] is False
    assert gate["reason"] == "pending"
    assert gate["sent_today"] == 0
    assert gate["pending"] == 1


@pytest.mark.asyncio
async def test_budget_gate_ignores_another_agents_open_draft(clean_db):
    await _pending_card(clean_db, agent=OTHER)
    gate = await ActivityEnvironment().run(_acts(clean_db).check_profile_budget, AGENT, 1)
    assert gate["reason"] == "ok"


@pytest.mark.asyncio
async def test_budget_gate_ignores_another_flows_open_card(clean_db):
    await _pending_card(clean_db, origin="curiosity")
    gate = await ActivityEnvironment().run(_acts(clean_db).check_profile_budget, AGENT, 1)
    assert gate["reason"] == "ok"


@pytest.mark.asyncio
async def test_record_profile_card_charges_the_notification_budget(clean_db):
    out = await ActivityEnvironment().run(_acts(clean_db).record_profile_card, AGENT, True)
    assert out == {"recorded": True, "sent": True}
    rows = await clean_db.fetch(
        "SELECT agent_id, sent FROM notification_log WHERE log_event = $1 AND agent_id = $2",
        PURPOSE,
        AGENT,
    )
    assert len(rows) == 1
    assert rows[0]["sent"] is True
