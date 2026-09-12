-- What each RSS feed delivered, one row per entry (#511).
--
-- Nothing recorded which channel an RSS entry came from: `ingest_idempotency`
-- keys on the entry id alone, and the knowledge row carries only the tag
-- `rss`. So "is this feed worth keeping?" had no answer. This table gives one.
-- An entry's `content_id` is the knowledge row it produced (sha1 of its URL,
-- services/knowledge.py::_content_id_for), and joining it to
-- `knowledge_injection_log.content_ids` says whether a prompt ever used it.
--
-- `mode` is what happened to the entry: `full` (the page was fetched and
-- stored), `abstract` (title and summary only, no fetch — #512) or `failed`.
--
-- The backfill attributes what RSS already stored by host: an entry of
-- https://arxiv.org/rss/cs.AI lives on arxiv.org. A feed whose links point
-- somewhere else (Hacker News) gets nothing, and its history starts now.
--
-- Idempotent: the runner keys on the filename and re-runs a renamed file.
CREATE TABLE IF NOT EXISTS feed_entries (
    channel_id  uuid NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    external_id text NOT NULL,
    link        text NOT NULL DEFAULT '',
    content_id  text,
    mode        text NOT NULL CHECK (mode IN ('full', 'abstract', 'failed')),
    published   text NOT NULL DEFAULT '',
    seen_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (channel_id, external_id)
);

CREATE INDEX IF NOT EXISTS feed_entries_seen_idx ON feed_entries (channel_id, seen_at);
CREATE INDEX IF NOT EXISTS feed_entries_content_idx ON feed_entries (content_id);

INSERT INTO feed_entries (channel_id, external_id, link, content_id, mode, seen_at)
SELECT ch.id, c.url, c.url, c.content_id, 'full', c.ingested_at
FROM knowledge_content c
JOIN channels ch
  ON ch.kind = 'rss'
 AND lower(split_part(c.url, '/', 3)) = lower(split_part(ch.identifier, '/', 3))
WHERE 'rss' = ANY(c.tags)
  AND c.url LIKE 'http%'
ON CONFLICT DO NOTHING;
