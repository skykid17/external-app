# Build prompt: External App POC

Build a small, self-contained web app in Docker that lets an HxGN OnCall
Dispatch CRE server rule hand off an event to a browser tab for a full,
read-only view of that event's data. Keep the code minimal and clean, no
unnecessary abstraction layers, frameworks, or config beyond what is
listed below.

## Stack

- Python 3.12, FastAPI, `uvicorn`
- SQLite (via `sqlite3` standard library, no ORM)
- Plain HTML + vanilla JS for the single frontend page (no build step, no
  frontend framework)
- `httpx` for outbound calls to OnCall
- Docker + `docker-compose.yml`, single service, single container

## Environment variables

| Variable | Purpose |
|---|---|
| `ONCALL_TOKEN_URL` | OIDC token endpoint, e.g. `https://oncall.../oncall.identity/connect/token` |
| `ONCALL_CLIENT_ID` | Service account client ID |
| `ONCALL_CLIENT_SECRET` | Service account client secret |
| `ONCALL_API_BASE` | Base URL for the OnCall REST API |
| `EXCHANGE_SHARED_SECRET` | Shared secret that OnCall's Outbound Web Service call must present |
| `CODE_TTL_SECONDS` | Opaque code lifetime, default `60` |

## Data model (SQLite, single table)

```sql
CREATE TABLE exchange_codes (
    code TEXT PRIMARY KEY,
    event_number TEXT NOT NULL,
    emp_id TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0
);
```

## Endpoints

### `POST /exchange`

Called by OnCall's `SWK_ExternalApp` server rule (via Invoke
Outbound Service). Not reachable by browsers.

- Require header `X-OnCall-Service-Key` to equal `EXCHANGE_SHARED_SECRET`.
  Reject with `401` if missing or wrong.
- Request body (JSON): `{ "eventNumber": "SPF-2026071000006", "empId": "12345" }`
- Generate `code = secrets.token_urlsafe(32)`.
- Insert a row with `expires_at = now + CODE_TTL_SECONDS`, `used = 0`.
- Respond `{ "code": "<code>" }`.

### `GET /event-view?code=<code>`

Serves the single HTML page (static shell). The page's JS immediately
calls `/event-view-data` using the same `code` from the query string.
No server-side logic beyond serving the file.

### `GET /event-view-data?code=<code>`

- Look up `code` in `exchange_codes`.
- If missing, already `used = 1`, or past `expires_at`: respond `410`
  with `{ "error": "This link has expired or already been used." }`.
- Otherwise, in the same transaction, set `used = 1` immediately (before
  doing anything else), so a retried or duplicated request can't reuse it.
- Fetch/reuse a cached OIDC access token:
  - `client_credentials` grant against `ONCALL_TOKEN_URL`, using
    `ONCALL_CLIENT_ID` / `ONCALL_CLIENT_SECRET`, `scope=api`.
  - Cache the token in memory with its expiry; only re-fetch when
    expired or absent. Do not fetch a new token per request.
- Call OnCall's `GET AgencyEvent/{event_number}` (the real endpoint path,
  confirm against the OnCall API schema) using
  `Authorization: Bearer <token>`.
- Apply a placeholder permission filter function
  `filter_for_emp_id(event_json, emp_id)`. For this POC, implement it as
  a clearly-labelled pass-through stub (returns the data unfiltered) with
  a `# TODO:` comment noting that real per-dispatcher filtering logic is
  pending confirmation from the project lead. Do not attempt to build
  real filtering logic.
- Return the (filtered) event JSON to the frontend.

### Frontend (`/event-view` page)

- On load, read `code` from the query string, call
  `/event-view-data?code=...`.
- On success, render the JSON as a readable key/value table (or
  pretty-printed JSON block, whichever is less code).
- On `410`/error, show a plain message: "This link has expired or already
  been used." No retry button, no auto-refresh.

## Non-goals (do not implement)

- No user login/auth on the frontend page itself, the opaque code is the
  only gate.
- No write-back to OnCall.
- No real per-dispatcher permission logic, the stub described above is
  sufficient.
- No background scheduler/cron for cleaning up expired codes, a simple
  `DELETE FROM exchange_codes WHERE expires_at < now` at the start of
  `POST /exchange` is enough.
- No frontend build tooling, bundlers, or JS frameworks.

## Docker

- Single `Dockerfile`, Python slim base image.
- `docker-compose.yml` exposing one port, mapping the env vars above,
  mounting the SQLite file to a volume so data survives container
  restarts.

## Acceptance criteria

- `docker compose up` starts the app with no manual steps beyond setting
  the `.env` file.
- `curl -X POST /exchange` with the correct shared-secret header and a
  valid body returns a code.
- Visiting `/event-view?code=<that code>` in a browser renders the event
  JSON once, then shows the expired message if reloaded.
- Missing/incorrect shared secret on `/exchange` returns `401`.
- Code reuse or expiry returns `410`, not a stack trace.
