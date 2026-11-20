import os
import sqlite3
import sys
from contextlib import closing
from importlib.resources import files
from pathlib import Path
from typing import Any, Mapping, Optional

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


def connect_db() -> sqlite3.Connection:
    """Open the SQLite database with foreign-key enforcement enabled."""
    Path(DB_FILE).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def setup_database() -> None:
    """Create missing schema objects atomically without replacing existing data."""
    schemas = []
    for schema_name in SCHEMA_FILES:
        schema_path = SCHEMA_DIR.joinpath(schema_name)
        if not schema_path.is_file():
            raise FileNotFoundError(f"SQLite schema file not found: {schema_path}")
        schemas.append(schema_path.read_text(encoding="utf-8"))

    with closing(connect_db()) as conn, conn:
        conn.executescript("BEGIN IMMEDIATE;\n" + "\n".join(schemas))


def insert_data(table: str, data: Mapping[str, Any]) -> None:
    """Insert a mapping into a known table using bound SQLite parameters.

    The table allowlist and live column check constrain SQL identifiers; values
    are always passed separately to SQLite.
    """
    if not data:
        raise ValueError("data must contain at least one column")
    if not isinstance(table, str) or table not in INSERTABLE_TABLES:
        raise ValueError(f"Unknown database table: {table}")

    with closing(connect_db()) as conn, conn:
        valid_columns = {
            row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')
        }
        columns = list(data)
        if any(
            not isinstance(column, str) or column not in valid_columns
            for column in columns
        ):
            raise ValueError(f"Unknown column for table {table}")

        quoted_columns = ", ".join(f'"{column}"' for column in columns)
        placeholders = ", ".join("?" for _ in columns)
        conn.execute(
            f'INSERT INTO "{table}" ({quoted_columns}) VALUES ({placeholders})',
            tuple(data[column] for column in columns),
        )


def query_database(query: str, parameters: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    """Execute a SQL query and return its rows.

    This is retained for the local CLI. Web routes should use specific,
    parameterized repository functions rather than accepting arbitrary SQL.
    """
    with closing(connect_db()) as conn:
        cursor = conn.execute(query, parameters)
        return cursor.fetchall()


def save_tool_result(
    tool_name: str, query_text: str, result_text: Optional[str]
):
    """Persist a tool run and return its result row ID."""
    with closing(connect_db()) as conn, conn:
        cursor = conn.execute(
            """INSERT INTO ToolResults (toolName, queryText, resultText)
               VALUES (?, ?, ?)""",
            (tool_name, query_text, result_text),
        )
        return cursor.lastrowid


def list_tool_results(limit: int = 100) -> list[tuple[Any, ...]]:
    """Return recent tool runs, newest first, with a bounded page size."""
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    with closing(connect_db()) as conn:
        return conn.execute(
            """SELECT resultID, toolName, queryText, resultText, createdAt
               FROM ToolResults ORDER BY resultID DESC LIMIT ?""",
            (limit,),
        ).fetchall()


def start_tool_run(tool_name: str, query_text: str):
    """Create a running record before starting an external check."""
    with closing(connect_db()) as conn, conn:
        cursor = conn.execute(
            "INSERT INTO ToolRuns (toolName, queryText, status) VALUES (?, ?, 'running')",
            (tool_name, query_text),
        )
        return cursor.lastrowid


def get_tool_run(run_id: int) -> Optional[tuple[Any, ...]]:
    """Return one run and its result, or None if it does not exist."""
    with closing(connect_db()) as conn:
        return conn.execute(
            """SELECT runID, toolName, queryText, status, resultText, errorText,
                      startedAt, finishedAt FROM ToolRuns WHERE runID = ?""",
            (run_id,),
        ).fetchone()


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
    with closing(connect_db()) as conn, conn:
        cursor = conn.execute(
            """UPDATE ToolRuns SET status = ?, resultText = ?, errorText = ?,
               finishedAt = CURRENT_TIMESTAMP WHERE runID = ? AND status = 'running'""",
            (status, result_text, error_text, run_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("Run does not exist or is already finished.")
        if status == "success":
            conn.execute(
                """INSERT INTO ToolResults (toolName, queryText, resultText)
                   SELECT toolName, queryText, resultText FROM ToolRuns WHERE runID = ?""",
                (run_id,),
            )
