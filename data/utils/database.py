import json
import os
import re
import sqlite3
import sys
from collections.abc import Mapping
from importlib.resources import files
from pathlib import Path
from typing import Any

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
SQLITE_MAX_INTEGER = (1 << 63) - 1
MAX_SQLITE_INTEGER = SQLITE_MAX_INTEGER
MAX_REDACTION_QUERY_CHARS = 4096
DEFAULT_REDACTED_RESULT_TEXT_CHARS = 16384
_PERCENT_BYTE = re.compile(r"%[0-9a-f]{2}", re.IGNORECASE)


def _decoded_text_boundaries(text: str) -> tuple[str, dict[int, int]]:
    decoded = []
    boundaries = {0: 0}
    position = 0
    while position < len(text):
        character = text[position]
        end = position + 1
        if _PERCENT_BYTE.match(text, position):
            first = int(text[position + 1:position + 3], 16)
            size = (
                1 if first < 0x80 else
                2 if 0xC2 <= first <= 0xDF else
                3 if 0xE0 <= first <= 0xEF else
                4 if 0xF0 <= first <= 0xF4 else 0
            )
            if size and all(
                _PERCENT_BYTE.match(text, position + 3 * offset)
                for offset in range(size)
            ):
                encoded = bytes(
                    int(text[position + 3 * offset + 1:position + 3 * offset + 3], 16)
                    for offset in range(size)
                )
                try:
                    character = encoded.decode("utf-8")
                    end = position + 3 * size
                except UnicodeDecodeError:
                    pass
        decoded.append(character)
        boundaries[end] = len(decoded)
        position = end
    return "".join(decoded), boundaries


def _is_token_character(character: str) -> bool:
    return character.isalnum() or character in "_+-"


def _sensitive_text_pattern(sensitive_value: str) -> re.Pattern:
    alternatives = []
    for char in sensitive_value:
        encoded = "".join(f"%{byte:02X}" for byte in char.encode("utf-8"))
        representations = [re.escape(char), re.escape(encoded)]
        if char == " ":
            representations.append(r"\+")
        alternatives.append(f"(?:{'|'.join(representations)})")
    return re.compile("".join(alternatives), re.IGNORECASE)


def redact_sensitive_text(text: str | None, sensitive_value: str) -> str | None:
    """Redact complete raw or partially URL-encoded input occurrences."""
    if text is None or not sensitive_value or sensitive_value == "[redacted]":
        return text
    pattern = _sensitive_text_pattern(sensitive_value)
    decoded, boundaries = _decoded_text_boundaries(text)

    def is_standalone(match):
        start = boundaries.get(match.start())
        end = boundaries.get(match.end())
        if start is None or end is None:
            return False
        left = start - 1
        while left >= 0 and decoded[left] == ".":
            left -= 1
        right = end
        while right < len(decoded) and decoded[right] == ".":
            right += 1
        if (
            left >= 0 and _is_token_character(decoded[left])
            or right < len(decoded) and _is_token_character(decoded[right])
        ):
            return False
        return True

    fragments = []
    cursor = position = 0
    while match := pattern.search(text, position):
        if is_standalone(match):
            fragments.extend((text[cursor:match.start()], "[redacted]"))
            cursor = position = match.end()
        else:
            position = match.start() + 1
    fragments.append(text[cursor:])
    return "".join(fragments)


def _trailing_sensitive_prefix_length(text: str, sensitive_value: str) -> int:
    if not sensitive_value or sensitive_value == "[redacted]":
        return 0
    representations = []
    max_length = 0
    for char in sensitive_value:
        encoded = "".join(f"%{byte:02X}" for byte in char.encode("utf-8"))
        options = [char, encoded]
        if char == " ":
            options.append("+")
        representations.append([
            [re.compile(re.escape(character), re.IGNORECASE) for character in option]
            for option in options
        ])
        max_length += max(map(len, options))

    window = text[-max_length:]
    # Bound state-machine work linearly; ambiguous repetitive input fails closed.
    budget = 16 * (len(window) + max_length)
    active = {}
    for position, character in enumerate(window):
        for option in range(len(representations[0])):
            active.setdefault((0, option, 0), position)
        following = {}
        for (index, option, offset), start in active.items():
            budget -= 1
            if budget < 0:
                return len(window)
            matchers = representations[index][option]
            if not matchers[offset].fullmatch(character):
                continue
            if offset + 1 < len(matchers):
                states = [(index, option, offset + 1)]
            elif index + 1 < len(representations):
                states = [
                    (index + 1, next_option, 0)
                    for next_option in range(len(representations[index + 1]))
                ]
            else:
                states = []
            for state in states:
                following[state] = min(start, following.get(state, start))
        active = following
    return len(window) - min(active.values()) if active else 0


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
    """Execute a read-only SQL query and return its rows.

    Web routes should use specific, parameterized repository functions rather
    than accepting arbitrary SQL. Use explicit persistence helpers for writes.
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
    """Return saved results, optionally redacting and truncating result text."""
    _validate_list_limit(limit)
    if redact_sensitive and max_result_text_chars is None:
        max_result_text_chars = DEFAULT_REDACTED_RESULT_TEXT_CHARS
    if max_result_text_chars is not None and (
        not isinstance(max_result_text_chars, int)
        or isinstance(max_result_text_chars, bool)
        or max_result_text_chars < 1
    ):
        raise ValueError("max_result_text_chars must be a positive integer")
    if redact_sensitive and max_result_text_chars is not None:
        rows = _get_storage().list_tool_results(
            limit,
            max_result_text_chars,
            include_truncation_status=True,
            max_query_text_chars=MAX_REDACTION_QUERY_CHARS,
        )
    else:
        rows = _get_storage().list_tool_results(limit, max_result_text_chars)
    if not redact_sensitive:
        return rows

    def redact_result(row):
        source_truncated = (
            max_result_text_chars is not None
            and len(row) > 5
            and row[5]
        )
        if len(row) > 6 and row[6]:
            return "[redacted]"[:max_result_text_chars]
        raw_result = row[3]
        if source_truncated and raw_result is not None:
            partial_length = _trailing_sensitive_prefix_length(raw_result, row[2])
            if partial_length:
                retained = len(raw_result) - partial_length
                # Do not cut a complete occurrence overlapped by a partial suffix.
                pattern = _sensitive_text_pattern(row[2])
                position = 0
                component_start = component_end = 0
                while match := pattern.search(raw_result, position):
                    if match.start() >= retained:
                        break
                    if match.start() < component_end:
                        component_end = max(component_end, match.end())
                    else:
                        component_start, component_end = match.span()
                    position = match.start() + 1
                if component_start < retained < component_end:
                    retained = component_start
                raw_result = raw_result[:retained]
        result = redact_sensitive_text(raw_result, row[2])
        if source_truncated and result is not None:
            marker = "… [truncated]"[:max_result_text_chars]
            return result[:max_result_text_chars - len(marker)] + marker
        if (
            result is not None
            and max_result_text_chars is not None
            and len(result) > max_result_text_chars
        ):
            marker = "… [truncated]"[:max_result_text_chars]
            result = result[:max_result_text_chars - len(marker)] + marker
        return result

    return [
        (
            row[0],
            row[1],
            "[redacted]",
            redact_result(row),
            *row[4:5],
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
) -> int | None:
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
