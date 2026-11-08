import sqlite3
from pathlib import Path
from typing import Any, Mapping, Optional

from sqlalchemy import MetaData, Table, create_engine

# Keep the default database alongside the repository, independent of the current
# working directory. Tests and deployments may override this value.
DB_FILE = str(Path(__file__).resolve().parent.parent / "osintbeast.db")
SCHEMA_DIR = Path(__file__).resolve().parent.parent / "data"
SCHEMA_FILES = ("sqlite-infoschema.sql", "sqlite-toolschema.sql")

# SQL identifiers cannot be bound as query parameters. Restrict reflected table
# names to this fixed set, then let SQLAlchemy Core compile insert identifiers.
INSERTABLE_TABLES = frozenset(
    {"Platforms", "Names", "Locations", "Domains", "Services", "Urls", "Usernames", "ToolCatalog", "ToolResults"}
)


def _database_url() -> str:
    """Build a SQLAlchemy URL for the configured SQLite database file."""
    return f"sqlite:///{Path(DB_FILE).resolve().as_posix()}"


def connect_db() -> sqlite3.Connection:
    """Open the SQLite database with foreign-key enforcement enabled."""
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def setup_database() -> None:
    """Create all application tables from the checked-in SQLite schemas."""
    with connect_db() as conn:
        for schema_name in SCHEMA_FILES:
            schema_path = SCHEMA_DIR / schema_name
            if not schema_path.is_file():
                raise FileNotFoundError(f"SQLite schema file not found: {schema_path}")
            conn.executescript(schema_path.read_text(encoding="utf-8"))


def insert_data(table: str, data: Mapping[str, Any]) -> None:
    """Insert a mapping into a known table using SQLAlchemy Core.

    Identifiers are selected only from the fixed INSERTABLE_TABLES allowlist;
    values are bound by SQLAlchemy rather than interpolated into SQL text.
    """
    if not data:
        raise ValueError("data must contain at least one column")
    if table not in INSERTABLE_TABLES:
        raise ValueError(f"Unknown database table: {table}")

    engine = create_engine(_database_url())
    try:
        metadata = MetaData()
        reflected_table = Table(table, metadata, autoload_with=engine)
        valid_columns = {column.name for column in reflected_table.columns}
        if any(column not in valid_columns for column in data):
            raise ValueError(f"Unknown column for table {table}")

        with engine.begin() as conn:
            conn.execute(reflected_table.insert().values(**dict(data)))
    finally:
        engine.dispose()


def query_database(query: str, parameters: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    """Execute a SQL query and return its rows.

    This is retained for the local CLI. Web routes should use specific,
    parameterized repository functions rather than accepting arbitrary SQL.
    """
    with connect_db() as conn:
        cursor = conn.execute(query, parameters)
        return cursor.fetchall()


def save_tool_result(
    tool_name: str, query_text: str, result_text: Optional[str]
) -> int:
    """Persist a tool run and return its result row ID."""
    with connect_db() as conn:
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
    with connect_db() as conn:
        return conn.execute(
            """SELECT resultID, toolName, queryText, resultText, createdAt
               FROM ToolResults ORDER BY resultID DESC LIMIT ?""",
            (limit,),
        ).fetchall()
