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

    runs = backend_services.get_recent_tool_runs()
    results = backend_services.get_saved_tool_results()

    assert runs
    assert runs[0].run_id == run_id
    assert runs[0].tool_name == "holehe"
    assert runs[0].query_text == "[redacted]"
    assert runs[0].status == "success"
    assert runs[0].error_text is None
    assert results
    assert results[0].tool_name == "holehe"
    assert results[0].query_text == "[redacted]"
    assert results[0].result_text == "output"
    assert isinstance(runs[0], backend_services.RecentToolRun)
    assert isinstance(results[0], backend_services.SavedToolResult)


def test_saved_result_service_caps_result_text_in_storage(initialized_db):
    output = "x" * (backend_services.MAX_SAVED_RESULT_TEXT_CHARS + 100)
    run_id = database.start_tool_run("holehe", "person@example.com")
    database.finish_tool_run(run_id, "success", result_text=output)

    result = backend_services.get_saved_tool_results()[0]

    assert len(result.result_text) == backend_services.MAX_SAVED_RESULT_TEXT_CHARS
    assert result.result_text.startswith("x" * 100)
    assert result.result_text.endswith("… [truncated]")
    assert database.get_tool_run(run_id)[4] == output


def test_saved_result_service_enforces_page_size(initialized_db):
    with pytest.raises(ValueError, match="between 1 and 20"):
        backend_services.get_saved_tool_results(limit=21)


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
    ("view", "service_name"),
    [("runs", "get_recent_tool_runs"), ("results", "get_saved_tool_results")],
)
def test_query_view_uses_approved_service_methods(
    monkeypatch, capsys, view, service_name
):
    service = Mock(return_value=[("approved",)])
    monkeypatch.setattr(main, service_name, service)
    monkeypatch.setattr(
        database, "query_database",
        Mock(side_effect=AssertionError("raw SQL persistence used")),
    )

    main.query_view(view)

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
    assert main.query_view("runs") == 1

    assert capsys.readouterr().out == (
        "Database query failed: database unavailable. "
        "Initialize the database and check its permissions.\n"
    )
