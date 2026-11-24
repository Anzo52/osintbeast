import json
import os
import sqlite3
import sys
from importlib.resources import files
from pathlib import Path
from typing import Any, Mapping, Optional

from data.utils.storage import (
    IdempotencyConflictError as IdempotencyConflictError,
    MySQLStorage,
    SQLiteStorage,
    Storage,
    StorageError,
)

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
    """Create missing schema objects and migrate legacy SQLite records."""
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
    tool_name: str, query_text: str, result_text: Optional[str]
):
    """Persist a tool run and return its result row ID."""
    return _get_storage().save_tool_result(tool_name, query_text, result_text)


def list_tool_results(
    limit: int = 100, max_result_text_chars: Optional[int] = None
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
        return _get_storage().list_tool_results(limit)
    return _get_storage().list_tool_results(limit, max_result_text_chars)


def _validate_list_limit(limit: int) -> None:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")


def list_tool_runs(
    limit: int = 20, offset: int = 0, tool_name: Optional[str] = None,
    status: Optional[str] = None,
) -> list[tuple[Any, ...]]:
    """Return a page of structured run history, newest first."""
    _validate_list_limit(limit)
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    if tool_name is not None and (not isinstance(tool_name, str) or not tool_name):
        raise ValueError("tool_name must be a non-empty string")
    if status is not None and status not in {
        "running", "success", "failed", "timed_out",
    }:
        raise ValueError("Invalid run status")

    return _get_storage().query(
        """SELECT runID, toolName, queryText, status, startedAt, finishedAt,
                  errorText
           FROM ToolRuns
           WHERE (? IS NULL OR toolName = ?)
             AND (? IS NULL OR status = ?)
           ORDER BY runID DESC LIMIT ? OFFSET ?""",
        (tool_name, tool_name, status, status, limit, offset),
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


def _validate_list_limit(limit: int) -> None:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")


def get_tool_run(run_id: int) -> Optional[tuple[Any, ...]]:
    """Return a run and its persisted result or error, if it exists."""
    rows = _get_storage().query(
        """SELECT runID, toolName, queryText, status, resultText, errorText,
                  startedAt, finishedAt
           FROM ToolRuns WHERE runID = ?""",
        (run_id,),
    )
    return rows[0] if rows else None


def get_tool_result(result_id: int) -> Optional[dict[str, Any]]:
    """Return one saved result without exposing its original query text."""
    rows = _get_storage().query(
        """SELECT resultID, toolName, resultText, createdAt
           FROM ToolResults WHERE resultID = ?""",
        (result_id,),
    )
    if not rows:
        return None
    return dict(zip(("result_id", "tool", "result", "created_at"), rows[0]))


def export_tool_runs_json(
    run_ids: list[int], include_sensitive: bool = False,
) -> str:
    """Serialize selected run records as JSON, redacting sensitive text by default."""
    if not run_ids or len(run_ids) > 500:
        raise ValueError("Select between 1 and 500 runs")
    if any(
        not isinstance(run_id, int) or isinstance(run_id, bool) or run_id < 1
        for run_id in run_ids
    ):
        raise ValueError("run_ids must contain positive integers")
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


def _validate_idempotency_key(idempotency_key: str, request_hash: str) -> None:
    if (
        not isinstance(idempotency_key, str)
        or not 16 <= len(idempotency_key) <= 128
        or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in idempotency_key)
    ):
        raise ValueError("Invalid idempotency key.")
    if (
        not isinstance(request_hash, str)
        or len(request_hash) != 64
        or any(char not in "0123456789abcdef" for char in request_hash)
    ):
        raise ValueError("Invalid request hash.")


def get_idempotent_tool_run(
    idempotency_key: str, request_hash: str
) -> Optional[int]:
    """Return a previously created run for a matching request key."""
    _validate_idempotency_key(idempotency_key, request_hash)
    return _get_storage().get_idempotent_tool_run(idempotency_key, request_hash)


def start_idempotent_tool_run(
    tool_name: str,
    query_text: str,
    idempotency_key: str,
    request_hash: str,
) -> tuple[int, bool]:
    """Create one run per key and return its ID and whether it was created."""
    _validate_idempotency_key(idempotency_key, request_hash)
    return _get_storage().start_idempotent_tool_run(
        tool_name, query_text, idempotency_key, request_hash
    )


def finish_tool_run(
    run_id: int, status: str, result_text: Optional[str] = None,
    error_text: Optional[str] = None,
) -> None:
    """Finish a run and retain successful results in the legacy results table."""
    if status not in {"success", "failed", "timed_out"}:
        raise ValueError(f"Invalid terminal run status: {status}")
    if (status == "success" and (result_text is None or error_text is not None)) or (
        status != "success" and (error_text is None or result_text is not None)
    ):
        raise ValueError("Success requires a result; failures require an error.")
    _get_storage().finish_tool_run(run_id, status, result_text, error_text)


def get_tool_run_status(run_id: int) -> Optional[dict[str, Any]]:
    """Return non-sensitive status fields for one run."""
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
