-- 016: the `life` schema + its first table, life.people — a registry of the
-- humans in the user's life (name, aliases, relationship, key dates, notes,
-- last contact).
--
-- Why a new schema rather than `public`: this follows the existing per-domain
-- precedent (`finance`, `pandoras_actor` from 001_baseline.sql). Life entities
-- are a distinct domain from AEGIS's own operational tables, and later
-- additions (observations, expiring documents, household assets) land here
-- too, so `\dt`, backups and retention sweeps stay organised by domain.
--
-- Why not extend `resources`/`infra`: both carry infra-specific columns
-- (ssh_user, docker_context, encrypted credentials) that make no sense for a
-- person.
--
-- Retention: DELIBERATELY NOT registered with the cleanup flow. Every row here
-- is hand-entered (or, later, enriched) user-curated data, not an ops log —
-- pruning it by age would silently delete the people the user talks to least,
-- which is the exact opposite of what a contact registry is for. Growth is
-- bounded by the number of humans the user knows. Do NOT add `life.people` to
-- _TIMESTAMP_COLUMNS / _DEFAULT_RETENTIONS in aegis_worker.{activities,flows}.cleanup.

CREATE SCHEMA IF NOT EXISTS life;

CREATE TABLE IF NOT EXISTS life.people (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name         text NOT NULL,
    -- Other names this person is known by: nicknames, maiden names, and (from
    -- the passive-enrichment pass) email addresses. Stored LOWERCASED by the
    -- service layer so the GIN index below answers case-insensitive lookups
    -- with a plain `aliases @> ARRAY[$1]` containment probe.
    aliases      text[] NOT NULL DEFAULT '{}',
    -- Free-form ('brother', 'colleague', 'GP') — deliberately not an enum so a
    -- new kind of relationship needs no migration.
    relationship text,
    -- {"birthday": "1985-04-02", "anniversary": "2016-11-19", ...}. ISO dates
    -- as strings; a birthday whose year is unknown is written "--04-02".
    key_dates    jsonb NOT NULL DEFAULT '{}'::jsonb,
    notes        text,
    -- Last time the user and this person were in contact. NULL = never
    -- recorded. Hand-editable now; maintained automatically once email/
    -- calendar enrichment lands.
    last_contact timestamptz,
    metadata     jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);

-- "find the person called X" — case-insensitive exact lookup by name.
CREATE INDEX IF NOT EXISTS idx_life_people_name_lower ON life.people (lower(name));

-- "find the person whose alias/email is X" — array containment.
CREATE INDEX IF NOT EXISTS idx_life_people_aliases ON life.people USING gin (aliases);
