-- #675: every area story the morning brief showed, the Slack message it was
-- posted as, and the owner's verdict on it (a 👍/👎 reaction). The judge reads
-- recent verdicts as examples; the monthly scorecard reads the whole table.
CREATE TABLE IF NOT EXISTS area_stories (
    id          bigserial PRIMARY KEY,
    story_key   text        NOT NULL UNIQUE,
    area        text        NOT NULL,
    title       text        NOT NULL,
    url         text        NOT NULL DEFAULT '',
    why         text        NOT NULL DEFAULT '',
    shown_at    timestamptz NOT NULL DEFAULT now(),
    -- The story's own Slack message (a reply in the brief's thread); NULL when
    -- the brief went somewhere with no message ref.
    channel     text,
    ts          text,
    verdict     text CHECK (verdict IN ('up', 'down')),
    verdict_at  timestamptz
);
CREATE INDEX IF NOT EXISTS area_stories_message ON area_stories (channel, ts);
CREATE INDEX IF NOT EXISTS area_stories_area_shown ON area_stories (area, shown_at);
