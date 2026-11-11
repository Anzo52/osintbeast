import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import database
import downloader
import holehe_integration
import main


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
            "Urls", "Usernames", "ToolCatalog", "ToolResults",
        } <= tables
        assert conn.execute("PRAGMA foreign_keys").fetchone() == (1,)


def test_setup_database_preserves_existing_data_and_repairs_missing_objects(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "existing.db"))
    database.setup_database()
    database.insert_data("Platforms", {"platform": "existing"})
    with database.connect_db() as conn:
        conn.execute("DROP INDEX idx_usernames_name")

    database.setup_database()

    assert database.query_database("SELECT platform FROM Platforms") == [("existing",)]
    assert database.query_database(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND name = ?",
        ("idx_usernames_name",),
    ) == [("idx_usernames_name",)]


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
    holehe_integration.save_tool_result("holehe", "person@example.com", "output")

    assert database.query_database(
        "SELECT toolName, queryText, resultText FROM ToolResults"
    ) == [("holehe", "person@example.com", "output")]


def test_database_path_override_is_shared_across_modules(tmp_path):
    env = os.environ.copy()
    env["OSINTBEAST_DB_PATH"] = "custom.db"
    env["PYTHONPATH"] = str(Path(database.__file__).resolve().parent)
    subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import database, downloader, holehe_integration; "
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


def test_cli_reports_initialization_failure(monkeypatch, capsys):
    def fail():
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(main, "setup_database", fail)
    main.init_db()

    assert capsys.readouterr().out == (
        "Database initialization failed: unable to open database file\n"
    )


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
