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


def test_books_write_flow_is_gated_on_money_hygiene():
    """Issue #403: BooksWriteFlow's only activity, books_write, lives on
    MoneyActivities, which main() builds only when money_hygiene_enabled is
    on. An unflagged registration means the workflow schedules an activity no
    worker serves, and the task sits unassigned until the 540s timeout. The
    flow must be registered iff the flag is on."""
    spec = next(s for s in FLOWS if s.name == "BooksWriteFlow")
    assert spec.feature_flag == "money_hygiene_enabled"
    assert "BooksWriteFlow" in {c.__name__ for c in workflows_for(_flags(money=True))}
    assert "BooksWriteFlow" not in {c.__name__ for c in workflows_for(_flags(money=False))}


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
        # report_flow_health) — unflagged, so all three rows move. Then +2
        # activities and NO new flow from #225's stuck-post watchdog
        # (find_stuck_posts, report_stuck_posts on the existing
        # SocialActivities, driven by the existing SocialMetricsFlow). Then +2
        # more activities and again NO new flow from #182/#183
        # (sync_postiz_channels, retire_unpublishable_tasks — both on
        # SocialActivities, both driven by the existing SocialPublishFlow).
        # Then +1 activity and NO new flow from #215's `deliver_briefing`, which
        # renders the health block and sends it inside one activity so the
        # readings never become an argument or a result. Then +1 flow and +2
        # activities from AgentRunFlow / AgentRunActivities (launch_agent_run,
        # check_agent_run) — event-driven (dispatched by the
        # `dispatch_agent_run` chat tool), unflagged, so all three rows move.
        # Then +1 activity and NO new flow from #300's `cleanup_agent_run`,
        # which removes the run's per-run worktree on AgentRunFlow's terminal
        # paths (nothing else ever did, so every run leaked one). Then +1
        # activity and NO new flow from `ingest_idempotency_release` on the
        # existing ChannelActivities — RssIngestFlow hands a claim back when
        # `process_content` fails, so the entry is retried on the next poll
        # instead of being read as an already-handled dup. Then +1 activity and
        # NO new flow from #321's `find_dead_llm_purposes` — a third detector on
        # the existing FlowHealthActivities, driven by the existing
        # FlowHealthWatchdogFlow, so it moves in every row. Then +1 activity and
        # NO new flow from `resolve_comms_inbound_alert` on the existing
        # HomelabActivities — DeliveryWatchdogFlow closes the inbound-outage task
        # on recovery, which is what re-arms the one-open-task-at-a-time guard.
        # HomelabActivities is homelab-flagged, so only the homelab rows move.
        # Then +1 flow and +2 activities from MeetingNotesFlow /
        # MeetingActivities (fetch_meeting_document, analyse_meeting) — a child
        # of GmailIngestFlow's `meeting` tag fan-out, unflagged, so all three
        # rows move. Then +1 activity and NO new flow from
        # `gather_meeting_week` on the existing ReviewActivities — the weekly
        # meetings block is SQL aggregation driven by the existing
        # WeeklyReviewFlow, so it moves in every row. Then +1 activity and NO
        # new flow from `record_analysis_outcome` on the existing
        # MeetingActivities — MeetingNotesFlow stamps the analysis verdict back
        # onto the row it already filed, so the weekly block can warn about
        # meetings filed without a review. MeetingActivities is unflagged, so
        # it moves in every row. Then +1 flow and +2 activities from
        # MeetingSweepFlow (meeting_sender_addresses, unstored_meeting_messages
        # on the existing MeetingActivities) — the scheduled safety net for the
        # `meeting` fan-out, which finds notes mail the `is:unread` hourly query
        # never saw. Unflagged, so all three rows move. Then +4 activities and
        # NO new flow from the task-session lane: the three one-shot coding
        # activities (run_task_investigation, collect_coding_run,
        # run_task_implementation) are replaced by seven (load_task,
        # ensure_task_session, check_task_collision, launch_task_turn,
        # kill_task_turn, record_task_turn, find_task_turns_due) on the
        # existing AgentTaskActivities, which is unflagged — so all three rows
        # move by the same +4. Then +1 activity and NO new flow from
        # `set_task_slack_ref` on the same class: the coding path mirrors every
        # task message into one Slack thread per task, and this is what
        # remembers that thread's root on the session row. Then +1 activity and
        # NO new flow from `cleanup_work_sessions` on the existing
        # CleanupActivities — the finished-session worktree sweep is a step in
        # the existing CleanupFlow, and CleanupActivities is unflagged, so it
        # moves in every row. Then +2 activities and NO new flow from the
        # full-email-body path, and they land in DIFFERENT rows:
        # `fetch_message_body` is on the existing GmailActivities, which is
        # unflagged, so it moves all three rows; `store_receipt_body` is on the
        # existing MoneyActivities, which IS money-flagged, so it moves only
        # the money=True row. That is why this bump is +2/+1/+1, not +2/+2/+2.
        # Then +6 activities and NO new flow from the books lane, split the
        # same way: `capture_task`, `capture_due` and `complete_captured_task`
        # are on the existing CaptureActivities, which is unflagged, so they
        # move all three rows; `parse_money_email`, `post_money_event` and
        # `store_money_result` are on the money-flagged MoneyActivities, so
        # they move only the money=True row. Hence +6/+3/+3. No new flow —
        # MoneyProcessFlow already exists and these are its new steps.
        # Then +3 activities and NO new flow from the money brief / month close
        # data layer (`refresh_fx_prices`, `build_money_brief`,
        # `build_month_close`). All three are on the money-flagged
        # MoneyActivities, so only the money=True row moves: +3/+0/+0.
        # Then +2 flows and +4 activities from the rendering half of the same
        # lane: MoneyBriefFlow and MonthCloseFlow, plus render_money_brief,
        # render_month_close, notify_money_message and write_money_report on
        # the money-flagged MoneyActivities. Both flows are money-flagged too,
        # so again only the money=True row moves: +2 flows / +4 activities,
        # and +0/+0 for the other two.
        # Then MINUS 2 flows and MINUS 8 activities: the v1 subscription
        # tracker is deleted. MoneyHygieneDailyFlow and SubscriptionAuditFlow
        # go, and with them upsert_charges, classify_and_extract,
        # detect_cancellations, evaluate_renewal_alerts, notify_renewal_alert,
        # notify_cancellation, build_subscription_digest and
        # notify_subscription_digest. All eight are on the money-flagged
        # MoneyActivities and both flows are money-flagged, so — like every
        # bump above — only the money=True row moves: -2/-8, and 0 for the
        # other two, which never counted them in the first place.
        # Then +1 flow in every row and +1 activity in the money row only:
        # BooksWriteFlow carries a chat tool's books write on its own workflow
        # (issue #388) and the `books_write` activity it calls is on the
        # money-flagged MoneyActivities. So +1/+1/+1 flows and +1/+0/+0
        # activities. Then #403 gates BooksWriteFlow itself on
        # money_hygiene_enabled — its only activity is unserved when the flag
        # is off, so the flow must not be registered either — moving it OUT of
        # the money=False rows: −1 flow for those two, activities unchanged.
        # Then +1 flow and +1 activity in every row from the problem hub's
        # PR 2: HubSweepFlow and the two HubActivities it and the heartbeat
        # call (`promote_expired_suppressions`, `clear_converged_deploys`)
        # are unflagged, and the same PR deletes ActiveWorkActivities'
        # single `check_active_work` — so +1/+1/+1 flows, and +2−1 = +1
        # activities in each row.
        # Then +1 activity in every row from PR 3a: HubActivities gains
        # `project_pending` (the Todoist projector, unflagged). No new flow.
        # Then PR 3b: HubActivities gains ingest_alert, problem_status,
        # record_investigation, mute_problem and stale_stuck_problems (+5,
        # unflagged) while AlertActivities loses check_dedup,
        # find_open_task_for_signature, record_signature_recurrence,
        # record_signature_new_task, log_alert, check_alert_resolved and
        # get_verification_delay (−7, unflagged), AlertGovernanceActivities
        # loses check_alert_mute and write_alert_mute (−2, unflagged), and
        # HomelabActivities loses record_heartbeat_resolved (−1, homelab
        # flagged). So −4 in every row and one more off the two homelab rows.
        # ...plus HubActivities.verification_delay (+1, unflagged).
        # PR 4a: HubActivities.ingest_finding (+1, unflagged).
        # PR 4b: HubActivities.reconcile_findings (+1, unflagged) while
        # HomelabActivities loses alert_comms_inbound_down and
        # resolve_comms_inbound_alert (−2, homelab flagged).
        # PR 5a: AgentTaskActivities.reconcile_work_sessions (+1, unflagged) —
        # the session registry's liveness cross-check, run by the existing
        # AgentTaskSweepFlow. `check_task_collision` stays (same name, now a
        # registry lookup) and `cleanup_task_sessions` is renamed
        # `cleanup_work_sessions` (±0). No new flow.
        # PR 5b: HubActivities.record_plan (+1, unflagged) — a coding turn's
        # plan becomes subtasks through the projector. No new flow.
        # PR 6a: HubActivities gains `build_digest` and `close_resolved_problems`
        # (+2, unflagged) while AlertActivities loses `accumulate_digest_item`
        # and `build_alert_digest` (−2, unflagged) — the digest is a query over
        # `problem_events` now, not a settings buffer four branches appended
        # to. Net 0 in every row, and no new flow.
        # Then +3 activities and NO new flow from the hub's grouping step
        # (find_group_candidates, judge_group, apply_group), which rides
        # HubSweepFlow. Unflagged, so all three rows move.
        # The statement lane's tick (spec §14 step 7): StatementReconcileFlow
        # (+1 flow) and StatementActivities' intake_statements and
        # reconcile_statements (+2 activities). Both are money-flagged, so only
        # the money-on row moves.
        # Then +1 activity and NO new flow from #473:
        # HubActivities.reconcile_completed_tasks, a step on the existing
        # HubSweepFlow that resolves a problem whose task a person completed.
        # Unflagged, so all three rows move.
        # Then +2 activities and NO new flow from #344: `prepare_agent_ask`
        # (the `ask` verb's input) and `plan_infra_task` (the infra verb's
        # plan, by the problem behind the task) on the existing
        # AgentTaskActivities, which is unflagged — so all three rows move.
        # Then +1 activity and NO new flow from #501: `recent_auto_restart` on
        # the existing AlertActivities (was this problem restarted inside the
        # window?), a step of AlertInvestigationFlow. Unflagged, so all three
        # rows move.
        # Then +2 activities and NO new flow from #502: `follow_fix_pr` (a
        # GitHubAlertFlow step on a closed PR) and `verify_fixes` (a
        # HubSweepFlow step), both on the existing HubActivities. Unflagged,
        # so all three rows move.
        # Then +1 activity and NO new flow from #508: `load_tracked_topics` on
        # the existing IntelligenceActivities (the topics `track_topic` saves),
        # a step of IntelligenceScanFlow. Unflagged, so all three rows move.
        # Then +1 flow and +5 activities from #509: ResearchFlow (started by the
        # `research_topic` tool and by AgentTaskFlow's `research` verb) and the
        # new ResearchActivities class — research_gather, research_read,
        # research_synthesize, research_save, research_task_problem. Unflagged,
        # so all three rows move.
        # Then +5 activities and NO new flow from #511/#512: the feed record
        # (`load_gate_terms`, `record_feed_entries`, `record_feed_run` on
        # RssActivities), `store_feed_abstract` on ContentActivities and
        # `feed_review_line` on BriefingActivities. Unflagged, so all three
        # rows move.
        # Then +1 flow and +1 activity from #510: CalibreSyncFlow (daily, the
        # book index) and CalibreActivities.sync_calibre_library, a new class.
        # Unflagged, so all three rows move.
        # Then +1 activity and NO new flow from #513: `attach_topic_items` on
        # the existing IntelligenceActivities (tracked topics' rounds in the
        # hub), a step of IntelligenceScanFlow and RssIngestFlow. Unflagged, so
        # all three rows move.
        # Then +3 flows and +4 activities from #514, all unflagged: NotesWriteFlow
        # (a chat write), NotesSyncFlow (the hourly vault index, seeded) and
        # NotesBackfillFlow (hand-started), served by the new NotesActivities
        # class (notes_write, notes_journal_write, notes_index_vault,
        # notes_backfill_journal). All three rows move.
        # Then +1 flow and +1 activity from Maou's paper trading desk:
        # TradingDeskFlow and TradingDeskActivities.desk_tick, both on the money
        # flag, so only the money-on row moves.
        (True, True, 51, 246),
        (False, False, 40, 212),
        (True, False, 44, 228),
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
