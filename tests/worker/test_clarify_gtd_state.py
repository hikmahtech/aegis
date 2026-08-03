"""Issue #139 — every terminal clarify outcome leaves a GTD state behind.

GTD state in AEGIS lives entirely in labels (`@next` / `@someday` / `@waiting` /
`@reference`). A task that exits ClarifyFlow carrying none of them is in no GTD
state: invisible to "what's next", to "what am I blocked on", and to every review
filter. In production 82 of the 83 non-trash tasks clarify had marked terminal
were in exactly that limbo, concentrated in the outcomes that stamped only a
*context* label (`@5min`), only a *person* label (`@me`), or nothing at all.

The tests here are deliberately of three kinds:

1. `test_gtd_state_contract_covers_every_classification` derives the outcome
   vocabulary from `clarify.py`'s own AST, so a future classification added
   without a deliberate state decision fails here rather than silently joining
   the limbo pile.
2. `test_every_mapped_outcome_lands_its_state_label` drives the real
   `apply_outcome` once per table entry — the table is the spec, the Todoist
   command batch is the subject.
3. Per-outcome tests assert **literal** label strings, so a mistake that
   corrupted both the table and the code path still fails.
"""

from __future__ import annotations

import ast
import datetime as dt
import pathlib
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aegis_worker.activities import clarify as clarify_mod
from aegis_worker.activities.clarify import (
    _FOLLOWUP_SUFFIX,
    _GTD_STATE_FOR,
    GTD_STATE_LABELS,
    ClarifyActivities,
    gtd_state_label,
)

_CLARIFY_SRC = pathlib.Path(clarify_mod.__file__)


@pytest_asyncio.fixture(autouse=True, loop_scope="function")
async def _auto_content_route(seed_app_route):
    """Route-driven outcomes (`route_apply`, `pandora_investigation`) need the
    seeded APP- content route, same as the main clarify-activities module."""
    yield


@pytest_asyncio.fixture(loop_scope="function")
async def _inbox(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES "
            "('todoist_managed_project_ids', $1) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            {"inbox": "P_INBOX"},
        )
    yield db_pool


def _acts(db_pool):
    connector = AsyncMock()
    connector.commands = AsyncMock(
        return_value={"ok": True, "data": {"sync_status": {}, "temp_id_mapping": {}}}
    )
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=connector, llm_client=AsyncMock())
    return acts, connector


def _labels_written(connector) -> set[str]:
    """Union of every label set clarify asked Todoist to write."""
    out: set[str] = set()
    for call in connector.commands.await_args_list:
        for cmd in call.args[0]:
            if cmd.get("type") == "item_update" and "labels" in (cmd.get("args") or {}):
                out |= set(cmd["args"]["labels"])
    return out


def _decision(classification: str, **over) -> dict:
    base = {
        "classification": classification,
        "confidence": 1.0,
        "assignee": "@me",
        "contexts": [],
        "reason": "test",
        "llm_model": "rules",
        "source_tag": "#email",
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# 1. The contract itself — derived from the source, not hand-listed.
# ---------------------------------------------------------------------------


def _classification_literals_in_source() -> set[str]:
    """Every classification string clarify.py statically produces or branches on.

    Four shapes carry a classification in this module:
      * `{"classification": "<lit>", ...}` — classify_one's return dicts
      * `decision["classification"] = "<lit>"` — apply_clarify_resolution
      * `classification == "<lit>"` — apply_outcome's branch chain
      * `classification = ... or "<lit>"` — apply_outcome's `unknown` fallback
    """
    tree = ast.parse(_CLARIFY_SRC.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=False):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "classification"
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                ):
                    found.add(value.value)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value == "classification"
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                ):
                    found.add(node.value.value)
                elif (
                    isinstance(target, ast.Name)
                    and target.id == "classification"
                    and isinstance(node.value, ast.BoolOp)
                ):
                    found |= {
                        v.value
                        for v in node.value.values
                        if isinstance(v, ast.Constant) and isinstance(v.value, str)
                    }
        elif (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Name)
            and node.left.id == "classification"
            and len(node.ops) == 1
            and isinstance(node.ops[0], ast.Eq)
            and isinstance(node.comparators[0], ast.Constant)
            and isinstance(node.comparators[0].value, str)
        ):
            found.add(node.comparators[0].value)
    return found


def test_source_scan_finds_the_known_outcomes() -> None:
    """Guard the guard: if any arm of the AST walk stops matching, the coverage
    test below quietly narrows and starts passing on outcomes it never saw.

    Each assertion below pins ONE arm, using a literal that only that arm can
    reach — checking a literal several arms find (e.g. "mine", which is both a
    dict value and an `==` comparand) would leave the other arms unprotected.
    """
    found = _classification_literals_in_source()
    # dict-value arm: `skipped` only ever appears as a classify_one return value.
    assert "skipped" in found
    # BoolOp arm: `unknown` only appears as apply_outcome's `or "unknown"`.
    assert "unknown" in found
    # Compare arm: apply_outcome's branch chain — the shape a NEW outcome takes.
    assert {"trash", "reference", "someday", "next_action", "2_min", "mine"} <= found
    assert len(found) >= 10


def test_gtd_state_contract_covers_every_classification() -> None:
    """Every classification clarify.py can produce has a deliberate entry in
    `_GTD_STATE_FOR` — a state label, or an explicit None with the reason.

    A new outcome added without an entry fails here. That is the point: the
    runtime default in `gtd_state_label` exists so an unexpected model response
    still lands somewhere visible, NOT as a licence to skip the table.
    """
    uncovered = {
        c
        for c in _classification_literals_in_source()
        if c not in _GTD_STATE_FOR and not c.endswith(_FOLLOWUP_SUFFIX)
    }
    assert uncovered == set(), (
        f"classifications with no GTD state decision: {sorted(uncovered)} — "
        "add them to _GTD_STATE_FOR in worker/src/aegis_worker/activities/clarify.py"
    )


def test_mapped_states_are_real_gtd_labels() -> None:
    for classification, label in _GTD_STATE_FOR.items():
        assert label is None or label in GTD_STATE_LABELS, (
            f"{classification} maps to {label!r}, which is not a GTD state label"
        )


def test_clarify_never_stamps_the_agent_task_park_label() -> None:
    """`@waiting` is the agent-task executor's park label — reaching it removes
    a task from `find_actionable_tasks`' eligible pool. Clarify's assignee labels
    mean "an AEGIS agent should work this", so clarify stamping @waiting would
    silently stop the executor. @waiting is applied by `agent_task.park_task`,
    at the end of a run, which is the only point the task is genuinely blocked.
    """
    from aegis_worker.activities.agent_task import PARK_LABEL

    assert PARK_LABEL == "@waiting"
    assert PARK_LABEL in GTD_STATE_LABELS  # it IS a valid GTD state...
    offenders = {c for c, label in _GTD_STATE_FOR.items() if label == PARK_LABEL}
    assert offenders == set(), f"clarify must not park tasks: {sorted(offenders)}"


def test_agent_handoff_outcomes_stay_eligible_for_the_executor() -> None:
    """The outcomes that hand a task to an AEGIS agent must not carry either
    park label (`@someday` / `@waiting`), or the agent-task sweep will never
    pick the task up."""
    from aegis_worker.activities.agent_task import EXCLUDED_LABELS

    for classification in ("route_apply", "pandora_investigation", "pandora_owned"):
        assert gtd_state_label(classification) not in EXCLUDED_LABELS


def test_unknown_classification_defaults_to_the_visible_state() -> None:
    """A classification outside the vocabulary must land somewhere VISIBLE. The
    `skip_inbox` rule map is admin-editable (`settings.gtd_rules`), so its values
    are arbitrary strings that no static table can enumerate."""
    assert gtd_state_label("something-an-admin-typed") == "@next"
    assert gtd_state_label("raphael_followup") == "@next"
    assert gtd_state_label("some-brand-new-agent_followup") == "@next"


# ---------------------------------------------------------------------------
# 2. Table-driven sweep over the real apply_outcome.
# ---------------------------------------------------------------------------

# Enough of a task/decision for each branch to run. Keyed by classification so a
# new table entry without a case here fails loudly rather than being skipped.
_CASES: dict[str, dict] = {
    "next_action": {"task": {"id": "G_NEXT", "labels": ["#email"]}},
    "2_min": {
        "task": {"id": "G_2MIN", "labels": ["#email"]},
        "decision": {"contexts": ["@5min"]},
        "kwargs": {"_now": dt.datetime(2026, 5, 19, 3, 0, 0, tzinfo=dt.UTC)},
    },
    "mine": {"task": {"id": "G_MINE", "labels": ["#email"]}},
    "route_apply": {
        "task": {"id": "G_ROUTE", "content": "APP-1: broken", "labels": []},
        "decision": {"assignee": "@pandora", "contexts": ["@code"]},
    },
    "pandora_investigation": {
        "task": {"id": "G_INV", "content": "APP-2: broken", "labels": []},
        "decision": {"assignee": "@pandora", "contexts": ["@code"]},
    },
    "pandora_owned": {"task": {"id": "G_OWNED", "labels": ["@pandora"]}},
    "pandora_followup": {
        "task": {"id": "G_FOLLOW", "content": "APP-3: broken", "labels": ["@pandora"]},
    },
    "someday": {"task": {"id": "G_SOMEDAY", "labels": ["#email"]}},
    "leave": {"task": {"id": "G_LEAVE", "labels": ["#email"]}},
    "reference": {"task": {"id": "G_REF", "labels": ["#research"]}},
    "unknown": {"task": {"id": "G_UNKNOWN", "labels": ["#email"]}},
}


def test_every_mapped_outcome_has_a_case() -> None:
    """The sweep below must exercise every outcome the table says gets a label."""
    labelled = {c for c, label in _GTD_STATE_FOR.items() if label is not None}
    assert labelled == set(_CASES), (
        f"missing apply_outcome cases for {sorted(labelled - set(_CASES))}; "
        f"stale cases for {sorted(set(_CASES) - labelled)}"
    )


@pytest.mark.parametrize(
    "classification", sorted(c for c, label in _GTD_STATE_FOR.items() if label is not None)
)
@pytest.mark.asyncio
async def test_every_mapped_outcome_lands_its_state_label(
    db_pool, _inbox, classification
) -> None:
    case = _CASES[classification]
    acts, connector = _acts(db_pool)
    out = await acts.apply_outcome(
        case["task"],
        _decision(classification, **case.get("decision", {})),
        force_apply=True,
        **case.get("kwargs", {}),
    )
    assert out["applied"] is True
    written = _labels_written(connector)
    assert _GTD_STATE_FOR[classification] in written, (
        f"{classification} wrote labels {sorted(written)} — no GTD state"
    )
    # Exactly one state: two states at once is as unanswerable as none.
    assert len(written & set(GTD_STATE_LABELS)) == 1


# ---------------------------------------------------------------------------
# 3. Literal per-outcome assertions (independent of the table).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_2min_out_of_window_now_gets_next_not_just_a_context(db_pool, _inbox) -> None:
    """The old branch stamped only `@5min` — a CONTEXT (where the work happens),
    never a state. All 9 all-time 2_min tasks ended up in limbo."""
    acts, connector = _acts(db_pool)
    # 03:00 UTC is outside the 8-22 window, so no card: the label path runs.
    out = await acts.apply_outcome(
        {"id": "L_2MIN", "labels": ["#email"]},
        _decision("2_min", contexts=["@5min"]),
        _now=dt.datetime(2026, 5, 19, 3, 0, 0, tzinfo=dt.UTC),
    )
    assert out["applied"] is True
    assert out["interaction_spawned"] is False
    labels = _labels_written(connector)
    assert "@5min" in labels  # context preserved
    assert "@next" in labels  # state added


@pytest.mark.asyncio
async def test_mine_gets_next_alongside_the_person_label(db_pool, _inbox) -> None:
    """`@me` says WHO, not WHAT STATE. 17/17 all-time `mine` tasks were limbo."""
    acts, connector = _acts(db_pool)
    out = await acts.apply_outcome({"id": "L_MINE", "labels": ["#email"]}, _decision("mine"))
    assert out["applied"] is True
    labels = _labels_written(connector)
    assert "@me" in labels
    assert "@next" in labels


@pytest.mark.asyncio
async def test_route_apply_gets_next(db_pool, _inbox) -> None:
    acts, connector = _acts(db_pool)
    out = await acts.apply_outcome(
        {"id": "L_ROUTE", "content": "APP-9: thing", "labels": []},
        _decision("route_apply", assignee="@pandora", contexts=["@code"]),
    )
    assert out["applied"] is True
    labels = _labels_written(connector)
    assert "@pandora" in labels
    assert "@next" in labels


@pytest.mark.asyncio
async def test_pandora_investigation_gets_next(db_pool, _inbox) -> None:
    acts, connector = _acts(db_pool)
    out = await acts.apply_outcome(
        {"id": "L_INV", "content": "APP-8: thing", "labels": []},
        _decision("pandora_investigation", assignee="@pandora", contexts=["@code"]),
    )
    assert out["applied"] is True
    assert out["interaction_spawned"] is True
    labels = _labels_written(connector)
    assert "@pandora" in labels
    assert "@next" in labels


@pytest.mark.asyncio
async def test_leave_gets_someday_not_just_review(db_pool, _inbox) -> None:
    """`leave` is the user's answer to a card ("Leave for later") — a decision,
    so record it as `@someday` rather than asking again."""
    acts, connector = _acts(db_pool)
    out = await acts.apply_outcome(
        {"id": "L_LEAVE", "content": "x", "labels": ["#email"]},
        _decision("leave"),
        force_apply=True,
    )
    assert out["applied"] is True
    labels = _labels_written(connector)
    assert "@review" in labels
    assert "@someday" in labels
    assert "@next" not in labels


@pytest.mark.asyncio
async def test_trash_gets_no_state_label(db_pool, _inbox) -> None:
    """The one outcome where no state is genuinely correct: item_complete fires,
    so the row leaves every view and a state label would pollute "what's next"
    with completed junk."""
    acts, connector = _acts(db_pool)
    out = await acts.apply_outcome({"id": "L_TRASH", "labels": ["#email"]}, _decision("trash"))
    assert out["applied"] is True
    sent = connector.commands.await_args.args[0]
    assert "item_complete" in [c["type"] for c in sent]
    written = _labels_written(connector)
    assert "#trash" in written  # labels WERE written — the emptiness below is real
    assert written & set(GTD_STATE_LABELS) == set()


@pytest.mark.asyncio
async def test_pandora_gate_still_writes_nothing(db_pool, _inbox) -> None:
    """A card is pending — the resolution sets the state. Stamping one here
    would pre-empt the user's answer."""
    acts, connector = _acts(db_pool)
    out = await acts.apply_outcome(
        {"id": "L_GATE", "content": "APP-7: thing", "labels": [], "source_tag": None},
        _decision("pandora_gate", assignee="@pandora"),
    )
    assert out["applied"] is False
    assert out["interaction_spawned"] is True
    assert out["commands_sent"] == 0
    connector.commands.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_classification_lands_visible_not_in_limbo(db_pool, _inbox) -> None:
    """A model response outside the vocabulary used to fall through to a
    label-only update with no state at all."""
    acts, connector = _acts(db_pool)
    out = await acts.apply_outcome(
        {"id": "L_UNKNOWN", "labels": ["#email"]},
        _decision("teleport_to_mars", contexts=["@deep"]),
    )
    assert out["applied"] is True
    assert "@next" in _labels_written(connector)


@pytest.mark.asyncio
async def test_agent_followup_stamps_next(db_pool, _inbox) -> None:
    """The comment-channel branch hands the reply to AgentChatReplyFlow and used
    to touch no labels at all, leaving an open, agent-assigned task under active
    discussion invisible to "what's next"."""
    acts, connector = _acts(db_pool)
    out = await acts.apply_outcome(
        {"id": "L_SEBAS", "content": "do the thing", "labels": ["@sebas"]},
        _decision("sebas_followup", assignee="@sebas"),
    )
    assert out["applied"] is True
    assert out["interaction_payload"]["spawn_kind"] == "agent_chat_reply"
    labels = _labels_written(connector)
    assert "@sebas" in labels
    assert "@next" in labels


@pytest.mark.asyncio
async def test_followup_stamp_is_idempotent(db_pool, _inbox) -> None:
    """A conversation produces a followup per user note; only the first needs a
    write."""
    acts, connector = _acts(db_pool)
    out = await acts.apply_outcome(
        {"id": "L_SEBAS2", "content": "do the thing", "labels": ["@sebas", "@next"]},
        _decision("sebas_followup", assignee="@sebas"),
    )
    assert out["applied"] is True
    assert out["commands_sent"] == 0
    connector.commands.assert_not_awaited()
