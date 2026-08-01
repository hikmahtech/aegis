-- 018: life.expiring_items — the expiry radar's registry. Anything with a
-- renewal/expiry date lives here: passport, visa, driving licence, insurance
-- policy, warranty, medication refill, domain name, vehicle inspection.
--
-- Lives in the `life` schema next to life.people (016) and life.observations
-- (017) — same domain, same backup story.
--
-- `kind` is an open string (passport | visa | licence | insurance | warranty |
-- medication | domain | asset_service | ...), NOT an enum, matching how
-- public.resources.kind and public.infra.kind are plain text with a
-- convention. A new kind of document must never need a migration.
--
-- `lead_days` is per-item because the useful warning distance differs wildly:
-- a passport wants months (renewal takes weeks), a medication refill wants
-- days. Default {30,7,1}.
--
-- `person_id` is ON DELETE SET NULL, not RESTRICT: deleting a person from
-- life.people must keep working (services/people.delete_person issues a bare
-- DELETE and would otherwise raise ForeignKeyViolation → HTTP 500), and the
-- document itself is still worth tracking with the owner detached.
--
-- `asset_id` is deliberately a bare uuid with NO foreign key — life.assets
-- does not exist yet (planned as C7). Adding the constraint later is one
-- ALTER TABLE; blocking this table on that one would be the wrong order.
--
-- Retention: DELIBERATELY NOT registered with the cleanup flow, like
-- life.people and unlike life.observations. Two reasons:
--   1. expiring_items is user-curated, and an item whose expiry is long past
--      is the single most important row in the table — it is the document
--      that still needs renewing. Age-pruning would delete exactly the alarm
--      the radar exists to raise.
--   2. expiring_item_alerts is the dedup ledger, and per migration 013 the
--      fix for duplicate alerts (#113, 166 rows in 3 weeks) was an existence
--      check across ALL time. Age-pruning that ledger re-arms every already-
--      fired threshold, reintroducing precisely the bug 013 closed.
-- Growth is bounded either way: items × thresholds × renewal cycles, i.e.
-- low hundreds of rows a year for a person tracking 50 documents. Do NOT add
-- either table to _TIMESTAMP_COLUMNS / _DEFAULT_RETENTIONS in
-- aegis_worker.{activities,flows}.cleanup.

CREATE SCHEMA IF NOT EXISTS life;

CREATE TABLE IF NOT EXISTS life.expiring_items (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Open string, lowercased by the service layer.
    kind       text NOT NULL,
    title      text NOT NULL,
    -- A calendar date, not a timestamp: "my passport expires on the 4th" is
    -- timezone-free, and the radar compares it against today's date.
    expires_on date NOT NULL,
    -- Days-before-expiry at which to warn. Normalised by the service layer to
    -- a de-duplicated, descending list of non-negative integers.
    lead_days  integer[] NOT NULL DEFAULT '{30,7,1}',
    person_id  uuid REFERENCES life.people(id) ON DELETE SET NULL,
    -- Forward reference to life.assets (C7). No FK yet — see header.
    asset_id   uuid,
    notes      text,
    metadata   jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- The radar's only hot query: "what expires within N days", ordered.
CREATE INDEX IF NOT EXISTS idx_life_expiring_items_expires_on
    ON life.expiring_items (expires_on);

-- "everything belonging to this person" — the birthday/person view, and the
-- lookup the ON DELETE SET NULL trigger walks.
CREATE INDEX IF NOT EXISTS idx_life_expiring_items_person
    ON life.expiring_items (person_id) WHERE person_id IS NOT NULL;

-- Dedup ledger: one row per (item, threshold, expiry cycle) that has ever
-- fired. C6's daily flow checks for existence here BEFORE alerting.
--
-- The dedup key deliberately includes `expires_on` rather than a day-bucket
-- of `fired_at` (the shape that caused #113) or a single sticky
-- `last_alert_threshold` column (the shape pandoras_actor.cert_expiry still
-- uses). Keying on the expiry cycle means: fires at most once per threshold
-- per cycle, forever; and renewing the item — i.e. moving `expires_on`
-- forward — re-arms every threshold automatically with no extra bookkeeping.
CREATE TABLE IF NOT EXISTS life.expiring_item_alerts (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    item_id        uuid NOT NULL REFERENCES life.expiring_items(id) ON DELETE CASCADE,
    threshold_days integer NOT NULL,
    -- The item's expires_on AT FIRE TIME. Part of the dedup key, so it is
    -- stored, not joined for.
    expires_on     date NOT NULL,
    fired_at       timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_life_expiring_item_alerts_dedup
    ON life.expiring_item_alerts (item_id, threshold_days, expires_on);
