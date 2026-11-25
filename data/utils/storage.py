import sqlite3
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol
from sqlalchemy import MetaData, Table, create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.engine import URL

_UNPERSISTED_RUN_ERROR = "Run outcome could not be persisted."


class StorageError(Exception):
    """A backend-neutral storage error for application-level handling."""


class IdempotencyConflictError(ValueError):
    """An idempotency key was reused for a different request."""


def _matching_idempotent_run(row, request_hash):
    if row is None:
        return None
    if row[0] != request_hash:
        raise IdempotencyConflictError(
            "Idempotency-Key was already used for a different request."
        )
    return row[1]

class Storage(Protocol):
    def connect(self): ...

    def setup(self) -> None: ...

    def insert(self, table: str, data: Mapping[str, Any]) -> None: ...

    def query(
        self, query: str, parameters: tuple[Any, ...] = ()
    ) -> list[tuple[Any, ...]]: ...

    def save_tool_result(
        self, tool_name: str, query_text: str, result_text: Optional[str]
    ) -> int: ...

    def list_tool_results(
        self, limit: int, max_result_text_chars: Optional[int] = None
    ) -> list[tuple[Any, ...]]: ...

    def start_tool_run(self, tool_name: str, query_text: str) -> int: ...

    def get_idempotent_tool_run(
        self, idempotency_key: str, request_hash: str
    ) -> Optional[int]: ...

    def start_idempotent_tool_run(
        self, tool_name: str, query_text: str, idempotency_key: str,
        request_hash: str,
    ) -> tuple[int, bool]: ...

    def finish_tool_run(
        self,
        run_id: int,
        status: str,
        result_text: Optional[str],
        error_text: Optional[str],
    ) -> None: ...

    def reconcile_stale_tool_runs(self, older_than_seconds: int) -> int: ...


class SQLiteStorage:
    _PRIVATE_FILE_MODE = 0o600
    _COMPANION_SUFFIXES = ("-journal", "-shm", "-wal")

    def __init__(
        self,
        db_file: str,
        schema_dir: Path,
        schema_files: tuple[str, ...],
        insertable_tables: frozenset[str],
    ):
        self.db_file = db_file
        self.schema_dir = schema_dir
        self.schema_files = schema_files
        self.insertable_tables = insertable_tables

    def connect(self) -> sqlite3.Connection:
        db_path = Path(self.db_file)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._enforce_private_permissions()
        conn = sqlite3.connect(self.db_file)
        self._enforce_private_permissions()
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _enforce_private_permissions(self) -> None:
        for path in (
            Path(self.db_file),
            *(Path(self.db_file + suffix) for suffix in self._COMPANION_SUFFIXES),
        ):
            if path.exists():
                path.chmod(self._PRIVATE_FILE_MODE)

    def setup(self) -> None:
        schemas = []
        for schema_name in self.schema_files:
            schema_path = self.schema_dir.joinpath(schema_name)
            if not schema_path.is_file():
                raise FileNotFoundError(
                    f"SQLite schema file not found: {schema_path}"
                )
            schemas.append(schema_path.read_text(encoding="utf-8"))

        with closing(self.connect()) as conn, conn:
            conn.executescript("BEGIN IMMEDIATE;\n" + "\n".join(schemas))

    def insert(self, table: str, data: Mapping[str, Any]) -> None:
        if not data:
            raise ValueError("data must contain at least one column")
        if not isinstance(table, str) or table not in self.insertable_tables:
            raise ValueError(f"Unknown database table: {table}")

        db_path = Path(self.db_file)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._enforce_private_permissions()
        engine = create_engine(URL.create("sqlite", database=str(db_path.resolve())))
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql("PRAGMA foreign_keys = ON")
                self._enforce_private_permissions()
                reflected = Table(table, MetaData(), autoload_with=connection)
                known_columns = {column.name for column in reflected.columns}
                if any(
                    not isinstance(column, str) or column not in known_columns
                    for column in data
                ):
                    raise ValueError(f"Unknown column for table {table}")
                connection.execute(reflected.insert().values(**data))
        finally:
            engine.dispose()

    def query(
        self, query: str, parameters: tuple[Any, ...] = ()
    ) -> list[tuple[Any, ...]]:
        with closing(self.connect()) as conn:
            conn.execute("PRAGMA query_only = ON")
            conn.set_authorizer(
                lambda action, _arg1, _arg2, _database, _source: (
                    sqlite3.SQLITE_DENY
                    if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH)
                    else sqlite3.SQLITE_OK
                )
            )
            return conn.execute(query, parameters).fetchall()

    def save_tool_result(
        self, tool_name: str, query_text: str, result_text: Optional[str]
    ) -> int:
        with closing(self.connect()) as conn, conn:
            cursor = conn.execute(
                """INSERT INTO ToolResults (toolName, queryText, resultText)
                   VALUES (?, ?, ?)""",
                (tool_name, query_text, result_text),
            )
            return cursor.lastrowid

    def list_tool_results(
        self, limit: int, max_result_text_chars: Optional[int] = None
    ) -> list[tuple[Any, ...]]:
        with closing(self.connect()) as conn:
            if max_result_text_chars is None:
                return conn.execute(
                    """SELECT resultID, toolName, queryText, resultText, createdAt
                       FROM ToolResults ORDER BY resultID DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
            marker = "… [truncated]"[:max_result_text_chars]
            prefix_length = max(0, max_result_text_chars - len(marker))
            rows = conn.execute(
                """SELECT resultID, toolName, queryText,
                          SUBSTR(resultText, 1, ?),
                          CASE WHEN SUBSTR(resultText, ?, 1) <> '' THEN 1 ELSE 0 END,
                          createdAt
                   FROM ToolResults ORDER BY resultID DESC LIMIT ?""",
                (prefix_length, prefix_length + 1, limit),
            ).fetchall()
        return [
            (
                row[0],
                row[1],
                row[2],
                None if row[3] is None else row[3] + (marker if row[4] else ""),
                row[5],
            )
            for row in rows
        ]

    def start_tool_run(self, tool_name: str, query_text: str) -> int:
        with closing(self.connect()) as conn, conn:
            cursor = conn.execute(
                """INSERT INTO ToolRuns (toolName, queryText, status)
                   VALUES (?, ?, 'running')""",
                (tool_name, query_text),
            )
            return cursor.lastrowid

    def get_idempotent_tool_run(
        self, idempotency_key: str, request_hash: str
    ) -> Optional[int]:
        with closing(self.connect()) as conn:
            row = conn.execute(
                """SELECT requestHash, runID FROM ToolRunRequests
                   WHERE idempotencyKey = ?""",
                (idempotency_key,),
            ).fetchone()
        return _matching_idempotent_run(row, request_hash)

    def start_idempotent_tool_run(
        self, tool_name: str, query_text: str, idempotency_key: str,
        request_hash: str,
    ) -> tuple[int, bool]:
        with closing(self.connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT requestHash, runID FROM ToolRunRequests
                   WHERE idempotencyKey = ?""",
                (idempotency_key,),
            ).fetchone()
            existing = _matching_idempotent_run(row, request_hash)
            if existing is not None:
                return existing, False
            cursor = conn.execute(
                """INSERT INTO ToolRuns (toolName, queryText, status)
                   VALUES (?, ?, 'running')""",
                (tool_name, query_text),
            )
            run_id = cursor.lastrowid
            conn.execute(
                """INSERT INTO ToolRunRequests
                   (idempotencyKey, requestHash, runID) VALUES (?, ?, ?)""",
                (idempotency_key, request_hash, run_id),
            )
            return run_id, True

    def finish_tool_run(
        self,
        run_id: int,
        status: str,
        result_text: Optional[str],
        error_text: Optional[str],
    ) -> None:
        with closing(self.connect()) as conn, conn:
            cursor = conn.execute(
                """UPDATE ToolRuns SET status = ?, resultText = ?, errorText = ?,
                   finishedAt = CURRENT_TIMESTAMP
                   WHERE runID = ? AND (
                       status = 'running' OR
                       (status = 'failed' AND errorText = ?)
                   )""",
                (status, result_text, error_text, run_id, _UNPERSISTED_RUN_ERROR),
            )
            if cursor.rowcount != 1:
                row = conn.execute(
                    "SELECT status, resultText, errorText FROM ToolRuns WHERE runID = ?",
                    (run_id,),
                ).fetchone()
                if row == (status, result_text, error_text):
                    return
                raise ValueError("Run does not exist or is already finished.")
            if status == "success":
                conn.execute(
                    """INSERT INTO ToolResults (toolName, queryText, resultText)
                       SELECT toolName, queryText, resultText
                       FROM ToolRuns WHERE runID = ?""",
                    (run_id,),
                )

    def reconcile_stale_tool_runs(self, older_than_seconds: int) -> int:
        with closing(self.connect()) as conn, conn:
            cursor = conn.execute(
                """UPDATE ToolRuns SET status = 'failed',
                          errorText = 'Run outcome could not be persisted.',
                          finishedAt = CURRENT_TIMESTAMP
                   WHERE status = 'running' AND startedAt <= datetime('now', ?)""",
                (f"-{older_than_seconds} seconds",),
            )
            return cursor.rowcount


class MySQLStorage:
    def __init__(
        self,
        database_url: str,
        schema_dir: Path,
        schema_files: tuple[str, ...],
        insertable_tables: frozenset[str],
    ):
        self.database_url = database_url
        self.schema_dir = schema_dir
        self.schema_files = schema_files
        self.insertable_tables = insertable_tables
        self._engine = None

    def _get_engine(self):
        if self._engine is None:
            try:
                from sqlalchemy import create_engine
                from sqlalchemy.engine import make_url
            except ImportError as exc:
                raise StorageError(
                    "MySQL support requires the mysql extra: "
                    "python -m pip install 'osintbeast[mysql]'"
                ) from exc

            url = make_url(self.database_url)
            if url.drivername == "mysql":
                url = url.set(drivername="mysql+pymysql")
            elif url.drivername != "mysql+pymysql":
                raise ValueError(
                    "OSINTBEAST_DATABASE_URL must use mysql:// or "
                    "mysql+pymysql://."
                )
            try:
                self._engine = create_engine(url, pool_pre_ping=True)
            except Exception as exc:
                raise StorageError(f"Unable to initialize MySQL driver: {exc}") from exc
        return self._engine

    def connect(self):
        return self._get_engine().raw_connection()

    @staticmethod
    def _validate_query(query: str, sql_modes: frozenset[str]) -> None:
        index = 0
        seen_select = False
        previous = None
        first_word = None
        terminated = False
        while index < len(query):
            char = query[index]
            following = query[index + 1] if index + 1 < len(query) else ""
            if char.isspace():
                index += 1
            elif char == "#" or (
                char == "-" and following == "-" and index + 2 < len(query)
                and (
                    query[index + 2].isspace()
                    or ord(query[index + 2]) < 32
                    or ord(query[index + 2]) == 127
                )
            ):
                index += 1
                while index < len(query) and query[index] not in "\r\n":
                    index += 1
            elif char == "/" and following == "*":
                if query[index + 2:index + 3] == "!" or (
                    query[index + 2:index + 4].upper() == "M!"
                ):
                    raise ValueError("Executable SQL comments are not allowed")
                end = query.find("*/", index + 2)
                if end == -1:
                    raise ValueError("Unterminated SQL comment")
                index = end + 2
            elif char in ("'", '"', "`"):
                if first_word is None or terminated:
                    raise ValueError("MySQL queries must be SELECT or WITH statements")
                quote = char
                backslash_escapes = (
                    "NO_BACKSLASH_ESCAPES" not in sql_modes
                    and quote != "`"
                    and not (quote == '"' and "ANSI_QUOTES" in sql_modes)
                )
                index += 1
                while index < len(query):
                    if backslash_escapes and query[index] == "\\":
                        index += 2
                    elif query[index] == quote:
                        index += 1
                        if index < len(query) and query[index] == quote:
                            index += 1
                        else:
                            break
                    else:
                        index += 1
                else:
                    raise ValueError("Unterminated SQL quote")
                previous = None
            elif char.isalnum() or char in "_$":
                if terminated:
                    raise ValueError("Multiple SQL statements are not allowed")
                start = index
                index += 1
                while index < len(query) and (
                    query[index].isalnum() or query[index] in "_$"
                ):
                    index += 1
                word = query[start:index].upper()
                if first_word is None:
                    first_word = word
                    if word not in ("SELECT", "WITH"):
                        raise ValueError("MySQL queries must be SELECT or WITH statements")
                if seen_select and previous == "INTO" and word in ("OUTFILE", "DUMPFILE"):
                    raise ValueError("SELECT INTO OUTFILE or DUMPFILE is not allowed")
                seen_select = seen_select or word == "SELECT"
                previous = word
            else:
                if first_word is None or terminated:
                    raise ValueError("MySQL queries must be SELECT or WITH statements")
                if char == ";":
                    terminated = True
                previous = None
                index += 1
        if first_word is None:
            raise ValueError("MySQL queries must be SELECT or WITH statements")

    @staticmethod
    def _mysql_placeholders(
        query: str, parameters: tuple[Any, ...], sql_modes: frozenset[str] = frozenset()
    ) -> str:
        if not parameters:
            return query

        converted = []
        quote = None
        escaped = False
        count = 0
        line_comment = False
        block_comment = False
        index = 0
        while index < len(query):
            char = query[index]
            following = query[index + 1] if index + 1 < len(query) else ""
            if line_comment:
                converted.append(char)
                line_comment = char not in "\r\n"
            elif block_comment:
                converted.append(char)
                if char == "*" and following == "/":
                    converted.append(following)
                    block_comment = False
                    index += 1
            elif quote:
                converted.append(char)
                if escaped:
                    escaped = False
                elif (
                    char == "\\" and "NO_BACKSLASH_ESCAPES" not in sql_modes
                    and quote != "`"
                    and not (quote == '"' and "ANSI_QUOTES" in sql_modes)
                ):
                    escaped = True
                elif char == quote:
                    quote = None
            elif char == "#" or (
                char == "-" and following == "-" and
                (
                    index + 2 == len(query)
                    or query[index + 2].isspace()
                    or ord(query[index + 2]) < 32
                    or ord(query[index + 2]) == 127
                )
            ):
                converted.append(char)
                if char == "-":
                    converted.append(following)
                    index += 1
                line_comment = True
            elif char == "/" and following == "*":
                converted.extend((char, following))
                block_comment = True
                index += 1
            elif char in ("'", '"', "`"):
                quote = char
                converted.append(char)
            elif char == "?":
                converted.append("%s")
                count += 1
            else:
                converted.append(char)
            index += 1
        if count != len(parameters):
            raise ValueError("SQL placeholder count does not match parameters")
        return "".join(converted)

    @staticmethod
    def _normalize_rows(rows) -> list[tuple[Any, ...]]:
        return [
            tuple(
                value.isoformat(sep=" ")
                if isinstance(value, datetime)
                else value.isoformat()
                if isinstance(value, date)
                else value
                for value in row
            )
            for row in rows
        ]

    def setup(self) -> None:
        schemas = []
        for schema_name in self.schema_files:
            schema_path = self.schema_dir.joinpath(schema_name)
            if not schema_path.is_file():
                raise FileNotFoundError(
                    f"MySQL schema file not found: {schema_path}"
                )
            schemas.extend(
                statement.strip()
                for statement in schema_path.read_text(encoding="utf-8").split(";")
                if statement.strip()
            )

        try:
            with self._get_engine().begin() as connection:
                for statement in schemas:
                    connection.exec_driver_sql(statement)
        except Exception as exc:
            if exc.__class__.__module__.startswith("sqlalchemy"):
                raise StorageError(str(exc)) from exc
            raise

    def insert(self, table: str, data: Mapping[str, Any]) -> None:
        if not data:
            raise ValueError("data must contain at least one column")
        if not isinstance(table, str) or table not in self.insertable_tables:
            raise ValueError(f"Unknown database table: {table}")

        columns = list(data)
        quoted_columns = ", ".join(f"`{column}`" for column in columns)
        placeholders = ", ".join("%s" for _ in columns)
        query = (
            f"INSERT INTO `{table}` ({quoted_columns}) VALUES ({placeholders})"
        )
        try:
            with self._get_engine().begin() as connection:
                valid_columns = {
                    row[0]
                    for row in connection.exec_driver_sql(
                        """SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
                           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s""",
                        (table,),
                    ).fetchall()
                }
                if any(
                    not isinstance(column, str) or column not in valid_columns
                    for column in columns
                ):
                    raise ValueError(f"Unknown column for table {table}")
                connection.exec_driver_sql(
                    query, tuple(data[column] for column in columns)
                )
        except Exception as exc:
            if exc.__class__.__module__.startswith("sqlalchemy"):
                raise StorageError(str(exc)) from exc
            raise

    def query(
        self, query: str, parameters: tuple[Any, ...] = ()
    ) -> list[tuple[Any, ...]]:
        try:
            with self._get_engine().connect() as connection:
                sql_mode = connection.exec_driver_sql(
                    "SELECT @@SESSION.sql_mode"
                ).scalar_one()
                sql_modes = frozenset(mode.strip().upper() for mode in sql_mode.split(","))
                self._validate_query(query, sql_modes)
                query = self._mysql_placeholders(query, parameters, sql_modes)
                connection.exec_driver_sql("START TRANSACTION READ ONLY")
                result = connection.exec_driver_sql(query, parameters)
                if not getattr(result, "returns_rows", True):
                    return []
                return self._normalize_rows(result.fetchall())
        except Exception as exc:
            if exc.__class__.__module__.startswith("sqlalchemy"):
                raise StorageError(str(exc)) from exc
            raise

    def save_tool_result(
        self, tool_name: str, query_text: str, result_text: Optional[str]
    ) -> int:
        try:
            with self._get_engine().begin() as connection:
                result = connection.exec_driver_sql(
                    """INSERT INTO ToolResults (toolName, queryText, resultText)
                       VALUES (%s, %s, %s)""",
                    (tool_name, query_text, result_text),
                )
                return result.lastrowid
        except Exception as exc:
            if exc.__class__.__module__.startswith("sqlalchemy"):
                raise StorageError(str(exc)) from exc
            raise

    def list_tool_results(
        self, limit: int, max_result_text_chars: Optional[int] = None
    ) -> list[tuple[Any, ...]]:
        try:
            with self._get_engine().connect() as connection:
                if max_result_text_chars is None:
                    rows = connection.exec_driver_sql(
                        """SELECT resultID, toolName, queryText, resultText, createdAt
                           FROM ToolResults ORDER BY resultID DESC LIMIT %s""",
                        (limit,),
                    ).fetchall()
                else:
                    marker = "… [truncated]"[:max_result_text_chars]
                    prefix_length = max(0, max_result_text_chars - len(marker))
                    rows = connection.exec_driver_sql(
                        """SELECT resultID, toolName, queryText,
                                  SUBSTR(resultText, 1, %s),
                                  CASE WHEN SUBSTR(resultText, %s, 1) <> ''
                                       THEN 1 ELSE 0 END,
                                  createdAt
                           FROM ToolResults ORDER BY resultID DESC LIMIT %s""",
                        (prefix_length, prefix_length + 1, limit),
                    ).fetchall()
                    rows = [
                        (
                            row[0],
                            row[1],
                            row[2],
                            None if row[3] is None else row[3] + (marker if row[4] else ""),
                            row[5],
                        )
                        for row in rows
                    ]
                return self._normalize_rows(rows)
        except Exception as exc:
            if exc.__class__.__module__.startswith("sqlalchemy"):
                raise StorageError(str(exc)) from exc
            raise

    def start_tool_run(self, tool_name: str, query_text: str) -> int:
        try:
            with self._get_engine().begin() as connection:
                result = connection.exec_driver_sql(
                    """INSERT INTO ToolRuns (toolName, queryText, status)
                       VALUES (%s, %s, 'running')""",
                    (tool_name, query_text),
                )
                return result.lastrowid
        except Exception as exc:
            if exc.__class__.__module__.startswith("sqlalchemy"):
                raise StorageError(str(exc)) from exc
            raise

    def get_idempotent_tool_run(
        self, idempotency_key: str, request_hash: str
    ) -> Optional[int]:
        try:
            with self._get_engine().connect() as connection:
                row = connection.exec_driver_sql(
                    """SELECT requestHash, runID FROM ToolRunRequests
                       WHERE idempotencyKey = %s""",
                    (idempotency_key,),
                ).fetchone()
        except Exception as exc:
            if exc.__class__.__module__.startswith("sqlalchemy"):
                raise StorageError(str(exc)) from exc
            raise
        return _matching_idempotent_run(row, request_hash)

    def start_idempotent_tool_run(
        self, tool_name: str, query_text: str, idempotency_key: str,
        request_hash: str,
    ) -> tuple[int, bool]:
        try:
            with self._get_engine().begin() as connection:
                row = connection.exec_driver_sql(
                    """SELECT requestHash, runID FROM ToolRunRequests
                       WHERE idempotencyKey = %s FOR UPDATE""",
                    (idempotency_key,),
                ).fetchone()
                existing = _matching_idempotent_run(row, request_hash)
                if existing is not None:
                    return existing, False
                result = connection.exec_driver_sql(
                    """INSERT INTO ToolRuns (toolName, queryText, status)
                       VALUES (%s, %s, 'running')""",
                    (tool_name, query_text),
                )
                run_id = result.lastrowid
                connection.exec_driver_sql(
                    """INSERT INTO ToolRunRequests
                       (idempotencyKey, requestHash, runID) VALUES (%s, %s, %s)""",
                    (idempotency_key, request_hash, run_id),
                )
                return run_id, True
        except IntegrityError as exc:
            existing = self.get_idempotent_tool_run(
                idempotency_key, request_hash
            )
            if existing is not None:
                return existing, False
            raise StorageError(str(exc)) from exc
        except Exception as exc:
            if exc.__class__.__module__.startswith("sqlalchemy"):
                raise StorageError(str(exc)) from exc
            raise

    def finish_tool_run(
        self,
        run_id: int,
        status: str,
        result_text: Optional[str],
        error_text: Optional[str],
    ) -> None:
        try:
            with self._get_engine().begin() as connection:
                result = connection.exec_driver_sql(
                    """UPDATE ToolRuns SET status = %s, resultText = %s,
                       errorText = %s, finishedAt = CURRENT_TIMESTAMP
                       WHERE runID = %s AND (
                           status = 'running' OR
                           (status = 'failed' AND errorText = %s)
                       )""",
                    (status, result_text, error_text, run_id, _UNPERSISTED_RUN_ERROR),
                )
                if result.rowcount != 1:
                    row = connection.exec_driver_sql(
                        "SELECT status, resultText, errorText FROM ToolRuns WHERE runID = %s",
                        (run_id,),
                    ).fetchone()
                    if row == (status, result_text, error_text):
                        return
                    raise ValueError("Run does not exist or is already finished.")
                if status == "success":
                    connection.exec_driver_sql(
                        """INSERT INTO ToolResults (toolName, queryText, resultText)
                           SELECT toolName, queryText, resultText
                           FROM ToolRuns WHERE runID = %s""",
                        (run_id,),
                    )
        except Exception as exc:
            if exc.__class__.__module__.startswith("sqlalchemy"):
                raise StorageError(str(exc)) from exc
            raise

    def reconcile_stale_tool_runs(self, older_than_seconds: int) -> int:
        try:
            with self._get_engine().begin() as connection:
                result = connection.exec_driver_sql(
                    """UPDATE ToolRuns SET status = 'failed',
                              errorText = 'Run outcome could not be persisted.',
                              finishedAt = UTC_TIMESTAMP()
                       WHERE status = 'running' AND
                             startedAt <= DATE_SUB(UTC_TIMESTAMP(), INTERVAL %s SECOND)""",
                    (older_than_seconds,),
                )
                return result.rowcount
        except Exception as exc:
            if exc.__class__.__module__.startswith("sqlalchemy"):
                raise StorageError(str(exc)) from exc
            raise
