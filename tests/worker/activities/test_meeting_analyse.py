"""analyse_meeting — code-computed stats + observations + one LLM review."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from aegis.llm import LLMTruncationError
from aegis_worker.activities.meeting import MeetingActivities

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


class _FakeLLM:
    def __init__(self, response=None, exc=None):
        self.response, self.exc, self.calls = response, exc, []

    async def think(self, **kw):
        self.calls.append(kw)
        if self.exc:
            raise self.exc
        return {"response": self.response}


class _RulesPool:
    """Answers the settings read; every other query returns None.

    `record_external_observation` issues an `INSERT … RETURNING` through
    `fetchrow`, and a None return means "already ingested" — so the
    observation path runs without a database and simply writes nothing.
    """

    def __init__(self, names):
        self._names = names

    async def fetchrow(self, sql, *args):
        if "settings" in sql:
            return {"value": {"self_names": self._names}}
        return None


def _act(pool, llm):
    return MeetingActivities(
        gmail_credentials_file="c", gmail_token_dir="t", db_pool=pool, llm_client=llm,
        model_balanced="balanced-model", agent_id="sebas",
    )


async def test_empty_self_names_skips_without_touching_llm():
    llm = _FakeLLM(json.dumps(REVIEW))
    out = await _act(_RulesPool([]), llm).analyse_meeting(DOC)
    assert out["skipped"] == "no_self_names"
    assert llm.calls == []


async def test_review_path_builds_prompt_from_own_lines_only():
    llm = _FakeLLM(json.dumps(REVIEW))
    out = await _act(_RulesPool(["Sam"]), llm).analyse_meeting(DOC)
    assert "skipped" not in out
    assert out["self_matched"] is True
    assert out["stats"]["self"]["turns"] == 2
    assert out["review"] == REVIEW
    assert out["rendered"].startswith("# Meeting review: Widget Standup")
    assert "Parity script is slow" in out["rendered"]
    call = llm.calls[0]
    assert call["purpose"] == "meeting_review" and call["model"] == "balanced-model"
    assert call["max_tokens"] >= 3000
    assert "parity script" in call["prompt"]
    assert "Anything blocking" not in call["prompt"]  # Ada's line never reaches the LLM
    assert "Rollout status" in call["prompt"]


async def test_llm_truncation_and_bad_json_are_skipped_not_raised():
    out = await _act(_RulesPool(["Sam"]), _FakeLLM(exc=LLMTruncationError("cut"))).analyse_meeting(
        DOC
    )
    assert out["skipped"] == "llm_failed" and out["stats"]["self"]["matched"] is True
    out = await _act(_RulesPool(["Sam"]), _FakeLLM("not json at all")).analyse_meeting(DOC)
    assert out["skipped"] == "llm_failed"


async def test_thin_body_without_own_lines_is_too_thin():
    thin = {**DOC, "notes": "short", "transcript": [], "doc_status": "no_link"}
    out = await _act(_RulesPool(["Sam"]), _FakeLLM(json.dumps(REVIEW))).analyse_meeting(thin)
    assert out["skipped"] == "too_thin"


async def test_lists_are_capped_and_coerced():
    resp = {
        "contributions": list(range(9)),
        "problems_raised": None,
        "commitments": "x",
        "verbosity_note": 5,
    }
    out = await _act(_RulesPool(["Sam"]), _FakeLLM(json.dumps(resp))).analyse_meeting(DOC)
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
    act = _act(obs_pool, _FakeLLM(json.dumps(REVIEW)))
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
