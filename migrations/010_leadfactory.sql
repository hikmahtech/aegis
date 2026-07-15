-- Lead Factory (real-estate lead-gen service) — P0 groundwork.
-- Multi-client by design: leadfactory.clients is the tenant (a client website/
-- brand like manasrealty.com — the first client of the service); everything
-- else hangs off it. Onboarding another website is data entry, not a build.
-- The bot may only ever state facts present in leadfactory.projects; anything
-- else is a human handoff. Design reference:
-- ~/Workspace/real-estate/lead-factory-build-reference.html + BUILD-PLAN.md.

CREATE SCHEMA IF NOT EXISTS leadfactory;

-- The tenant: a client website/business whose leads this service runs.
-- Per-client WhatsApp credentials as Fernet stored-secret dicts via
-- aegis.crypto ({value, encrypted} jsonb), same convention as social_accounts.
CREATE TABLE IF NOT EXISTS leadfactory.clients (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    slug                text UNIQUE NOT NULL,          -- 'manasrealty'
    name                text NOT NULL,
    domain              text,                          -- 'manasrealty.com'
    wa_phone_number_id  text UNIQUE,                   -- Cloud API number id — inbound routing key
    waba_id             text,
    meta_page_id        text,                          -- CTWA / leadform routing key
    meta_ad_account_id  text,
    wa_token_enc        jsonb,                         -- {value, encrypted} via aegis.crypto
    partner_phones      text[] DEFAULT '{}' NOT NULL,  -- digest recipients, 1:1 (Cloud API has no groups)
    broker_phones       text[] DEFAULT '{}' NOT NULL,  -- broker-command allowlist (Flow E)
    timezone            text DEFAULT 'Asia/Kolkata' NOT NULL,
    active              boolean DEFAULT true NOT NULL,
    created_at          timestamptz DEFAULT now() NOT NULL,
    updated_at          timestamptz DEFAULT now() NOT NULL
);

-- The catalogue: what a client sells, and the only facts the bot may state.
CREATE TABLE IF NOT EXISTS leadfactory.projects (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    client_id       bigint NOT NULL REFERENCES leadfactory.clients(id),
    slug            text NOT NULL,                     -- 'malhar-24-east'
    name            text NOT NULL,
    rera_no         text NOT NULL,                     -- on every ad + page (MahaRERA mandate)
    locality        text NOT NULL,
    configs         text[] DEFAULT '{}' NOT NULL,      -- {'1BHK','2BHK'}
    price_min_lakh  integer,
    price_max_lakh  integer,                           -- the approved band, nothing finer
    possession_by   date,
    cost_sheet_url  text,                              -- the gated artifacts we send
    floorplan_url   text,
    site_pin_url    text,                              -- Maps pin for visit reminders
    status          text DEFAULT 'active' NOT NULL,
    meta            jsonb DEFAULT '{}'::jsonb NOT NULL, -- extra approved facts
    created_at      timestamptz DEFAULT now() NOT NULL,
    UNIQUE (client_id, slug)
);

-- One row per buyer per client; "where is this person right now".
CREATE TABLE IF NOT EXISTS leadfactory.leads (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    client_id          bigint NOT NULL REFERENCES leadfactory.clients(id),
    project_id         bigint REFERENCES leadfactory.projects(id),
    phone              text NOT NULL,                  -- E.164
    name               text,
    language           text DEFAULT 'en' NOT NULL,     -- en | hi | mr — bot mirrors it
    -- attribution: stamped at birth, never edited (Law 5)
    source             text NOT NULL CHECK (source IN
                         ('meta_leadform','ctwa','gbp','organic','referral','manual')),
    utm_source         text,
    utm_medium         text,
    utm_campaign       text,
    meta_campaign_id   text,
    meta_adset_id      text,
    meta_ad_id         text,
    ctwa_clid          text,
    -- the three qualifying answers
    budget_min_lakh    integer,
    budget_max_lakh    integer,
    timeline           text CHECK (timeline IS NULL OR timeline IN
                         ('ready','3m','6m','12m_plus')),
    preferred_locality text,
    -- state machine: one lead, one state, always. Every non-terminal state
    -- sets next_action_at — the tick flow alerts on leads with no alarm.
    state              text DEFAULT 'NEW' NOT NULL CHECK (state IN
                         ('NEW','QUALIFYING','QUALIFIED','VISIT_BOOKED','VISITED',
                          'NEGOTIATING','LONG_TAIL','DORMANT','DISQUALIFIED',
                          'CLOSED_WON','CLOSED_LOST')),
    next_action        text,                           -- e.g. 'send_nudge_N2'
    next_action_at     timestamptz,                    -- the alarm clock
    owner              text DEFAULT 'machine' NOT NULL, -- machine | broker
    opted_out          boolean DEFAULT false NOT NULL, -- STOP: instant, forever
    created_at         timestamptz DEFAULT now() NOT NULL,
    updated_at         timestamptz DEFAULT now() NOT NULL,
    UNIQUE (client_id, phone)
);

-- The tick's one query: leads whose alarm is due.
CREATE INDEX IF NOT EXISTS leads_next_action_idx
    ON leadfactory.leads (next_action_at) WHERE next_action_at IS NOT NULL;

-- Every WhatsApp message, both directions.
CREATE TABLE IF NOT EXISTS leadfactory.messages (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    lead_id        bigint NOT NULL REFERENCES leadfactory.leads(id),
    direction      text NOT NULL CHECK (direction IN ('in','out')),
    template       text,                               -- 'T1','N2'…; NULL = session msg
    body           text,
    wa_message_id  text,
    status         text,                               -- sent | delivered | read | failed
    payload        jsonb,                              -- raw webhook / send payload
    at             timestamptz DEFAULT now() NOT NULL
);

CREATE INDEX IF NOT EXISTS messages_lead_idx
    ON leadfactory.messages (lead_id, at);

-- Webhook replays are idempotent on the WA message id.
CREATE UNIQUE INDEX IF NOT EXISTS messages_wa_id_idx
    ON leadfactory.messages (wa_message_id) WHERE wa_message_id IS NOT NULL;

-- The diary: append-only, trigger-enforced (disputes are settled by the log).
CREATE TABLE IF NOT EXISTS leadfactory.lead_events (
    id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    lead_id  bigint NOT NULL REFERENCES leadfactory.leads(id),
    actor    text NOT NULL,           -- machine | arshad | broker | buyer
    event    text NOT NULL,           -- created | state_change | visit_booked |
                                      -- visit_done | no_show | won | lost |
                                      -- note | consent | optout | override
    detail   jsonb,                   -- {"from":"QUALIFYING","to":"QUALIFIED"} …
    at       timestamptz DEFAULT now() NOT NULL
);

CREATE INDEX IF NOT EXISTS lead_events_lead_idx
    ON leadfactory.lead_events (lead_id, at);

CREATE OR REPLACE FUNCTION leadfactory.forbid_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'leadfactory.lead_events is append-only';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS lead_events_append_only ON leadfactory.lead_events;
CREATE TRIGGER lead_events_append_only
    BEFORE UPDATE OR DELETE ON leadfactory.lead_events
    FOR EACH ROW EXECUTE FUNCTION leadfactory.forbid_mutation();

-- Monday digest audit: generated from the views below, never hand-compiled.
CREATE TABLE IF NOT EXISTS leadfactory.digest_log (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    client_id   bigint NOT NULL REFERENCES leadfactory.clients(id),
    week_start  date NOT NULL,
    payload     jsonb NOT NULL,
    sent_at     timestamptz DEFAULT now() NOT NULL,
    UNIQUE (client_id, week_start)
);

-- Metrics are views — never stored, always recomputable from the diary.
CREATE OR REPLACE VIEW leadfactory.v_response_times AS
SELECT l.id AS lead_id,
       l.client_id,
       l.created_at,
       min(m.at) FILTER (WHERE m.direction = 'out') - l.created_at AS first_response
FROM leadfactory.leads l
LEFT JOIN leadfactory.messages m ON m.lead_id = l.id
GROUP BY l.id, l.client_id, l.created_at;

-- "Ever reached" flags come from the diary, not current state, so a lead that
-- qualified and later closed still counts as qualified in its cohort week.
CREATE OR REPLACE VIEW leadfactory.v_funnel_by_adset AS
SELECT client_id,
       meta_campaign_id,
       meta_adset_id,
       date_trunc('week', created_at) AS week,
       count(*)                               AS leads,
       count(*) FILTER (WHERE ever_qualified) AS qualified,
       count(*) FILTER (WHERE ever_booked)    AS visits_booked,
       count(*) FILTER (WHERE ever_visited)   AS visits_done
FROM (
    SELECT l.*,
           EXISTS (SELECT 1 FROM leadfactory.lead_events e
                   WHERE e.lead_id = l.id AND e.event = 'state_change'
                     AND e.detail->>'to' = 'QUALIFIED')                 AS ever_qualified,
           EXISTS (SELECT 1 FROM leadfactory.lead_events e
                   WHERE e.lead_id = l.id AND e.event = 'visit_booked') AS ever_booked,
           EXISTS (SELECT 1 FROM leadfactory.lead_events e
                   WHERE e.lead_id = l.id AND e.event = 'visit_done')   AS ever_visited
    FROM leadfactory.leads l
) flagged
GROUP BY client_id, meta_campaign_id, meta_adset_id, week;

-- Ships disabled — same kill-switch convention as social_publishing_enabled.
INSERT INTO settings (key, value) VALUES
    ('leadfactory_enabled', 'false'::jsonb)
    ON CONFLICT (key) DO NOTHING;
