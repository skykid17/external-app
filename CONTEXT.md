# External App — OnCall Event View (POC)

Small self-contained web app that lets an HxGN OnCall Dispatch CRE server
rule hand off an event to a browser tab for a full, read-only view of that
event's data.

## Stack

- Python 3.12, FastAPI, `uvicorn`
- `httpx` for outbound calls to OnCall
- `openpyxl` for the XLSX export
- Plain HTML + vanilla JS single page (no build step)
- Docker + `docker-compose.yml`, single service, one container
- Cloudflare tunnel service (`cloudflared`) for external reachability

## Environment variables

| Variable | Purpose |
|---|---|
| `ONCALL_API_BASE` | Base URL for the OnCall REST API (default: POC dev host) |

## Architecture

### Push flow

OnCall CRE calls `POST /view-event` (or `POST /api/view-event`) via
Invoke Outbound Service with the dispatcher's session token and the event
number. The app stores the latest pair in memory
(`app/main.py:_store`) and never echoes the token back.

The OnCall payload is tolerant of field naming: PascalCase (`AccessToken`,
`EventNumber`), camelCase, plus a `NewParameter1/2/3` fallback because OnCall
CRE cannot send the JWT through the AccessToken field (known OnCall bug).
See `_extract_token_and_event` in `app/main.py`.

The frontend (`GET /event-view`) polls `GET /view-event-status` every 1.5s.
Once a push arrives it locks the manual inputs and fetches the bundle.
Manual entry (AccessToken + EventNumber in the top bar) remains available
before any push arrives.

`POST /view-event-clear` resets the stored pair (test/diagnostic helper).

### Read flow

`GET /event-view-bundle` fetches seven OnCall endpoints concurrently for the
stored (or query-param) event and returns them as one JSON bundle:
event (`/api/v3/Event/{id}`), comments (v2), calls, dispositions, assigned
units, cross-reference, history (all v1). A failed sub-fetch does not abort
the bundle — it comes back as `{"_error": ..., "_status": ...}` under its key.

`GET /event-view-data` is the single-event proxy (GET `/api/v2/Event/{id}`)
through the shared boundary in `app/oncall_api.py`; it applies the
placeholder permission filter `filter_for_emp_id` (pass-through stub, real
per-dispatcher filtering pending confirmation from the project lead —
`app/oncall.py`).

### Export flow

`POST /export-events` with `{"eventIds": [...]}` fetches all events
concurrently through the same shared boundary (semaphore-capped at 10) and
streams back an XLSX: one row per event, flattened dot-notation columns
from `app/export_xlsx.py`, plus an `Error` column for IDs that failed
instead of aborting the batch.

### Frontend (`app/static/event-view.html`)

Single page: hero summary (location, type, priority, times, coordinates,
placeholder map pin), stat pills, chronology rail built from the event
create time + comments (searchable, system-comments toggle), cards for
assigned units, dispositions, call/cross-reference/history, and the raw
event JSON. Print stylesheet for report printing. Banner auto-dismisses
3s after a push is received.

## Files

| File | Role |
|---|---|
| `app/main.py` | All HTTP endpoints, push store, bundle fetch |
| `app/oncall_api.py` | Shared OnCall API boundary (v2 event fetch, batch fetch, error markers) |
| `app/oncall.py` | `filter_for_emp_id` pass-through stub |
| `app/export_xlsx.py` | Flatten + build XLSX workbook |
| `app/static/event-view.html` | The single frontend page |

## Docker

`docker compose up` starts the app on port 8000 with no manual steps beyond
setting `.env`. `cloudflared-quick` runs a quick tunnel pointing at the app
(`https://app:8000`); swap in a token-based named tunnel for stable URLs.

## Non-goals

- No login/auth on the frontend; the pushed token is the only gate.
- No write-back to OnCall — strictly read-only.
- No real per-dispatcher permission logic (stub only).
- No persistent storage — the push store is in-memory by design; a
  container restart simply waits for the next push.

## Future expansion

The natural growth axes for this external-app mode, in rough order:

- **Real per-dispatcher filtering**: replace `filter_for_emp_id` in
  `app/oncall.py` once the filtering rules are confirmed. It is already the
  single choke point for the view flow.
- **More event sub-resources**: add paths to `BUNDLE_PATHS` in
  `app/main.py` and a card in the HTML — the bundle fetch fans out
  concurrently and tolerates per-key failure by design.
- **Named/per-session stores**: `_store` is a single slot serving one
  dispatcher at a time. If multiple dispatchers view events simultaneously,
  key it by session or correlation id instead of adding tables.
- **Persistent handoff links**: if links must survive restarts or be
  single-use/expire, reintroduce a SQLite `exchange_codes` table
  (opaque code -> event, TTL, one-time use) at the push boundary; the
  current in-memory push already keeps the same contract shape.
- **Write-back**: any future write-back belongs behind a new explicit
  endpoint in `app/oncall_api.py`, never inside the read proxies.

## Tests

`tests/` (gitignored locally) pin the HTTP contracts: push flow
(PascalCase/camelCase/NewParameter tolerance, token never echoed), bundle
keys and partial failure, export XLSX shape, old `/receive`/`/exchange`
paths gone, and HTML shell invariants (no URL event-number parsing, empty
state, manual inputs, auto-dismiss banner). Run with `python -m pytest tests`.
