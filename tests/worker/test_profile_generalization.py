"""A5 — `ProfileActivities.propose_generalizations`, against a real Postgres.

The activity turns "the same lesson keeps showing up in agent_memory" into a
claim the weekly `draft_review` card carries. It writes nothing: the pool is a
SELECT, the ledger is the `interactions` row A2's flow already creates, and the
only thing that promotes anything is a human pressing Approve.

Three gates decide whether a claim survives, and they are proven ONE AT A TIME
below — a claim that fails two of them would pass a test that only ever exercises
the pair together:

  ownership   an id the agent does not own is discarded (`_foreign_ids`)
  support     fewer than `min_support` distinct owned ids and the claim dies
              (`_two_memories`)
  confidence  below `min_confidence` it is counted and logged, never carded
              (`_low_confidence`)

The FakeLLM here reads the memory ids OUT OF THE PROMPT it is given rather than
being handed them by the test. That is what makes the pool query falsifiable:
if the already-promoted rows stopped being excluded from the pool, the model
would see them, name them, and a candidate would come back.
"""

from __future__ import annotations

import json
import re

import pytest
import pytest_asyncio
from aegis_worker.activities.profile import ProfileActivities
from temporalio.testing import ActivityEnvironment

from tests.llm_stub import StubbedLLMClient

AGENT = "zza5-gen"
OTHER_AGENT = "zza5-gen-other"
PURPOSE = "profile_generalization"

# Three rows saying the same thing (the promotable pattern) and two saying a
# different same thing (the coincidence that must NOT promote).
TRIPLE = [
    "Owner refuses meetings before 11am",
    "Owner moved the standup because it was before 11am",
    "Owner asked never to be booked in the morning",
]
PAIR = [
    "Owner prefers dark roast coffee",
    "Owner bought dark roast again",
]

CLAIM = "The owner does not take meetings in the morning."


# --------------------------------------------------------------------- fixtures


async def _wipe(conn):
    for agent in (AGENT, OTHER_AGENT):
        await conn.execute("DELETE FROM agent_profile_revisions WHERE agent_id = $1", agent)
        await conn.execute("DELETE FROM agent_personalities WHERE agent_id = $1", agent)
        await conn.execute("DELETE FROM interactions WHERE agent_id = $1", agent)
        await conn.execute("DELETE FROM agent_memory WHERE agent_id = $1", agent)
        await conn.execute("DELETE FROM llm_calls WHERE agent_id = $1", agent)


@pytest_asyncio.fixture(loop_scope="function")
async def clean_db(db_pool):
    async with db_pool.acquire() as conn:
        for agent in (AGENT, OTHER_AGENT):
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
        for agent in (AGENT, OTHER_AGENT):
            await conn.execute("DELETE FROM agents WHERE id = $1", agent)


async def _seed(pool, contents: list[str], agent: str = AGENT) -> list[int]:
    ids = []
    for content in contents:
        ids.append(
            await pool.fetchval(
                "INSERT INTO agent_memory (agent_id, content, importance, source) "
                "VALUES ($1, $2, 0.7, 'correction') RETURNING id",
                agent,
                content,
            )
        )
    return ids


class PromptReadingLLM:
    """Answers with claims built from the ids it can SEE in the prompt.

    `pick` chooses which of the prompt's ids go into the claim, so a test can
    say "the model named every row it was shown" without the test knowing which
    rows those were — the point of the pool assertions below.
    """

    def __init__(self, *, confidence: float = 0.9, pick=None, extra_ids=(), raises=None):
        self.confidence = confidence
        self.pick = pick or (lambda ids: ids)
        self.extra_ids = list(extra_ids)
        self.raises = raises
        self.prompts: list[str] = []

    async def think(self, **kwargs):
        self.prompts.append(kwargs.get("prompt") or "")
        if self.raises is not None:
            raise self.raises
        seen = [int(m) for m in re.findall(r'"id":\s*(\d+)', kwargs.get("prompt") or "")]
        ids = list(self.pick(seen)) + self.extra_ids
        body = (
            [{"claim": CLAIM, "supporting_memory_ids": ids, "confidence": self.confidence}]
            if ids
            else []
        )
        return {
            "response": json.dumps(body),
            "model": "fake-balanced",
            "prompt_tokens": 11,
            "completion_tokens": 13,
        }


class RawLLM:
    """Returns a fixed string — for the malformed/unparseable paths."""

    def __init__(self, response: str):
        self.response = response

    async def think(self, **kwargs):
        return {
            "response": self.response,
            "model": "fake-balanced",
            "prompt_tokens": 1,
            "completion_tokens": 1,
        }


def _acts(pool, llm=None, **kw) -> ProfileActivities:
    return ProfileActivities(db_pool=pool, llm_client=llm, **kw)


async def _run(acts: ProfileActivities, agent: str = AGENT) -> dict:
    return await ActivityEnvironment().run(acts.propose_generalizations, agent)


async def _approved_card(pool, memory_ids: list[int], *, action: str, revision: bool) -> str:
    """An interactions row shaped exactly like the one A2's flow writes, plus
    (optionally) the agent_profile_revisions row a real apply would have left."""
    interaction_id = await pool.fetchval(
        "INSERT INTO interactions (flow_run_id, agent_id, kind, origin, prompt, status, "
        "response, metadata, resolved_at) "
        "VALUES ('wf-zza5', $1, 'draft_review', 'profile_reflection', 'draft', 'resolved', "
        "$2, $3, now()) RETURNING id",
        AGENT,
        {"action": action, "edited_doc": "x" * 400},
        {"agent_id": AGENT, "kind": "user", "promoted_memory_ids": memory_ids},
    )
    if revision:
        await pool.execute(
            "INSERT INTO agent_profile_revisions "
            "(agent_id, kind, before_content, after_content, source, interaction_id) "
            "VALUES ($1, 'user', 'before', 'after', 'profile_reflection', $2)",
            AGENT,
            interaction_id,
        )
    return str(interaction_id)


# --------------------------------------------------------- the three gates


@pytest.mark.asyncio
async def test_three_memories_make_one_candidate_carrying_all_three(clean_db):
    """Acceptance: three rows saying the same thing → exactly one candidate,
    holding exactly those three ids."""
    triple = await _seed(clean_db, TRIPLE)
    llm = PromptReadingLLM()

    result = await _run(_acts(clean_db, llm))

    assert result["status"] == "ok"
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["supporting_memory_ids"] == sorted(triple)
    assert result["candidates"][0]["claim"] == CLAIM
    assert result["candidates"][0]["confidence"] == 0.9
    assert result["pool"] == 3
    assert result["excluded"] == 0


@pytest.mark.asyncio
async def test_two_memories_make_no_candidate(clean_db):
    """The support gate ALONE. Confidence is high and every id is owned — the
    only thing wrong with this claim is that two rows is not a pattern."""
    await _seed(clean_db, PAIR)
    # min_support defaults to 3; the pool must still be big enough to reach the
    # model, or this would prove the "too_few_memories" short-circuit instead.
    await _seed(clean_db, ["Owner likes hiking"])
    llm = PromptReadingLLM(confidence=0.99, pick=lambda ids: ids[:2])

    result = await _run(_acts(clean_db, llm))

    assert result["status"] == "ok"
    assert result["candidates"] == []
    assert result["dropped"] == 1
    assert result["below_threshold"] == 0, "this claim was rejected for support, not confidence"
    assert llm.prompts, "the model was never called — wrong code path"


@pytest.mark.asyncio
async def test_a_low_confidence_claim_is_counted_not_carded(clean_db):
    """The confidence gate ALONE: three owned ids, well-formed, just unsure."""
    triple = await _seed(clean_db, TRIPLE)
    llm = PromptReadingLLM(confidence=0.2)

    result = await _run(_acts(clean_db, llm))

    assert result["candidates"] == []
    assert result["below_threshold"] == 1
    assert result["dropped"] == 0, "this claim was rejected for confidence, not support"
    # And the same claim at the same support DOES pass when confidence clears
    # the bar — otherwise this test would pass with the whole activity stubbed
    # to return nothing.
    ok = await _run(_acts(clean_db, PromptReadingLLM(confidence=0.75)))
    assert [c["supporting_memory_ids"] for c in ok["candidates"]] == [sorted(triple)]


@pytest.mark.asyncio
async def test_ids_the_agent_does_not_own_are_discarded(clean_db):
    """The ownership gate ALONE: three real ids survive, and a foreign row's id
    plus an invented one are stripped out of the same claim."""
    triple = await _seed(clean_db, TRIPLE)
    foreign = await _seed(clean_db, ["Someone else's memory"], agent=OTHER_AGENT)
    llm = PromptReadingLLM(extra_ids=[*foreign, 99999999])

    result = await _run(_acts(clean_db, llm))

    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["supporting_memory_ids"] == sorted(triple)
    assert result["pool"] == 3, "another agent's rows leaked into the pool"


@pytest.mark.asyncio
async def test_min_support_cannot_be_configured_below_two(clean_db):
    """`min_support` is a plain dataclass field, so a deployment could set it to
    1 and turn "a repeated lesson" into "anything an LLM said once". The floor
    makes that unreachable."""
    await _seed(clean_db, TRIPLE)
    llm = PromptReadingLLM(pick=lambda ids: ids[:1])

    result = await _run(_acts(clean_db, llm, min_support=1))

    assert result["candidates"] == []
    assert result["dropped"] == 1


@pytest.mark.asyncio
async def test_the_candidate_list_is_capped(clean_db):
    """A model that decides everything is a pattern must not produce a card
    listing twenty inferences about the owner."""
    await _seed(clean_db, [f"Owner does thing {n}" for n in range(12)])

    class ManyClaims(PromptReadingLLM):
        async def think(self, **kwargs):
            seen = [int(m) for m in re.findall(r'"id":\s*(\d+)', kwargs.get("prompt") or "")]
            return {
                "response": json.dumps(
                    [
                        {
                            "claim": f"claim {n}",
                            "supporting_memory_ids": seen,
                            "confidence": 0.8,
                        }
                        for n in range(9)
                    ]
                ),
                "model": "fake-balanced",
                "prompt_tokens": 1,
                "completion_tokens": 1,
            }

    result = await _run(_acts(clean_db, ManyClaims(), max_generalizations=3))
    assert len(result["candidates"]) == 3


# ------------------------------------------------------------- idempotence


@pytest.mark.asyncio
async def test_promoted_memories_leave_the_pool_and_stop_reproposing(clean_db):
    """Acceptance: after an approved promotion, a second run over the same data
    produces zero candidates.

    Two assertions, because the weaker one alone would also pass if the model
    simply never named the rows: the promoted CONTENT is not in the prompt (the
    pool query excluded it) AND no candidate comes back.
    """
    triple = await _seed(clean_db, TRIPLE)
    await _approved_card(clean_db, triple, action="approve", revision=True)
    llm = PromptReadingLLM()

    result = await _run(_acts(clean_db, llm))

    assert result["status"] == "skipped"
    assert result["reason"] == "too_few_memories"
    assert result["candidates"] == []
    assert result["excluded"] == 3
    assert result["pool"] == 0
    assert llm.prompts == [], "the promoted rows were still sent to the model"


@pytest.mark.asyncio
async def test_promotion_excludes_only_the_promoted_rows(clean_db):
    """The exclusion is per-row, not per-agent: unpromoted memories still reach
    the model. Without this, "zero candidates" above could just mean the pool
    query is broken in the other direction."""
    triple = await _seed(clean_db, TRIPLE)
    fresh = await _seed(clean_db, ["Owner ships on Fridays", "Owner shipped Friday again", "Friday release again"])
    await _approved_card(clean_db, triple, action="approve", revision=True)

    llm = PromptReadingLLM()
    result = await _run(_acts(clean_db, llm))

    assert result["excluded"] == 3
    assert result["pool"] == 3
    assert result["candidates"][0]["supporting_memory_ids"] == sorted(fresh)
    for content in TRIPLE:
        assert content not in llm.prompts[0]


@pytest.mark.asyncio
async def test_a_memory_a4_retired_never_supports_a_promoted_claim(clean_db):
    """A4 composition, and the sharpest edge of it.

    A retired row is one the consolidator judged redundant, contradicted or
    wrong. If the pool query does not filter `superseded_at IS NULL`, that row
    can be one of the >=3 supporting ids behind a claim the human then approves
    into the `user` persona doc — the most durable store in the system, injected
    into every prompt. Retirement would make the belief permanent instead of
    removing it.

    Retired through the real `apply_consolidation` (which issues `_SQL_RETIRE`),
    so this breaks if A4 ever changes its retirement marker.

    Deliberately seeded so the retired row is the NEWEST at equal importance:
    unfiltered it would sort FIRST in the pool, not fall off the end — the
    result cannot be right by an ordering accident.
    """
    from aegis.services.memory import apply_consolidation

    triple = await _seed(clean_db, TRIPLE)
    retired_text = "zzret-retired: owner is happy to meet at 8am"
    (retired,) = await _seed(clean_db, [retired_text])
    outcome = await apply_consolidation(
        clean_db,
        AGENT,
        [{"op": "DELETE", "id": retired, "apply": True}],
        run_id="zza5-retire",
        dry_run=False,
    )
    assert outcome["applied"] == 1, "nothing was retired — the test proves nothing"
    assert (
        await clean_db.fetchval(
            "SELECT superseded_at IS NOT NULL FROM agent_memory WHERE id = $1", retired
        )
        is True
    ), "the row was hard-deleted, not soft-retired"

    llm = PromptReadingLLM()
    result = await _run(_acts(clean_db, llm))

    # Content first, deliberately: a count alone could be right by accident.
    # The model was called, and saw the three live rows and only those.
    assert llm.prompts, "the model was never called — wrong code path"
    assert retired_text not in llm.prompts[0]
    for content in TRIPLE:
        assert content in llm.prompts[0], "a live row was dropped too"
    # ids too, because ids are what a claim is allowed to cite.
    assert sorted(int(m) for m in re.findall(r'"id":\s*(\d+)', llm.prompts[0])) == sorted(triple)

    # Nothing derived from the pool cites it either.
    assert result["candidates"][0]["supporting_memory_ids"] == sorted(triple)
    assert retired not in result["candidates"][0]["supporting_memory_ids"]

    assert result["pool"] == len(triple), "the retired row was still counted into the pool"
    assert result["excluded"] == 0, "retirement is not the promotion ledger"


@pytest.mark.asyncio
async def test_a_rejected_card_leaves_the_memories_in_the_pool_and_untouched(clean_db):
    """Acceptance: rejection leaves all supporting memories untouched and
    un-retired — so the same rows are still candidates."""
    triple = await _seed(clean_db, TRIPLE)
    before = await clean_db.fetch(
        "SELECT id, content, source, importance FROM agent_memory WHERE agent_id = $1 ORDER BY id",
        AGENT,
    )
    await _approved_card(clean_db, triple, action="reject", revision=False)

    result = await _run(_acts(clean_db, PromptReadingLLM()))

    assert result["excluded"] == 0
    assert result["candidates"][0]["supporting_memory_ids"] == sorted(triple)
    after = await clean_db.fetch(
        "SELECT id, content, source, importance FROM agent_memory WHERE agent_id = $1 ORDER BY id",
        AGENT,
    )
    assert [dict(r) for r in after] == [dict(r) for r in before]


@pytest.mark.asyncio
async def test_a_rejection_is_not_a_promotion_even_with_a_revision_row(clean_db):
    """The approve condition, proven ALONE.

    The plain-rejection test above cannot prove it: a rejected card also has no
    `agent_profile_revisions` row, so the two conditions in the exclusion query
    mask each other and deleting the `action = 'approve'` check leaves every
    assertion passing (verified — it did).

    So here the revision row exists and the answer was still Reject. A revision
    can point at an interaction without that interaction having approved a
    promotion — `revert_profile_revision`, a chat tool, or any later writer that
    cites the same decision. Only the human's `approve` promotes.
    """
    triple = await _seed(clean_db, TRIPLE)
    await _approved_card(clean_db, triple, action="reject", revision=True)

    result = await _run(_acts(clean_db, PromptReadingLLM()))

    assert result["excluded"] == 0
    assert result["candidates"][0]["supporting_memory_ids"] == sorted(triple)


@pytest.mark.asyncio
async def test_an_approval_that_never_wrote_does_not_count_as_promotion(clean_db):
    """An approve whose apply was refused (A1's shrink guard) leaves no
    agent_profile_revisions row — so the memories were never promoted and must
    stay in the pool."""
    triple = await _seed(clean_db, TRIPLE)
    await _approved_card(clean_db, triple, action="approve", revision=False)

    result = await _run(_acts(clean_db, PromptReadingLLM()))

    assert result["excluded"] == 0
    assert result["candidates"][0]["supporting_memory_ids"] == sorted(triple)


# ------------------------------------------------------- quiet failure paths


@pytest.mark.asyncio
async def test_no_llm_client_is_a_quiet_skip(clean_db):
    await _seed(clean_db, TRIPLE)
    result = await _run(_acts(clean_db, None))
    assert result == {
        "candidates": [],
        "below_threshold": 0,
        "dropped": 0,
        "pool": 3,
        "excluded": 0,
        "status": "skipped",
        "reason": "no_llm_client",
    }


@pytest.mark.asyncio
async def test_an_llm_blow_up_is_a_quiet_skip(clean_db):
    await _seed(clean_db, TRIPLE)
    result = await _run(_acts(clean_db, PromptReadingLLM(raises=RuntimeError("proxy down"))))
    assert result["status"] == "skipped"
    assert result["reason"] == "llm_failed"
    assert result["candidates"] == []


@pytest.mark.asyncio
async def test_unparseable_output_can_never_be_read_as_a_promotion(clean_db):
    await _seed(clean_db, TRIPLE)
    result = await _run(_acts(clean_db, RawLLM("I think the owner likes mornings, actually")))
    assert result["candidates"] == []
    assert result["status"] == "unparseable"


@pytest.mark.asyncio
async def test_too_few_memories_never_calls_the_model(clean_db):
    llm = PromptReadingLLM()
    await _seed(clean_db, TRIPLE[:2])
    result = await _run(_acts(clean_db, llm))
    assert result["status"] == "skipped"
    assert result["reason"] == "too_few_memories"
    assert llm.prompts == []


# ------------------------------------------------------------- llm_calls rows


@pytest.mark.asyncio
def _prompt_reading_client(pool, **kw) -> StubbedLLMClient:
    """`PromptReadingLLM`'s behaviour behind a REAL `LLMClient`.

    The llm_calls row is written inside `LLMClient._record_call` (issue #106),
    so a stand-in with its own `think()` bypasses the code these two tests
    exist to protect. Only `chat.completions.create` is stubbed.
    """

    def _respond(create_kwargs: dict) -> str:
        prompt = "\n".join(m.get("content") or "" for m in create_kwargs.get("messages", []))
        ids = [int(m) for m in re.findall(r'"id":\s*(\d+)', prompt)]
        body = (
            [{"claim": CLAIM, "supporting_memory_ids": ids, "confidence": 0.9}] if ids else []
        )
        return json.dumps(body)

    return StubbedLLMClient(
        db_pool=pool, responder=_respond, prompt_tokens=11, completion_tokens=13, **kw
    )


@pytest.mark.asyncio
async def test_a_successful_pass_records_an_llm_calls_row(clean_db):
    """Without this row a working generalisation pass is invisible in spend."""
    await _seed(clean_db, TRIPLE)
    await _run(_acts(clean_db, _prompt_reading_client(clean_db)))
    rows = await clean_db.fetch(
        "SELECT purpose, status, output_tokens FROM llm_calls WHERE agent_id = $1", AGENT
    )
    # Exactly one — two would mean the activity records on top of the choke
    # point and this pass reports double its real spend.
    assert len(rows) == 1, f"expected one {PURPOSE} row, got {len(rows)}"
    assert rows[0]["purpose"] == PURPOSE
    assert rows[0]["status"] == "success"
    assert rows[0]["output_tokens"] == 13


@pytest.mark.asyncio
async def test_a_truncated_reply_records_an_error_row(clean_db):
    """`LLMTruncationError` is raised after a real, billed upstream call, so a
    truncating model would otherwise look like zero traffic. Two rows since
    #321: the empty truncation is re-rolled once at a bigger budget, and both
    attempts are billed."""
    await _seed(clean_db, TRIPLE)
    result = await _run(
        _acts(clean_db, StubbedLLMClient(db_pool=clean_db, content="", finish_reason="length"))
    )
    assert result["status"] == "skipped"
    assert result["reason"] == "truncated"
    rows = await clean_db.fetch(
        "SELECT purpose, status, error FROM llm_calls WHERE agent_id = $1", AGENT
    )
    assert len(rows) == 2, f"expected one {PURPOSE} row per billed call, got {len(rows)}"
    assert {(r["purpose"], r["status"]) for r in rows} == {(PURPOSE, "error")}
    assert all("truncated" in r["error"] for r in rows)


# ----------------------------------------------------- nothing here writes


@pytest.mark.asyncio
async def test_the_pass_writes_nothing_to_agent_memory(clean_db):
    """The whole safety story: proposing a promotion mutates no memory row and
    no persona doc. Only an approved card does."""
    await _seed(clean_db, TRIPLE)
    before = [dict(r) for r in await clean_db.fetch(
        "SELECT * FROM agent_memory WHERE agent_id = $1 ORDER BY id", AGENT
    )]

    result = await _run(_acts(clean_db, PromptReadingLLM()))

    assert result["candidates"], "the pass did nothing at all — assertion below is vacuous"
    after = [dict(r) for r in await clean_db.fetch(
        "SELECT * FROM agent_memory WHERE agent_id = $1 ORDER BY id", AGENT
    )]
    assert after == before
    assert await clean_db.fetchval(
        "SELECT count(*) FROM agent_profile_revisions WHERE agent_id = $1", AGENT
    ) == 0
    assert await clean_db.fetchval(
        "SELECT count(*) FROM agent_personalities WHERE agent_id = $1", AGENT
    ) == 0


# ------------------------------------------------------ what the apply reports


@pytest.mark.asyncio
async def test_the_applier_reports_which_memories_the_approval_promoted(clean_db):
    """`apply_profile_reflection` reads `promoted_memory_ids` back off the card
    so the run result and the log say what graduated. It is the only place that
    number is observable — the memory rows themselves are never marked."""
    triple = await _seed(clean_db, TRIPLE)
    await clean_db.execute(
        "INSERT INTO agent_personalities (agent_id, kind, content) VALUES ($1,'user',$2) "
        "ON CONFLICT (agent_id, kind) DO UPDATE SET content = EXCLUDED.content",
        AGENT,
        "d" * 400,
    )
    from aegis.services.personalities import invalidate

    invalidate(AGENT)
    acts = _acts(clean_db)
    interaction_id = await _approved_card(clean_db, triple, action="approve", revision=False)

    result = await ActivityEnvironment().run(
        acts.apply_profile_reflection,
        interaction_id,
        {"action": "approve", "edited_doc": "e" * 500},
        {"agent_id": AGENT, "kind": "user", "promoted_memory_ids": triple},
    )

    assert result["applied"] is True
    assert result["promoted_memory_ids"] == sorted(triple)
    # A card with no generalisations reports none — not the previous card's.
    plain = await ActivityEnvironment().run(
        acts.apply_profile_reflection,
        interaction_id,
        {"action": "approve", "edited_doc": "f" * 500},
        {"agent_id": AGENT, "kind": "user"},
    )
    assert plain["promoted_memory_ids"] == []
    invalidate(AGENT)


# ------------------------------------------------- generalisations reach the prompt


@pytest.mark.asyncio
async def test_candidates_are_rendered_into_the_patch_prompt(clean_db):
    """The claims have to actually reach `propose_profile_patch`'s prompt — a
    generalisation the drafting model never sees promotes nothing."""
    triple = await _seed(clean_db, TRIPLE)
    gen = await _run(_acts(clean_db, PromptReadingLLM()))

    patch_llm = RawLLM(json.dumps({"proposed_doc": "doc", "rationale": "r", "changed_lines": []}))
    seen: list[str] = []

    async def _think(**kwargs):
        seen.append(kwargs.get("prompt") or "")
        return await RawLLM.think(patch_llm, **kwargs)

    patch_llm.think = _think
    acts = _acts(clean_db, patch_llm)
    await ActivityEnvironment().run(
        acts.propose_profile_patch,
        AGENT,
        {"memories": ["something"], "lookback_days": 7},
        "current doc",
        gen["candidates"],
    )

    assert CLAIM in seen[0]
    assert f"backed by {len(triple)} memories" in seen[0]
    # And the empty case says so rather than leaving a blank section.
    seen.clear()
    await ActivityEnvironment().run(
        acts.propose_profile_patch, AGENT, {"memories": ["x"], "lookback_days": 7}, "doc", []
    )
    assert "(none)" in seen[0]
