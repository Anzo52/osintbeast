import os
import sqlite3
import sys
from importlib.resources import files
from pathlib import Path
from typing import Any, Mapping, Optional

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

    This is retained for the local CLI. Web routes should use specific,
    parameterized repository functions rather than accepting arbitrary SQL.
    """
    return _get_storage().query(query, parameters)


def save_tool_result(
    tool_name: str, query_text: str, result_text: Optional[str]
):
    """Persist a tool run and return its result row ID."""
    return _get_storage().save_tool_result(tool_name, query_text, result_text)


def list_tool_results(limit: int = 100) -> list[tuple[Any, ...]]:
    """Return recent tool runs, newest first, with a bounded page size."""
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    return _get_storage().list_tool_results(limit)


def list_tool_runs(limit: int = 100) -> list[tuple[Any, ...]]:
    """Return recent tool runs, newest first, with a bounded page size."""
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    with closing(connect_db()) as conn:
        return conn.execute(
            """SELECT runID, toolName, queryText, status, startedAt, finishedAt
               FROM ToolRuns ORDER BY runID DESC LIMIT ?""",
            (limit,),
        ).fetchall()


def get_tool_run(run_id: int) -> Optional[tuple[Any, ...]]:
    """Return a run and its persisted result or error, if it exists."""
    with closing(connect_db()) as conn:
        return conn.execute(
            """SELECT runID, toolName, queryText, status, resultText, errorText,
                      startedAt, finishedAt
               FROM ToolRuns WHERE runID = ?""",
            (run_id,),
        ).fetchone()


def start_tool_run(tool_name: str, query_text: str):
    """Create a running record before starting an external check."""
    return _get_storage().start_tool_run(tool_name, query_text)


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
