-- Drop knowledge_source_quality (audit-confirmed inert): nothing in code or
-- migrations ever INSERTs/UPDATEs it, so it has 0 rows in prod and every
-- consumer (chat.py auto_confidence gating, admin knowledge-health list)
-- always fell through to its hardcoded default. Re-add when a producer
-- actually exists. Table is EMPTY in prod — this drop is data-safe.
DROP TABLE IF EXISTS public.knowledge_source_quality;
