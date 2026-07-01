# Local Development

## Prerequisites

- Python 3.12+
- Docker + Docker Compose
- Node.js 18+ (for admin panel)

## Quick Start

```bash
# 1. Clone and setup
python -m venv .venv && source .venv/bin/activate
pip install -e "core[dev]" -e "worker[dev]" -e "comms[dev]"

# 2. Start infrastructure
docker compose up -d postgres redis temporal temporal-ui

# 3. Start Core API (runs migrations + serves admin panel)
python -m aegis

# 4. Start Worker (registers schedules, runs flows)
python -m aegis_worker

# 5. Start Comms bot (Slack Socket Mode — needs Slack tokens in config/.env)
python -m aegis_comms
```

## Docker Compose (full stack)

```bash
# Build all images
docker compose build

# Start everything
docker compose up -d

# Check status
docker compose ps

# View logs
docker compose logs core --tail 50 -f
docker compose logs worker --tail 50 -f
```

## Service Ports

| Service | Local Dev | Docker |
|---------|-----------|--------|
| Core API | 8080 (or 8090 if 8080 busy) | 8080 |
| Telegram | 8081 | 8081 |
| Knowledge | 8000 | 8000 |
| Postgres | 25432 | 25432 |
| Redis | 26379 | 26379 |
| Temporal | 7233 | 7233 |
| Temporal UI | 8233 | 8233 |

## Testing

```bash
pytest                    # full suite (asyncio_mode=auto)
pytest tests/core/        # core only
pytest tests/worker/      # worker only
pytest tests/telegram/    # telegram only
pytest -x                 # stop on first failure
ruff check .              # lint
ruff format .             # format
```

pytest config lives in the root `pyproject.toml` (not under `core/`) because rootdir is the project root.

## Configuration

Copy `config/.env.example` to `config/.env` and fill in secrets:

```bash
cp config/.env.example config/.env
# Edit config/.env with your tokens
```

Key settings:
- `AEGIS_DATABASE_URL` — PostgreSQL connection
- `AEGIS_LITELLM_URL` + `AEGIS_LITELLM_API_KEY` — LLM gateway
- `AEGIS_TELEGRAM_BOT_TOKEN` + `AEGIS_TELEGRAM_CHAT_ID` — Telegram. The dev vs prod bot handles + tokens live in the infra-gitops Ansible vault. **Warning:** if these env vars are set in your shell, they override the Ansible default during deploys.
- `AEGIS_GITHUB_TOKEN` — GitHub API
- `AEGIS_GMAIL_ACCOUNTS` — Gmail OAuth (format: `name:email,name:email`)

## Admin Panel Development

```bash
cd admin-panel/frontend
npm install
npm run dev     # Dev server on port 5173
npm run build   # Build for production (served by Core)
```

## Content Extraction Setup

The worker's content extraction pipeline requires system-level dependencies for PDF, image, and media processing:

```bash
# macOS
brew install tesseract poppler ffmpeg

# Ubuntu/Debian
apt-get install tesseract-ocr poppler-utils ffmpeg
```

Playwright (Tier 2 article extraction) requires a browser install:

```bash
playwright install chromium
```

**Optional:** Media transcription (fallback when a URL has no captions) uses ElevenLabs Scribe — a hosted vendor (NOT the LiteLLM proxy, which serves text LLMs only). Set `AEGIS_ELEVENLABS_API_KEY` to enable. YouTube captions work without it.

Kill switches:
- `AEGIS_CONTENT_EXTRACTION_ENABLED=false` — disables all content extraction
- `AEGIS_ELEVENLABS_API_KEY=""` (empty) — disables media transcription only
- `AEGIS_TTS_ENABLED=false` (default) — disables outbound per-persona voice notes

## Adding a New Connector

1. Create `core/src/aegis/connectors/{name}.py` with async methods
2. Add config fields to `core/src/aegis/config.py`
3. Wire in `worker/src/aegis_worker/bootstrap.py`
4. Write tests in `tests/core/test_{name}_connector.py`

## Adding a New Flow

1. Create `worker/src/aegis_worker/flows/{name}.py` with `@workflow.defn`. The flow's config dataclass must include `agent_id: str` as its first field so `WorkflowRunRecorderInterceptor` can populate `workflow_runs.agent_id`.
2. Create activities in `worker/src/aegis_worker/activities/{name}.py`.
3. Register in `worker/src/aegis_worker/__main__.py` (workflows + activities lists) — both lists are explicit; nothing is auto-discovered.
4. If scheduled, add a `_ACTIVITY_TYPE_MAP` entry in `worker/src/aegis_worker/schedule_sync.py` keyed by the PascalCase class name (e.g. `"CleanupFlow"`).
5. Insert a seed row in `config/seed/activities.yaml`; `schedule_sync` registers the Temporal schedule on next worker startup.
6. Write tests in `tests/worker/test_{name}.py`. Use `WorkflowEnvironment.start_time_skipping()` + `Worker` for workflow tests; `ActivityEnvironment` + `respx` for activity tests.
7. For human-in-the-loop steps, spawn `InteractionFlow` as a child workflow rather than building custom callback logic.

## Adding a New Chat Tool

1. Add tool schema to `CHAT_TOOLS` list in `core/src/aegis/services/chat.py` (OpenAI function-calling format)
2. Create executor function: `async def _exec_tool_name(pool, args, ctx: ToolContext) -> str`
3. Add to `TOOL_EXECUTORS` dict
4. Gate per-personality in `AGENT_TOOL_SETS` — Core's `_validate_agent_tool_sets` refuses to boot if a personality references a tool not in `CHAT_TOOLS`
5. If the tool needs new connectors on `ToolContext`, add the field and wire it in `send_message()`
6. Write tests in `tests/core/test_{tool_name}_tool.py`

## Adding Intelligence Topics

Intelligence monitoring topics are stored in `settings` key `intelligence_topics`. Manage them via the `track_topic` chat tool (Raphael), or directly:

```sql
-- View current topics
SELECT value FROM settings WHERE key = 'intelligence_topics';

-- Update via API
PUT /api/settings/intelligence_topics
```

`IntelligenceScanFlow` (Raphael, scheduled per source) reads this config when scanning `hn`, `news`, or `finance` sources.

## Todoist (local dev)

For local development against the real Todoist API:

1. Personal API key in `config/.env`:
   ```
   AEGIS_TODOIST_API_KEY=<your key>
   AEGIS_TODOIST_EMAIL=<your email>
   AEGIS_TODOIST_WEBHOOK_SECRET=<any string for local — webhooks won't reach localhost anyway>
   ```
2. Boot Core + worker as usual. `TodoistSyncFlow` will fire every 5 minutes against your real Todoist account.
3. For webhook testing without exposing localhost, hand-craft an HMAC-signed request:
   ```bash
   SECRET=<your secret>
   BODY='{"event_name":"item:added","event_data":{"id":1}}'
   SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')
   curl -X POST http://localhost:8080/api/webhooks/todoist \
     -H "X-Todoist-Hmac-SHA256: $SIG" -d "$BODY"
   ```
4. To reset the projection between dev runs:
   ```sql
   TRUNCATE todoist_tasks, todoist_projects, todoist_labels, todoist_webhook_events, todoist_outbox;
   UPDATE todoist_sync_state SET sync_token = '*' WHERE key = 'main';
   DELETE FROM settings WHERE key = 'todoist_managed_project_ids';
   ```
   Then the next sync fires bootstrap + full sync again.

### Phase 2 — local dev

The capture helper reads two `settings` rows: `todoist_capture_enabled` (boolean) and `todoist_managed_project_ids` (JSONB dict with at least `inbox` key). Both are populated by migration 011 + the Phase 1 bootstrap.

To exercise the capture path locally without going through a full ingest flow:

```python
import asyncio, os
from aegis.db import create_pool
from aegis.connectors.todoist import TodoistConnector
from aegis_worker.activities.capture import CaptureActivities

async def main():
    pool = await create_pool("postgresql://aegis:aegis_dev@localhost:25432/aegis")
    conn = TodoistConnector(api_key=os.environ["AEGIS_TODOIST_API_KEY"])
    act = CaptureActivities(db_pool=pool, connector=conn)
    ref = await act.capture_to_inbox(
        source_tag="#manual",
        external_id="local-test-1",
        title="Phase 2 local test",
        description="Triggered from a script",
    )
    print("Captured ref:", ref)

asyncio.run(main())
```

To reset capture state between runs:

```sql
TRUNCATE todoist_capture_idempotency;
UPDATE settings SET value = 'true'::jsonb WHERE key = 'todoist_capture_enabled';
```
