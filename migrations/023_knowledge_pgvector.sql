-- Native pgvector knowledge subsystem — replaces the external knowledge-service.
-- Embedding dim 768 matches the default model nomic-embed-text; change the model
-- => re-create this column at the new dim and re-embed (see docs/design open-platform).

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS knowledge_content (
    content_id  TEXT PRIMARY KEY,
    url         TEXT,
    title       TEXT,
    source_type TEXT,
    summary     TEXT,
    tags        TEXT[]      NOT NULL DEFAULT '{}',
    metadata    JSONB       NOT NULL DEFAULT '{}',
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS knowledge_content_source_type_idx
    ON knowledge_content (source_type, ingested_at DESC);

CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id          BIGSERIAL PRIMARY KEY,
    content_id  TEXT NOT NULL REFERENCES knowledge_content (content_id) ON DELETE CASCADE,
    chunk_index INT  NOT NULL DEFAULT 0,
    chunk_text  TEXT NOT NULL,
    embedding   vector(768)
);

CREATE INDEX IF NOT EXISTS knowledge_chunks_content_id_idx
    ON knowledge_chunks (content_id);

-- HNSW cosine index for ANN search. Built lazily; fine for the personal-scale corpus.
CREATE INDEX IF NOT EXISTS knowledge_chunks_embedding_idx
    ON knowledge_chunks USING hnsw (embedding vector_cosine_ops);
