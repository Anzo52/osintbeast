import sqlite3
from collections.abc import Mapping
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import MetaData, Table, create_engine
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError

_UNPERSISTED_RUN_ERROR = "Run outcome could not be persisted."
_INDETERMINATE_RUN_ERROR = "Worker outcome is unknown after process termination."
_DATABASE_TIMEOUT_SECONDS = 5


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
        self, tool_name: str, query_text: str, result_text: str | None
    ) -> int: ...

    def list_tool_results(
        self,
        limit: int,
        max_result_text_chars: int | None = None,
        *,
        include_truncation_status: bool = False,
        max_query_text_chars: int | None = None,
    ) -> list[tuple[Any, ...]]: ...

    def start_tool_run(self, tool_name: str, query_text: str) -> int: ...

    def create_background_run(
        self, tool_name: str, query_text: str, key_hash: str | None,
        input_fingerprint: str | None, owner_token: str, lease_expires_at: int,
    ) -> tuple[int, bool, int | None]: ...

    def ensure_background_run(self, run_id: int) -> None: ...

    def claim_background_run(
        self, run_id: int, owner_token: str, lease_expires_at: int
    ) -> int | None: ...

    def renew_background_run(
        self, run_id: int, owner_token: str, attempt: int, now: int,
        lease_expires_at: int,
    ) -> bool: ...

    def get_background_run_state(self, run_id: int) -> tuple[Any, ...] | None: ...

    def list_background_runs_for_recovery(self, now: int) -> list[tuple[Any, ...]]: ...

    def finish_background_run(
        self, run_id: int, owner_token: str, attempt: int, status: str,
        result_text: str | None, error_text: str | None,
    ) -> bool: ...

    def cancel_background_run(self, run_id: int) -> bool: ...

    def abandon_queued_background_run(self, run_id: int, now: int) -> bool: ...

    def mark_background_run_indeterminate(
        self, run_id: int, owner_token: str, attempt: int, now: int
    ) -> bool: ...

    def get_idempotent_tool_run(
        self, idempotency_key: str, request_hash: str
    ) -> int | None: ...

    def start_idempotent_tool_run(
        self, tool_name: str, query_text: str, idempotency_key: str,
        request_hash: str,
    ) -> tuple[int, bool]: ...

    def finish_tool_run(
        self,
        run_id: int,
        status: str,
        result_text: str | None,
        error_text: str | None,
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
        conn = sqlite3.connect(self.db_file, timeout=_DATABASE_TIMEOUT_SECONDS)
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
        engine = create_engine(
            URL.create("sqlite", database=str(db_path.resolve())),
            connect_args={"timeout": _DATABASE_TIMEOUT_SECONDS},
        )
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
        self, tool_name: str, query_text: str, result_text: str | None
    ) -> int:
        with closing(self.connect()) as conn, conn:
            cursor = conn.execute(
                """INSERT INTO ToolResults (toolName, queryText, resultText)
                   VALUES (?, ?, ?)""",
                (tool_name, query_text, result_text),
            )
            return cursor.lastrowid

    def list_tool_results(
        self,
        limit: int,
        max_result_text_chars: int | None = None,
        *,
        include_truncation_status: bool = False,
        max_query_text_chars: int | None = None,
    ) -> list[tuple[Any, ...]]:
        if max_query_text_chars is not None and (
            not isinstance(max_query_text_chars, int)
            or isinstance(max_query_text_chars, bool)
            or max_query_text_chars < 1
            or max_result_text_chars is None
            or not include_truncation_status
        ):
            raise ValueError("max_query_text_chars requires a bounded redaction read")
        with closing(self.connect()) as conn:
            if max_result_text_chars is None:
                rows = conn.execute(
                    """SELECT resultID, toolName, queryText, resultText, createdAt
                       FROM ToolResults ORDER BY resultID DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
                if include_truncation_status:
                    return [(*row, False) for row in rows]
                return rows
            if max_query_text_chars is not None:
                rows = conn.execute(
                    """SELECT resultID, toolName,
                              CASE
                                  WHEN LENGTH(SUBSTR(queryText, ?, 1)) > 0
                                  THEN NULL ELSE queryText
                              END,
                              SUBSTR(resultText, 1, ?),
                              CASE WHEN LENGTH(SUBSTR(resultText, ?, 1)) > 0 THEN 1 ELSE 0 END,
                              createdAt,
                              CASE
                                  WHEN LENGTH(SUBSTR(queryText, ?, 1)) > 0
                                  THEN 1 ELSE 0
                              END
                       FROM ToolResults ORDER BY resultID DESC LIMIT ?""",
                    (
                        max_query_text_chars + 1,
                        max_result_text_chars,
                        max_result_text_chars + 1,
                        max_query_text_chars + 1,
                        limit,
                    ),
                ).fetchall()
                return [
                    (
                        row[0],
                        row[1],
                        row[2],
                        row[3],
                        row[5],
                        bool(row[4]),
                        bool(row[6]),
                    )
                    for row in rows
                ]
            if include_truncation_status:
                rows = conn.execute(
                    """SELECT resultID, toolName, queryText,
                              SUBSTR(resultText, 1, ?),
                              CASE WHEN LENGTH(SUBSTR(resultText, ?, 1)) > 0 THEN 1 ELSE 0 END,
                              createdAt
                       FROM ToolResults ORDER BY resultID DESC LIMIT ?""",
                    (max_result_text_chars, max_result_text_chars + 1, limit),
                ).fetchall()
                return [
                    (row[0], row[1], row[2], row[3], row[5], bool(row[4]))
                    for row in rows
                ]
            marker = "… [truncated]"[:max_result_text_chars]
            prefix_length = max(0, max_result_text_chars - len(marker))
            rows = conn.execute(
                """SELECT resultID, toolName, queryText,
                          SUBSTR(resultText, 1, ?),
                          CASE WHEN LENGTH(SUBSTR(resultText, ?, 1)) > 0 THEN 1 ELSE 0 END,
                          createdAt
                   FROM ToolResults ORDER BY resultID DESC LIMIT ?""",
                (max_result_text_chars, max_result_text_chars + 1, limit),
            ).fetchall()
        return [
            (
                row[0],
                row[1],
                row[2],
                None if row[3] is None else (
                    row[3][:prefix_length] + marker if row[4] else row[3]
                ),
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
            run_id = cursor.lastrowid
            return run_id

    def create_background_run(
        self, tool_name: str, query_text: str, key_hash: str | None,
        input_fingerprint: str | None, owner_token: str, lease_expires_at: int,
    ) -> tuple[int, bool, int | None]:
        with closing(self.connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            if key_hash is not None:
                row = conn.execute(
                    """SELECT runID, toolName, inputFingerprint
                       FROM ToolRunIdempotency WHERE keyHash = ?""",
                    (key_hash,),
                ).fetchone()
                if row is not None:
                    if row[1:] != (tool_name, input_fingerprint):
                        raise IdempotencyConflictError(
                            "Idempotency-Key was already used for a different request."
                        )
                    return row[0], False, None
            cursor = conn.execute(
                """INSERT INTO ToolRuns (toolName, queryText, status)
                   VALUES (?, ?, 'running')""",
                (tool_name, query_text),
            )
            run_id = cursor.lastrowid
            conn.execute(
                """INSERT INTO ToolRunExecutions
                   (runID, state, attempt, ownerToken, leaseExpiresAt)
                   VALUES (?, 'running', 1, ?, ?)""",
                (run_id, owner_token, lease_expires_at),
            )
            if key_hash is not None:
                conn.execute(
                    """INSERT INTO ToolRunIdempotency
                       (keyHash, toolName, inputFingerprint, runID)
                       VALUES (?, ?, ?, ?)""",
                    (key_hash, tool_name, input_fingerprint, run_id),
                )
            return run_id, True, 1

    def ensure_background_run(self, run_id: int) -> None:
        with closing(self.connect()) as conn, conn:
            conn.execute(
                """INSERT OR IGNORE INTO ToolRunExecutions (runID, state)
                   VALUES (?, 'queued')""",
                (run_id,),
            )

    def claim_background_run(
        self, run_id: int, owner_token: str, lease_expires_at: int
    ) -> int | None:
        with closing(self.connect()) as conn, conn:
            cursor = conn.execute(
                """UPDATE ToolRunExecutions
                   SET state = 'running', attempt = attempt + 1,
                       ownerToken = ?, leaseExpiresAt = ?
                   WHERE runID = ? AND state = 'queued'""",
                (owner_token, lease_expires_at, run_id),
            )
            if cursor.rowcount != 1:
                return None
            return conn.execute(
                "SELECT attempt FROM ToolRunExecutions WHERE runID = ?",
                (run_id,),
            ).fetchone()[0]

    def renew_background_run(
        self, run_id: int, owner_token: str, attempt: int, now: int,
        lease_expires_at: int,
    ) -> bool:
        with closing(self.connect()) as conn, conn:
            cursor = conn.execute(
                """UPDATE ToolRunExecutions SET leaseExpiresAt = ?
                   WHERE runID = ? AND state = 'running'
                     AND ownerToken = ? AND attempt = ?
                     AND leaseExpiresAt > ? AND leaseExpiresAt < ?""",
                (lease_expires_at, run_id, owner_token, attempt, now, lease_expires_at),
            )
            return cursor.rowcount == 1

    def get_background_run_state(self, run_id: int) -> tuple[Any, ...] | None:
        with closing(self.connect()) as conn:
            return conn.execute(
                """SELECT state, attempt, ownerToken, leaseExpiresAt
                   FROM ToolRunExecutions WHERE runID = ?""",
                (run_id,),
            ).fetchone()

    def list_background_runs_for_recovery(self, now: int) -> list[tuple[Any, ...]]:
        with closing(self.connect()) as conn:
            return conn.execute(
                """SELECT runID, state, attempt, ownerToken, leaseExpiresAt,
                          toolName, queryText
                   FROM ToolRunExecutions
                   JOIN ToolRuns USING (runID)
                   WHERE (state = 'queued'
                          AND CAST(strftime('%s', startedAt) AS INTEGER) * 1000 <= ? - 30000)
                      OR (state = 'running' AND leaseExpiresAt <= ?)
                   ORDER BY runID""",
                (now, now),
            ).fetchall()

    def finish_background_run(
        self, run_id: int, owner_token: str, attempt: int, status: str,
        result_text: str | None, error_text: str | None,
    ) -> bool:
        run_status = "failed" if status in {"cancelled", "indeterminate"} else status
        with closing(self.connect()) as conn, conn:
            cursor = conn.execute(
                """UPDATE ToolRunExecutions
                   SET state = ?, ownerToken = NULL, leaseExpiresAt = NULL
                   WHERE runID = ? AND state = 'running'
                     AND ownerToken = ? AND attempt = ?""",
                (status, run_id, owner_token, attempt),
            )
            if cursor.rowcount != 1:
                row = conn.execute(
                    """SELECT state FROM ToolRunExecutions
                       WHERE runID = ?""",
                    (run_id,),
                ).fetchone()
                if row is None or row[0] != status:
                    return False
                current = conn.execute(
                    "SELECT resultText, errorText FROM ToolRuns WHERE runID = ?",
                    (run_id,),
                ).fetchone()
                return current == (result_text, error_text)
            conn.execute(
                """UPDATE ToolRuns SET status = ?, resultText = ?, errorText = ?,
                   finishedAt = CURRENT_TIMESTAMP WHERE runID = ?""",
                (run_status, result_text, error_text, run_id),
            )
            if status == "success":
                conn.execute(
                    """INSERT INTO ToolResults (toolName, queryText, resultText)
                       SELECT toolName, queryText, resultText
                       FROM ToolRuns WHERE runID = ?""",
                    (run_id,),
                )
            return True

    def cancel_background_run(self, run_id: int) -> bool:
        with closing(self.connect()) as conn, conn:
            cursor = conn.execute(
                """UPDATE ToolRunExecutions
                   SET state = 'cancelled', ownerToken = NULL,
                       leaseExpiresAt = NULL
                   WHERE runID = ? AND state IN ('queued', 'running')""",
                (run_id,),
            )
            if cursor.rowcount != 1:
                return False
            conn.execute(
                """UPDATE ToolRuns SET status = 'failed', resultText = NULL,
                   errorText = 'Run cancelled.', finishedAt = CURRENT_TIMESTAMP
                   WHERE runID = ?""",
                (run_id,),
            )
            return True

    def abandon_queued_background_run(self, run_id: int, now: int) -> bool:
        with closing(self.connect()) as conn, conn:
            cursor = conn.execute(
                """UPDATE ToolRunExecutions SET state = 'indeterminate'
                   WHERE runID = ? AND state = 'queued' AND EXISTS (
                       SELECT 1 FROM ToolRuns
                       WHERE runID = ?
                         AND CAST(strftime('%s', startedAt) AS INTEGER) * 1000 <= ? - 30000
                   )""",
                (run_id, run_id, now),
            )
            if cursor.rowcount != 1:
                return False
            conn.execute(
                """UPDATE ToolRuns SET status = 'failed', errorText = ?,
                   finishedAt = CURRENT_TIMESTAMP WHERE runID = ?""",
                (_INDETERMINATE_RUN_ERROR, run_id),
            )
            return True

    def mark_background_run_indeterminate(
        self, run_id: int, owner_token: str, attempt: int, now: int
    ) -> bool:
        with closing(self.connect()) as conn, conn:
            cursor = conn.execute(
                """UPDATE ToolRunExecutions
                   SET state = 'indeterminate', ownerToken = NULL,
                       leaseExpiresAt = NULL
                   WHERE runID = ? AND state = 'running'
                     AND ownerToken = ? AND attempt = ? AND leaseExpiresAt <= ?""",
                (run_id, owner_token, attempt, now),
            )
            if cursor.rowcount != 1:
                return False
            conn.execute(
                """UPDATE ToolRuns SET status = 'failed', resultText = NULL,
                   errorText = ?, finishedAt = CURRENT_TIMESTAMP
                   WHERE runID = ?""",
                (_INDETERMINATE_RUN_ERROR, run_id),
            )
            return True

    def recover_expired_background_run(
        self, run_id: int, owner_token: str, attempt: int, now: int,
        can_retry: bool,
    ) -> bool:
        with closing(self.connect()) as conn, conn:
            if can_retry:
                cursor = conn.execute(
                    """UPDATE ToolRunExecutions
                       SET state = 'queued', ownerToken = NULL, leaseExpiresAt = NULL
                       WHERE runID = ? AND state = 'running'
                         AND ownerToken = ? AND attempt = ? AND leaseExpiresAt <= ?""",
                    (run_id, owner_token, attempt, now),
                )
                return cursor.rowcount == 1
            cursor = conn.execute(
                """UPDATE ToolRunExecutions
                   SET state = 'indeterminate', ownerToken = NULL,
                       leaseExpiresAt = NULL
                   WHERE runID = ? AND state = 'running'
                     AND ownerToken = ? AND attempt = ? AND leaseExpiresAt <= ?""",
                (run_id, owner_token, attempt, now),
            )
            if cursor.rowcount != 1:
                return False
            conn.execute(
                """UPDATE ToolRuns SET status = 'failed', resultText = NULL,
                   errorText = ?, finishedAt = CURRENT_TIMESTAMP
                   WHERE runID = ?""",
                (_INDETERMINATE_RUN_ERROR, run_id),
            )
            return True

    def get_idempotent_tool_run(
        self, idempotency_key: str, request_hash: str
    ) -> int | None:
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
        result_text: str | None,
        error_text: str | None,
    ) -> None:
        with closing(self.connect()) as conn, conn:
            cursor = conn.execute(
                """UPDATE ToolRuns SET status = ?, resultText = ?, errorText = ?,
                   finishedAt = CURRENT_TIMESTAMP
                   WHERE runID = ? AND NOT EXISTS (
                       SELECT 1 FROM ToolRunExecutions WHERE runID = ToolRuns.runID
                   ) AND (
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
                   WHERE status = 'running' AND startedAt <= datetime('now', ?)
                     AND NOT EXISTS (
                         SELECT 1 FROM ToolRunExecutions
                         WHERE runID = ToolRuns.runID
                     )""",
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
                self._engine = create_engine(
                    url,
                    pool_pre_ping=True,
                    pool_timeout=_DATABASE_TIMEOUT_SECONDS,
                    connect_args={
                        "connect_timeout": _DATABASE_TIMEOUT_SECONDS,
                        "read_timeout": _DATABASE_TIMEOUT_SECONDS,
                        "write_timeout": _DATABASE_TIMEOUT_SECONDS,
                    },
                )
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
                return self._normalize_rows(
                    result.fetchall()
                )
        except Exception as exc:
            if exc.__class__.__module__.startswith("sqlalchemy"):
                raise StorageError(str(exc)) from exc
            raise

    def save_tool_result(
        self, tool_name: str, query_text: str, result_text: str | None
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
        self,
        limit: int,
        max_result_text_chars: int | None = None,
        *,
        include_truncation_status: bool = False,
        max_query_text_chars: int | None = None,
    ) -> list[tuple[Any, ...]]:
        if max_query_text_chars is not None and (
            not isinstance(max_query_text_chars, int)
            or isinstance(max_query_text_chars, bool)
            or max_query_text_chars < 1
            or max_result_text_chars is None
            or not include_truncation_status
        ):
            raise ValueError("max_query_text_chars requires a bounded redaction read")
        try:
            with self._get_engine().connect() as connection:
                if max_result_text_chars is None:
                    rows = connection.exec_driver_sql(
                        """SELECT resultID, toolName, queryText, resultText, createdAt
                           FROM ToolResults ORDER BY resultID DESC LIMIT %s""",
                        (limit,),
                    ).fetchall()
                    if include_truncation_status:
                        rows = [(*row, False) for row in rows]
                else:
                    if max_query_text_chars is not None:
                        rows = connection.exec_driver_sql(
                            """SELECT resultID, toolName,
                                      CASE
                                          WHEN LENGTH(SUBSTR(queryText, %s, 1)) > 0
                                          THEN NULL ELSE queryText
                                      END,
                                      SUBSTR(resultText, 1, %s),
                                      CASE WHEN LENGTH(SUBSTR(resultText, %s, 1)) > 0
                                           THEN 1 ELSE 0 END,
                                      createdAt,
                                      CASE
                                          WHEN LENGTH(SUBSTR(queryText, %s, 1)) > 0
                                          THEN 1 ELSE 0
                                      END
                               FROM ToolResults ORDER BY resultID DESC LIMIT %s""",
                            (
                                max_query_text_chars + 1,
                                max_result_text_chars,
                                max_result_text_chars + 1,
                                max_query_text_chars + 1,
                                limit,
                            ),
                        ).fetchall()
                        rows = [
                            (
                                row[0],
                                row[1],
                                row[2],
                                row[3],
                                row[5],
                                bool(row[4]),
                                bool(row[6]),
                            )
                            for row in rows
                        ]
                        return self._normalize_rows(rows)
                    rows = connection.exec_driver_sql(
                        """SELECT resultID, toolName, queryText,
                                  SUBSTR(resultText, 1, %s),
                                  CASE WHEN LENGTH(SUBSTR(resultText, %s, 1)) > 0
                                       THEN 1 ELSE 0 END,
                                  createdAt
                           FROM ToolResults ORDER BY resultID DESC LIMIT %s""",
                        (max_result_text_chars, max_result_text_chars + 1, limit),
                    ).fetchall()
                    if include_truncation_status:
                        rows = [
                            (row[0], row[1], row[2], row[3], row[5], bool(row[4]))
                            for row in rows
                        ]
                    else:
                        marker = "… [truncated]"[:max_result_text_chars]
                        prefix_length = max(0, max_result_text_chars - len(marker))
                        rows = [
                            (
                                row[0],
                                row[1],
                                row[2],
                                None if row[3] is None else (
                                    row[3][:prefix_length] + marker if row[4] else row[3]
                                ),
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
                run_id = result.lastrowid
                return run_id
        except Exception as exc:
            if exc.__class__.__module__.startswith("sqlalchemy"):
                raise StorageError(str(exc)) from exc
            raise

    def create_background_run(
        self, tool_name: str, query_text: str, key_hash: str | None,
        input_fingerprint: str | None, owner_token: str, lease_expires_at: int,
    ) -> tuple[int, bool, int | None]:
        try:
            with self._get_engine().begin() as connection:
                if key_hash is not None:
                    row = connection.exec_driver_sql(
                        """SELECT runID, toolName, inputFingerprint
                           FROM ToolRunIdempotency WHERE keyHash = %s""",
                        (key_hash,),
                    ).fetchone()
                    if row is not None:
                        if tuple(row[1:]) != (tool_name, input_fingerprint):
                            raise IdempotencyConflictError(
                                "Idempotency-Key was already used for a different request."
                            )
                        return row[0], False, None
                result = connection.exec_driver_sql(
                    """INSERT INTO ToolRuns (toolName, queryText, status)
                       VALUES (%s, %s, 'running')""",
                    (tool_name, query_text),
                )
                run_id = result.lastrowid
                connection.exec_driver_sql(
                    """INSERT INTO ToolRunExecutions
                       (runID, state, attempt, ownerToken, leaseExpiresAt)
                       VALUES (%s, 'running', 1, %s, %s)""",
                    (run_id, owner_token, lease_expires_at),
                )
                if key_hash is not None:
                    connection.exec_driver_sql(
                        """INSERT INTO ToolRunIdempotency
                           (keyHash, toolName, inputFingerprint, runID)
                           VALUES (%s, %s, %s, %s)""",
                        (key_hash, tool_name, input_fingerprint, run_id),
                    )
                return run_id, True, 1
        except IntegrityError as exc:
            if key_hash is None:
                raise StorageError(str(exc)) from exc
            try:
                with self._get_engine().connect() as connection:
                    row = connection.exec_driver_sql(
                        """SELECT runID, toolName, inputFingerprint
                           FROM ToolRunIdempotency WHERE keyHash = %s""",
                        (key_hash,),
                    ).fetchone()
            except Exception:
                row = None
            if row is not None:
                if tuple(row[1:]) != (tool_name, input_fingerprint):
                    raise IdempotencyConflictError(
                        "Idempotency-Key was already used for a different request."
                    ) from exc
                return row[0], False, None
            raise StorageError(str(exc)) from exc
        except Exception as exc:
            if exc.__class__.__module__.startswith("sqlalchemy"):
                raise StorageError(str(exc)) from exc
            raise

    def ensure_background_run(self, run_id: int) -> None:
        try:
            with self._get_engine().begin() as connection:
                connection.exec_driver_sql(
                    """INSERT IGNORE INTO ToolRunExecutions (runID, state)
                       VALUES (%s, 'queued')""",
                    (run_id,),
                )
        except Exception as exc:
            if exc.__class__.__module__.startswith("sqlalchemy"):
                raise StorageError(str(exc)) from exc
            raise

    def claim_background_run(
        self, run_id: int, owner_token: str, lease_expires_at: int
    ) -> int | None:
        try:
            with self._get_engine().begin() as connection:
                result = connection.exec_driver_sql(
                    """UPDATE ToolRunExecutions
                       SET state = 'running', attempt = attempt + 1,
                           ownerToken = %s, leaseExpiresAt = %s
                       WHERE runID = %s AND state = 'queued'""",
                    (owner_token, lease_expires_at, run_id),
                )
                if result.rowcount != 1:
                    return None
                row = connection.exec_driver_sql(
                    "SELECT attempt FROM ToolRunExecutions WHERE runID = %s",
                    (run_id,),
                ).fetchone()
                return row[0]
        except Exception as exc:
            if exc.__class__.__module__.startswith("sqlalchemy"):
                raise StorageError(str(exc)) from exc
            raise

    def renew_background_run(
        self, run_id: int, owner_token: str, attempt: int, now: int,
        lease_expires_at: int,
    ) -> bool:
        try:
            with self._get_engine().begin() as connection:
                result = connection.exec_driver_sql(
                    """UPDATE ToolRunExecutions SET leaseExpiresAt = %s
                       WHERE runID = %s AND state = 'running'
                         AND ownerToken = %s AND attempt = %s
                         AND leaseExpiresAt > %s AND leaseExpiresAt < %s""",
                    (lease_expires_at, run_id, owner_token, attempt, now, lease_expires_at),
                )
                return result.rowcount == 1
        except Exception as exc:
            if exc.__class__.__module__.startswith("sqlalchemy"):
                raise StorageError(str(exc)) from exc
            raise

    def get_background_run_state(self, run_id: int) -> tuple[Any, ...] | None:
        try:
            with self._get_engine().connect() as connection:
                return connection.exec_driver_sql(
                    """SELECT state, attempt, ownerToken, leaseExpiresAt
                       FROM ToolRunExecutions WHERE runID = %s""",
                    (run_id,),
                ).fetchone()
        except Exception as exc:
            if exc.__class__.__module__.startswith("sqlalchemy"):
                raise StorageError(str(exc)) from exc
            raise

    def list_background_runs_for_recovery(self, now: int) -> list[tuple[Any, ...]]:
        try:
            with self._get_engine().connect() as connection:
                return connection.exec_driver_sql(
                    """SELECT runID, state, attempt, ownerToken, leaseExpiresAt,
                              toolName, queryText
                       FROM ToolRunExecutions
                       JOIN ToolRuns USING (runID)
                       WHERE (state = 'queued' AND UNIX_TIMESTAMP(startedAt) * 1000 <= %s - 30000)
                          OR (state = 'running' AND leaseExpiresAt <= %s)
                       ORDER BY runID""",
                    (now, now),
                ).fetchall()
        except Exception as exc:
            if exc.__class__.__module__.startswith("sqlalchemy"):
                raise StorageError(str(exc)) from exc
            raise

    def finish_background_run(
        self, run_id: int, owner_token: str, attempt: int, status: str,
        result_text: str | None, error_text: str | None,
    ) -> bool:
        run_status = "failed" if status in {"cancelled", "indeterminate"} else status
        try:
            with self._get_engine().begin() as connection:
                result = connection.exec_driver_sql(
                    """UPDATE ToolRunExecutions
                       SET state = %s, ownerToken = NULL, leaseExpiresAt = NULL
                       WHERE runID = %s AND state = 'running'
                         AND ownerToken = %s AND attempt = %s""",
                    (status, run_id, owner_token, attempt),
                )
                if result.rowcount != 1:
                    row = connection.exec_driver_sql(
                        """SELECT state FROM ToolRunExecutions WHERE runID = %s""",
                        (run_id,),
                    ).fetchone()
                    if row is None or row[0] != status:
                        return False
                    current = connection.exec_driver_sql(
                        """SELECT resultText, errorText FROM ToolRuns
                           WHERE runID = %s""",
                        (run_id,),
                    ).fetchone()
                    return current == (result_text, error_text)
                connection.exec_driver_sql(
                    """UPDATE ToolRuns SET status = %s, resultText = %s,
                       errorText = %s, finishedAt = CURRENT_TIMESTAMP
                       WHERE runID = %s""",
                    (run_status, result_text, error_text, run_id),
                )
                if status == "success":
                    connection.exec_driver_sql(
                        """INSERT INTO ToolResults (toolName, queryText, resultText)
                           SELECT toolName, queryText, resultText
                           FROM ToolRuns WHERE runID = %s""",
                        (run_id,),
                    )
                return True
        except Exception as exc:
            if exc.__class__.__module__.startswith("sqlalchemy"):
                raise StorageError(str(exc)) from exc
            raise

    def cancel_background_run(self, run_id: int) -> bool:
        try:
            with self._get_engine().begin() as connection:
                result = connection.exec_driver_sql(
                    """UPDATE ToolRunExecutions
                       SET state = 'cancelled', ownerToken = NULL,
                           leaseExpiresAt = NULL
                       WHERE runID = %s AND state IN ('queued', 'running')""",
                    (run_id,),
                )
                if result.rowcount != 1:
                    return False
                connection.exec_driver_sql(
                    """UPDATE ToolRuns SET status = 'failed', resultText = NULL,
                       errorText = 'Run cancelled.', finishedAt = CURRENT_TIMESTAMP
                       WHERE runID = %s""",
                    (run_id,),
                )
                return True
        except Exception as exc:
            if exc.__class__.__module__.startswith("sqlalchemy"):
                raise StorageError(str(exc)) from exc
            raise

    def abandon_queued_background_run(self, run_id: int, now: int) -> bool:
        try:
            with self._get_engine().begin() as connection:
                result = connection.exec_driver_sql(
                    """UPDATE ToolRunExecutions SET state = 'indeterminate'
                       WHERE runID = %s AND state = 'queued' AND EXISTS (
                           SELECT 1 FROM ToolRuns
                           WHERE runID = %s
                             AND UNIX_TIMESTAMP(startedAt) * 1000 <= %s - 30000
                       )""",
                    (run_id, run_id, now),
                )
                if result.rowcount != 1:
                    return False
                connection.exec_driver_sql(
                    """UPDATE ToolRuns SET status = 'failed', errorText = %s,
                       finishedAt = CURRENT_TIMESTAMP WHERE runID = %s""",
                    (_INDETERMINATE_RUN_ERROR, run_id),
                )
                return True
        except Exception as exc:
            if exc.__class__.__module__.startswith("sqlalchemy"):
                raise StorageError(str(exc)) from exc
            raise

    def mark_background_run_indeterminate(
        self, run_id: int, owner_token: str, attempt: int, now: int
    ) -> bool:
        try:
            with self._get_engine().begin() as connection:
                result = connection.exec_driver_sql(
                    """UPDATE ToolRunExecutions
                       SET state = 'indeterminate', ownerToken = NULL,
                           leaseExpiresAt = NULL
                       WHERE runID = %s AND state = 'running'
                         AND ownerToken = %s AND attempt = %s
                         AND leaseExpiresAt <= %s""",
                    (run_id, owner_token, attempt, now),
                )
                if result.rowcount != 1:
                    return False
                connection.exec_driver_sql(
                    """UPDATE ToolRuns SET status = 'failed', resultText = NULL,
                       errorText = %s, finishedAt = CURRENT_TIMESTAMP
                       WHERE runID = %s""",
                    (_INDETERMINATE_RUN_ERROR, run_id),
                )
                return True
        except Exception as exc:
            if exc.__class__.__module__.startswith("sqlalchemy"):
                raise StorageError(str(exc)) from exc
            raise

    def recover_expired_background_run(
        self, run_id: int, owner_token: str, attempt: int, now: int,
        can_retry: bool,
    ) -> bool:
        try:
                with self._get_engine().begin() as connection:
                    if can_retry:
                        result = connection.exec_driver_sql(
                            """UPDATE ToolRunExecutions
                               SET state = 'queued', ownerToken = NULL,
                                   leaseExpiresAt = NULL
                               WHERE runID = %s AND state = 'running'
                                 AND ownerToken = %s AND attempt = %s
                                 AND leaseExpiresAt <= %s""",
                            (run_id, owner_token, attempt, now),
                        )
                        return result.rowcount == 1
                    result = connection.exec_driver_sql(
                        """UPDATE ToolRunExecutions
                           SET state = 'indeterminate', ownerToken = NULL,
                               leaseExpiresAt = NULL
                           WHERE runID = %s AND state = 'running'
                             AND ownerToken = %s AND attempt = %s
                             AND leaseExpiresAt <= %s""",
                        (run_id, owner_token, attempt, now),
                    )
                    if result.rowcount != 1:
                        return False
                    connection.exec_driver_sql(
                        """UPDATE ToolRuns SET status = 'failed', resultText = NULL,
                           errorText = %s, finishedAt = CURRENT_TIMESTAMP
                           WHERE runID = %s""",
                        (_INDETERMINATE_RUN_ERROR, run_id),
                    )
                    return True
        except Exception as exc:
                if exc.__class__.__module__.startswith("sqlalchemy"):
                    raise StorageError(str(exc)) from exc
                raise

    def get_idempotent_tool_run(
        self, idempotency_key: str, request_hash: str
    ) -> int | None:
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
        result_text: str | None,
        error_text: str | None,
    ) -> None:
        try:
            with self._get_engine().begin() as connection:
                result = connection.exec_driver_sql(
                    """UPDATE ToolRuns SET status = %s, resultText = %s,
                       errorText = %s, finishedAt = CURRENT_TIMESTAMP
                       WHERE runID = %s AND NOT EXISTS (
                           SELECT 1 FROM ToolRunExecutions WHERE runID = ToolRuns.runID
                       ) AND (
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
                             startedAt <= DATE_SUB(UTC_TIMESTAMP(), INTERVAL %s SECOND)
                         AND NOT EXISTS (
                             SELECT 1 FROM ToolRunExecutions
                             WHERE runID = ToolRuns.runID
                         )""",
                    (older_than_seconds,),
                )
                return result.rowcount
        except Exception as exc:
            if exc.__class__.__module__.startswith("sqlalchemy"):
                raise StorageError(str(exc)) from exc
            raise
