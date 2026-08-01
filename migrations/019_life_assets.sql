-- 019: life.assets — the household/asset registry. Cars, appliances, home
-- systems: the physical things that get bought, warrantied and serviced.
--
-- Lives in the `life` schema next to life.people (016), life.observations
-- (017) and life.expiring_items (018) — same domain, same backup story.
--
-- This generalises the `public.infra` table pattern (slug + name + open
-- `kind` + metadata jsonb) WITHOUT inheriting infra's encrypted-credentials,
-- SSH and provisioning machinery: a washing machine is data, not an
-- actuation target. If some future asset genuinely needs a secret (a
-- smart-lock PIN), add the column then rather than speculatively now.
--
-- `kind` is an open string (car | appliance | hvac | plumbing | electronics
-- | ...), NOT an enum, matching public.infra.kind, public.resources.kind and
-- life.expiring_items.kind. A new kind of thing must never need a migration.
--
-- Manuals and receipts deliberately do NOT get a column or a child table:
-- they belong in the existing public.resources / knowledge store, by
-- convention tagged `asset:<slug>`. (Note: as of this migration nothing
-- queries `resources.tags` server-side — the convention is storage-only, see
-- the PR for C7. Wiring a tag-filtered read is a separate, opt-in change.)
--
-- Service-due feed: services/assets.py mirrors an asset with both
-- `service_interval_days` and `last_serviced_at` set into a
-- life.expiring_items row (kind='asset_service', asset_id set,
-- expires_on = last_serviced_at + service_interval_days) so the daily expiry
-- radar picks it up with no new flow. Marking the asset serviced again moves
-- expires_on forward, which re-arms every alert threshold for free (the
-- dedup key in life.expiring_item_alerts includes expires_on).
--
-- Retention: DELIBERATELY NOT registered with the cleanup flow, like
-- life.people (016) and life.expiring_items (018), and unlike the
-- machine-written life.observations (017). This table is user-curated: rows
-- are typed in by hand, one per physical object owned, and the oldest row is
-- the boiler installed in 2009 — precisely the asset whose service history
-- matters most. Growth is bounded by how many things a household owns (tens),
-- not by time. Do NOT add life.assets to _TIMESTAMP_COLUMNS /
-- _DEFAULT_RETENTIONS in aegis_worker.{activities,flows}.cleanup.

CREATE SCHEMA IF NOT EXISTS life;

CREATE TABLE IF NOT EXISTS life.assets (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Stable handle used by the `asset:<slug>` tagging convention, so a
    -- renamed asset does not orphan its manuals. Derived from `name` and
    -- de-duplicated by the service layer (mirrors services/infra.py).
    slug                  text NOT NULL UNIQUE,
    name                  text NOT NULL,
    -- Open string, lowercased by the service layer.
    kind                  text NOT NULL,
    -- Calendar dates, not timestamps: "bought on the 4th" is timezone-free,
    -- same reasoning as life.expiring_items.expires_on.
    purchase_date         date,
    warranty_until        date,
    -- Both NULL-able and only meaningful together — the service-due mirror
    -- fires only when BOTH are set.
    service_interval_days integer,
    last_serviced_at      date,
    location              text,
    notes                 text,
    metadata              jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now()
);

-- The registry's only filtered read: "show me the cars".
CREATE INDEX IF NOT EXISTS idx_life_assets_kind ON life.assets (kind);

-- ---------------------------------------------------------------------------
-- Wire up life.expiring_items.asset_id, which migration 018 declared as a
-- bare uuid because life.assets did not exist yet.
-- ---------------------------------------------------------------------------

-- Any asset_id written before this migration points at a table that did not
-- exist, so it dangles by construction and the FK below would refuse to
-- validate. Null those pointers out — a uuid referencing nothing is already
-- meaningless, and the item itself is untouched.
UPDATE life.expiring_items
   SET asset_id = NULL
 WHERE asset_id IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM life.assets a WHERE a.id = life.expiring_items.asset_id);

-- ON DELETE SET NULL, not the default RESTRICT — exactly the reasoning
-- migration 018 applied to person_id. services/assets.delete_asset issues a
-- bare `DELETE FROM life.assets`; a RESTRICT foreign key here would turn
-- "delete an asset that owns a tracked document" into an unhandled
-- asyncpg.ForeignKeyViolationError, i.e. HTTP 500 on the Assets page. And it
-- must not be CASCADE either: a hand-written warranty row that merely
-- REFERENCES the asset is user data and must survive with the link detached.
--
-- The one row that should NOT survive is the machine-generated
-- kind='asset_service' mirror — with asset_id NULLed it could never be
-- refreshed or re-linked, so it would nag forever about a thing you no longer
-- own. delete_asset deletes those explicitly before dropping the asset.
--
-- No `ADD CONSTRAINT IF NOT EXISTS` in Postgres 16, hence the guard: this
-- file must stay safe to re-run.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'life_expiring_items_asset_id_fkey'
           AND conrelid = 'life.expiring_items'::regclass
    ) THEN
        ALTER TABLE life.expiring_items
            ADD CONSTRAINT life_expiring_items_asset_id_fkey
            FOREIGN KEY (asset_id) REFERENCES life.assets(id) ON DELETE SET NULL;
    END IF;
END
$$;

-- Indexes the FK's SET NULL sweep and the service-due mirror's lookup
-- ("does this asset already have an asset_service row?") both walk. Mirrors
-- idx_life_expiring_items_person from 018.
CREATE INDEX IF NOT EXISTS idx_life_expiring_items_asset
    ON life.expiring_items (asset_id) WHERE asset_id IS NOT NULL;
