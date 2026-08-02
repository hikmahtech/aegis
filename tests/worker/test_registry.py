"""The flow/activity registry and its boot-time completeness check (D6, #188).

Two families of test here:

* **wiring** — the runtime really is built from `registry`, and the check
  really does run in `main()` before `Worker(...)` is constructed. These are
  AST assertions, never source-string matches: a mention in a comment or a
  docstring cannot satisfy them.
* **falsifiability** — remove a flow/activity from exactly ONE of the places
  it has to appear and prove `check_registration` raises. One test per place.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from types import SimpleNamespace

import aegis_worker.__main__ as worker_main
import pytest
import yaml
from aegis_worker import registry
from aegis_worker.registry import (
    FLOWS,
    FlowSpec,
    RegistrationError,
    activity_classes,
    activity_type_map,
    all_activity_methods,
    base_workflows,
    check_registration,
    expected_activity_names,
    feature_flagged_types,
    flow_classes,
    workflows_for,
)
from temporalio import activity

REPO_ROOT = Path(__file__).resolve().parents[2]
SEED_DIR = REPO_ROOT / "config" / "seed"


def _flags(homelab: bool = True, money: bool = True) -> SimpleNamespace:
    """Prod's settings: both feature flags on."""
    return SimpleNamespace(homelab_enabled=homelab, money_hygiene_enabled=money)


def _activities_for(settings) -> list:
    """Stand-in for the bound methods main() hands to Worker().

    The unbound functions carry the same @activity.defn name, which is all
    check_registration compares.
    """
    names = expected_activity_names(settings)
    return [
        m
        for m in all_activity_methods()
        if activity._Definition.must_from_callable(m).name in names
    ]


# --------------------------------------------------------------------------
# wiring: the runtime is the registry
# --------------------------------------------------------------------------


def _main_ast() -> ast.AsyncFunctionDef:
    tree = ast.parse(inspect.getsource(worker_main))
    return next(
        n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "main"
    )


def _call_named(node: ast.AST, func_name: str) -> ast.Call | None:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id == func_name:
            return sub
    return None


def _assigned_from(main_fn: ast.AsyncFunctionDef, target: str) -> str | None:
    """Name of the function whose result is assigned to `target` in main()."""
    for node in ast.walk(main_fn):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == target
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
        ):
            return node.value.func.id
    return None


def test_worker_gets_the_registry_lists_and_no_parallel_ones():
    """Issue #188: the registration tests used to assert membership in the
    module-level WORKFLOWS while `Worker(...)` was handed a *separately built*
    local list, so a flow removed from only the local list stayed green.

    There is no second list any more — assert that structurally: the names
    `Worker(workflows=..., activities=...)` receives must be the ones bound to
    `workflows_for(...)` and `collect_activities(...)`.
    """
    main_fn = _main_ast()
    worker_call = _call_named(main_fn, "Worker")
    assert worker_call is not None, "no Worker(...) call found in main()"

    kwargs = {kw.arg: kw.value for kw in worker_call.keywords}
    for arg in ("workflows", "activities"):
        assert isinstance(kwargs.get(arg), ast.Name), (
            f"Worker({arg}=...) must be a plain name bound to a registry call"
        )

    assert _assigned_from(main_fn, kwargs["workflows"].id) == "workflows_for", (
        "Worker's workflows must come from registry.workflows_for(settings)"
    )
    assert _assigned_from(main_fn, kwargs["activities"].id) == "collect_activities", (
        "Worker's activities must come from registry.collect_activities(...)"
    )


def test_boot_check_runs_before_the_worker_starts():
    """A check that only runs in tests is worth much less — prove `main()`
    calls it, and calls it BEFORE constructing the Worker."""
    main_fn = _main_ast()
    positions = {}
    for node in ast.walk(main_fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ("check_registration", "Worker")
        ):
            positions.setdefault(node.func.id, node.lineno)

    assert "check_registration" in positions, "main() never calls check_registration()"
    assert positions["check_registration"] < positions["Worker"], (
        "check_registration() must run before Worker(...) is constructed"
    )


def test_every_activity_class_is_handed_to_collect_activities():
    """`collect_activities` reads activities off the instances it is given, so
    the one thing that can still be forgotten is a whole new Activities class.
    Cross-check main()'s argument list against the package."""
    tree = ast.parse(inspect.getsource(worker_main))
    instance_class: dict[str, str] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
        ):
            instance_class[node.targets[0].id] = node.value.func.id

    call = _call_named(_main_ast(), "collect_activities")
    assert call is not None, "main() no longer calls collect_activities(...)"
    wired = {
        instance_class[a.id]
        for a in call.args
        if isinstance(a, ast.Name) and a.id in instance_class
    }

    on_disk = set(activity_classes())
    missing = sorted(on_disk - wired)
    assert not missing, (
        f"activity classes never constructed/passed in main(): {missing} — their "
        "activities would die at call time with 'activity type not registered'"
    )


def test_every_flow_on_disk_is_declared_in_the_registry():
    declared = {s.name for s in FLOWS}
    on_disk = set(flow_classes())
    assert on_disk == declared, (
        f"undeclared={sorted(on_disk - declared)} declared-but-missing={sorted(declared - on_disk)}"
    )


def test_schedule_map_and_feature_flags_are_derived_from_the_registry():
    from aegis_worker import schedule_sync

    scheduled = {s.name for s in FLOWS if s.scheduled}
    assert set(schedule_sync._ACTIVITY_TYPE_MAP) == scheduled
    for name, mapper in activity_type_map().items():
        cls, _cfg = mapper(
            {"agent_id": "sebas", "config": {}, "_settings": {}},
        )
        assert cls.__name__ == name, f"{name} mapper returns {cls.__name__}"

    assert feature_flagged_types() == schedule_sync._FEATURE_FLAGGED_TYPES
    for types in feature_flagged_types().values():
        for t in types:
            assert t in scheduled


def test_module_workflows_is_the_unflagged_registry():
    assert base_workflows() == worker_main.WORKFLOWS
    assert [c.__name__ for c in base_workflows()] == [
        s.name for s in FLOWS if s.feature_flag is None
    ]


# --------------------------------------------------------------------------
# the real registration passes — and the counts have not moved
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("homelab", "money", "flows", "activities"),
    [
        # prod: `worker_starting activities=171 flows=35`, +1 flow and +2
        # activities from B7's WearableIngestFlow / WearableActivities, then
        # +1 flow and +5 activities from A2's ProfileReflectionFlow
        # (gather_profile_evidence, propose_profile_patch, check_profile_budget,
        # record_profile_card, apply_profile_reflection), then +1 activity and
        # NO new flow from A5 (propose_generalizations, which rides A2's flow),
        # then +1 flow and +3 activities from #226's FlowHealthWatchdogFlow /
        # FlowHealthActivities (find_failing_flows, find_stale_flows,
        # report_flow_health) — unflagged, so all three rows move.
        (True, True, 38, 182),
        (False, False, 30, 153),
        (True, False, 34, 171),
    ],
)
def test_real_registration_passes_the_boot_check(homelab, money, flows, activities):
    settings = _flags(homelab, money)
    wfs = workflows_for(settings)
    acts = _activities_for(settings)
    assert (len(wfs), len(acts)) == (flows, activities)
    check_registration(settings, wfs, acts, SEED_DIR)  # must not raise


# --------------------------------------------------------------------------
# falsifiability: one test per place a flow/activity has to appear
# --------------------------------------------------------------------------


def _expect(match: str, settings=None, workflows=None, activities=None, seed_dir=SEED_DIR):
    settings = settings or _flags()
    with pytest.raises(RegistrationError, match=match):
        check_registration(
            settings,
            workflows if workflows is not None else workflows_for(settings),
            activities if activities is not None else _activities_for(settings),
            seed_dir,
        )


def test_fails_when_a_flow_is_missing_from_the_registry(monkeypatch):
    """Place 1: the FlowSpec in registry.FLOWS."""
    monkeypatch.setattr(
        registry, "FLOWS", tuple(s for s in FLOWS if s.name != "ExpiryRadarFlow")
    )
    _expect("not declared in registry.FLOWS.*ExpiryRadarFlow")


def test_fails_when_a_flow_is_declared_twice(monkeypatch):
    dup = next(s for s in FLOWS if s.name == "CleanupFlow")
    monkeypatch.setattr(registry, "FLOWS", (*FLOWS, dup))
    _expect("declared twice")


def test_fails_when_the_registry_holds_a_non_workflow(monkeypatch):
    class NotAFlow:
        pass

    monkeypatch.setattr(registry, "FLOWS", (*FLOWS, FlowSpec(NotAFlow)))
    _expect("not a @workflow.defn class")


def test_fails_when_a_flow_is_missing_from_the_worker_list():
    """Place 2: the list handed to Worker(...). This is issue #188 exactly —
    under the old design this removal passed every registration test."""
    settings = _flags()
    crippled = [c for c in workflows_for(settings) if c.__name__ != "SocialPublishFlow"]
    _expect("workflows handed to Worker.*SocialPublishFlow", workflows=crippled)


def test_fails_when_an_activity_is_missing_from_the_worker_list():
    """Place 3: the activity instance in main()'s collect_activities(...) call.
    Dropping the instance drops all of its activities."""
    settings = _flags()
    crippled = [
        a
        for a in _activities_for(settings)
        if activity._Definition.must_from_callable(a).name
        not in {"find_curiosity_gaps", "check_curiosity_budget"}
    ]
    _expect(
        "activities handed to Worker.*find_curiosity_gaps",
        activities=crippled,
    )


def test_fails_when_a_flag_gated_activity_is_registered_with_the_flag_off():
    """The mirror direction: money activities served while money is disabled
    (the flows that call them are not registered)."""
    off = _flags(homelab=True, money=False)
    _expect(
        "activities handed to Worker.*unexpected=.*store_receipt_email",
        settings=off,
        activities=_activities_for(_flags()),
    )


def test_fails_when_a_schedulable_flow_has_no_seed_row(tmp_path):
    """Place 4: the row in config/seed/activities.yaml. Without it the flow is
    registered and mapped but nothing ever starts it."""
    rows = yaml.safe_load((SEED_DIR / "activities.yaml").read_text())["activities"]
    kept = [r for r in rows if r["workflow_type"] != "CuriosityCardFlow"]
    assert len(kept) < len(rows), "fixture did not actually remove a row"
    (tmp_path / "activities.yaml").write_text(yaml.safe_dump({"activities": kept}))
    _expect("CuriosityCardFlow has a schedule config but no row", seed_dir=tmp_path)


def test_fails_when_a_seed_row_names_a_flow_with_no_schedule_config(tmp_path):
    """Place 5: the schedule config on the FlowSpec. A seed row whose flow has
    no config builder is skipped by schedule_sync with only a warning."""
    rows = yaml.safe_load((SEED_DIR / "activities.yaml").read_text())["activities"]
    rows.append(
        {
            "slug": "typo-row",
            "workflow_type": "AgentTaskFlow",  # real flow, but never scheduled
            "agent_id": "sebas",
            "schedule_cron": "0 * * * *",
            "config": {},
            "active": True,
        }
    )
    (tmp_path / "activities.yaml").write_text(yaml.safe_dump({"activities": rows}))
    _expect("typo-row.*has no schedule config", seed_dir=tmp_path)


def test_missing_seed_file_warns_instead_of_failing(tmp_path):
    """A deployment may mount a different seed dir; that must not brick boot."""
    settings = _flags()
    check_registration(settings, workflows_for(settings), _activities_for(settings), tmp_path)
