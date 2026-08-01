-- 017: life.observations — a generic, append-only store for life metrics:
-- weight, sleep hours, steps, a home-sensor temperature, a location ping.
-- One row per reading; whatever the reading needs beyond a number lives in
-- `metadata` (unit, device id, lat/lon, …).
--
-- Why generic rather than a table per metric: every one of these is
-- (source, metric, value, when) and nothing else. A weight table and a
-- temperature table would be the same DDL twice, and each new signal would
-- need a migration before it could be recorded at all.
--
-- Lives in the `life` schema next to life.people (migration 016) — same
-- domain, same backup/retention story.
--
-- `value` is nullable on purpose: an observation can be purely categorical
-- (a location ping, a door-open event) with everything in `metadata`. The
-- aggregate helpers in services/observations.py ignore NULL values, per
-- normal SQL aggregate semantics.
--
-- Retention: DELIBERATELY PRUNED, unlike life.people. These rows are written
-- by machines (ingest flows, sensors, phone pings) at an unbounded rate, and
-- an old individual reading has no standalone value once it has left the
-- trend window. Registered at 365 days in BOTH
-- aegis_worker.flows.cleanup._DEFAULT_RETENTIONS and
-- aegis_worker.activities.cleanup._TIMESTAMP_COLUMNS (the allowlist derives
-- from the latter, so an entry in only one of them is a silent no-op).
-- Pruned by `observed_at`, not `created_at`: the row's age is when the
-- reading happened, so a backfilled two-year-old reading is dropped on the
-- next sweep instead of lingering a year past its relevance.
--
-- Known trade-off of a 365-day window: a strict year-on-year comparison
-- ("this weight vs the same week last year") loses its far end. Accepted —
-- bounded growth matters more for a machine-written table, and the window is
-- config, not code (CleanupConfig.retentions / activities.config in the DB).

CREATE SCHEMA IF NOT EXISTS life;

CREATE TABLE IF NOT EXISTS life.observations (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Who produced the reading ('withings', 'phone', 'home-assistant',
    -- 'manual'). Stored lowercased by the service layer.
    source      text NOT NULL,
    -- What was measured ('weight_kg', 'sleep_hours', 'steps'). Open string,
    -- lowercased by the service layer so lookups are case-insensitive
    -- without a functional index.
    metric      text NOT NULL,
    value       numeric,
    -- When the reading happened (NOT when the row was written — see
    -- created_at for that). Every query and the retention sweep use this.
    observed_at timestamptz NOT NULL,
    metadata    jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Trend/summary queries: "this metric over this window", ordered by time.
CREATE INDEX IF NOT EXISTS idx_life_observations_metric_time
    ON life.observations (metric, observed_at);

-- Per-source sweeps: "everything the phone sent since X".
CREATE INDEX IF NOT EXISTS idx_life_observations_source_time
    ON life.observations (source, observed_at);
