import json
import os
import re
import sqlite3
import sys
from collections.abc import Mapping
from importlib.resources import files
from pathlib import Path
from typing import Any

from data.utils.storage import MySQLStorage, SQLiteStorage, Storage, StorageError


# Keep source-checkout paths independent of the current working directory.
def setup_database_paths(module_file):
    module_path = Path(module_file).resolve()
    project_root = module_path.parents[2]
    checkout_schema_dir = module_path.parent.parent / "schemas"
    try:
        if checkout_schema_dir.is_dir():
            schema_dir = checkout_schema_dir
            default_db_file = project_root / "osintbeast.db"
        else:
            schema_dir = files("osintbeast_schemas")
            default_db_file = _user_data_directory() / "osintbeast.db"
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            "Database schema resources are not installed."
        ) from exc
    db_file = str(
        Path(
            os.environ.get("OSINTBEAST_DB_PATH", str(default_db_file))
        ).expanduser().resolve()
    )
    schema_files = ("sqlite-infoschema.sql", "sqlite-toolschema.sql")

    # SQL identifiers cannot be bound, so restrict them to a fixed allowlist.
    insertable_tables = frozenset(
        {
            "Platforms", "Names", "Locations", "Domains", "Services", "Urls",
            "Usernames", "ToolCatalog", "ToolResults",
        }
    )
    return db_file, schema_dir, schema_files, insertable_tables


def _user_data_directory():
    if os.name == "nt":
        base_dir = Path(
            os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming"
        )
    elif sys.platform == "darwin":
        base_dir = Path.home() / "Library" / "Application Support"
    else:
        base_dir = Path(
            os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
        )
    return base_dir / "osintbeast"


DB_FILE, SCHEMA_DIR, SCHEMA_FILES, INSERTABLE_TABLES = setup_database_paths(__file__)
MYSQL_SCHEMA_FILES = ("mysql-infoschema.sql", "mysql-toolschema.sql")
_STORAGE_CACHE: dict[tuple[Any, ...], Storage] = {}
DATABASE_ERRORS = (sqlite3.Error, StorageError)
SQLITE_MAX_INTEGER = (1 << 63) - 1
MAX_SQLITE_INTEGER = SQLITE_MAX_INTEGER


def redact_sensitive_text(text: str | None, sensitive_value: str) -> str | None:
    """Redact complete raw or partially URL-encoded input occurrences."""
    if text is None or not sensitive_value or sensitive_value == "[redacted]":
        return text
    alternatives = []
    for char in sensitive_value:
        encoded = "".join(f"%{byte:02X}" for byte in char.encode("utf-8"))
        representations = [re.escape(char), re.escape(encoded)]
        if char == " ":
            representations.append(r"\+")
        alternatives.append(f"(?:{'|'.join(representations)})")
    pattern = re.compile(
        rf"(?<![A-Za-z0-9_.+-])(?<!\[){''.join(alternatives)}"
        r"(?![A-Za-z0-9_.+-])(?!\])",
        re.IGNORECASE,
    )
    return pattern.sub("[redacted]", text)


def _get_storage() -> Storage:
    database_url = os.environ.get("OSINTBEAST_DATABASE_URL")
    if database_url:
        if not database_url.startswith(("mysql://", "mysql+pymysql://")):
            raise StorageError(
                "OSINTBEAST_DATABASE_URL must use mysql:// or mysql+pymysql://."
            )
        key = ("mysql", database_url, str(SCHEMA_DIR))
        if key not in _STORAGE_CACHE:
            _STORAGE_CACHE[key] = MySQLStorage(
                database_url,
                SCHEMA_DIR,
                MYSQL_SCHEMA_FILES,
                INSERTABLE_TABLES,
            )
        return _STORAGE_CACHE[key]

    key = ("sqlite", DB_FILE, str(SCHEMA_DIR), SCHEMA_FILES)
    if key not in _STORAGE_CACHE:
        _STORAGE_CACHE[key] = SQLiteStorage(
            DB_FILE, SCHEMA_DIR, SCHEMA_FILES, INSERTABLE_TABLES
        )
    return _STORAGE_CACHE[key]


def connect_db():
    """Open the selected backend's connection for legacy callers."""
    return _get_storage().connect()


def setup_database() -> None:
    """Create missing schema objects without replacing existing data."""
    _get_storage().setup()


def insert_data(table: str, data: Mapping[str, Any]) -> None:
    """Insert a mapping into an allowlisted table using bound values."""
    _get_storage().insert(table, data)


def query_database(
    query: str, parameters: tuple[Any, ...] = ()
) -> list[tuple[Any, ...]]:
    """Execute a SQL query and return its rows.

    Web routes should use specific, parameterized repository functions rather
    than accepting arbitrary SQL.
    """
    return _get_storage().query(query, parameters)


def save_tool_result(
    tool_name: str, query_text: str, result_text: str | None
):
    """Persist a tool run and return its result row ID."""
    return _get_storage().save_tool_result(tool_name, query_text, result_text)


def list_tool_results(
    limit: int = 100, max_result_text_chars: int | None = None, *,
    redact_sensitive: bool = False,
) -> list[tuple[Any, ...]]:
    """Return saved results, optionally truncating result text in storage."""
    _validate_list_limit(limit)
    if max_result_text_chars is not None and (
        not isinstance(max_result_text_chars, int)
        or isinstance(max_result_text_chars, bool)
        or max_result_text_chars < 1
    ):
        raise ValueError("max_result_text_chars must be a positive integer")
    if max_result_text_chars is None:
        rows = _get_storage().list_tool_results(limit)
    else:
        rows = _get_storage().list_tool_results(limit, max_result_text_chars)
    if not redact_sensitive:
        return rows
    return [
        (
            row[0],
            row[1],
            "[redacted]",
            redact_sensitive_text(row[3], row[2]),
            *row[4:],
        )
        for row in rows
    ]


def _validate_list_limit(limit: int) -> None:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")


def list_tool_runs(
    limit: int = 20, offset: int = 0, tool_name: str | None = None,
    status: str | None = None, cursor: int | None = None,
    before_run_id: int | None = None, *, redact_sensitive: bool = False,
) -> list[tuple[Any, ...]]:
    """Return a bounded, newest-first run page using stable run-ID cursors."""
    _validate_list_limit(limit)
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    if tool_name is not None and (not isinstance(tool_name, str) or not tool_name):
        raise ValueError("tool_name must be a non-empty string")
    if status is not None and (
        not isinstance(status, str)
        or status not in {"running", "success", "failed", "timed_out"}
    ):
        raise ValueError("Invalid run status")
    if before_run_id is not None:
        if cursor is not None:
            raise ValueError("Specify only one run-ID cursor")
        cursor = before_run_id
    if cursor is not None:
        cursor = _validate_run_id(cursor)

    if cursor is None:
        query = (
            """SELECT runID, toolName, queryText, status, startedAt, finishedAt,
                      errorText
               FROM ToolRuns
               WHERE (? IS NULL OR toolName = ?)
                 AND (? IS NULL OR status = ?)
               ORDER BY runID DESC LIMIT ? OFFSET ?"""
        )
        parameters = (tool_name, tool_name, status, status, limit, offset)
    else:
        query = (
            """SELECT runID, toolName, queryText, status, startedAt, finishedAt,
                      errorText
               FROM ToolRuns
               WHERE runID < ?
                 AND (? IS NULL OR toolName = ?)
                 AND (? IS NULL OR status = ?)
               ORDER BY runID DESC LIMIT ?"""
        )
        parameters = (cursor, tool_name, tool_name, status, status, limit)
    rows = _get_storage().query(query, parameters)
    if not redact_sensitive:
        return rows
    return [
        (
            row[0],
            row[1],
            "[redacted]",
            row[3],
            row[4],
            row[5],
            redact_sensitive_text(row[6], row[2]),
        )
        for row in rows
    ]


def _validate_run_id(run_id: int) -> int:
    if (
        not isinstance(run_id, int)
        or isinstance(run_id, bool)
        or run_id < 1
        or run_id > SQLITE_MAX_INTEGER
    ):
        raise ValueError("run ID must be a positive SQLite integer")
    return run_id


def get_tool_run(
    run_id: int, *, redact_sensitive: bool = False
) -> tuple[Any, ...] | None:
    """Return a run and its persisted result or error, if it exists."""
    run_id = _validate_run_id(run_id)
    rows = _get_storage().query(
        """SELECT runID, toolName, queryText, status, resultText, errorText,
                  startedAt, finishedAt
           FROM ToolRuns WHERE runID = ?""",
        (run_id,),
    )
    if not rows:
        return None
    row = rows[0]
    if not redact_sensitive:
        return row
    return (
        row[0],
        row[1],
        "[redacted]",
        row[3],
        redact_sensitive_text(row[4], row[2]),
        redact_sensitive_text(row[5], row[2]),
        row[6],
        row[7],
    )


def reconcile_stale_tool_runs(older_than_seconds: int = 600) -> int:
    """Mark runs older than the tool timeout as interrupted after lost outcomes."""
    if (
        not isinstance(older_than_seconds, int)
        or isinstance(older_than_seconds, bool)
        or older_than_seconds < 1
    ):
        raise ValueError("older_than_seconds must be a positive integer")
    return _get_storage().reconcile_stale_tool_runs(older_than_seconds)


def get_tool_result(
    result_id: int, *, redact_sensitive: bool = False
) -> dict[str, Any] | None:
    """Return one saved result without exposing its original query text."""
    result_id = _validate_run_id(result_id)
    rows = _get_storage().query(
        """SELECT resultID, toolName, queryText, resultText, createdAt
           FROM ToolResults WHERE resultID = ?""",
        (result_id,),
    )
    if not rows:
        return None
    row = rows[0]
    return {
        "result_id": row[0],
        "tool": row[1],
        "result": (
            redact_sensitive_text(row[3], row[2])
            if redact_sensitive else row[3]
        ),
        "created_at": row[4],
    }


def export_tool_runs_json(
    run_ids: list[int], include_sensitive: bool = False,
) -> str:
    """Serialize selected run records as JSON, redacting sensitive text by default."""
    if not isinstance(include_sensitive, bool):
        raise ValueError("include_sensitive must be a boolean")
    if not isinstance(run_ids, list) or not run_ids or len(run_ids) > 500:
        raise ValueError("Select between 1 and 500 runs")
    for run_id in run_ids:
        try:
            _validate_run_id(run_id)
        except ValueError as exc:
            raise ValueError(
                "run_ids must contain positive SQLite integers"
            ) from exc
    if len(set(run_ids)) != len(run_ids):
        raise ValueError("run_ids must not contain duplicates")

    placeholders = ", ".join("?" for _ in run_ids)
    rows = _get_storage().query(
        f"""SELECT runID, toolName, queryText, status, startedAt, finishedAt,
                   resultText, errorText
            FROM ToolRuns WHERE runID IN ({placeholders})""",
        tuple(run_ids),
    )
    by_id = {row[0]: row for row in rows}
    if len(by_id) != len(run_ids):
        raise ValueError("One or more selected runs do not exist")

    records = []
    for run_id in run_ids:
        row = by_id[run_id]
        sensitive_values = row[2], row[6], row[7]
        if not include_sensitive:
            sensitive_values = tuple(
                "[REDACTED]" if value is not None else None
                for value in sensitive_values
            )
        records.append(
            {
                "run_id": row[0],
                "tool": row[1],
                "input": sensitive_values[0],
                "status": row[3],
                "started_at": row[4],
                "finished_at": row[5],
                "result": sensitive_values[1],
                "error": sensitive_values[2],
            }
        )
    return json.dumps(records, ensure_ascii=False, indent=2)


def start_tool_run(tool_name: str, query_text: str):
    """Create a running record before starting an external check."""
    return _get_storage().start_tool_run(tool_name, query_text)


def get_idempotent_tool_run(key: str, request_hash: str) -> int | None:
    return _get_storage().get_idempotent_tool_run(key, request_hash)


def start_idempotent_tool_run(
    tool_name: str, query_text: str, key: str, request_hash: str
) -> tuple[int, bool]:
    return _get_storage().start_idempotent_tool_run(
        tool_name, query_text, key, request_hash
    )


def finish_tool_run(
    run_id: int, status: str, result_text: str | None = None,
    error_text: str | None = None,
) -> None:
    """Finish a run and retain successful results in the legacy results table."""
    run_id = _validate_run_id(run_id)
    if status not in {"success", "failed", "timed_out"}:
        raise ValueError(f"Invalid terminal run status: {status}")
    if (status == "success" and (result_text is None or error_text is not None)) or (
        status != "success" and (error_text is None or result_text is not None)
    ):
        raise ValueError("Success requires a result; failures require an error.")
    _get_storage().finish_tool_run(run_id, status, result_text, error_text)


def get_tool_run_status(run_id: int) -> dict[str, Any] | None:
    """Return non-sensitive status fields for one run."""
    run_id = _validate_run_id(run_id)
    rows = _get_storage().query(
        """SELECT runID, toolName, status, startedAt, finishedAt
           FROM ToolRuns WHERE runID = ?""",
        (run_id,),
    )
    if not rows:
        return None
    return dict(
        zip(
            ("run_id", "tool", "status", "started_at", "finished_at"),
            rows[0],
        )
    )
