-- Per-agent routing metadata (Phase 3 de-hardcode). Holds intent_keywords,
-- knowledge_domains, tool_set, etc. so chat routing is data-driven from the
-- agents table instead of hardcoded dicts. The in-code dicts remain as the
-- shipped example-agent defaults / fallback.
ALTER TABLE agents ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}';
