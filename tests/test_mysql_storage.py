from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine

import data.utils.database as database
from data.utils.storage import MySQLStorage, SQLiteStorage, StorageError


class FakeResult:
    lastrowid = 41
    rowcount = 1

    def __init__(self, rows=None):
        self.rows = [("platform",), ("platformID",)] if rows is None else rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeConnection:
    def __init__(self):
        self.statements = []
        self.fail_on = None
        self.idempotency_row = None

    def exec_driver_sql(self, statement, parameters=()):
        self.statements.append((statement, parameters))
        if self.fail_on and self.fail_on in statement:
            raise FakeSQLAlchemyError("database unavailable")
        if "SELECT runID, toolName, inputFingerprint" in statement:
            return FakeResult(
                [] if self.idempotency_row is None else [self.idempotency_row]
            )
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
    assert any(
        "CREATE TABLE IF NOT EXISTS ToolRunIdempotency" in statement
        for statement in statements
    )
    assert any(
        "idempotencyKey VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin"
        in statement
        for statement in statements
    )


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


def test_mysql_idempotent_run_creation_is_transactional(mysql_storage):
    storage, engine = mysql_storage

    assert storage.start_idempotent_tool_run(
        "holehe", "[redacted]", "k" * 64, "f" * 64
    ) == (41, True)

    assert any(
        "INSERT INTO ToolRunIdempotency" in sql
        and params == ("k" * 64, "holehe", "f" * 64, 41)
        for sql, params in engine.connection.statements
    )
    assert engine.commits == 1


def test_mysql_idempotent_run_returns_existing_association(mysql_storage):
    storage, engine = mysql_storage
    engine.connection.idempotency_row = (17, "holehe", "f" * 64)

    assert storage.start_idempotent_tool_run(
        "holehe", "[redacted]", "k" * 64, "f" * 64
    ) == (17, False)
    assert not any(
        "INSERT INTO ToolRuns" in sql for sql, _ in engine.connection.statements
    )


def test_sqlite_distinct_idempotency_apis_replay_independently(tmp_path, monkeypatch):
    schema_dir = Path(__file__).resolve().parents[1] / "data" / "schemas"
    db_file = str(tmp_path / "runs.db")

    def make_storage():
        return SQLiteStorage(
            db_file, schema_dir, database.SCHEMA_FILES, database.INSERTABLE_TABLES
        )

    storage = make_storage()
    storage.setup()
    monkeypatch.setattr(database, "_get_storage", lambda: storage)
    key = "a" * 64
    fingerprint = "b" * 64
    request_hash = "c" * 64
    background_id, background_created = database.start_idempotent_tool_run(
        "holehe", "[redacted]", key, fingerprint
    )
    request_id, request_created = database.start_idempotent_request(
        "holehe", "[redacted]", key, request_hash
    )
    assert background_created and request_created
    assert background_id != request_id

    storage = make_storage()
    storage.setup()
    assert database.get_idempotent_tool_run(key) == (
        background_id, "holehe", fingerprint
    )
    assert database.get_idempotent_request(key, request_hash) == request_id
    assert database.start_idempotent_tool_run(
        "holehe", "[redacted]", key, fingerprint
    ) == (background_id, False)
    assert database.start_idempotent_request(
        "holehe", "[redacted]", key, request_hash
    ) == (request_id, False)


def test_mysql_query_uses_a_read_only_transaction_without_committing(mysql_storage):
    storage, engine = mysql_storage

    assert storage.query("SELECT platform FROM Platforms WHERE platform = ?", ("x",)) == [
        ("platform",), ("platformID",)
    ]

    assert engine.commits == 0
    assert engine.connection.statements == [
        ("START TRANSACTION READ ONLY", ()),
        ("SELECT platform FROM Platforms WHERE platform = %s", ("x",)),
    ]


@pytest.mark.parametrize(
    "query",
    [
        "INSERT INTO Platforms (platform) VALUES ('x')",
        "UPDATE Platforms SET platform = 'x'",
        "DELETE FROM Platforms",
        "DROP TABLE Platforms",
        "ALTER TABLE Platforms ADD COLUMN x INTEGER",
        "CREATE TABLE x (id INTEGER)",
        "COMMIT",
        "SET TRANSACTION READ WRITE",
        "/*!50000 DROP TABLE Platforms */",
    ],
)
def test_mysql_query_rejects_non_queries_before_execution(mysql_storage, query):
    storage, engine = mysql_storage
    with pytest.raises(ValueError, match="SELECT or WITH"):
        storage.query(query)
    assert engine.commits == 0
    assert engine.connection.statements == []


def test_mysql_cte_mutation_is_rejected_in_a_read_only_transaction(mysql_storage):
    storage, engine = mysql_storage
    query = "WITH target AS (SELECT 1) DELETE FROM Platforms"
    engine.connection.fail_on = query
    with pytest.raises(StorageError):
        storage.query(query)
    assert engine.connection.statements[0] == ("START TRANSACTION READ ONLY", ())
    assert engine.commits == 0


def test_mysql_engine_configures_bounded_database_waits(monkeypatch):
    import sqlalchemy

    create_engine = Mock()
    monkeypatch.setattr(sqlalchemy, "create_engine", create_engine)
    storage = MySQLStorage(
        "mysql://osintbeast@localhost/osintbeast", Path(), (), frozenset()
    )
    storage._get_engine()
    assert create_engine.call_args.kwargs == {
        "pool_pre_ping": True,
        "pool_timeout": 5,
        "connect_args": {"connect_timeout": 5, "read_timeout": 5, "write_timeout": 5},
    }


def test_mysql_result_listing_caps_output_in_the_query(mysql_storage):
    storage, engine = mysql_storage

    assert storage.list_tool_results(20, max_result_text_chars=17) == [
        (1, "holehe", "query", "xxxx… [truncated]", "created")
    ]
    statement, parameters = engine.connection.statements[-1]
    assert "SUBSTR(resultText, 1, %s)" in statement
    assert parameters == (4, 5, 20)


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO items VALUES ('added')",
        "UPDATE items SET value = 'updated'",
        "DELETE FROM items",
    ],
)
def test_mysql_query_rejects_state_changing_statements(
    mysql_storage, tmp_path, monkeypatch, statement
):
    storage, _ = mysql_storage
    engine = create_engine(f"sqlite:///{tmp_path / 'transactions.db'}")
    monkeypatch.setattr(storage, "_get_engine", lambda: engine)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE items (value TEXT)")
            connection.exec_driver_sql("INSERT INTO items VALUES ('original')")

        with pytest.raises(ValueError, match="SELECT or WITH"):
            storage.query(statement)
        expected = [("original",)]
        with engine.connect() as connection:
            assert connection.exec_driver_sql(
                "SELECT value FROM items ORDER BY rowid"
            ).fetchall() == expected
    finally:
        engine.dispose()


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
