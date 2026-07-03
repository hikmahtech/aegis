# Social Publishing — build guide

Todoist-scheduled social media posting with human approval. A Todoist task *is* a post:
labels pick the platforms, the due datetime is the publish time, and nothing goes out
until the user approves a card in the comms channel (Slack/Telegram).

This doc is the implementation plan. It reuses aegis primitives everywhere it can —
the only genuinely new things are a `social_accounts` token table, one connector, and
one flow. Status: **MVP built (X/Twitter only)** — migration `005_social.sql`,
`connectors/social.py`, `routes/social_auth.py`, `flows/social_publish.py` +
`activities/social.py`. Ships disabled (`social_publishing_enabled=false`); flip the
settings row and connect an X account (`/api/admin/social/x/connect?label=…`) to go live.

Implementation deviations from the plan below (each for a concrete reason):

- **Migration is `005_social.sql`** (002–004 were taken by the time this landed), and
  the token columns are `jsonb`, not text — they hold `aegis.crypto` stored-secret
  dicts (`{value, encrypted}`) directly.
- **Approval cards are spawned ABANDONED with a `post_resolve_activity` hook**
  (`apply_social_approval`), not awaited: Temporal schedules default to overlap=SKIP,
  so a tick blocked on a human for hours would starve every later tick (same reason
  ClarifyFlow spawns abandoned children). The deterministic child id
  `social-approve-<task_id>` dedupes cards across ticks; approve applies
  enqueue→post→complete inside the hook, and the scheduled flow's drain/complete
  steps are the retry net.
- **Skip strips the `@publish` label** (via todoist_outbox + optimistic projection
  update) instead of doing nothing — a plain "do nothing" would re-card the still-due
  task every 5 minutes. Re-adding the label re-arms the post.

## What it does (target behavior)

1. You create a Todoist task: content = the post text, description = link/long text,
   labels = `@publish` + platform labels (`@x`, `@linkedin`, `@facebook`, `@youtube`),
   due datetime = when it should go out.
2. `SocialPublishFlow` (scheduled every 5 min) finds due `@publish` tasks in the
   already-mirrored `todoist_tasks` table.
3. For each, it spawns an `InteractionFlow` card: post preview + [Approve] [Skip].
4. On approve, the post is queued in `social_outbox` and published to each labeled
   platform; the Todoist task is completed via the existing `todoist_outbox`.
5. Failures retry with attempt counting (same semantics as `todoist_outbox`); a post
   that keeps failing surfaces as a failed outbox row, never a silent drop.

Out of scope (deliberately): Reddit/HN (karma is anti-automation by design — stay
manual), personal LinkedIn/Facebook profiles (no API; browser automation risks the
account), Medium (no API since ~2023; a later Playwright script via
`RemoteScriptConnector` if ever), media generation.

## Platform reality — the part that gates everything

Each platform requires **a developer app you register yourself** (BYO app, exactly like
the Google OAuth client — see `core/src/aegis/services/google_oauth.py` for why: the
maintainer's app can't be committed and wouldn't authorize forkers). Budget real
calendar time for the approvals.

| Platform | App to create | Auth model | Token lifetime | Gotchas |
|---|---|---|---|---|
| X (Twitter) | developer.x.com project+app | OAuth 2.0 PKCE, scopes `tweet.read tweet.write users.read offline.access` | access 2h, refresh token rotates **on every refresh** — must persist the new one atomically | Free tier ≈ 500 posts/mo — fine for this use. `POST /2/tweets`. |
| LinkedIn (company page) | developer app + request **Community Management API** (or legacy w_organization_social via Marketing tier) | OAuth 2.0 auth-code | access ~60 days, programmatic refresh only with approved access | The app-approval process is the long pole (days–weeks). Post via `POST /rest/posts` with `author: urn:li:organization:<id>`. |
| Facebook Page | developers.facebook.com app, `pages_manage_posts` | OAuth → exchange to **long-lived Page token** | Page token effectively non-expiring (if obtained from a long-lived user token) | App review needed to leave dev mode; only Pages, never profiles. `POST /<page-id>/feed`. |
| YouTube | Google Cloud project, YouTube Data API v3 | Google OAuth (can **reuse the existing google client + reauth flow** with an added scope) | refresh token, standard Google | Upload quota ≈ 6 videos/day default; videos upload as `resumable` POST. |

Because lifetimes and refresh semantics differ per platform, tokens live in a table
with `expires_at` + refresh-at-use — not in env vars.

## Architecture (all existing seams)

```
Todoist task (@publish + @x, due 9:00)
        │  (already mirrored by TodoistSyncFlow every 5 min → todoist_tasks)
        ▼
SocialPublishFlow (cron */5)                          worker/flows/social_publish.py
  find_due_posts ──► InteractionFlow card ──► user taps Approve in Slack
        │                                             (existing HITL primitive)
        ▼
  social_outbox row(s), one per platform             migrations/005_social.sql
        ▼
  drain_social_outbox ──► SocialConnector.post(...)  core/connectors/social.py
        │                    (refresh token first if expires_at near)
        ▼
  complete Todoist task via todoist_outbox           existing pattern
```

## Build steps

### 1. Migration — `migrations/005_social.sql`

Auto-applies on core boot (`core/src/aegis/db/pool.py` owns `schema_migrations`).
Idempotent SQL, modeled on `todoist_outbox` (baseline:559) and the `infra` table:

```sql
CREATE TABLE IF NOT EXISTS social_accounts (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    platform     text NOT NULL,              -- 'x' | 'linkedin' | 'facebook' | 'youtube'
    label        text NOT NULL,              -- 'hikmah' | 'personal' — supports multiple accounts per platform
    access_token_enc  text,                  -- Fernet via aegis.crypto (settings.secret_key)
    refresh_token_enc text,
    expires_at   timestamptz,
    meta         jsonb DEFAULT '{}'::jsonb NOT NULL,  -- page_id, org_urn, channel_id, scopes…
    updated_at   timestamptz DEFAULT now() NOT NULL,
    UNIQUE (platform, label)
);

CREATE TABLE IF NOT EXISTS social_outbox (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    todoist_task_id text,                    -- provenance, nullable (API-created posts later)
    account_id   bigint NOT NULL REFERENCES social_accounts(id),
    payload      jsonb NOT NULL,             -- {text, link, media_refs…} — connector renders per platform
    status       text DEFAULT 'pending' NOT NULL,  -- pending|posted|failed
    attempt_count integer DEFAULT 0 NOT NULL,
    last_attempt_at timestamptz,
    posted_ref   text,                       -- platform post id/url after success
    created_at   timestamptz DEFAULT now() NOT NULL
);
CREATE INDEX IF NOT EXISTS social_outbox_pending_idx
    ON social_outbox (created_at) WHERE status = 'pending';

INSERT INTO settings (key, value) VALUES
    ('social_publishing_enabled', 'false'::jsonb),
    ('social_publish_label',      '"publish"'::jsonb),
    ('social_platform_labels',    '{"x":"x","linkedin":"linkedin","facebook":"facebook","youtube":"youtube"}'::jsonb)
    ON CONFLICT (key) DO NOTHING;
```

Ships **disabled** (`social_publishing_enabled=false`) — same kill-switch convention as
`todoist_capture_enabled` (baseline:1334). Label names are settings, not hardcoded,
per the OSS convention (config is edited, not coded — CLAUDE.md).

### 2. OAuth client config — extend `CONFIG_REGISTRY`

Add per-platform app credentials to `CONFIG_REGISTRY` in
`core/src/aegis/services/integrations_config.py` (stored encrypted under
`integration:*`, editable in the admin Integrations page, `_HIDDEN_KEYS`/prefix
protection already covers `integration:*`):

```python
ConfigKey("x_client_id",        "OAuth client id",     "X (Twitter)", False),
ConfigKey("x_client_secret",    "OAuth client secret", "X (Twitter)", True),
ConfigKey("linkedin_client_id",     "OAuth client id",     "LinkedIn", False),
ConfigKey("linkedin_client_secret", "OAuth client secret", "LinkedIn", True),
ConfigKey("facebook_app_id",     "App id",     "Facebook", False),
ConfigKey("facebook_app_secret", "App secret", "Facebook", True),
```

Plus matching optional fields on `Settings` (`core/src/aegis/config.py`, snake_case,
no `aegis_` prefix in Python). YouTube needs no new client — reuse `google_oauth` and
add the YouTube upload scope during reauth.

### 3. Connect-account routes — copy the gmail_reauth shape

`core/src/aegis/api/routes/social_auth.py`, modeled on
`core/src/aegis/api/routes/gmail_reauth.py`:

- `GET /api/admin/social/{platform}/connect?label=hikmah` → build the platform's
  authorize URL (PKCE for X), stash state, redirect.
- `GET /api/admin/social/{platform}/callback` → exchange code, encrypt tokens with
  `aegis.crypto.encrypt_secret`, upsert into `social_accounts`. **Unlike Gmail's
  legacy token files, tokens go in the table.**
- `GET /api/admin/social/accounts` → list platform/label/expires_at (never token
  values) for the admin page.

One-time per account. Facebook's flow additionally exchanges the short-lived user
token for a long-lived Page token (`meta.page_id` chosen at connect time).

### 4. Connector — `core/src/aegis/connectors/social.py`

Extends `HTTPConnector` (`core/src/aegis/connectors/_base.py`; set
`connector_name = "social"` for `connector_calls` audit rows). One public method:

```python
async def post(self, account: SocialAccount, payload: dict) -> str:
    """Publish payload to account's platform; returns platform post ref."""
```

Internally: `_refresh_if_needed(account)` (compare `expires_at` against now+5min,
POST the platform's token endpoint, **persist rotated refresh token before using
it** — X invalidates the old one), then dispatch to `_post_x` / `_post_linkedin` /
`_post_facebook` / `_post_youtube`. MVP implements `_post_x` only; each platform
after is one method (~30–60 lines).

Wire in `worker/src/aegis_worker/bootstrap.py` (connectors dict — worker DI lives
here, not in core) and inject into the activities dataclass in
`worker/src/aegis_worker/__main__.py`, same as TodoistConnector.

### 5. Flow — `worker/src/aegis_worker/flows/social_publish.py`

Per the CLAUDE.md "new scheduled flow" recipe. Config dataclass with `agent_id: str`
**first** (interceptor requirement):

```python
@dataclass
class SocialPublishConfig:
    agent_id: str
    lookahead_minutes: int = 10
```

Workflow steps (activities in `worker/src/aegis_worker/activities/social.py`):

1. `find_due_posts` — SELECT from `todoist_tasks` WHERE not completed, labels
   contain the publish label, and due time ≤ now + lookahead. **Note:**
   `todoist_tasks.due_date` is a `date` column; the post *time* comes from
   `raw->'due'->>'datetime'` when present, else default to a configured hour of the
   due date. Skip tasks that already have `social_outbox` rows (idempotency).
2. For each due task: spawn `InteractionFlow` as a child workflow
   (`worker/src/aegis_worker/flows/interaction.py`, `InteractionFlowInput(agent_id,
   kind="choice", origin="social_publish", prompt=<preview>, options={approve/skip},
   timeout_policy="archive")`) and await the result. Skipped or timed-out → do
   nothing (task stays until its labels change or it's completed by hand).
3. On approve: `enqueue_outbox` — one `social_outbox` row per platform label,
   resolving `(platform, label)` → `social_accounts` row.
4. `drain_social_outbox` — pick pending rows, call `SocialConnector.post()`,
   mark `posted` + `posted_ref`, or bump `attempt_count` (give up → `failed` after 5,
   mirroring todoist_outbox semantics).
5. `complete_todoist_task` — enqueue an `item_complete` command via the existing
   todoist outbox (`worker/src/aegis_worker/activities/todoist.py` pattern); the
   5-min TodoistSyncFlow drains it.

Wrap step failures as
`ApplicationError(f"social_publish_failed at step=X: {exc!r}", non_retryable=True)`
per convention.

### 6. Registration (nothing is auto-discovered)

- Add `SocialPublishFlow` to `WORKFLOWS` and the stub-bound activity methods to
  `ACTIVITIES` in `worker/src/aegis_worker/__main__.py`.
- Add `"SocialPublishFlow"` to `_ACTIVITY_TYPE_MAP` in
  `worker/src/aegis_worker/schedule_sync.py`.
- Seed in `config/seed/activities.yaml`:

```yaml
  - slug: social-publish-5min
    workflow_type: SocialPublishFlow
    agent_id: sebas
    schedule_cron: "*/5 * * * *"
    config: {}
    active: true
```

- Gate execution on the `social_publishing_enabled` settings row (check it first in
  the flow; exit early when false) so the seed can ship active but inert.

### 7. Tests

Convention per CLAUDE.md: `ActivityEnvironment` + `respx` for the activities (mock
platform endpoints; assert refresh-token rotation is persisted before use),
`WorkflowEnvironment.start_time_skipping()` + `Worker` for the flow (assert: due task
→ interaction spawned; approve → outbox row; skip → nothing). Run with
`... 2>&1 | tee logs/test-social.log`.

## Build order (MVP = X only, end to end)

1. Migration + settings/CONFIG_REGISTRY entries.
2. X developer app (manual, your side) + connect/callback routes; verify a
   `social_accounts` row appears with `expires_at`.
3. `SocialConnector._post_x` + refresh; prove one post from a script/test.
4. Flow + registration; prove: Todoist task `@publish @x` due now → Slack card →
   approve → tweet → task completed.
5. Then, in order of API sanity: Facebook Page → YouTube (reuse google client) →
   LinkedIn (start its app-approval paperwork on day 1 — it's the slowest).

Each subsequent platform is: app registration (manual) + one `_post_*` method + one
label. No new tables, flows, or routes.

## Deferred / known ceilings

- **Media:** `payload.media_refs` exists in the schema but MVP posts text+link only.
  Platform media upload (chunked for X, resumable for YouTube) lands per-platform
  when needed.
- **Threads/multi-post:** one task = one post. A thread is N tasks.
- **Medium/browser platforms:** a Playwright script run via `RemoteScriptConnector`
  writing back into `social_outbox` — only if republishing there ever matters enough.
- **Cross-posting articles from the websites:** later, a small `POST /api/social/queue`
  route can insert `social_outbox` rows directly, bypassing Todoist — the outbox is
  already the seam.
