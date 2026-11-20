import inspect

import pytest

import backend_services
import data.utils.database as database


@pytest.fixture
def initialized_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "backend.db"))
    database.setup_database()


def test_backend_services_return_named_records_for_inserted_run(initialized_db):
    run_id = database.start_tool_run("holehe", "person@example.com")
    database.finish_tool_run(run_id, "success", result_text="output")

    runs = backend_services.get_recent_tool_runs()
    results = backend_services.get_saved_tool_results()

    assert runs
    assert runs[0].run_id == run_id
    assert runs[0].tool_name == "holehe"
    assert runs[0].query_text == "person@example.com"
    assert runs[0].status == "success"
    assert runs[0].error_text is None

    assert results
    assert results[0].tool_name == "holehe"
    assert results[0].query_text == "person@example.com"
    assert results[0].result_text == "output"
    assert isinstance(runs[0], backend_services.RecentToolRun)
    assert isinstance(results[0], backend_services.SavedToolResult)


def test_saved_result_response_text_is_capped(initialized_db):
    output = "x" * (backend_services.MAX_SAVED_RESULT_TEXT_CHARS + 100)
    run_id = database.start_tool_run("holehe", "person@example.com")
    database.finish_tool_run(run_id, "success", result_text=output)

    result = backend_services.get_saved_tool_results()[0]

    assert len(result.result_text) == backend_services.MAX_SAVED_RESULT_TEXT_CHARS
    assert result.result_text.startswith("x" * 100)
    assert result.result_text.endswith("… [truncated]")
    assert database.get_tool_run(run_id)[4] == output
    assert database.list_tool_results(max_result_text_chars=1)[0][3] == "…"


@pytest.mark.parametrize("operation", [
    backend_services.get_recent_tool_runs,
    backend_services.get_saved_tool_results,
])
@pytest.mark.parametrize("raw_sql", [
    "SELECT * FROM ToolRuns",
    "ToolRuns",
    "1; DROP TABLE ToolRuns; --",
])
def test_backend_services_reject_sql_as_limit(initialized_db, operation, raw_sql):
    with pytest.raises(ValueError):
        operation(raw_sql)


def test_saved_result_service_bounds_page_size(initialized_db):
    with pytest.raises(ValueError, match="between 1 and 20"):
        backend_services.get_saved_tool_results(limit=21)


def test_backend_service_operations_accept_only_limit_argument():
    assert list(inspect.signature(
        backend_services.get_recent_tool_runs
    ).parameters) == ["limit"]
    assert list(inspect.signature(
        backend_services.get_saved_tool_results
    ).parameters) == ["limit"]
