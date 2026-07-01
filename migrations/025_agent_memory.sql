-- Per-agent writable memory (Phase 4: memory that learns). Agents accumulate
-- durable lessons from human corrections and surface them in their chat system
-- prompt. Plain-text rows so memory stays human-auditable / prunable.
CREATE TABLE IF NOT EXISTS agent_memory (
    id         BIGSERIAL PRIMARY KEY,
    agent_id   TEXT NOT NULL REFERENCES agents (id),
    content    TEXT NOT NULL,
    importance REAL NOT NULL DEFAULT 0.5,
    source     TEXT NOT NULL DEFAULT 'correction',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS agent_memory_agent_idx
    ON agent_memory (agent_id, importance DESC, created_at DESC);
