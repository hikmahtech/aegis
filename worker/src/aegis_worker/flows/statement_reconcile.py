"""StatementReconcileFlow — the statement lane, on a schedule (spec §14 step 7).

Everything this lane does was already built and nothing ran it: intake, the
matcher and step 5's posting were driven from one-off scripts inside the worker
container. This is the tick that makes the lane a lane.

Three steps, in the only order that is safe:

1. **intake** — walk the Drive folders and store what parses. Idempotent on
   `row_id`, so running daily over a folder that gains a file monthly costs a
   Drive listing and nothing else.
2. **reconcile** — match every stored row against the journal, post each
   statement that is not yet reconciled, and move that account's watermark.
   The whole match is one activity: 2,580 rows and their candidates are not a
   Temporal payload.
3. **findings** — hand the run to the problem hub (§15.4). A new finding earns
   a Todoist task, and a finding that has gone since the last tick resolves
   itself. There is no alert wiring here and there should not be: the hub owns
   that, and hand-rolling it is what §15.4 replaced.

`post` defaults to FALSE. The first run of a schedule must not write to the
books unasked — the operator turns posting on in `activities.config` once a
dry run has shown what it would do.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis_worker.shared.retry import FAST, NO_RETRY

#: Drive listing plus a PDF parse per file. Generous: a 49-page FY statement
#: takes seconds and the whole folder is under twenty files.
_INTAKE = timedelta(minutes=10)
#: The match is CPU over a few thousand rows; the post spawns hledger under an
#: flock the books writer may already hold.
_RECONCILE = timedelta(minutes=15)
_FAST = timedelta(seconds=60)


@dataclass
class StatementReconcileConfig:
    agent_id: str = "maou"
    #: Write to the books. Off until an operator has read a dry run.
    post: bool = False
    #: ISO date. Statements whose period starts before it are matched and
    #: reported but never posted. The Drive folder holds every statement the
    #: bank ever sent, including an FY2024-25 Axis statement of 1,619 rows that
    #: predates the books by two years — posting that is a decision about what
    #: the ledger is for, not something a schedule does because the file is
    #: there. Empty means no limit.
    since: str = ""
    #: Send the digest to the agent's channel.
    silent: bool = False


@workflow.defn(name="StatementReconcileFlow")
class StatementReconcileFlow:
    @workflow.run
    async def run(self, config: StatementReconcileConfig) -> dict:
        intake = await workflow.execute_activity(
            "intake_statements",
            args=[False],
            start_to_close_timeout=_INTAKE,
            retry_policy=FAST,
        )

        # NO_RETRY: this writes the books. A retry after a partial write is the
        # one thing `books._write`'s revert cannot protect against, because the
        # second attempt sees the first attempt's commit as history rather than
        # as its own work. A failure here is for a human to read.
        reconcile = await workflow.execute_activity(
            "reconcile_statements",
            args=[config.post, config.since],
            start_to_close_timeout=_RECONCILE,
            retry_policy=NO_RETRY,
        )

        # Empty on all but one tick a month: the activity holds the marker and
        # decides when the digest is due (#464). The flow's only say is
        # `silent`, which is about delivery, not cadence.
        sent = False
        digest = reconcile.get("digest") or ""
        if digest and not config.silent:
            # `sent` reports delivery, not dispatch — the same distinction
            # MoneyBriefFlow makes.
            sent = bool(
                await workflow.execute_activity(
                    "notify_money_message",
                    args=[digest, "statement_digest_notify_failed"],
                    start_to_close_timeout=_FAST,
                    retry_policy=NO_RETRY,
                )
            )

        return {
            "stored": intake.get("stored", 0),
            "intake_failures": len(intake.get("failures") or []),
            "posted": reconcile.get("posted", 0),
            "promoted": reconcile.get("promoted", 0),
            "statements": reconcile.get("statements", 0),
            "findings": reconcile.get("findings", {}),
            "sent": sent,
        }
