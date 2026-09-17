"""analyse_meeting — code-computed stats + observations + one LLM review."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from aegis.llm import LLMTruncationError
from aegis_worker.activities.meeting import MeetingActivities

from tests.llm_stub import RecordingFakeLLM

pytestmark = pytest.mark.asyncio

TRANSCRIPT = [
    ["Ada Lovelace", "Morning all, let's start with the rollout."],
    ["Sam Doe", "I have the config store half migrated, the rest goes this week."],
    ["Ada Lovelace", "Anything blocking?"],
    ["Sam Doe", "Only the parity script, it is slow on the big collection."],
]
DOC = {
    "title": "Widget Standup",
    "meeting_date": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
    "doc_id": "doc-analyse-1",
    "message_id": "gm-analyse-1",
    "account": "acct",
    "doc_status": "ok",
    "notes": "Rollout status\n* Grace reported 40%.\n* Sam is moving the config store." * 8,
    "transcript": TRANSCRIPT,
    "speakers": ["Ada Lovelace", "Sam Doe"],
}
REVIEW = {
    "contributions": ["Migrated half the config store"],
    "problems_raised": ["Parity script is slow"],
    "commitments": ["Finish the migration this week"],
    "verbosity_note": "Your second turn could drop the preamble.",
}


class _RulesPool:
    """Answers the settings read; every other query returns None.

    `record_external_observation` issues an `INSERT … RETURNING` through
    `fetchrow`, and a None return means "already ingested" — so the
    observation path runs without a database and simply writes nothing.
    """

    def __init__(self, names):
        self._names = names

    async def fetchval(self, sql, *args):
        if "settings" in sql:
            return {"self_names": self._names}
        return None

    async def fetchrow(self, sql, *args):
        return None


def _act(pool, llm):
    return MeetingActivities(
        gmail_credentials_file="c", gmail_token_dir="t", db_pool=pool, llm_client=llm,
        model_balanced="balanced-model", agent_id="sebas",
    )


async def test_empty_self_names_skips_without_touching_llm():
    llm = RecordingFakeLLM(json.dumps(REVIEW))
    out = await _act(_RulesPool([]), llm).analyse_meeting(DOC)
    assert out["skipped"] == "no_self_names"
    assert llm.calls == []


async def test_review_path_builds_prompt_from_own_lines_only():
    llm = RecordingFakeLLM(json.dumps(REVIEW))
    out = await _act(_RulesPool(["Sam"]), llm).analyse_meeting(DOC)
    assert "skipped" not in out
    assert out["self_matched"] is True
    assert out["stats"]["self"]["turns"] == 2
    assert out["review"] == REVIEW
    assert out["rendered"].startswith("# Meeting review: Widget Standup")
    assert "Parity script is slow" in out["rendered"]
    call = llm.calls[0]
    assert call["purpose"] == "meeting_review" and call["model"] == "balanced-model"
    assert call["agent_id"] == "sebas"  # the activity's own default, no caller override
    assert call["max_tokens"] >= 3000
    assert "parity script" in call["prompt"]
    assert "Anything blocking" not in call["prompt"]  # Ada's line never reaches the LLM
    assert "Rollout status" in call["prompt"]


async def test_llm_truncation_and_bad_json_are_skipped_not_raised():
    out = await _act(_RulesPool(["Sam"]), RecordingFakeLLM(raises=LLMTruncationError("cut"))).analyse_meeting(
        DOC
    )
    assert out["skipped"] == "llm_failed" and out["stats"]["self"]["matched"] is True
    out = await _act(_RulesPool(["Sam"]), RecordingFakeLLM("not json at all")).analyse_meeting(DOC)
    assert out["skipped"] == "llm_failed"


async def test_thin_body_without_own_lines_is_too_thin():
    thin = {**DOC, "notes": "short", "transcript": [], "doc_status": "no_link"}
    out = await _act(_RulesPool(["Sam"]), RecordingFakeLLM(json.dumps(REVIEW))).analyse_meeting(thin)
    assert out["skipped"] == "too_thin"


async def test_lists_are_capped_and_coerced():
    resp = {
        "contributions": list(range(9)),
        "problems_raised": None,
        "commitments": "x",
        "verbosity_note": 5,
    }
    out = await _act(_RulesPool(["Sam"]), RecordingFakeLLM(json.dumps(resp))).analyse_meeting(DOC)
    assert out["review"]["contributions"] == ["0", "1", "2", "3", "4"]
    assert out["review"]["problems_raised"] == [] and out["review"]["commitments"] == []
    assert out["review"]["verbosity_note"] == "5"


@pytest_asyncio.fixture(loop_scope="function")
async def obs_pool(db_pool):
    await db_pool.execute(
        "DELETE FROM life.observations WHERE source='meeting' AND external_id LIKE 'doc-analyse-%'"
    )
    await db_pool.execute("DELETE FROM settings WHERE key='meeting_rules'")
    await db_pool.execute(
        "INSERT INTO settings (key, value) VALUES ('meeting_rules', $1)", {"self_names": ["Sam Doe"]}
    )
    yield db_pool
    await db_pool.execute(
        "DELETE FROM life.observations WHERE source='meeting' AND external_id LIKE 'doc-analyse-%'"
    )
    await db_pool.execute("DELETE FROM settings WHERE key='meeting_rules'")


async def test_observations_written_once_even_when_run_twice(obs_pool):
    act = _act(obs_pool, RecordingFakeLLM(json.dumps(REVIEW)))
    first = await act.analyse_meeting(DOC)
    second = await act.analyse_meeting(DOC)
    assert first["observations"] == 3
    assert second["observations"] == 0  # None from record_external_observation = already there
    rows = await obs_pool.fetch(
        "SELECT metric, value::float AS value FROM life.observations "
        "WHERE source='meeting' AND external_id=$1 ORDER BY metric",
        DOC["doc_id"],
    )
    assert [r["metric"] for r in rows] == ["talk_share_pct", "turns", "words_per_turn"]
    by = {r["metric"]: r["value"] for r in rows}
    assert by["turns"] == 2.0
    assert by["talk_share_pct"] == first["stats"]["self"]["talk_share_pct"]


async def test_a_transcript_that_matches_no_self_name_skips_the_review():
    """I1: a configured name matching nobody must not produce a review of
    somebody else's meeting, filed and searchable under the user's name."""
    llm = RecordingFakeLLM(json.dumps(REVIEW))
    out = await _act(_RulesPool(["Nobody"]), llm).analyse_meeting(DOC)
    assert out["skipped"] == "self_not_matched"
    assert out["observations"] == 0
    assert llm.calls == []


async def test_a_doc_with_no_transcript_still_reviews_the_notes():
    """The transcript-less case is different: there is nothing to attribute
    from, so the spec allows a notes-only review."""
    llm = RecordingFakeLLM(json.dumps(REVIEW))
    doc = {**DOC, "transcript": [], "speakers": [], "doc_status": "no_link"}
    out = await _act(_RulesPool(["Sam"]), llm).analyse_meeting(doc)
    assert "skipped" not in out
    assert len(llm.calls) == 1


async def test_agent_id_argument_attributes_the_llm_spend_to_the_caller():
    llm = RecordingFakeLLM(json.dumps(REVIEW))
    out = await _act(_RulesPool(["Sam"]), llm).analyse_meeting(DOC, agent_id="maou")
    assert "skipped" not in out
    assert llm.calls[0]["agent_id"] == "maou"


async def test_an_unparseable_review_is_retried_once_and_succeeds():
    """#363: prod saw the call succeed and the body fail to parse, then an
    unchanged re-run come back clean. One retry rescues that meeting."""
    llm = RecordingFakeLLM(["not json at all", json.dumps(REVIEW)])
    out = await _act(_RulesPool(["Sam"]), llm).analyse_meeting(DOC)
    assert "skipped" not in out
    assert out["review"] == REVIEW
    assert len(llm.calls) == 2
    first, second = llm.calls[0]["prompt"], llm.calls[1]["prompt"]
    assert "not valid JSON" not in first
    assert second.startswith(first)  # the SAME call, with one corrective line added
    assert "not valid JSON" in second[len(first) :]


async def test_an_unparseable_review_twice_is_llm_failed():
    llm = RecordingFakeLLM(["not json at all", "still not json"])
    out = await _act(_RulesPool(["Sam"]), llm).analyse_meeting(DOC)
    assert out["skipped"] == "llm_failed"
    assert len(llm.calls) == 2  # the retry never retries


async def test_a_parseable_review_is_not_retried():
    llm = RecordingFakeLLM(json.dumps(REVIEW))
    out = await _act(_RulesPool(["Sam"]), llm).analyse_meeting(DOC)
    assert "skipped" not in out
    assert len(llm.calls) == 1


async def test_a_truncated_review_is_not_retried():
    """`think()` has already spent its own re-roll by the time it raises, so a
    retry here would be a third upstream call."""
    llm = RecordingFakeLLM(raises=LLMTruncationError("cut"))
    out = await _act(_RulesPool(["Sam"]), llm).analyse_meeting(DOC)
    assert out["skipped"] == "llm_failed"
    assert len(llm.calls) == 1
