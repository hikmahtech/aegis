-- 021: life.observations.external_id — the dedup key for readings that come
-- from an external system with its own stable id (a wearable vendor's daily
-- summary, a phone location ping, a health-app push).
--
-- Why a column + unique index rather than a read-then-write check in Python:
-- a poll flow re-reads an overlapping window on every tick BY DESIGN (that is
-- how a missed or half-written run heals). "SELECT then INSERT if absent" is a
-- race between two overlapping runs — both read nothing, both insert. The
-- unique index makes Postgres the arbiter: the second INSERT loses to
-- ON CONFLICT DO NOTHING and the caller learns it was a duplicate from an
-- empty RETURNING, not from a prior SELECT.
--
-- Why (source, metric, external_id) and not (external_id) alone: one vendor
-- record commonly yields several metrics (an Oura daily_activity row carries
-- both a score and a step count), and two vendors can hand out the same id.
-- The triple is what is actually unique.
--
-- PARTIAL (WHERE external_id IS NOT NULL): every observation written before
-- this migration, and every hand/sensor reading that has no external id, keeps
-- NULL and is exempt. Postgres treats NULLs in a unique index as distinct
-- anyway, so the WHERE clause is about index size, not correctness — but it
-- also documents the intent: only externally-keyed rows are deduped.
--
-- Nothing writes external_id unless it opts in: `record_observation` leaves it
-- NULL, `record_external_observation` (services/observations.py) sets it.

ALTER TABLE life.observations
    ADD COLUMN IF NOT EXISTS external_id text;

CREATE UNIQUE INDEX IF NOT EXISTS uq_life_observations_external
    ON life.observations (source, metric, external_id)
    WHERE external_id IS NOT NULL;
