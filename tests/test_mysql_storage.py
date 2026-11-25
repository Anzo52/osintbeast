from datetime import datetime
from pathlib import Path

import pytest

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
