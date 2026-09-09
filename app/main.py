import asyncio
import datetime
import os
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from .export_xlsx import build_export_workbook
from .oncall import filter_for_emp_id
from .oncall_api import fetch_agency_event, fetch_agency_events, is_error_result

app = FastAPI()

STATIC_HTML = Path(__file__).parent / "static" / "event-view.html"

# In-memory store for the latest AccessToken + EventNumber pushed from OnCall CRE.
# Do not expose AccessToken to the client; only store server-side for proxying to CAD.
# ponytail: single-slot store serves one dispatcher session; move to per-session keys when multiple simultaneous viewers matter.
_store: dict[str, Any] = {"accessToken": None, "eventNumber": None}

ONCALL_API_BASE = os.getenv("ONCALL_API_BASE", "https://oncallvos2.southeastasia.cloudapp.azure.com:8080")

BUNDLE_PATHS = {
    "event": "/api/v3/Event/{eventNumber}",
    "comments": "/api/v2/Event/{eventNumber}/Comments",
    "calls": "/api/v1/Event/{eventNumber}/Call",
    "dispositions": "/api/v1/Event/{eventNumber}/Dispositions",
    "assignedUnits": "/api/v1/Event/{eventNumber}/AssignedUnits",
    "crossReference": "/api/v1/Event/{eventNumber}/CrossReference",
    "history": "/api/v1/Event/{eventNumber}/History",
}


def _extract_token_and_event(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    # PascalCase (OnCall CRE) + camelCase + NewParameter* fallback:
    # OnCall has a bug where the AccessToken field cannot be used, so CRE sends
    # the JWT as NewParameter1 (or 2/3). Check case-insensitively.
    low = {k.lower(): v for k, v in payload.items() if isinstance(k, str)}
    token = (
        payload.get("AccessToken")
        or payload.get("accessToken")
        or payload.get("access_token")
        or payload.get("token")
        or payload.get("Token")
        or low.get("newparameter1")
        or low.get("newparameter2")
        or low.get("newparameter3")
    )
    event_number = (
        payload.get("EventNumber")
        or payload.get("eventNumber")
        or payload.get("event_number")
        or payload.get("EventId")
        or payload.get("agencyEventId")
        or low.get("newparameter2")
        or low.get("newparameter1")
        or low.get("newparameter3")
    )
    if isinstance(token, str):
        token = token.strip()
    if isinstance(event_number, str):
        event_number = event_number.strip()
    return token, event_number


def _resolve(accessToken: str | None, eventNumber: str | None) -> tuple[str | None, str | None]:
    # Prefer stored values pushed via POST /view-event; fallback to manual query params.
    return _store.get("accessToken") or accessToken, _store.get("eventNumber") or eventNumber


def _missing() -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"error": "No AccessToken/EventNumber received yet. Waiting for OnCall push."},
    )


@app.get("/event-view", response_class=HTMLResponse)
def get_event_view():
    return HTMLResponse(content=STATIC_HTML.read_text(encoding="utf-8"))


@app.post("/view-event")
@app.post("/api/view-event")
async def post_view_event(payload: dict[str, Any]):
    token, event_number = _extract_token_and_event(payload)
    if not token or not event_number:
        return JSONResponse(
            status_code=422,
            content={"error": "AccessToken and EventNumber are required."},
        )
    _store["accessToken"] = token
    _store["eventNumber"] = event_number
    # Do not echo token back; only confirm receipt
    return JSONResponse(content={"status": "received", "eventNumber": event_number})


@app.get("/view-event-status")
def get_view_event_status():
    received = bool(_store.get("accessToken") and _store.get("eventNumber"))
    return JSONResponse(
        content={
            "received": received,
            "eventNumber": _store.get("eventNumber") if received else None,
        }
    )


@app.post("/view-event-clear")
def post_view_event_clear():
    _store["accessToken"] = None
    _store["eventNumber"] = None
    return JSONResponse(content={"status": "cleared"})


async def _fetch_bundle(token: str, event_number: str) -> dict[str, Any]:
    # Fetches through app.main.httpx (kept here so tests can patch that seam).
    async def one(client: httpx.AsyncClient, key: str, tpl: str) -> tuple[str, Any]:
        url = f"{ONCALL_API_BASE.rstrip('/')}{tpl.format(eventNumber=event_number)}"
        try:
            resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
        except Exception as e:
            return key, {"_error": f"Failed to contact CAD: {e}", "_status": 502}
        if resp.status_code != 200:
            try:
                body: Any = resp.json()
            except Exception:
                body = {"_error": resp.text[:2000], "_status": resp.status_code}
            else:
                if isinstance(body, dict):
                    body.setdefault("_status", resp.status_code)
                    body.setdefault("_error", body.get("error") or f"CAD {resp.status_code}")
                else:
                    body = {"_error": str(body)[:2000], "_status": resp.status_code}
            return key, body
        try:
            return key, resp.json()
        except Exception:
            return key, {"_error": "Invalid JSON from CAD", "_status": 502}

    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
        results = await asyncio.gather(*(one(client, k, t) for k, t in BUNDLE_PATHS.items()))
    bundle: dict[str, Any] = dict(results)
    bundle["fetchedAt"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    bundle["eventNumber"] = event_number
    return bundle


@app.get("/event-view-bundle")
async def get_event_view_bundle(accessToken: str | None = None, eventNumber: str | None = None):
    token, event_number = _resolve(accessToken, eventNumber)
    if not token or not event_number:
        return _missing()
    return JSONResponse(content=await _fetch_bundle(token, event_number))


@app.get("/event-view-data")
async def get_event_view_data(accessToken: str | None = None, eventNumber: str | None = None):
    token, event_number = _resolve(accessToken, eventNumber)
    if not token or not event_number:
        return _missing()
    data = await fetch_agency_event(token, event_number)
    if is_error_result(data):
        status = data.get("_status", 502)
        body = dict(data)
        body.pop("_status", None)
        if "_error" in body:
            body.setdefault("error", body.pop("_error"))
        return JSONResponse(status_code=status if status >= 400 else 502, content=body)
    # TODO: real per-dispatcher filtering pending confirmation from the project lead; pass-through stub
    return JSONResponse(content=filter_for_emp_id(data, ""))


@app.post("/export-events")
async def post_export_events(payload: dict[str, Any]):
    event_ids = payload.get("eventIds") or payload.get("EventIds")
    if not event_ids:
        return JSONResponse(
            status_code=400,
            content={"error": "eventIds must be a non-empty array of event IDs."},
        )
    token = _store.get("accessToken")
    if not token:
        return _missing()
    results = await fetch_agency_events(token, event_ids)
    buf = build_export_workbook(event_ids, results)
    filename = f"export_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
