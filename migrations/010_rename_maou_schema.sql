-- Rename schema maou -> finance (issue #43): decouple the finance data model
-- from the character name. Existing deploys: catalog-only rename, instant;
-- tables, indexes and constraints follow the schema automatically.
-- Fresh installs: 001_baseline.sql now creates `finance` directly, so the
-- rename is skipped. If both schemas somehow exist, ALTER fails loudly
-- rather than silently splitting data across two schemas.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.schemata WHERE schema_name = 'maou') THEN
        ALTER SCHEMA maou RENAME TO finance;
    END IF;
END $$;
