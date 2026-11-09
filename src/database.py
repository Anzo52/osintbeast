import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Mapping, Optional

# Keep the default database alongside the repository, independent of the current
# working directory. Tests and deployments may override this value.
DB_FILE = str(Path(__file__).resolve().parent.parent / "osintbeast.db")
SCHEMA_DIR = Path(__file__).resolve().parent.parent / "data"
SCHEMA_FILES = ("sqlite-infoschema.sql", "sqlite-toolschema.sql")

# SQL identifiers cannot be bound as query parameters, so table names are
# restricted to this fixed set and columns are checked against the live schema.
INSERTABLE_TABLES = frozenset(
    {"Platforms", "Names", "Locations", "Domains", "Services", "Urls", "Usernames", "ToolCatalog", "ToolResults"}
)


def connect_db() -> sqlite3.Connection:
    """Open the SQLite database with foreign-key enforcement enabled."""
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def setup_database() -> None:
    """Create all application tables from the checked-in SQLite schemas."""
    with closing(connect_db()) as conn, conn:
        for schema_name in SCHEMA_FILES:
            schema_path = SCHEMA_DIR / schema_name
            if not schema_path.is_file():
                raise FileNotFoundError(f"SQLite schema file not found: {schema_path}")
            conn.executescript(schema_path.read_text(encoding="utf-8"))


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
) -> int:
    """Persist a tool run and return its result row ID."""
    with closing(connect_db()) as conn, conn:
        cursor = conn.execute(
            """INSERT INTO ToolResults (toolName, queryText, resultText)
               VALUES (?, ?, ?)""",
            (tool_name, query_text, result_text),
        )
        return int(cursor.lastrowid)


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
