"""Activity-to-schedule synchronization.

Reads active activities from the database and registers them as
Temporal schedules. Idempotent — safe to call on every worker startup.
"""

from __future__ import annotations

import asyncpg
import structlog
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleSpec,
    ScheduleUpdate,
    ScheduleUpdateInput,
)

from aegis_worker.registry import activity_type_map, feature_flagged_types

logger = structlog.get_logger()

# Map activity type -> (workflow class, config builder). Derived from the ONE
# registry in aegis_worker/registry.py — a flow declared there is schedulable
# here automatically, and check_registration() at worker boot refuses to start
# if the two ever disagree.
_ACTIVITY_TYPE_MAP = activity_type_map()

# Activity types whose owning flow is only registered on the worker behind a
# feature flag (registry.FlowSpec.feature_flag). Their seed rows ship
# active=true, so without this gate schedule_sync would create Temporal
# schedules that fire against a workflow type the worker never registered.
# Keyed by the settings flag -> the types it guards. When the flag is off we
# skip the row (and, since it never enters expected_ids, the prune pass deletes
# any stale schedule — so toggling a flag off cleans up too).
_FEATURE_FLAGGED_TYPES = feature_flagged_types()


def _disabled_by_feature_flag(act_type: str, settings: object | None) -> str | None:
    """Return the settings flag name gating `act_type` if it's off, else None.

    settings=None (e.g. some tests) means "don't gate" — behaves as before.
    """
    if settings is None:
        return None
    for flag, types in _FEATURE_FLAGGED_TYPES.items():
        if act_type in types and not getattr(settings, flag, False):
            return flag
    return None


async def sync_schedules(
    client: Client,
    pool: asyncpg.Pool,
    task_queue: str = "aegis-main",
    settings: object | None = None,
) -> int:
    """Sync Temporal schedules from active activities in the database.

    Returns the number of schedules registered.

    When settings is provided, type-specific defaults are injected into the
    activity config — e.g. cert_radar domains fall back to
    settings.homelab_public_domains so a freshly-seeded activity row with
    an empty config still probes the right domains.
    """
    import dataclasses
    import hashlib
    import json

    # Fetch active activities with cron schedules (v3 schema)
    rows = await pool.fetch(
        "SELECT id, slug, workflow_type, agent_id, schedule_cron, config "
        "FROM activities WHERE active = TRUE AND schedule_cron IS NOT NULL"
    )

    registered = 0
    expected_ids = set()

    for row in rows:
        act = dict(row)
        act_name = act["slug"]
        act_type = act["workflow_type"]
        cron = act["schedule_cron"]

        # Skip flows whose owning feature flag is off — the worker didn't
        # register the workflow type, so a schedule for it would only error.
        gated_off = _disabled_by_feature_flag(act_type, settings)
        if gated_off:
            logger.info(
                "schedule_skipped_feature_off",
                activity=act_name,
                type=act_type,
                flag=gated_off,
            )
            continue

        # Parse config
        config = act.get("config")
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except (json.JSONDecodeError, TypeError):
                config = {}
        act["config"] = config or {}

        # Settings-backed defaults for homelab flows
        if (
            settings is not None
            and act_type == "CertRadarFlow"
            and not act["config"].get("domains")
        ):
            fallback = getattr(settings, "homelab_public_domains", None) or []
            if fallback:
                act["config"]["domains"] = list(fallback)

        # Expose selected settings fields to mappers (Gmail/Receipt reauth link,
        # comms service URL). Mappers read via act["_settings"].get(...).
        act["_settings"] = {
            "aegis_ui_url": getattr(settings, "aegis_ui_url", "") if settings else "",
            "comms_url": (
                getattr(settings, "comms_url", "") if settings else ""
            ),
        }

        # Map to workflow + config
        mapper = _ACTIVITY_TYPE_MAP.get(act_type)
        if not mapper:
            logger.warning("schedule_unknown_type", activity=act_name, type=act_type)
            continue

        workflow_cls, flow_config = mapper(act)
        schedule_id = act_name
        expected_ids.add(schedule_id)

        # Fingerprint of everything the schedule is built from, embedded in
        # the action's workflow-id prefix. describe() hands that prefix back,
        # so an unchanged schedule is skipped instead of rewritten — before
        # this, every ~5-min tick rewrote all schedules unconditionally
        # (issue #11: schedule_updated log spam + Temporal history churn).
        fp = hashlib.sha1(
            json.dumps(
                {
                    "cron": cron,
                    "wf": workflow_cls.__name__,
                    "cfg": dataclasses.asdict(flow_config),
                },
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()[:10]
        action_id = f"scheduled-{schedule_id}--v{fp}"

        schedule = Schedule(
            action=ScheduleActionStartWorkflow(
                workflow_cls.run,
                args=[flow_config],
                task_queue=task_queue,
                id=action_id,
            ),
            spec=ScheduleSpec(cron_expressions=[cron]),
        )

        try:
            # Try to update existing schedule — skip when nothing changed.
            handle = client.get_schedule_handle(schedule_id)
            desc = await handle.describe()
            if getattr(desc.schedule.action, "id", None) == action_id:
                registered += 1
                continue

            async def _updater(_input: ScheduleUpdateInput, s=schedule) -> ScheduleUpdate:
                return ScheduleUpdate(schedule=s)

            await handle.update(_updater)
            logger.info("schedule_updated", schedule_id=schedule_id)
        except Exception:
            # Create new schedule
            try:
                await client.create_schedule(schedule_id, schedule)
                logger.info("schedule_created", schedule_id=schedule_id, cron=cron)
            except Exception as e:
                logger.warning("schedule_create_failed", schedule_id=schedule_id, error=str(e))
                continue

        registered += 1

    # Prune orphaned schedules
    try:
        async for sched in await client.list_schedules():
            if sched.id not in expected_ids:
                try:
                    handle = client.get_schedule_handle(sched.id)
                    await handle.delete()
                    logger.info("schedule_deleted_orphan", schedule_id=sched.id)
                except Exception as exc:
                    logger.warning(
                        "schedule_delete_orphan_failed",
                        schedule_id=sched.id,
                        error=str(exc),
                    )
    except Exception as exc:
        logger.warning("schedule_prune_failed", error=str(exc))

    logger.info("schedule_sync_complete", registered=registered, total_activities=len(rows))
    return registered
