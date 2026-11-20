import json
import re
from collections.abc import Callable
from urllib.parse import quote, quote_plus

import data.utils.database as database


def _redact_sensitive_value(text: str, sensitive_value: str) -> str:
    """Redact complete query tokens from text and JSON string values."""
    variants = sorted(
        {sensitive_value, quote(sensitive_value, safe=""), quote_plus(sensitive_value)},
        key=len,
        reverse=True,
    )
    patterns = [
        re.compile(
            rf"(?<![A-Za-z0-9_.-]){re.escape(variant)}(?![A-Za-z0-9_.-])",
            re.IGNORECASE,
        )
        for variant in variants
        if variant
    ]

    def redact_text(value: str) -> str:
        for pattern in patterns:
            value = pattern.sub("[redacted]", value)
        return value

    def redact_json_value(value):
        if isinstance(value, str):
            redacted = redact_text(value)
            return redacted, redacted != value
        if isinstance(value, list):
            redacted_values = [redact_json_value(item) for item in value]
            return [item for item, _ in redacted_values], any(
                changed for _, changed in redacted_values
            )
        if isinstance(value, dict):
            redacted_values = {
                key: redact_json_value(item) for key, item in value.items()
            }
            return (
                {key: item for key, (item, _) in redacted_values.items()},
                any(changed for _, changed in redacted_values.values()),
            )
        return value, False

    try:
        parsed = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return redact_text(text)

    redacted, changed = redact_json_value(parsed)
    return json.dumps(redacted, ensure_ascii=False, indent=2) if changed else text


def execute_tool_run(
    tool_name: str,
    query_text: str,
    runner: Callable[[str], str],
    run_id: int | None = None,
    *,
    sensitive_input: bool = False,
) -> tuple[str, str]:
    """Persist the run outcome; propagate persistence errors to the caller."""
    if run_id is None:
        stored_query = "[redacted]" if sensitive_input else query_text
        run_id = database.start_tool_run(tool_name, stored_query)
    if run_id is None:
        raise RuntimeError("Failed to start tool run.")
    try:
        output = runner(query_text)
    except TimeoutError as exc:
        message = str(exc) or f"{tool_name} exceeded its time limit."
        if sensitive_input:
            message = _redact_sensitive_value(message, query_text)
        database.finish_tool_run(run_id, "timed_out", error_text=message)
        return "timed_out", message
    except (RuntimeError, OSError, ImportError, ValueError) as exc:
        message = str(exc) or f"{tool_name} failed ({type(exc).__name__})."
        if sensitive_input:
            message = _redact_sensitive_value(message, query_text)
        database.finish_tool_run(run_id, "failed", error_text=message)
        return "failed", message
    if sensitive_input:
        output = _redact_sensitive_value(output, query_text)
    database.finish_tool_run(run_id, "success", result_text=output)
    return "success", output
