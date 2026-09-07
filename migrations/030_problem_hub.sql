-- Problem hub (docs/superpowers/specs/2026-09-07-problem-hub-design.md, PR 1).
--
-- One `problems` row per thing that is wrong; every signal about it is a
-- `problem_events` row. The Todoist task is a projection of the problem (PR 3),
-- never its identity, which is what lets an occurrence dedupe against a
-- problem that has no task yet, or whose task was closed.
--
-- `service_state` (deploy/maintenance windows) ships with PR 2 and the
-- `work_sessions` widening with PR 5: each table lands with its first reader.

CREATE TABLE IF NOT EXISTS problems (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- '{class}:{subject_kind}:{subject}' from services/hub.py::correlation_key.
    -- '' means "could not be correlated": such a problem is created, never
    -- attached to, and is exempt from the open-key uniqueness below.
    correlation_key text NOT NULL,
    class           text NOT NULL,
    subject         text NOT NULL DEFAULT '',
    subject_kind    text NOT NULL DEFAULT '',
    title           text NOT NULL,
    severity        text NOT NULL DEFAULT 'warning',
    status          text NOT NULL DEFAULT 'open',
    first_seen_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at    timestamptz NOT NULL DEFAULT now(),
    occurrences     int NOT NULL DEFAULT 1,
    muted_until     timestamptz,
    resolved_at     timestamptz,
    closed_at       timestamptz,
    todoist_task_id text,
    github_issue    text,
    metadata        jsonb NOT NULL DEFAULT '{}'::jsonb
);

-- One open problem per key. A resolved problem still holds its key until it is
-- closed, so an occurrence inside the reopen window reopens it and one after
-- the window closes it and starts a new problem (hub.py::decide).
CREATE UNIQUE INDEX IF NOT EXISTS problems_open_key
    ON problems (correlation_key)
    WHERE closed_at IS NULL AND correlation_key <> '';
CREATE INDEX IF NOT EXISTS problems_status_last_seen
    ON problems (status, last_seen_at DESC);

CREATE TABLE IF NOT EXISTS problem_events (
    id           bigserial PRIMARY KEY,
    problem_id   uuid NOT NULL REFERENCES problems(id),
    source       text NOT NULL,
    -- Idempotency within the source: a producer retrying the same signal
    -- attaches nothing twice. Occurrence ids must therefore differ per
    -- occurrence (fingerprint + start time), not per alert rule.
    external_id  text NOT NULL,
    kind         text NOT NULL,
    severity     text,
    payload      jsonb NOT NULL DEFAULT '{}'::jsonb,
    occurred_at  timestamptz NOT NULL DEFAULT now(),
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source, external_id)
);
CREATE INDEX IF NOT EXISTS problem_events_problem
    ON problem_events (problem_id, occurred_at DESC);

CREATE TABLE IF NOT EXISTS problem_links (
    problem_id  uuid NOT NULL REFERENCES problems(id),
    link_kind   text NOT NULL,   -- problem | todoist_task | github_issue | github_pr | workflow_run | interaction | work_session
    ref         text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (problem_id, link_kind, ref)
);
