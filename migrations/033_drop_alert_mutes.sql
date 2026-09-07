-- Problem hub, PR 4a (migration 033): the last two readers of `alert_mutes` (the flow-health
-- and stuck-post watchdogs) now mute through `problems.muted_until`, like
-- the alert pipeline since PR 3b. Four key namespaces in one PK column,
-- none of them documented together, are gone with it.
DROP TABLE IF EXISTS alert_mutes;
