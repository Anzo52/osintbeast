from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError

import data.utils.database as database
from data.utils.storage import IdempotencyConflict, MySQLStorage, StorageError


class FakeResult:
    lastrowid = 41
    rowcount = 1

    def __init__(self, rows=None):
        self.rows = rows or [("platform",), ("platformID",)]

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self):
        self.statements = []
        self.fail_on = None

    def exec_driver_sql(self, statement, parameters=()):
        self.statements.append((statement, parameters))
        if self.fail_on and self.fail_on in statement:
            raise FakeSQLAlchemyError("database unavailable")
        if "SUBSTR(resultText" in statement:
            return FakeResult([(1, "holehe", "query", "xxxx", 1, "created")])
        return FakeResult()


class FakeSQLAlchemyError(Exception):
    pass


FakeSQLAlchemyError.__module__ = "sqlalchemy.exc"


class FakeEngine:
    def __init__(self):
        self.connection = FakeConnection()
        self.transaction_errors = []
        self.commits = 0

    def begin(self):
        return self.connection_context(commit=True)

    def connect(self):
        return self.connection_context()

    def connection_context(self, commit=False):
        engine = self

        class Context:
            def __enter__(self):
                return engine.connection

            def __exit__(self, exc_type, exc_value, _):
                if exc_type is not None:
                    engine.transaction_errors.append(exc_value)
                elif commit:
                    engine.commits += 1
                return False

        return Context()


@pytest.fixture
def mysql_storage(monkeypatch):
    schema_dir = Path(__file__).resolve().parents[1] / "data" / "schemas"
    storage = MySQLStorage(
        "mysql://osintbeast@localhost/osintbeast",
        schema_dir,
        database.MYSQL_SCHEMA_FILES,
        database.INSERTABLE_TABLES,
    )
    engine = FakeEngine()
    monkeypatch.setattr(storage, "_get_engine", lambda: engine)
    return storage, engine


def test_mysql_initialization_executes_both_idempotent_schema_files(mysql_storage):
    storage, engine = mysql_storage

    storage.setup()
    storage.setup()

    statements = [statement for statement, _ in engine.connection.statements]
    assert sum(
        statement.startswith("CREATE TABLE IF NOT EXISTS Platforms")
        for statement in statements
    ) == 2
    assert any("CREATE TABLE IF NOT EXISTS ToolRuns" in statement for statement in statements)


@pytest.mark.parametrize("error_code", [1062, 1205, 1213])
@pytest.mark.parametrize("same_request", [True, False])
def test_mysql_idempotency_retries_transaction_after_contention(
    mysql_storage, monkeypatch, error_code, same_request
):
    storage, engine = mysql_storage
    selects = 0

    class RowResult:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return self.row

    def execute(statement, parameters=()):
        nonlocal selects
        engine.connection.statements.append((statement, parameters))
        if statement.startswith("SELECT requestHash"):
            selects += 1
            return RowResult(None if selects == 1 else (
                "a" * 64 if same_request else "b" * 64, 42
            ))
        if statement.startswith("INSERT INTO ToolRunRequests"):
            raise OperationalError(statement, parameters, Exception(error_code, "contention"))
        return FakeResult()

    monkeypatch.setattr(engine.connection, "exec_driver_sql", execute)
    if same_request:
        assert storage.start_idempotent_tool_run("holehe", "[redacted]", "key", "a" * 64) == (
            42, False
        )
    else:
        with pytest.raises(IdempotencyConflict):
            storage.start_idempotent_tool_run("holehe", "[redacted]", "key", "a" * 64)
    assert selects == 2
    assert len(engine.transaction_errors) >= 1


def test_mysql_core_operations_use_bound_values_and_preserve_transactional_flow(
    mysql_storage,
):
    storage, engine = mysql_storage

    storage.insert("Platforms", {"platform": "example"})
    assert storage.start_tool_run("holehe", "person@example.com") == 41
    storage.finish_tool_run(41, "success", "output", None)
    assert storage.save_tool_result("holehe", "person@example.com", "output") == 41
    assert storage.list_tool_results(20) == [("platform",), ("platformID",)]

    statements = engine.connection.statements
    assert statements[1] == (
        "INSERT INTO `Platforms` (`platform`) VALUES (%s)",
        ("example",),
    )
    assert any(
        "INSERT INTO ToolRuns" in sql and params == ("holehe", "person@example.com")
        for sql, params in statements
    )
    assert any("INSERT INTO ToolResults" in sql for sql, _ in statements)


def test_mysql_query_commits_state_changes(mysql_storage):
    storage, engine = mysql_storage

    assert storage.query("INSERT INTO Platforms (platform) VALUES (?)", ("x",)) == [
        ("platform",), ("platformID",)
    ]

    assert engine.commits == 1


def test_mysql_result_listing_caps_output_in_the_query(mysql_storage):
    storage, engine = mysql_storage

    assert storage.list_tool_results(20, max_result_text_chars=17) == [
        (1, "holehe", "query", "xxxx… [truncated]", "created")
    ]
    statement, parameters = engine.connection.statements[-1]
    assert "SUBSTR(resultText, 1, %s)" in statement
    assert parameters == (4, 5, 20)


def test_mysql_identifier_validation_and_placeholders(mysql_storage):
    storage, engine = mysql_storage

    with pytest.raises(ValueError, match="Unknown database table"):
        storage.insert("Platforms; DROP TABLE ToolRuns", {"platform": "x"})
    with pytest.raises(ValueError, match="Unknown column"):
        storage.insert("Platforms", {"platform; DROP TABLE ToolRuns": "x"})

    assert engine.connection.statements[-1][0].startswith(
        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS"
    )
    assert MySQLStorage._mysql_placeholders(
        "SELECT '?' AS marker, value FROM table WHERE id = ?", (1,)
    ) == "SELECT '?' AS marker, value FROM table WHERE id = %s"
    assert MySQLStorage._mysql_placeholders(
        "SELECT ? -- ? ignored\n/* ? ignored */", (1,)
    ) == "SELECT %s -- ? ignored\n/* ? ignored */"
    assert MySQLStorage._normalize_rows(
        [(datetime(2025, 1, 2, 3, 4, 5),)]
    ) == [("2025-01-02 03:04:05",)]


def test_mysql_backend_is_selected_without_changing_sqlite_default(monkeypatch):
    monkeypatch.setenv(
        "OSINTBEAST_DATABASE_URL", "mysql://osintbeast@localhost/osintbeast"
    )
    assert isinstance(database._get_storage(), MySQLStorage)

    monkeypatch.delenv("OSINTBEAST_DATABASE_URL")
    assert database._get_storage().__class__.__name__ == "SQLiteStorage"


def test_mysql_failures_are_reported_as_storage_errors(mysql_storage):
    storage, engine = mysql_storage
    engine.connection.fail_on = "INSERT INTO ToolRuns"

    with pytest.raises(StorageError, match="database unavailable"):
        storage.start_tool_run("holehe", "test")


def test_mysql_result_failure_aborts_tool_run_transaction(mysql_storage):
    storage, engine = mysql_storage
    engine.connection.fail_on = "INSERT INTO ToolResults"

    with pytest.raises(StorageError, match="database unavailable"):
        storage.finish_tool_run(41, "success", "output", None)

    assert len(engine.transaction_errors) == 1
    assert isinstance(engine.transaction_errors[0], FakeSQLAlchemyError)


def test_mysql_reconciles_stale_runs_with_bound_age(mysql_storage):
    storage, engine = mysql_storage

    assert storage.reconcile_stale_tool_runs(600) == 1

    statement, parameters = engine.connection.statements[-1]
    assert "DATE_SUB(UTC_TIMESTAMP(), INTERVAL %s SECOND)" in statement
    assert parameters == (600,)
