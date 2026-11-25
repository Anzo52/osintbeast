import sqlite3
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine

import data.utils.database as database
from data.utils.storage import MySQLStorage, StorageError


class FakeResult:
    lastrowid = 41
    rowcount = 1

    def __init__(self, rows=None):
        self.rows = rows or [("platform",), ("platformID",)]

    def fetchall(self):
        return self.rows

    def scalar_one(self):
        return self.rows[0][0]


class FakeConnection:
    def __init__(self):
        self.statements = []
        self.fail_on = None
        self.read_only = False
        self.platforms = ["original"]
        self.sql_mode = ""

    def exec_driver_sql(self, statement, parameters=()):
        self.statements.append((statement, parameters))
        if self.fail_on and self.fail_on in statement:
            raise FakeSQLAlchemyError("database unavailable")
        if statement == "SELECT @@SESSION.sql_mode":
            return FakeResult([(self.sql_mode,)])
        if statement == "START TRANSACTION READ ONLY":
            self.read_only = True
        elif statement == "WITH target AS (SELECT 1) DELETE FROM Platforms":
            if self.read_only:
                raise FakeSQLAlchemyError("read-only transaction")
            self.platforms.clear()
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
                engine.connection.read_only = False
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


def test_mysql_query_uses_a_read_only_transaction(mysql_storage):
    storage, engine = mysql_storage

    assert storage.query("SELECT platform FROM Platforms WHERE platform = ?", ("x",)) == [
        ("platform",), ("platformID",)
    ]

    assert engine.commits == 0
    assert engine.connection.statements == [
        ("SELECT @@SESSION.sql_mode", ()),
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
    with pytest.raises(ValueError):
        storage.query(query)
    assert engine.commits == 0
    assert engine.connection.statements == [("SELECT @@SESSION.sql_mode", ())]


def test_mysql_cte_mutation_does_not_commit(mysql_storage):
    storage, engine = mysql_storage
    query = "WITH target AS (SELECT 1) DELETE FROM Platforms"
    with pytest.raises(StorageError):
        storage.query(query)
    assert engine.connection.statements[1] == ("START TRANSACTION READ ONLY", ())
    assert engine.connection.platforms == ["original"]
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
    assert parameters == (17, 18, 20)


def test_mysql_result_listing_keeps_full_text_within_limit(mysql_storage, monkeypatch):
    storage, engine = mysql_storage
    execute = engine.connection.exec_driver_sql

    def execute_with_result(statement, parameters=()):
        if "SUBSTR(resultText" in statement:
            return FakeResult([(1, "holehe", "query", "123456789012345", 0, "created")])
        return execute(statement, parameters)

    monkeypatch.setattr(engine.connection, "exec_driver_sql", execute_with_result)
    assert storage.list_tool_results(20, max_result_text_chars=17) == [
        (1, "holehe", "query", "123456789012345", "created")
    ]


def test_mysql_result_listing_returns_bounded_raw_prefix_for_redaction(
    mysql_storage, monkeypatch
):
    storage, engine = mysql_storage
    execute = engine.connection.exec_driver_sql
    oversized_query = False

    def execute_with_result(statement, parameters=()):
        if "SUBSTR(resultText" in statement:
            engine.connection.statements.append((statement, parameters))
            return FakeResult(
                [
                    (
                        1,
                        "holehe",
                        None if oversized_query else "alice@example.com",
                        "alice%40exampl",
                        1,
                        "created",
                        int(oversized_query),
                    )
                ]
            )
        return execute(statement, parameters)

    monkeypatch.setattr(engine.connection, "exec_driver_sql", execute_with_result)
    assert storage.list_tool_results(
        20, max_result_text_chars=14, include_truncation_status=True
    ) == [
        (1, "holehe", "alice@example.com", "alice%40exampl", "created", True)
    ]
    statement, parameters = engine.connection.statements[-1]
    assert "SUBSTR(resultText, 1, %s)" in statement
    assert parameters == (14, 15, 20)
    monkeypatch.setattr(database, "_get_storage", lambda: storage)
    assert database.list_tool_results(
        20, max_result_text_chars=14, redact_sensitive=True
    )[0][3] == "… [truncated]"
    statement, parameters = engine.connection.statements[-1]
    assert "SUBSTR(queryText, %s, 1)" in statement
    assert parameters == (4097, 14, 15, 4097, 20)

    oversized_query = True
    assert database.list_tool_results(
        20, max_result_text_chars=14, redact_sensitive=True
    )[0][3] == "[redacted]"


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO Platforms VALUES ('added')",
        "UPDATE Platforms SET platform = 'updated'",
        "DELETE FROM Platforms",
        "DROP TABLE Platforms",
        "SET TRANSACTION READ WRITE",
        "SELECT 1; DELETE FROM Platforms",
    ],
)
def test_mysql_query_rejects_non_read_queries_before_execution(mysql_storage, statement):
    storage, engine = mysql_storage
    with pytest.raises(ValueError):
        storage.query(statement)
    assert engine.connection.statements == [("SELECT @@SESSION.sql_mode", ())]


def test_mysql_cte_mutation_is_rejected_in_a_read_only_transaction(mysql_storage):
    storage, engine = mysql_storage
    statement = "WITH target AS (SELECT 1) DELETE FROM Platforms"

    with engine.begin() as connection:
        connection.exec_driver_sql(statement)
    assert engine.connection.platforms == []
    engine.connection.platforms = ["original"]

    with pytest.raises(StorageError, match="read-only"):
        storage.query(statement)
    assert engine.connection.platforms == ["original"]
    assert engine.connection.statements[-2:] == [
        ("START TRANSACTION READ ONLY", ()),
        (statement, ()),
    ]


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT 1 INTO OUTFILE '/server/file'",
        "select 1 into dumpfile '/server/file'",
        "SELECT 1 INTO/**/OUTFILE '/server/file'",
        "SELECT 1 INTO -- comment\n DUMPFILE '/server/file'",
        "SELECT 1 INTO --\x7fcomment\r OUTFILE '/server/file'",
        "WITH t AS (SELECT 1) SELECT * FROM t INTO\nOUTFILE '/server/file'",
        "SELECT 'x' FROM t INTO OUTFILE '/server/file'",
    ],
)
def test_mysql_rejects_file_output_before_opening_transaction(mysql_storage, statement):
    storage, engine = mysql_storage
    with pytest.raises(ValueError, match="OUTFILE or DUMPFILE"):
        storage.query(statement)
    assert engine.connection.statements == [("SELECT @@SESSION.sql_mode", ())]


def test_mysql_parses_strings_using_session_sql_mode(mysql_storage):
    storage, engine = mysql_storage
    statement = r"SELECT 'x\' INTO OUTFILE text', ?"
    storage.query(statement, (1,))
    assert engine.connection.statements[-1] == (
        r"SELECT 'x\' INTO OUTFILE text', %s", (1,)
    )

    engine.connection.sql_mode = "NO_BACKSLASH_ESCAPES"
    with pytest.raises(ValueError, match="OUTFILE or DUMPFILE"):
        storage.query(r"SELECT 'x\' INTO OUTFILE '/server/file'")
    assert engine.connection.statements[-1] == ("SELECT @@SESSION.sql_mode", ())

    storage.query(r"SELECT 'x\', ?", (1,))
    assert engine.connection.statements[-1] == (r"SELECT 'x\', %s", (1,))


@pytest.mark.parametrize(
    ("mode", "statement"),
    [
        ("", "SELECT `x\\` INTO OUTFILE '/server/file'"),
        ("ANSI_QUOTES", 'SELECT "x\\" INTO OUTFILE \'/server/file\''),
    ],
)
def test_mysql_quoted_identifiers_do_not_use_string_escapes(mysql_storage, mode, statement):
    storage, engine = mysql_storage
    engine.connection.sql_mode = mode
    with pytest.raises(ValueError, match="OUTFILE or DUMPFILE"):
        storage.query(statement)
    assert engine.connection.statements == [("SELECT @@SESSION.sql_mode", ())]


@pytest.mark.parametrize(
    "statement",
    [
        "/* tracing */ SELECT 1",
        " -- tracing\n# another comment\nSELECT 'INTO OUTFILE'",
        "/*+ hint */ WITH t AS (SELECT 1) SELECT * FROM t;",
        "SELECT `into`, 'OUTFILE', \"DUMPFILE\"",
        "SELECT 1 /* INTO OUTFILE */",
    ],
)
def test_mysql_accepts_leading_comments_and_non_executable_text(mysql_storage, statement):
    storage, engine = mysql_storage
    storage.query(statement)
    assert engine.connection.statements[-1] == (statement, ())


@pytest.mark.parametrize(
    "statement",
    [
        "/*!50000 SELECT 1 */ SELECT 1",
        "/*M!100100 SELECT 1 */ SELECT 1",
        "SELECT 1 /*!50000 INTO OUTFILE '/server/file' */",
        "SELECT 1 /*m!100100 INTO DUMPFILE '/server/file' */",
        "/* unfinished SELECT 1",
        "/* comment only */",
    ],
)
def test_mysql_rejects_executable_or_incomplete_comments(mysql_storage, statement):
    storage, engine = mysql_storage
    with pytest.raises(ValueError):
        storage.query(statement)
    assert engine.connection.statements == [("SELECT @@SESSION.sql_mode", ())]


@pytest.mark.parametrize(
    ("query_length", "trailing_space", "result", "expected"),
    [
        (4097, False, "x" * 100, "[redacted]"),
        (4097, False, "report", "[redacted]"),
        (4096, False, "report", "report"),
        (12, False, "report", "report"),
        (4097, True, "report", "[redacted]"),
        (4097, True, "x" * 100, "[redacted]"),
        (12, False, "z" * 20 + " ", "zzzzzzz… [truncated]"),
    ],
)
def test_mysql_bounded_redaction_enforces_query_limit_independently(
    mysql_storage, monkeypatch, query_length, trailing_space, result, expected
):
    storage, engine = mysql_storage
    query = "x" * (query_length - 1) + (" " if trailing_space else "x")
    sql_rows = []
    with sqlite3.connect(":memory:") as conn:
        conn.execute(
            """CREATE TABLE ToolResults (
                   resultID INTEGER, toolName TEXT, queryText TEXT,
                   resultText TEXT, createdAt TEXT
               )"""
        )
        conn.execute(
            "INSERT INTO ToolResults VALUES (?, ?, ?, ?, ?)",
            (1, "holehe", query, result, "created"),
        )

        def execute_listing(statement, parameters=()):
            engine.connection.statements.append((statement, parameters))
            rows = conn.execute(statement.replace("%s", "?"), parameters).fetchall()
            sql_rows.extend(rows)
            return FakeResult(rows)

        monkeypatch.setattr(engine.connection, "exec_driver_sql", execute_listing)
        monkeypatch.setattr(database, "_get_storage", lambda: storage)
        row = database.list_tool_results(
            limit=1, max_result_text_chars=20, redact_sensitive=True
        )[0]

    oversized = query_length > database.MAX_REDACTION_QUERY_CHARS
    assert row[3] == expected
    assert len(row) == 5
    assert sql_rows[0][2] == (None if oversized else query)
    assert sql_rows[0][3] == result[:20]
    assert sql_rows[0][4] == int(len(result) > 20)
    assert sql_rows[0][6] == int(oversized)
    statement, parameters = engine.connection.statements[-1]
    assert parameters == (4097, 20, 21, 4097, 1)
    assert (
        "CASE WHEN LENGTH(SUBSTR(queryText, %s, 1)) > 0 THEN NULL ELSE queryText END"
        in " ".join(statement.split())
    )


@pytest.mark.parametrize(
    ("query", "result", "expected"),
    [
        ("alice", "report", "report"),
        ("x" * 4097, "report", "[redacted]"),
        ("alice", "z" * 20000, "z" * (16384 - 13) + "… [truncated]"),
    ],
)
def test_mysql_redacted_listing_without_cap_is_bounded(
    mysql_storage, monkeypatch, query, result, expected
):
    storage, engine = mysql_storage
    with sqlite3.connect(":memory:") as conn:
        conn.execute(
            """CREATE TABLE ToolResults (
                   resultID INTEGER, toolName TEXT, queryText TEXT,
                   resultText TEXT, createdAt TEXT
               )"""
        )
        conn.execute(
            "INSERT INTO ToolResults VALUES (?, ?, ?, ?, ?)",
            (1, "holehe", query, result, "created"),
        )

        def execute_listing(statement, parameters=()):
            engine.connection.statements.append((statement, parameters))
            return FakeResult(
                conn.execute(statement.replace("%s", "?"), parameters).fetchall()
            )

        monkeypatch.setattr(engine.connection, "exec_driver_sql", execute_listing)
        monkeypatch.setattr(database, "_get_storage", lambda: storage)
        assert database.list_tool_results(1, redact_sensitive=True)[0][3] == expected
    statement, parameters = engine.connection.statements[-1]
    assert parameters == (4097, 16384, 16385, 4097, 1)
    assert "LENGTH(SUBSTR(queryText" in statement
    assert "SUBSTR(resultText, 1, %s)" in statement


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
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE items (value TEXT)")
            connection.exec_driver_sql("INSERT INTO items VALUES ('original')")

        with engine.connect() as connection:
            proxy = Mock()
            proxy.exec_driver_sql.side_effect = lambda sql, parameters=(): (
                FakeResult([("",)]) if sql == "SELECT @@SESSION.sql_mode"
                else connection.exec_driver_sql(sql, parameters)
            )
            mysql_engine = Mock()
            mysql_engine.connect.return_value = nullcontext(proxy)
            monkeypatch.setattr(storage, "_get_engine", lambda: mysql_engine)
            with pytest.raises(ValueError, match="SELECT or WITH"):
                storage.query(statement)
            expected = [("original",)]
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
    assert MySQLStorage._mysql_placeholders(
        "--\x7f? ignored\rSELECT ?", (1,)
    ) == "--\x7f? ignored\rSELECT %s"
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
