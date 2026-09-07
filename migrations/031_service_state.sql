-- Problem hub, PR 2: what is happening to a service right now.
--
-- An occurrence the hub ingests while its subject is `deploying` or in
-- `maintenance` is recorded and counted but not projected (no task, no card),
-- and the problem it creates sits in status `suppressed` until the window
-- passes — at which point `hub.promote_expired_suppressions` opens it, because
-- the deploy did not make it go away. Writers: the Ansible deploy role (over
-- POST /api/hub/service-state), the `set_service_state` chat tool, and the
-- heartbeat, which clears a `deploying` row once the service has converged.
--
-- `subject = '*'` is a wildcard: a whole-kind window (`subject_kind = 'node'`)
-- or, with `subject_kind = '*'`, everything — a planned power cut.
CREATE TABLE IF NOT EXISTS service_state (
    subject      text NOT NULL,
    subject_kind text NOT NULL DEFAULT 'service',
    state        text NOT NULL,           -- deploying | maintenance | degraded | ok (ok = row deleted)
    until_at     timestamptz,             -- NULL = until cleared
    set_by       text NOT NULL,           -- 'ansible' | 'chat:<agent>' | 'api' | ...
    note         text NOT NULL DEFAULT '',
    updated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (subject, subject_kind)
);
