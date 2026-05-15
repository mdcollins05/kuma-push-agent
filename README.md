# Kuma Push Agent

A Dockerized remote health-check agent for [Uptime Kuma](https://github.com/louislam/uptime-kuma). Deploy it inside a network where Uptime Kuma can't reach — behind a firewall, on a private VLAN, or in an isolated environment — and it will monitor your services locally and push results out to Uptime Kuma via **Push-type monitors**. Monitor auto-creation in Kuma is handled for you.

## Features

- Monitors multiple URLs at configurable intervals
- Per-monitor: expected HTTP status codes, response body keyword check, SSL verification toggle
- Auto-creates Push monitors in Uptime Kuma (via Socket.IO API)
- Web UI for managing monitors, viewing status, and configuring settings
- REST API with API key authentication
- No secrets in environment variables — all configured via the UI

## Quickstart

**Prerequisites:** Docker + Docker Compose

```bash
git clone https://github.com/mdcollins05/kuma-push-agent.git
cd kuma-push-agent
docker compose up -d
```

1. Visit **http://&lt;host&gt;:3002** — you'll be redirected to `/setup`.
2. Create an admin account and optionally connect an Uptime Kuma server.
3. Add monitors via the dashboard.

## Web UI

| Page | URL |
|---|---|
| Dashboard | http://&lt;host&gt;:3002/ |
| Add Monitor | http://&lt;host&gt;:3002/monitors/new |
| Settings | http://&lt;host&gt;:3002/settings |

### Monitor Actions (Edit page)

- **Save Changes** — update URL, interval, expected codes, keyword, SSL setting
- **Pause in Kuma** — suspends the Kuma monitor and stops local checks
- **Resume in Kuma** — re-enables a paused monitor
- **Remove from Agent** — stops local checks; Kuma monitor is left intact
- **Delete from Kuma** — permanently deletes the Kuma monitor and stops local checks

## REST API

All API requests require an `X-API-Key` header. Find your key at `/settings`.

```bash
API_KEY="your-key-here"
BASE="http://<host>:3002/api/v1"

# List all monitors
curl -H "X-API-Key: $API_KEY" $BASE/monitors

# Get one monitor
curl -H "X-API-Key: $API_KEY" $BASE/monitors/1

# Create a monitor
curl -X POST -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"name":"New Site","url":"https://example.org","interval":60,"expected_codes":[200]}' \
  $BASE/monitors

# Update a monitor
curl -X PUT -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"name":"New Site","url":"https://example.org","interval":30,"expected_codes":[200]}' \
  $BASE/monitors/1

# Delete (orphan) a monitor
curl -X DELETE -H "X-API-Key: $API_KEY" $BASE/monitors/1
```

## Configuration

All configuration is stored in the SQLite database at `/data/kuma_push_agent.db`.

| Volume | Purpose |
|---|---|
| `./data` → `/data` | SQLite database + session secret |

The only environment variable is `DATA_DIR` (default `/data`) and `CONFIG_DIR` (default `/config`), which you generally don't need to change.

## Uptime Kuma Compatibility

Tested with **Uptime Kuma v2** (`louislam/uptime-kuma:2`). The Socket.IO management API uses the `uptime-kuma-api-v2` Python library. The push heartbeat endpoint (`/api/push/<token>`) is a plain HTTP call and is stable across versions.

## Testing

Tests run inside Docker — no host Python install required.

```bash
docker compose --profile test run --rm test
```

This builds a separate image that includes dev dependencies (pytest), runs all tests against an in-memory SQLite database, and exits. No Kuma server or running agent is needed.

Tests cover:
- REST API endpoints (`/api/v1/monitors` — list, create, get, update, delete)
- Input validation (missing fields, invalid interval, invalid status codes)
- API key auth enforcement
- Status polling endpoints (`/monitors/statuses`, `/monitors/{id}/status`, `/jobs/status`)

## Architecture

```
Kuma Push Agent (port 3002)
├── FastAPI web app
│   ├── UI routes (session auth)
│   └── API routes (X-API-Key auth)
├── APScheduler background jobs (one per monitor)
│   └── httpx HTTP check → SQLite update → Kuma push
└── uptime-kuma-api-v2 (Socket.IO, threadpool)
    └── Creates/pauses/deletes Kuma Push monitors
```
