from typing import Any


def filter_for_emp_id(event_json: dict[str, Any], emp_id: str) -> dict[str, Any]:
    # TODO: real per-dispatcher filtering logic is pending confirmation from the project lead.
    # For this POC, pass through unfiltered.
    return event_json
