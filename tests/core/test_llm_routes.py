"""Purpose → category → model routing for `think()`.

The tier map answers "which model does this agent get"; routing answers "which
model does this job get". Both are process-global, so the fixture below
snapshots and restores `_ROUTES` the way `test_llm_tier.py` does for `_TIERS`.

The `think()` tests drive the REAL client with only `chat.completions.create`
stubbed (`tests/llm_stub.py`), because the whole point is what lands in the
upstream request — a faked `think()` would assert on the mock, not the code.
"""

from __future__ import annotations

import textwrap
import types

import aegis.llm.routes as _routes_mod
import pytest
import pytest_asyncio
from aegis.llm.routes import merge_routes, route_for_purpose, set_routes
from aegis.services.llm_backend import ROUTES_SETTINGS_KEY, get_llm_backend, invalidate

from tests.llm_stub import StubbedLLMClient


@pytest.fixture(autouse=True)
def _isolate_routes():
    """`set_routes` installs a process-global table; save and restore it so an
    install can never escape the test that did it (aegis#250, same trap)."""
    saved = {k: dict(v) if isinstance(v, dict) else v for k, v in _routes_mod._ROUTES.items()}
    yield
    _routes_mod._ROUTES.clear()
    _routes_mod._ROUTES.update(saved)


_TABLE = {
    "categories": {
        "extract": {"model": "qwen3.5:9b", "json": True},
        "write": {"model": "bedrock-glm-4.7-flash"},
    },
    "purposes": {"money_event_extraction": "extract", "daylog_narrative": "write"},
}


# ------------------------------------------------------------------ set_routes


def test_set_routes_installs_a_valid_table():
    installed = set_routes(_TABLE)
    assert installed["categories"]["extract"] == {"model": "qwen3.5:9b", "json": True}
    # `json` defaults to False rather than being absent, so every consumer sees
    # the same shape.
    assert installed["categories"]["write"] == {"model": "bedrock-glm-4.7-flash", "json": False}
    assert installed["purposes"]["daylog_narrative"] == "write"


def test_set_routes_returns_a_copy():
    installed = set_routes(_TABLE)
    installed["purposes"]["daylog_narrative"] = "extract"
    installed["categories"]["extract"]["model"] = "tampered"
    assert route_for_purpose("daylog_narrative") == ("bedrock-glm-4.7-flash", False)
    assert route_for_purpose("money_event_extraction") == ("qwen3.5:9b", True)


def test_none_installs_an_empty_table():
    set_routes(_TABLE)
    assert set_routes(None) == {"categories": {}, "purposes": {}}
    assert route_for_purpose("money_event_extraction") == (None, False)


def test_empty_mapping_installs_an_empty_table():
    set_routes(_TABLE)
    assert set_routes({}) == {"categories": {}, "purposes": {}}


def test_unknown_category_for_a_purpose_raises_naming_it():
    bad = {
        "categories": {"extract": {"model": "m"}},
        "purposes": {"daylog_narrative": "prose"},
    }
    with pytest.raises(ValueError) as exc:
        set_routes(bad)
    # The message has to name BOTH the purpose and the category it missed, or
    # an operator with 20 purposes cannot find the typo.
    assert "daylog_narrative" in str(exc.value) and "prose" in str(exc.value)


def test_empty_model_raises_naming_the_category():
    with pytest.raises(ValueError, match="extract"):
        set_routes({"categories": {"extract": {"model": "   "}}, "purposes": {}})


def test_missing_model_raises_naming_the_category():
    with pytest.raises(ValueError, match="extract"):
        set_routes({"categories": {"extract": {"json": True}}, "purposes": {}})


def test_non_bool_json_raises_naming_the_category():
    with pytest.raises(ValueError, match="classify"):
        set_routes({"categories": {"classify": {"model": "m", "json": "yes"}}, "purposes": {}})


def test_a_rejected_table_leaves_the_installed_one_untouched():
    """Validation must not half-apply: the boot sites carry on with whatever
    was installed, so a rejected table cannot be allowed to have shredded it."""
    set_routes(_TABLE)
    with pytest.raises(ValueError):
        set_routes({"categories": {"extract": {"model": "x"}}, "purposes": {"p": "nope"}})
    assert route_for_purpose("money_event_extraction") == ("qwen3.5:9b", True)


# ------------------------------------------------------------ route_for_purpose


def test_route_for_purpose_returns_model_and_json_flag():
    set_routes(_TABLE)
    assert route_for_purpose("money_event_extraction") == ("qwen3.5:9b", True)


def test_json_defaults_to_false():
    set_routes(_TABLE)
    assert route_for_purpose("daylog_narrative") == ("bedrock-glm-4.7-flash", False)


def test_unmapped_purpose_and_none_route_nowhere():
    set_routes(_TABLE)
    assert route_for_purpose("chat") == (None, False)
    assert route_for_purpose(None) == (None, False)
    assert route_for_purpose("") == (None, False)


def test_route_for_purpose_never_raises_on_an_empty_table():
    set_routes(None)
    assert route_for_purpose("anything") == (None, False)


# ----------------------------------------------------------------- merge_routes


def test_merge_adds_a_category_and_overrides_a_model():
    merged = merge_routes(
        _TABLE,
        {"categories": {"extract": {"model": "local-x"}, "classify": {"model": "c", "json": True}}},
    )
    # Merged per key: the new model lands, the category's own `json` survives.
    assert merged["categories"]["extract"] == {"model": "local-x", "json": True}
    assert merged["categories"]["classify"] == {"model": "c", "json": True}
    assert merged["categories"]["write"] == {"model": "bedrock-glm-4.7-flash"}


def test_merge_removes_a_purpose_with_null_and_leaves_the_rest():
    merged = merge_routes(
        _TABLE, {"purposes": {"daylog_narrative": None, "gmail_classification": "extract"}}
    )
    assert "daylog_narrative" not in merged["purposes"]
    assert merged["purposes"]["money_event_extraction"] == "extract"
    assert merged["purposes"]["gmail_classification"] == "extract"


def test_merge_removes_a_purpose_with_an_empty_string():
    merged = merge_routes(_TABLE, {"purposes": {"money_event_extraction": ""}})
    assert "money_event_extraction" not in merged["purposes"]


def test_merge_does_not_mutate_the_base():
    base = {"categories": {"extract": {"model": "a", "json": True}}, "purposes": {"p": "extract"}}
    merge_routes(base, {"categories": {"extract": {"model": "b"}}, "purposes": {"p": None}})
    assert base["categories"]["extract"]["model"] == "a"
    assert base["purposes"]["p"] == "extract"


def test_merge_with_no_override_is_the_base():
    assert merge_routes(_TABLE, None) == merge_routes(_TABLE, {})


# ------------------------------------------------------------------- think()


async def test_think_sends_the_routed_model_and_json_response_format():
    set_routes(_TABLE)
    client = StubbedLLMClient(content='{"ok": true}')

    result = await client.think(
        "extract this", model="gemma4:e2b", purpose="money_event_extraction"
    )

    sent = client.calls[0]
    assert sent["model"] == "qwen3.5:9b"
    assert sent["response_format"] == {"type": "json_object"}
    # The RECORDED model is the routed one too — an llm_calls row that names
    # the caller's model would make routed spend impossible to attribute.
    assert result["model"] == "qwen3.5:9b"


async def test_a_route_without_json_sends_no_response_format_key_at_all():
    """Absent, not null: providers reject `response_format: null`."""
    set_routes(_TABLE)
    client = StubbedLLMClient(content="a paragraph")

    await client.think("write this", model="gemma4:e2b", purpose="daylog_narrative")

    sent = client.calls[0]
    assert sent["model"] == "bedrock-glm-4.7-flash"
    assert "response_format" not in sent


async def test_unmapped_purpose_keeps_the_callers_model():
    set_routes(_TABLE)
    client = StubbedLLMClient(content="hi")

    await client.think("anything", model="gemma4:e2b", purpose="knowledge_ask")

    assert client.calls[0]["model"] == "gemma4:e2b"
    assert "response_format" not in client.calls[0]


async def test_no_routes_installed_changes_nothing():
    set_routes(None)
    client = StubbedLLMClient(content="hi")

    await client.think("anything", model="gemma4:e2b", purpose="money_event_extraction")

    assert client.calls[0]["model"] == "gemma4:e2b"
    assert "response_format" not in client.calls[0]


async def test_the_reasoning_floor_applies_to_the_routed_model():
    """THE ordering property. The caller's model needs no floor and the routed
    one does; route first, floor second, or a routed reasoning model goes out
    on a 512 budget and returns empty content — the #255 failure, re-created by
    a config change instead of a code change."""
    from aegis.llm import _REASONING_MIN_TOKENS

    set_routes(
        {
            "categories": {"deep": {"model": "bedrock-kimi-k2.5"}},
            "purposes": {"clarify_classification": "deep"},
        }
    )
    client = StubbedLLMClient(content='{"classification": "next"}')

    await client.think(
        "classify", model="gemma4:e2b", max_tokens=512, purpose="clarify_classification"
    )

    sent = client.calls[0]
    assert sent["model"] == "bedrock-kimi-k2.5"
    assert sent["max_tokens"] == _REASONING_MIN_TOKENS


async def test_the_truncation_retry_keeps_the_route_and_the_json_mode():
    """A re-roll must be the same request with more room — including the routed
    model and the JSON constraint, or the rescue attempt asks a different
    model a differently-shaped question."""
    from aegis.llm import _TRUNCATION_RETRY_TOKENS

    set_routes(_TABLE)
    client = StubbedLLMClient(content=["", '{"ok": true}'], finish_reason=["length", "stop"])

    await client.think(
        "extract", model="gemma4:e2b", max_tokens=512, purpose="money_event_extraction"
    )

    assert client.call_count == 2
    first, second = client.calls
    assert first["model"] == second["model"] == "qwen3.5:9b"
    assert second["response_format"] == {"type": "json_object"}
    assert second["max_tokens"] == _TRUNCATION_RETRY_TOKENS


async def test_chat_ignores_routes():
    """`chat()` is the tool-calling agent loop: the model is part of the
    agent's identity there, so a purpose must never swap it."""
    set_routes(_TABLE)
    client = StubbedLLMClient(content="hi")

    await client.chat(
        [{"role": "user", "content": "hi"}],
        model="gemma4:e2b",
        purpose="money_event_extraction",
    )

    assert client.calls[0]["model"] == "gemma4:e2b"
    assert "response_format" not in client.calls[0]


# -------------------------------------------------------------- llm_backend


def _settings(yaml_path: str):
    return types.SimpleNamespace(
        secret_key="",
        litellm_url="http://env-proxy:4000",
        litellm_api_key="env-key",
        model_fast="env-fast",
        model_balanced="env-balanced",
        model_smart="env-smart",
        models_yaml_path=yaml_path,
    )


def _write_yaml(tmp_path) -> str:
    path = tmp_path / "models.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            tiers:
              fast: "yaml-fast"
              balanced: "yaml-balanced"
              smart: "yaml-smart"
            routes:
              categories:
                extract: {model: "qwen3.5:9b", json: true}
                write: {model: "bedrock-glm-4.7-flash"}
              purposes:
                money_event_extraction: extract
                daylog_narrative: write
            """
        )
    )
    return str(path)


@pytest_asyncio.fixture(loop_scope="function")
async def clean_routes(db_pool):
    invalidate()
    await db_pool.execute(f"DELETE FROM settings WHERE key = '{ROUTES_SETTINGS_KEY}'")
    yield db_pool
    await db_pool.execute(f"DELETE FROM settings WHERE key = '{ROUTES_SETTINGS_KEY}'")
    invalidate()


async def test_backend_reads_routes_from_the_yaml(clean_routes, tmp_path):
    b = await get_llm_backend(clean_routes, _settings(_write_yaml(tmp_path)), use_cache=False)
    assert b["routes"]["categories"]["extract"] == {"model": "qwen3.5:9b", "json": True}
    assert b["routes"]["purposes"]["daylog_narrative"] == "write"


async def test_missing_yaml_yields_empty_routes(clean_routes):
    b = await get_llm_backend(clean_routes, _settings("/nonexistent/models.yaml"), use_cache=False)
    assert b["routes"] == {"categories": {}, "purposes": {}}


async def test_db_row_is_merged_over_the_yaml(clean_routes, tmp_path):
    await clean_routes.execute(
        "INSERT INTO settings (key, value) VALUES ($1, $2)",
        ROUTES_SETTINGS_KEY,
        {
            "categories": {"extract": {"model": "local-override"}},
            "purposes": {"daylog_narrative": None, "gmail_classification": "extract"},
        },
    )
    b = await get_llm_backend(clean_routes, _settings(_write_yaml(tmp_path)), use_cache=False)

    routes = b["routes"]
    # Partial override: the model moved, the category's json flag survived.
    assert routes["categories"]["extract"] == {"model": "local-override", "json": True}
    assert routes["categories"]["write"] == {"model": "bedrock-glm-4.7-flash"}
    assert routes["purposes"]["money_event_extraction"] == "extract"
    assert routes["purposes"]["gmail_classification"] == "extract"
    assert "daylog_narrative" not in routes["purposes"]
    # The merged result must still be installable.
    assert set_routes(routes)["purposes"]["gmail_classification"] == "extract"


async def test_a_junk_routes_row_does_not_break_the_backend_read(clean_routes, tmp_path):
    """A bad row degrades to the yaml, never to a failed boot."""
    await clean_routes.execute(
        "INSERT INTO settings (key, value) VALUES ($1, $2)",
        ROUTES_SETTINGS_KEY,
        ["not", "a", "mapping"],
    )
    b = await get_llm_backend(clean_routes, _settings(_write_yaml(tmp_path)), use_cache=False)
    assert b["routes"]["purposes"]["money_event_extraction"] == "extract"
