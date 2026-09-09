"""Build the export workbook: flatten events to dot-notation, one row per event."""
import io
import json
from typing import Any

from openpyxl import Workbook


def flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten nested dicts into dot-notation keys; lists become JSON strings."""
    out: dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, dict):
                out.update(flatten(v, key))
            elif isinstance(v, (list, tuple)):
                out[key] = json.dumps(v)
            else:
                out[key] = v
    return out


def build_export_workbook(
    event_ids: list[str], results: dict[str, dict[str, Any]]
) -> io.BytesIO:
    """One row per requested ID; Error column populated for failed fetches."""
    flattened: list[tuple[str, dict[str, Any], str | None]] = []
    columns: set[str] = set()
    for event_id in event_ids:
        data = results.get(event_id)
        error = None
        if data is None:
            data, error = {}, "No result returned for this event ID."
        elif isinstance(data, dict) and ("_error" in data or data.get("_status", 200) >= 400):
            error = str(data.get("_error") or data.get("error") or f"OnCall {data.get('_status')}")
            data = {}
        row = flatten(data)
        columns.update(row.keys())
        flattened.append((event_id, row, error))

    wb = Workbook()
    ws = wb.active
    header = ["EventId", *sorted(columns), "Error"]
    ws.append(header)
    for event_id, row, error in flattened:
        ws.append([event_id] + [row.get(c) for c in sorted(columns)] + [error])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf
