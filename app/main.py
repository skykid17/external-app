import asyncio
import datetime
import os
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from .oncall import filter_for_emp_id

app = FastAPI()

STATIC_HTML = Path(__file__).parent / "static" / "event-view.html"

# In-memory store for the latest AccessToken + EventNumber pushed from OnCall CRE
# Do not expose AccessToken to the client; only store server-side for proxying to CAD.
_store: dict[str, Any] = {"accessToken": None, "eventNumber": None}

ONCALL_API_BASE = os.getenv("ONCALL_API_BASE", "https://oncallvos2.southeastasia.cloudapp.azure.com:8080")


def _extract_token_and_event(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    # Support both PascalCase (OnCall CRE) and camelCase
    # + NewParameter* fallback: OnCall has a bug where AccessToken field cannot be used,
    #   so CRE sends the JWT as NewParameter1 (or 2/3). Check case-insensitively.
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
        # fallback if CRE maps EventNumber to a NewParameter as well
        or low.get("newparameter2")
        or low.get("newparameter1")
        or low.get("newparameter3")
    )
    if isinstance(token, str):
        token = token.strip()
    if isinstance(event_number, str):
        event_number = event_number.strip()
    return token, event_number


@app.get("/event-view", response_class=HTMLResponse)
def get_event_view():
    html = STATIC_HTML.read_text(encoding="utf-8")
    return HTMLResponse(content=html)


@app.post("/receive")
@app.post("/api/receive")
@app.post("/api/event-receive")
async def post_receive(payload: dict[str, Any]):
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


@app.get("/receive-status")
@app.get("/api/receive-status")
def get_receive_status():
    received = bool(_store.get("accessToken") and _store.get("eventNumber"))
    return JSONResponse(
        content={
            "received": received,
            "eventNumber": _store.get("eventNumber") if received else None,
        }
    )


@app.post("/event-view-data")
async def post_event_view_data(payload: dict[str, Any], api: str | None = None):
    # Direct POST with JSON body { "EventNumber": "...", "AccessToken": "...", "api": "v3_Event" }
    # Accepts PascalCase from OnCall CRE as well as camelCase; query ?api= overrides body
    token, event_number = _extract_token_and_event(payload)
    # allow api from query string to override body
    api_from_body = payload.get("api") or payload.get("Api") or payload.get("API")
    effective_api = api or api_from_body
    if not token or not event_number:
        return JSONResponse(
            status_code=400,
            content={"error": "AccessToken and EventNumber are required in JSON body. Example: {\"EventNumber\":\"SPF-...\",\"AccessToken\":\"<JWT>\"}"},
        )
    # store for subsequent GETs (browser) and reuse proxy logic
    _store["accessToken"] = token
    _store["eventNumber"] = event_number
    return await _proxy_to_cad(token, event_number, effective_api)


@app.get("/event-view-data")
async def get_event_view_data(
    accessToken: str | None = None,
    eventNumber: str | None = None,
    api: str | None = None,
):
    # Prefer stored values pushed via POST /receive or POST /event-view-data; fallback to query params
    token = _store.get("accessToken") or accessToken
    event_number = _store.get("eventNumber") or eventNumber

    # Also support lowercase query aliases
    # FastAPI already maps accessToken/eventNumber case-sensitive; we handle missing via store
    if not token or not event_number:
        return JSONResponse(
            status_code=400,
            content={"error": "No AccessToken/EventNumber received yet. Waiting for OnCall push."},
        )

    return await _proxy_to_cad(token, event_number, api)


BUNDLE_PATHS = {
    "event": "/api/v3/Event/{eventNumber}",
    "comments": "/api/v2/Event/{eventNumber}/Comments",
    "calls": "/api/v1/Event/{eventNumber}/Call",
    "dispositions": "/api/v1/Event/{eventNumber}/Dispositions",
    "assignedUnits": "/api/v1/Event/{eventNumber}/AssignedUnits",
    "crossReference": "/api/v1/Event/{eventNumber}/CrossReference",
    "history": "/api/v1/Event/{eventNumber}/History",
}


async def _fetch_one(client: httpx.AsyncClient, token: str, event_number: str, key: str, path_tpl: str):
    url = f"{ONCALL_API_BASE.rstrip('/')}{path_tpl.format(eventNumber=event_number)}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = await client.get(url, headers=headers)
    except Exception as e:
        return key, {"_error": f"Failed to contact CAD: {e}", "_status": 502}
    if resp.status_code != 200:
        try:
            body = resp.json()
        except Exception:
            body = {"_error": resp.text[:2000], "_status": resp.status_code}
        # normalize error marker
        if isinstance(body, dict):
            body.setdefault("_status", resp.status_code)
            body.setdefault("_error", body.get("error") or f"CAD {resp.status_code}")
        else:
            body = {"_error": str(body)[:2000], "_status": resp.status_code}
        return key, body
    try:
        data = resp.json()
    except Exception:
        return key, {"_error": "Invalid JSON from CAD", "_status": 502}
    return key, data


@app.get("/event-view-bundle")
async def get_event_view_bundle(
    accessToken: str | None = None,
    eventNumber: str | None = None,
):
    token = _store.get("accessToken") or accessToken
    event_number = _store.get("eventNumber") or eventNumber
    if not token or not event_number:
        return JSONResponse(
            status_code=400,
            content={"error": "No AccessToken/EventNumber received yet. Waiting for OnCall push."},
        )
    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
        tasks = [
            _fetch_one(client, token, event_number, key, tpl)
            for key, tpl in BUNDLE_PATHS.items()
        ]
        results = await asyncio.gather(*tasks)
    bundle: dict[str, Any] = {k: v for k, v in results}
    bundle["fetchedAt"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    bundle["eventNumber"] = event_number
    return JSONResponse(content=bundle)


@app.post("/event-view-bundle")
async def post_event_view_bundle(payload: dict[str, Any]):
    token, event_number = _extract_token_and_event(payload)
    # fallback to store if not in body
    token = token or _store.get("accessToken")
    event_number = event_number or _store.get("eventNumber")
    if token and event_number:
        _store["accessToken"] = token
        _store["eventNumber"] = event_number
    if not token or not event_number:
        return JSONResponse(
            status_code=400,
            content={"error": "No AccessToken/EventNumber received yet. Waiting for OnCall push."},
        )
    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
        tasks = [
            _fetch_one(client, token, event_number, key, tpl)
            for key, tpl in BUNDLE_PATHS.items()
        ]
        results = await asyncio.gather(*tasks)
    bundle: dict[str, Any] = {k: v for k, v in results}
    bundle["fetchedAt"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    bundle["eventNumber"] = event_number
    return JSONResponse(content=bundle)


async def _proxy_to_cad(token: str, event_number: str, api: str | None):
    api_map = {
        "v3_Event": f"/api/v3/Event/{event_number}",
        "v1_CommonEvent": f"/api/v1/Event/{event_number}/CommonEvent",
        "v1_AssignedUnits": f"/api/v1/Event/{event_number}/AssignedUnits",
        "v1_CaseIds": f"/api/v1/Event/{event_number}/CaseIds",
        "v1_Dispositions": f"/api/v1/Event/{event_number}/Dispositions",
        "v1_Call": f"/api/v1/Event/{event_number}/Call",
        "v2_ResultCode": f"/api/v2/Event/{event_number}/ResultCode",
        "v1_CrossReference": f"/api/v1/Event/{event_number}/CrossReference",
        "v2_Attachments": f"/api/v2/Event/{event_number}/Attachments",
        "v2_Comments": f"/api/v2/Event/{event_number}/Comments",
        "v1_History": f"/api/v1/Event/{event_number}/History",
    }
    path = api_map.get(api or "v3_Event", f"/api/v3/Event/{event_number}")
    url = f"{ONCALL_API_BASE.rstrip('/')}{path}"

    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
    except Exception as e:
        return JSONResponse(status_code=502, content={"error": f"Failed to contact CAD: {e}"})

    if resp.status_code != 200:
        try:
            body = resp.json()
        except Exception:
            body = {"error": resp.text[:2000], "status": resp.status_code}
        return JSONResponse(status_code=resp.status_code, content=body)

    try:
        data = resp.json()
    except Exception:
        return JSONResponse(status_code=502, content={"error": "Invalid JSON from CAD"})
    # TODO: real per-dispatcher filtering pending confirmation; pass-through stub
    try:
        filtered = filter_for_emp_id(data, "")
    except Exception:
        filtered = data
    return JSONResponse(content=filtered)


# Debug helper to clear stored token (not exposed in prod UI, useful for tests)
@app.post("/receive-clear")
def post_receive_clear():
    _store["accessToken"] = None
    _store["eventNumber"] = None
    return JSONResponse(content={"status": "cleared"})
