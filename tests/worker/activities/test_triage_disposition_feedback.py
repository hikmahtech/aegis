"""The Todoist-disposition feedback signal (2026-08).

Before this, the triage feedback loop could only learn in ONE direction. Its
"unimportant" branch requires Gmail's IMPORTANT and STARRED to both be absent —
but AEGIS stamps IMPORTANT on every important_* verdict itself, so the branch
was unreachable in practice: 76 corrections in production, 100% of them
unimportant→important, zero the other way, ever. Every correction relearned the
sender as `important_read`. Triage could only get noisier over time.

The negative signal was already in the database: when the user closes an
AEGIS-created `#email` task carrying `#trash` or `@reference`, they have said
"this needed nothing from me". In production that was 77 of 188 completed
captures — a 41% wrong-to-interrupt rate nothing read.

Real Postgres, no mocks — the whole point is a three-table join.
"""

from __future__ import annotations

import uuid

import pytest
from aegis_worker.activities.gmail import GmailActivities


def _acts(db_pool) -> GmailActivities:
    return GmailActivities(
        gmail_credentials_file="/tmp/x.json",
        gmail_token_dir="/tmp",
        db_pool=db_pool,
        agent_id="sebas",
    )


async def _seed(db_pool, *, email_id: str, sender: str, labels: list[str], completed: bool):
    """One triage prediction with a captured Todoist task hanging off it."""
    task_id = f"task-{uuid.uuid4().hex[:12]}"
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO triage_accuracy (email_id, predicted) VALUES ($1, 'important_action')",
            email_id,
        )
        await conn.execute(
            "INSERT INTO todoist_tasks (id, content, description, labels, is_completed, "
            "source_tag) VALUES ($1, $2, $3, $4, $5, '#email')",
            task_id,
            "Account Activity: New Sign-In detected",
            f"From: {sender}\n\n[Open in Gmail](https://mail.google.com/x)",
            labels,
            completed,
        )
        await conn.execute(
            "INSERT INTO todoist_capture_idempotency (source_tag, external_id, todoist_task_ref) "
            "VALUES ('#email', $1, $2)",
            f"gmail-{email_id}",
            task_id,
        )
    return task_id


async def _accuracy(db_pool, email_id: str):
    return await db_pool.fetchrow(
        "SELECT actual, corrected_by FROM triage_accuracy WHERE email_id=$1", email_id
    )


@pytest.mark.asyncio
async def test_trashed_task_records_a_correction_and_demotes_the_sender(db_pool):
    """The signal the loop never had: a `#trash` closure is the user saying the
    interrupt was wrong. Fails if the mining, the join, or the relearn breaks.
    """
    email_id = f"m-{uuid.uuid4().hex[:10]}"
    sender = f"no-reply-{uuid.uuid4().hex[:6]}@accounts.google.com"
    await _seed(db_pool, email_id=email_id, sender=sender, labels=["#email", "#trash"], completed=True)

    out = await _acts(db_pool)._mine_todoist_dispositions()

    assert out["corrected"] >= 1
    row = await _accuracy(db_pool, email_id)
    assert row["actual"] == "unimportant"
    assert row["corrected_by"] == "user_todoist"

    state = await db_pool.fetchrow(
        "SELECT state FROM triage_state WHERE email_addr=$1", sender
    )
    assert state is not None, "the sender must be taught, not just the prediction scored"
    assert state["state"] == "informational"


@pytest.mark.asyncio
async def test_reference_closure_counts_too(db_pool):
    """`@reference` means "filed, nothing to do" — the same verdict as trash."""
    email_id = f"m-{uuid.uuid4().hex[:10]}"
    await _seed(
        db_pool,
        email_id=email_id,
        sender=f"noreply-{uuid.uuid4().hex[:6]}@linkedin.com",
        labels=["#email", "@reference"],
        completed=True,
    )
    await _acts(db_pool)._mine_todoist_dispositions()
    assert (await _accuracy(db_pool, email_id))["actual"] == "unimportant"


@pytest.mark.asyncio
async def test_open_task_is_not_a_verdict(db_pool):
    """An uncompleted task is work in flight, not a judgement. Fails if the
    mining ever starts reading an open task as "you were wrong", which would
    demote senders the moment a task was tagged.
    """
    email_id = f"m-{uuid.uuid4().hex[:10]}"
    await _seed(
        db_pool,
        email_id=email_id,
        sender=f"boss-{uuid.uuid4().hex[:6]}@company.com",
        labels=["#email", "@reference"],
        completed=False,
    )
    await _acts(db_pool)._mine_todoist_dispositions()
    assert (await _accuracy(db_pool, email_id))["actual"] is None


@pytest.mark.asyncio
async def test_task_completed_as_real_work_is_not_a_correction(db_pool):
    """Closing a task normally means AEGIS was RIGHT to raise it. Only the
    noise labels count — fails if completion alone starts demoting senders,
    which would train the system to ignore the mail it got right.
    """
    email_id = f"m-{uuid.uuid4().hex[:10]}"
    sender = f"billing-{uuid.uuid4().hex[:6]}@aws.amazon.com"
    await _seed(
        db_pool,
        email_id=email_id,
        sender=sender,
        labels=["#email", "@next", "@sebas"],
        completed=True,
    )
    await _acts(db_pool)._mine_todoist_dispositions()
    assert (await _accuracy(db_pool, email_id))["actual"] is None
    assert await db_pool.fetchrow("SELECT 1 FROM triage_state WHERE email_addr=$1", sender) is None


@pytest.mark.asyncio
async def test_already_scored_prediction_is_left_alone(db_pool):
    """Each prediction records exactly one correction. Fails if a prediction the
    Gmail-label pass already scored gets re-mined, double-teaching the sender.
    """
    email_id = f"m-{uuid.uuid4().hex[:10]}"
    await _seed(
        db_pool,
        email_id=email_id,
        sender=f"x-{uuid.uuid4().hex[:6]}@y.com",
        labels=["#email", "#trash"],
        completed=True,
    )
    await db_pool.execute(
        "UPDATE triage_accuracy SET actual='important', corrected_by='user_gmail' "
        "WHERE email_id=$1",
        email_id,
    )
    await _acts(db_pool)._mine_todoist_dispositions()
    row = await _accuracy(db_pool, email_id)
    assert (row["actual"], row["corrected_by"]) == ("important", "user_gmail")
