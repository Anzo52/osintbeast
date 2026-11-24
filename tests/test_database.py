import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

import downloader
import holehe_integration
from data.utils import database, init_db


def test_setup_database_loads_both_idempotent_schemas(tmp_path, monkeypatch):
    db_path = tmp_path / "osintbeast_test.db"
    monkeypatch.setattr(database, "DB_FILE", str(db_path))

    database.setup_database()
    database.setup_database()

    with database.connect_db() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            "Platforms", "Names", "Locations", "Domains", "Services",
            "Urls", "Usernames", "ToolCatalog", "ToolSources", "ToolResults",
            "ToolRuns",
        } <= tables
        assert conn.execute("PRAGMA foreign_keys").fetchone() == (1,)
        location_types = {
            row[1]: row[2] for row in conn.execute("PRAGMA table_info(Locations)")
        }
        assert location_types["addressNum"] == "TEXT"
        assert location_types["apt"] == "TEXT"
        assert location_types["zip"] == "TEXT"


def test_database_paths_resolve_to_source_checkout(monkeypatch):
    monkeypatch.delenv("OSINTBEAST_DB_PATH", raising=False)
    project_root = Path(__file__).resolve().parents[1]

    db_file, schema_dir, _, _ = database.setup_database_paths(database.__file__)

    assert db_file == str(project_root / "osintbeast.db")
    assert schema_dir == project_root / "data" / "schemas"


def test_installed_database_defaults_to_user_data_directory(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("OSINTBEAST_DB_PATH", raising=False)
    if os.name == "nt":
        user_data_root = tmp_path / "AppData"
        monkeypatch.setenv("APPDATA", str(user_data_root))
        expected_root = user_data_root / "osintbeast"
    elif sys.platform == "darwin":
        home = tmp_path / "home"
        monkeypatch.setattr(Path, "home", lambda: home)
        expected_root = home / "Library" / "Application Support" / "osintbeast"
    else:
        user_data_root = tmp_path / "xdg-data"
        monkeypatch.setenv("XDG_DATA_HOME", str(user_data_root))
        expected_root = user_data_root / "osintbeast"

    installed_module = tmp_path / "site-packages" / "data" / "utils" / "database.py"
    installed_module.parent.mkdir(parents=True)
    schema_resources = tmp_path / "schema-resources"
    schema_resources.mkdir()
    monkeypatch.setattr(database, "files", lambda _: schema_resources)

    db_file, schema_dir, _, _ = database.setup_database_paths(installed_module)

    assert db_file == str(expected_root / "osintbeast.db")
    assert schema_dir == schema_resources


def test_mysql_schemas_are_split_by_domain_and_include_run_history():
    schema_dir = Path(__file__).resolve().parents[1] / "data" / "schemas"
    info_schema = (schema_dir / "mysql-infoschema.sql").read_text(encoding="utf-8")
    tool_schema = (schema_dir / "mysql-toolschema.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS Platforms" in info_schema
    assert "CREATE TABLE IF NOT EXISTS Locations" in info_schema
    assert "CREATE TABLE IF NOT EXISTS ToolCatalog" not in info_schema
    assert "zip VARCHAR(16)" in info_schema
    assert "KEY idx_services_platform (platformID)" in info_schema
    assert "CREATE TABLE IF NOT EXISTS ToolCatalog" in tool_schema
    assert "CREATE TABLE IF NOT EXISTS ToolSources" in tool_schema
    assert "CREATE TABLE IF NOT EXISTS ToolResults" in tool_schema
    assert "CREATE TABLE IF NOT EXISTS ToolRuns" in tool_schema
    assert "CREATE TABLE IF NOT EXISTS Platforms" not in tool_schema
    assert "UNIQUE KEY uq_domains_domain (domain)" in info_schema
    assert "CONSTRAINT fk_usernames_services" in info_schema
    assert "KEY idx_toolresults_tool_created (toolName, createdAt)" in tool_schema
    assert (
        "CHECK (status IN ('running', 'success', 'failed', 'timed_out'))"
        in tool_schema
    )


def test_setup_database_preserves_existing_data_and_repairs_missing_objects(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "existing.db"))
    database.setup_database()
    database.insert_data("Platforms", {"platform": "existing"})
    with database.connect_db() as conn:
        conn.execute("DROP INDEX idx_usernames_name")
        conn.execute("DROP INDEX idx_services_platform")

    database.setup_database()

    assert database.query_database("SELECT platform FROM Platforms") == [("existing",)]
    assert database.query_database(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND name = ?",
        ("idx_usernames_name",),
    ) == [("idx_usernames_name",)]
    assert database.query_database(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND name = ?",
        ("idx_services_platform",),
    ) == [("idx_services_platform",)]


def test_setup_database_redacts_legacy_runs_and_results_once(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "legacy.db"))
    database.setup_database()
    with database.connect_db() as conn:
        conn.execute("DELETE FROM OsintBeastMigrations")
        conn.execute(
            "INSERT INTO ToolResults VALUES (7, 'holehe', ?, ?, '2020-01-01')",
            ("Person+tag@example.com", "PERSON+TAG@example.com person%2btag%40example.com"),
        )
        conn.execute(
            "INSERT INTO ToolRuns VALUES (8, 'holehe', ?, 'failed', NULL, ?, "
            "'2020-01-01', '2020-01-02')",
            ("Person+tag@example.com", "failed: Person%2Btag%40example.com"),
        )
        conn.execute(
            "INSERT INTO ToolRuns VALUES (9, 'maigret', 'alice', 'success', "
            "'found ALICE', NULL, '2020-01-03', '2020-01-04')"
        )
        conn.execute(
            "INSERT INTO ToolRuns VALUES (10, 'maigret', '', 'running', "
            "NULL, NULL, '2020-01-05', NULL)"
        )
        conn.execute(
            "INSERT INTO ToolResults VALUES (11, 'holehe', '[redacted]', "
            "'already sanitized', '2020-01-06')"
        )

    database.setup_database()

    assert database.list_tool_results() == [
        (11, "holehe", "[redacted]", "already sanitized", "2020-01-06"),
        (7, "holehe", "[redacted]", "[redacted] [redacted]", "2020-01-01"),
    ]
    assert database.get_tool_run(8) == (
        8, "holehe", "[redacted]", "failed", None, "failed: [redacted]",
        "2020-01-01", "2020-01-02",
    )
    assert database.get_tool_run(9) == (
        9, "maigret", "[redacted]", "success", "found [redacted]", None,
        "2020-01-03", "2020-01-04",
    )
    assert database.get_tool_run(10) == (
        10, "maigret", "[redacted]", "running", None, None, "2020-01-05", None,
    )
    database.save_tool_result("holehe", "new", "new result")
    database.setup_database()
    assert database.list_tool_results()[0][2:4] == ("new", "new result")
    assert database.get_tool_run(8)[5] == "failed: [redacted]"


def test_legacy_redaction_rolls_back_on_error(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "rollback.db"))
    database.setup_database()
    with database.connect_db() as conn:
        conn.execute("DELETE FROM OsintBeastMigrations")
        conn.execute(
            "INSERT INTO ToolResults (toolName, queryText, resultText) "
            "VALUES ('holehe', 'private', 'private result')"
        )
        conn.execute(
            "INSERT INTO ToolRuns (toolName, queryText, status) "
            "VALUES ('holehe', 'private', 'running')"
        )
        conn.execute(
            "CREATE TRIGGER fail_redaction BEFORE UPDATE ON ToolRuns "
            "BEGIN SELECT RAISE(ABORT, 'migration failed'); END"
        )

    with pytest.raises(sqlite3.IntegrityError, match="migration failed"):
        database.setup_database()

    assert database.list_tool_results()[0][2:4] == ("private", "private result")
    assert database.list_tool_runs()[0][2] == "private"
    assert database.query_database("SELECT name FROM OsintBeastMigrations") == []
    with database.connect_db() as conn:
        conn.execute("DROP TRIGGER fail_redaction")
    database.setup_database()
    assert database.list_tool_results()[0][2:4] == ("[redacted]", "[redacted] result")


def test_setup_database_rolls_back_all_schemas_on_sql_error(tmp_path, monkeypatch):
    db_path = tmp_path / "broken.db"
    monkeypatch.setattr(database, "DB_FILE", str(db_path))
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE existing (value TEXT)")
        conn.execute("INSERT INTO existing VALUES ('preserved')")
    schema_dir = tmp_path / "schemas"
    schema_dir.mkdir()
    (schema_dir / "sqlite-infoschema.sql").write_text(
        "CREATE TABLE first_table (id INTEGER);", encoding="utf-8"
    )
    (schema_dir / "sqlite-toolschema.sql").write_text(
        "CREATE TABLE second_table (id INTEGER); INVALID SQL;", encoding="utf-8"
    )
    monkeypatch.setattr(database, "SCHEMA_DIR", schema_dir)

    with pytest.raises(sqlite3.OperationalError):
        database.setup_database()

    assert database.query_database(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ) == [("existing",)]
    assert database.query_database("SELECT value FROM existing") == [("preserved",)]


def test_missing_schema_does_not_create_database(tmp_path, monkeypatch):
    db_path = tmp_path / "missing.db"
    monkeypatch.setattr(database, "DB_FILE", str(db_path))
    monkeypatch.setattr(database, "SCHEMA_DIR", tmp_path)
    (tmp_path / "sqlite-infoschema.sql").write_text(
        "CREATE TABLE first_table (id INTEGER);", encoding="utf-8"
    )

    with pytest.raises(FileNotFoundError, match="sqlite-toolschema.sql"):
        database.setup_database()

    assert not db_path.exists()


def test_setup_database_is_independent_of_working_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "cwd.db"))
    monkeypatch.chdir(tmp_path)

    database.setup_database()

    assert database.query_database(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'ToolResults'"
    ) == [("ToolResults",)]


def test_legacy_downloader_initializes_full_schema(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "downloader.db"))
    downloader.setup_database()

    assert database.query_database(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'ToolResults'"
    ) == [("ToolResults",)]


def test_holehe_result_uses_initialized_database(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "results.db"))
    database.setup_database()
    holehe_integration.save_tool_result(
        "holehe",
        "person@example.com",
        "output for person@example.com and person@example.com.invalid",
    )

    assert database.query_database(
        "SELECT toolName, queryText, resultText FROM ToolResults"
    ) == [(
        "holehe",
        "[redacted]",
        "output for [redacted] and person@example.com.invalid",
    )]


def test_holehe_result_redacts_partially_encoded_input(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "encoded-results.db"))
    database.setup_database()
    query = "person+tag@example.com"
    holehe_integration.save_tool_result(
        "holehe", query, "Found person+tag%40example.com"
    )

    assert database.query_database(
        "SELECT queryText, resultText FROM ToolResults"
    ) == [("[redacted]", "Found [redacted]")]


def test_database_path_override_is_shared_across_modules(tmp_path):
    env = os.environ.copy()
    env["OSINTBEAST_DB_PATH"] = "custom.db"
    project_root = Path(database.__file__).resolve().parents[2]
    env["PYTHONPATH"] = os.pathsep.join(
        (str(project_root / "src"), str(project_root))
    )
    subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import data.utils.database as database; "
                "import downloader, holehe_integration; "
                "database.setup_database(); "
                "assert database.DB_FILE == downloader.DB_FILE; "
                "holehe_integration.save_tool_result('holehe', 'test', 'result'); "
                "assert database.list_tool_results()[0][3] == 'result'"
            ),
        ],
        cwd=tmp_path,
        env=env,
        check=True,
    )
    assert (tmp_path / "custom.db").is_file()


def test_initialization_failure_propagates(monkeypatch):
    def fail():
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(init_db, "setup_database", fail)
    with pytest.raises(sqlite3.OperationalError, match="unable to open database file"):
        init_db.init_db()



def test_insert_query_and_save_tool_result(tmp_path, monkeypatch):
    db_path = tmp_path / "osintbeast_results.db"
    monkeypatch.setattr(database, "DB_FILE", str(db_path))
    database.setup_database()

    database.insert_data("Platforms", {"platform": "example-social"})
    assert database.query_database(
        "SELECT platform FROM Platforms WHERE platform = ?", ("example-social",)
    ) == [("example-social",)]

    result_id = database.save_tool_result("holehe", "person@example.com", "output")
    results = database.list_tool_results()
    assert results[0] == (
        result_id, "holehe", "person@example.com", "output", results[0][4]
    )


def test_list_tool_results_orders_newest_first_and_enforces_limit(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "results.db"))
    database.setup_database()
    for query_text in ("oldest", "middle", "newest"):
        database.save_tool_result("holehe", query_text, query_text)

    assert [row[2] for row in database.list_tool_results(limit=2)] == [
        "newest", "middle"
    ]
    assert [row[2] for row in database.list_tool_results(limit=1)] == ["newest"]
    for limit in (0, 501):
        with pytest.raises(ValueError, match="between 1 and 500"):
            database.list_tool_results(limit=limit)


def test_insert_rejects_unknown_table_and_column(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "safe.db"))
    database.setup_database()

    with pytest.raises(ValueError):
        database.insert_data("NotATable", {"value": "x"})
    with pytest.raises(ValueError):
        database.insert_data("Platforms", {"missing_column": "x"})


def test_insert_binds_values_and_rejects_identifier_injection(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "injection.db"))
    database.setup_database()

    # A value containing SQL syntax remains ordinary data.
    payload = "safe'); DROP TABLE Platforms; --"
    database.insert_data("Platforms", {"platform": payload})
    assert database.query_database(
        "SELECT platform FROM Platforms WHERE platform = ?", (payload,)
    ) == [(payload,)]

    with pytest.raises(ValueError):
        database.insert_data('Platforms"; DROP TABLE Platforms; --', {"platform": "x"})
    with pytest.raises(ValueError):
        database.insert_data("Platforms", {'platform" ) VALUES ("x"); --': "x"})

    assert database.query_database(
        "SELECT platform FROM Platforms WHERE platform = ?", (payload,)
    ) == [(payload,)]


def test_insert_data_preserves_foreign_key_enforcement(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "foreign-key.db"))
    database.setup_database()

    with pytest.raises(IntegrityError):
        database.insert_data(
            "Services", {"serviceName": "orphan", "platformID": 999}
        )


def test_reconcile_stale_tool_runs_preserves_recent_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "reconcile.db"))
    database.setup_database()
    stale_id = database.start_tool_run("maigret", "stale")
    recent_id = database.start_tool_run("maigret", "recent")
    with database.connect_db() as conn:
        conn.execute(
            "UPDATE ToolRuns SET startedAt = datetime('now', '-11 minutes') "
            "WHERE runID = ?",
            (stale_id,),
        )

    assert database.reconcile_stale_tool_runs() == 1
    assert database.get_tool_run(stale_id)[3:6] == (
        "failed", None, "Run outcome could not be persisted."
    )
    assert database.get_tool_run(stale_id)[7] is not None
    assert database.get_tool_run(recent_id)[3] == "running"


@pytest.mark.parametrize("age", [0, -1, True, 1.5])
def test_reconcile_stale_tool_runs_rejects_invalid_age(age):
    with pytest.raises(ValueError, match="positive integer"):
        database.reconcile_stale_tool_runs(age)


def test_run_history_cursor_pagination_is_stable(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "history.db"))
    database.setup_database()
    first = database.start_tool_run("holehe", "one")
    second = database.start_tool_run("holehe", "two")
    third = database.start_tool_run("sherlock", "three")

    assert [row[0] for row in database.list_tool_runs(limit=1)] == [third]
    assert [row[0] for row in database.list_tool_runs(limit=1, cursor=third)] == [second]
    assert [row[0] for row in database.list_tool_runs(limit=1, cursor=second)] == [first]
    assert [row[0] for row in database.list_tool_runs(tool_name="holehe")] == [second, first]


@pytest.mark.parametrize("include_sensitive", [None, 1, "false", [], {}])
def test_export_requires_explicit_boolean_sensitive_opt_in(
    tmp_path, monkeypatch, include_sensitive
):
    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "export.db"))
    database.setup_database()
    run_id = database.start_tool_run("holehe", "private")
    with pytest.raises(ValueError, match="boolean"):
        database.export_tool_runs_json(
            [run_id], include_sensitive=include_sensitive
        )


def test_run_ids_reject_sqlite_overflow(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "ids.db"))
    database.setup_database()
    oversized = 1 << 63
    with pytest.raises(ValueError, match="positive SQLite integer"):
        database.get_tool_run(oversized)
    with pytest.raises(ValueError, match="positive SQLite integers"):
        database.export_tool_runs_json([oversized])
