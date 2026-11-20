import inspect
import sqlite3
from unittest.mock import Mock

import pytest

import backend_services
import data.utils.database as database
import main


@pytest.fixture
def initialized_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "backend.db"))
    database.setup_database()


def test_backend_services_return_recent_runs_and_saved_results(initialized_db):
    run_id = database.start_tool_run("holehe", "person@example.com")
    database.finish_tool_run(run_id, "success", result_text="output")

    assert backend_services.get_recent_tool_runs() == database.list_tool_runs()
    assert backend_services.get_saved_tool_results() == database.list_tool_results()


@pytest.mark.parametrize("operation", [
    backend_services.get_recent_tool_runs,
    backend_services.get_saved_tool_results,
])
@pytest.mark.parametrize("raw_sql", [
    "SELECT * FROM ToolRuns",
    "ToolRuns",
    "1; DROP TABLE ToolRuns; --",
])
def test_backend_services_reject_sql_or_identifiers_as_request_input(
    initialized_db, operation, raw_sql
):
    with pytest.raises(ValueError):
        operation(raw_sql)


def test_backend_service_operations_accept_no_query_or_table_arguments():
    assert list(inspect.signature(
        backend_services.get_recent_tool_runs
    ).parameters) == ["limit"]
    assert list(inspect.signature(
        backend_services.get_saved_tool_results
    ).parameters) == ["limit"]


@pytest.mark.parametrize(
    ("choice", "service_name"),
    [(0, "get_recent_tool_runs"), (1, "get_saved_tool_results")],
)
def test_history_menu_uses_approved_service_methods(
    monkeypatch, capsys, choice, service_name
):
    service = Mock(return_value=[("approved",)])
    monkeypatch.setattr(main, service_name, service)
    monkeypatch.setattr(
        database, "query_database",
        Mock(side_effect=AssertionError("raw SQL persistence used")),
    )
    monkeypatch.setattr(main, "_choose_option", lambda options, title: choice)

    main.query_menu()

    service.assert_called_once_with()
    assert capsys.readouterr().out == "Query result: [('approved',)]\n"


def test_history_menu_does_not_offer_custom_sql_or_prompt_for_it(monkeypatch):
    options_seen = []

    def choose(options, title):
        options_seen.extend(options)
        return 2

    prompt = Mock()
    monkeypatch.setattr(main, "_choose_option", choose)
    monkeypatch.setattr("builtins.input", prompt)

    main.query_menu()

    assert "Enter a Custom SQL Query" not in options_seen
    prompt.assert_not_called()


def test_history_menu_reports_database_errors(monkeypatch, capsys):
    monkeypatch.setattr(
        main, "get_recent_tool_runs",
        Mock(side_effect=sqlite3.OperationalError("database unavailable")),
    )
    monkeypatch.setattr(main, "_choose_option", lambda options, title: 0)

    main.query_menu()

    assert capsys.readouterr().out == (
        "Database query failed: database unavailable. "
        "Initialize the database and check its permissions.\n"
    )


def test_background_service_records_start_failure_and_releases_capacity(
    initialized_db, monkeypatch
):
    service = backend_services.BackgroundRunService(max_workers=1)
    with monkeypatch.context() as patch:
        patch.setattr(
            backend_services.threading, "Thread",
            Mock(side_effect=RuntimeError("thread unavailable")),
        )
        with pytest.raises(backend_services.RunStartError):
            service.submit("sherlock", "alice", Mock())

    rows = database.list_tool_runs()
    assert len(rows) == 1
    assert rows[0][3] == "failed"
    assert rows[0][6] == "The tool run could not be started."
    run_id = service.submit("sherlock", "bob", Mock(return_value="output"))
    assert run_id != rows[0][0]
    assert service._slots.acquire(timeout=2)
    service._slots.release()


def test_background_service_releases_capacity_when_database_start_fails(
    initialized_db, monkeypatch
):
    service = backend_services.BackgroundRunService(max_workers=1)
    with monkeypatch.context() as patch:
        patch.setattr(
            database, "start_tool_run",
            Mock(side_effect=sqlite3.OperationalError("database unavailable")),
        )
        with pytest.raises(sqlite3.OperationalError):
            service.submit("sherlock", "alice", Mock())

    assert service.submit("sherlock", "bob", Mock(return_value="output"))
    assert service._slots.acquire(timeout=2)
    service._slots.release()
