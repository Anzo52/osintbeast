import json
import re
import sqlite3

import database

HOLEHE_TIMEOUT_SECONDS = 300

_LOCAL_PART_RE = re.compile(
    r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*\Z"
)
_DOMAIN_LABEL_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")


class HoleheTimeoutError(TimeoutError):
    """Raised when an in-process Holehe run exceeds its time budget."""


def validate_email(email: str) -> str:
    """Validate a practical Internet email address before invoking Holehe."""
    if not isinstance(email, str):
        raise ValueError("A valid email address is required.")

    email = email.strip()
    if not email or len(email) > 254 or any(char.isspace() for char in email):
        raise ValueError("A valid email address is required.")
    if email.count("@") != 1:
        raise ValueError("A valid email address is required.")

    local_part, domain = email.rsplit("@", 1)
    if len(local_part) > 64 or not _LOCAL_PART_RE.fullmatch(local_part):
        raise ValueError("A valid email address is required.")

    try:
        ascii_domain = domain.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("A valid email address is required.") from exc

    labels = ascii_domain.split(".")
    if (
        len(ascii_domain) > 253
        or len(labels) < 2
        or any(not _DOMAIN_LABEL_RE.fullmatch(label) for label in labels)
    ):
        raise ValueError("A valid email address is required.")

    return email


async def _collect_holehe_results(email: str, timeout_seconds: int):
    import httpx
    import trio
    from holehe import core

    modules = core.import_submodules("holehe.modules")
    websites = core.get_functions(modules)
    results = []

    with trio.fail_after(timeout_seconds):
        async with httpx.AsyncClient(timeout=10) as client:
            async with trio.open_nursery() as nursery:
                for website in websites:
                    nursery.start_soon(core.launch_module, website, email, client, results)

    return sorted(results, key=lambda item: item.get("name", ""))


def _run_holehe(email: str):
    try:
        import trio
    except ImportError as exc:
        raise RuntimeError(
            "Holehe and its dependencies are required. Install them with `python -m pip install holehe`."
        ) from exc

    try:
        return trio.run(_collect_holehe_results, email, HOLEHE_TIMEOUT_SECONDS)
    except trio.TooSlowError as exc:
        raise HoleheTimeoutError(
            f"Holehe exceeded the {HOLEHE_TIMEOUT_SECONDS}-second time limit."
        ) from exc


def run_holehe(email: str):
    """Run Holehe in-process so the email is not exposed in process arguments."""
    email = validate_email(email)
    results = _run_holehe(email)
    return {
        "returncode": 0,
        "output": json.dumps(results, ensure_ascii=False, indent=2),
    }


def save_tool_result(tool_name: str, query_text: str, result_text: str):
    conn = sqlite3.connect(database.DB_FILE)
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS ToolResults (
                resultID INTEGER PRIMARY KEY AUTOINCREMENT,
                toolName TEXT NOT NULL,
                queryText TEXT NOT NULL,
                resultText TEXT,
                createdAt TEXT DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        conn.execute(
            "INSERT INTO ToolResults (toolName, queryText, resultText) VALUES (?, ?, ?)",
            (tool_name, query_text, result_text),
        )
        conn.commit()
    finally:
        conn.close()
