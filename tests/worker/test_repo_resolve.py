"""`repo_resolve.resolve_repo_by_text`: the coding lane's tier 2.

What remained of the alert resolver when the infra lane left v1: a token match
on the task's text, then the model. Only coding-enabled repositories are ever
candidates.
"""

from __future__ import annotations

import json
import uuid

import pytest_asyncio
from aegis_worker.activities.repo_resolve import resolve_repo_by_text


class _LLM:
    def __init__(self, answer: dict) -> None:
        self.answer = answer
        self.prompts: list[str] = []

    async def think(self, prompt: str, **kwargs) -> dict:
        self.prompts.append(prompt)
        return {"response": json.dumps(self.answer)}


@pytest_asyncio.fixture(loop_scope="function")
async def repos(db_pool):
    tag = uuid.uuid4().hex[:6]
    rows = {
        "zebraquill": (f"zebraquill{tag}", True),
        "otter": (f"otterharbour{tag}", True),
        "hidden": (f"lanternmoth{tag}", False),
    }
    ids = {}
    for key, (name, enabled) in rows.items():
        ids[key] = await db_pool.fetchval(
            "INSERT INTO resources (slug, kind, title, metadata) "
            "VALUES ($1, 'repository', $2, $3) RETURNING id::text",
            f"test-rr-{name}",
            name,
            {
                "github_repo": f"acme/{name}",
                "path": f"acme/{name}",
                "coding_enabled": "true" if enabled else "false",
            },
        )
    yield {k: (rows[k][0], ids[k]) for k in rows}
    await db_pool.execute("DELETE FROM resources WHERE slug LIKE 'test-rr-%'")


async def test_one_repo_named_in_the_title_resolves_without_the_model(db_pool, repos):
    name, _ = repos["zebraquill"]
    llm = _LLM({"resources": []})
    out = await resolve_repo_by_text(
        db_pool, llm, "m", {"title": f"Fix the {name} exporter", "description": ""}
    )
    assert out["source"] == "deterministic"
    assert out["github_repo"] == f"acme/{name}"
    assert out["confidence"] == 1.0
    assert llm.prompts == []


async def test_a_repo_not_enabled_for_coding_is_never_a_candidate(db_pool, repos):
    name, rid = repos["hidden"]
    # Even when the model names it, a disabled repo is not in the candidate set.
    llm = _LLM({"resources": [{"resource_id": rid, "resource_title": name, "confidence": 0.9}]})
    out = await resolve_repo_by_text(
        db_pool, llm, "m", {"title": f"Look at {name}", "description": ""}
    )
    assert out["github_repo"] == ""
    assert out["source"] == "none"
    assert all(name not in line for line in llm.prompts[0].splitlines() if "id=" in line)


async def test_the_model_picks_when_the_text_names_no_repo(db_pool, repos):
    name, rid = repos["otter"]
    llm = _LLM({"resources": [{"resource_id": rid, "resource_title": name, "confidence": 0.85}]})
    out = await resolve_repo_by_text(
        db_pool, llm, "m", {"title": "The nightly job writes duplicates", "description": ""}
    )
    assert out["source"] == "llm"
    assert out["github_repo"] == f"acme/{name}"
    assert out["resource_path"] == f"acme/{name}"
    assert out["confidence"] == 0.85


async def test_a_weak_pick_is_unconfirmed_not_a_guess(db_pool, repos):
    name, rid = repos["otter"]
    llm = _LLM({"resources": [{"resource_id": rid, "resource_title": name, "confidence": 0.3}]})
    out = await resolve_repo_by_text(
        db_pool, llm, "m", {"title": "Something vague", "description": ""}
    )
    assert out["source"] == "llm_unconfirmed"
    assert out["confidence"] == 0.3


async def test_no_model_and_no_token_match_is_none(db_pool, repos):
    out = await resolve_repo_by_text(
        db_pool, None, "", {"title": "Something vague", "description": ""}
    )
    assert out["source"] == "none"
    assert out["resources"] == []
