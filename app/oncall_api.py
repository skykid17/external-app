"""Shared OnCall API boundary used by /view-event flows and /export-events."""
import asyncio
import os
from typing import Any

import httpx

EVENT_PATH = "/api/v2/Event/{agency_event_id}"

# ponytail: hard-coded default is the POC dev host; make ONCALL_API_BASE required when this leaves the lab.
ONCALL_API_BASE = os.getenv("ONCALL_API_BASE", "https://oncallvos2.southeastasia.cloudapp.azure.com:8080")

CONCURRENCY = 10


async def fetch_agency_event_with_client(
    client: httpx.AsyncClient, token: str, agency_event_id: str
) -> tuple[str, dict[str, Any]]:
    """Fetch GET /v2/Event/{AgencyEventId} for one event.

    Returns (agency_event_id, event_json_or_error_dict). Never raises:
    transport or non-200 responses come back as {"_error": ..., "_status": ...}
    so a single failed ID cannot abort a batch.
    """
    url = f"{ONCALL_API_BASE.rstrip('/')}{EVENT_PATH.format(agency_event_id=agency_event_id)}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = await client.get(url, headers=headers)
    except Exception as e:
        return agency_event_id, {"_error": f"Failed to contact OnCall: {e}", "_status": 502}
    if resp.status_code != 200:
        try:
            body = resp.json()
        except Exception:
            body = {"_error": resp.text[:2000], "_status": resp.status_code}
        else:
            if isinstance(body, dict):
                body.setdefault("_status", resp.status_code)
                body.setdefault("_error", body.get("error") or f"OnCall {resp.status_code}")
            else:
                body = {"_error": str(body)[:2000], "_status": resp.status_code}
        return agency_event_id, body
    try:
        data = resp.json()
    except Exception:
        return agency_event_id, {"_error": "Invalid JSON from OnCall", "_status": 502}
    return agency_event_id, data


async def fetch_agency_event(token: str, agency_event_id: str) -> dict[str, Any]:
    """Single-event fetch with its own client; returns event JSON or error dict."""
    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
        _, data = await fetch_agency_event_with_client(client, token, agency_event_id)
    return data


async def fetch_agency_events(
    token: str, agency_event_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """Concurrently fetch several events, semaphore-capped at CONCURRENCY."""
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(agency_event_id: str) -> tuple[str, dict[str, Any]]:
        async with sem:
            return await fetch_agency_event_with_client(client, token, agency_event_id)

    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
        results = await asyncio.gather(*(one(i) for i in agency_event_ids))
    return dict(results)


def is_error_result(data: Any) -> bool:
    return isinstance(data, dict) and ("_error" in data or data.get("_status", 200) >= 400)
