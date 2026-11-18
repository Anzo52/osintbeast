import asyncio
import json
import re

HELIX_TIMEOUT_SECONDS = 300
HELIX_REPORT_SCHEMA = "helix.scan.v1"


def validate_username(username: str) -> str:
    if not isinstance(username, str):
        raise ValueError("A username is required.")
    username = username.strip().lstrip("@")
    if not username or len(username) > 50 or not re.fullmatch(r"[A-Za-z0-9_.-]+", username):
        raise ValueError(
            "Use a username of up to 50 letters, digits, dots, underscores or hyphens."
        )
    return username


async def _scan(username: str) -> dict:
    try:
        from helix_osint.api import LeftOut, scan
    except ImportError as exc:
        raise RuntimeError(
            "Helix OSINT is required. Install it with `python -m pip install '.[helix]'`."
        ) from exc

    try:
        return await asyncio.wait_for(
            scan(username, sources="builtin"),
            timeout=HELIX_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        raise TimeoutError(
            f"Helix scan timed out (overall limit: {HELIX_TIMEOUT_SECONDS} seconds)."
        ) from exc
    except LeftOut as exc:
        raise RuntimeError(str(exc)) from exc


def run_helix(username: str) -> str:
    username = validate_username(username)
    report = asyncio.run(_scan(username))
    if not isinstance(report, dict) or report.get("schema") != HELIX_REPORT_SCHEMA:
        raise RuntimeError("Helix returned an invalid or unsupported report.")
    try:
        return json.dumps(report, ensure_ascii=False, indent=2)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Helix returned a report that could not be serialized.") from exc
