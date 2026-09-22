"""RecordSeedFlow — first drafts of the owner's record (vault record spec §12).

Started by hand from the admin Vault page ("Draft the record",
`POST /api/admin/notes/record/seed`); no schedule and no activities row. Three
drafters, each run as its capability's holder, write `<dir>/<name>.draft.md`
once through the vault writer; a note that already has a draft gets none.
Then ONE message to the owner lists what was drafted. No cards: he accepts a
draft by renaming it, and nothing here watches for that.
"""

from __future__ import annotations

from dataclasses import dataclass

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis.agent_tags import GENERALIST_TAG
    from aegis.errors import error_text, logged_failure
    from aegis.services.record_seed import seed_message

    from aegis_worker.activities.agent_registry import AgentRegistryActivities
    from aegis_worker.activities.delivery import DeliveryActivities
    from aegis_worker.shared.retry import NO_RETRY, TIMEOUT_FAST, TIMEOUT_LLM, TIMEOUT_STANDARD

# (result key, activity, the capability whose holder drafts it)
_DRAFTERS = (
    ("general", "record_seed_general", GENERALIST_TAG),
    ("money", "record_seed_money", "finance"),
    ("interests", "record_seed_interests", "research"),
)


@dataclass
class RecordSeedConfig:
    # The run's owner, who gets the message; empty = the gtd holder.
    agent_id: str = ""


@workflow.defn(name="RecordSeedFlow")
class RecordSeedFlow:
    @workflow.run
    async def run(self, config: RecordSeedConfig) -> dict:
        resolved = await workflow.execute_activity_method(
            AgentRegistryActivities.resolve_agents,
            args=[[tag for _, _, tag in _DRAFTERS]],
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=NO_RETRY,
        ) or {}
        owner = config.agent_id or str(resolved.get(GENERALIST_TAG) or "")
        results: dict[str, dict] = {}
        for key, name, tag in _DRAFTERS:
            agent = str(resolved.get(tag) or "")
            if not agent:
                results[key] = {"status": "no_holder", "reason": f"no agent holds {tag}", "written": []}
                continue
            # NO_RETRY: a rerun is harmless (a draft is written once), and an
            # automatic retry would pay for the model call again.
            try:
                results[key] = await workflow.execute_activity(
                    name, args=[agent], start_to_close_timeout=TIMEOUT_LLM, retry_policy=NO_RETRY
                )
            except Exception as exc:  # noqa: BLE001 — one drafter must not stop the others
                results[key] = {"status": "error", "reason": error_text(exc, 200), "written": []}
        written = [p for r in results.values() for p in (r.get("written") or [])]
        if owner and written:
            with logged_failure("record_seed_message_failed", logger=workflow.logger):
                await workflow.execute_activity_method(
                    DeliveryActivities.send_message,
                    args=[owner, seed_message(results)],
                    start_to_close_timeout=TIMEOUT_STANDARD,
                    retry_policy=NO_RETRY,
                )
        return {"status": "ok", "written": written, "drafters": results}
