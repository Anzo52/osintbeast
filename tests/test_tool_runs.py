import sqlite3
from unittest.mock import Mock

import pytest

import data.utils.database as database
import main
import tool_runs
from holehe_integration import HoleheTimeoutError


@pytest.fixture
def initialized_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "runs.db"))
    database.setup_database()


@pytest.mark.parametrize(
    ("tool", "error", "status"),
    [
        ("holehe", RuntimeError("missing dependency"), "failed"),
        ("maigret", ValueError("invalid site database"), "failed"),
        ("maigret", OSError("network error"), "failed"),
        ("sherlock", RuntimeError("missing dependency"), "failed"),
        ("holehe", HoleheTimeoutError("timeout"), "timed_out"),
        ("maigret", TimeoutError(), "timed_out"),
        ("sherlock", TimeoutError(), "timed_out"),
    ],
)
def test_failed_runs_are_recorded(initialized_db, tool, error, status):
    def fail(_):
        raise error

    outcome, message = tool_runs.execute_tool_run(tool, "test", fail)

    assert outcome == status
    assert message
    assert database.query_database(
        "SELECT toolName, queryText, status, resultText, errorText, "
        "startedAt IS NOT NULL, finishedAt IS NOT NULL FROM ToolRuns"
    ) == [(tool, "test", status, None, message, 1, 1)]
    assert database.list_tool_results() == []


@pytest.mark.parametrize("tool", ["holehe", "maigret", "sherlock"])
def test_success_run_preserves_legacy_results(initialized_db, tool):
    assert tool_runs.execute_tool_run(tool, "test", lambda _: "output") == (
        "success", "output"
    )
    assert database.query_database(
        "SELECT toolName, queryText, status, resultText, errorText, "
        "startedAt IS NOT NULL, finishedAt IS NOT NULL FROM ToolRuns"
    ) == [(tool, "test", "success", "output", None, 1, 1)]
    assert database.query_database(
        "SELECT toolName, queryText, resultText FROM ToolResults"
    ) == [(tool, "test", "output")]


def test_short_username_redaction_preserves_unrelated_json_data(initialized_db):
    output = (
        '{"a": "a", "username": "a", "http_status": 200, '
        '"status": "available", "description": "marathon"}'
    )

    assert tool_runs.execute_tool_run(
        "sherlock", "a", lambda _: output, sensitive_input=True
    ) == (
        "success",
        '{\n  "a": "[redacted]",\n  "username": "[redacted]",\n  "http_status": 200,\n'
        '  "status": "available",\n  "description": "marathon"\n}',
    )
    assert database.query_database(
        "SELECT queryText, resultText FROM ToolRuns"
    ) == [("[redacted]", '{\n  "a": "[redacted]",\n  "username": "[redacted]",\n  "http_status": 200,\n'
                        '  "status": "available",\n  "description": "marathon"\n}')]


def test_short_username_redaction_only_replaces_complete_text_tokens():
    assert tool_runs._redact_sensitive_value(
        "a and abc; x_a; https://example.test/a", "a"
    ) == "[redacted] and abc; x_a; https://example.test/[redacted]"


def test_finish_is_atomic_if_legacy_result_insert_fails(initialized_db):
    run_id = database.start_tool_run("maigret", "test")
    with database.connect_db() as conn:
        conn.execute("DROP TABLE ToolResults")

    with pytest.raises(sqlite3.OperationalError):
        database.finish_tool_run(run_id, "success", "output")

    assert database.query_database(
        "SELECT status, resultText, finishedAt FROM ToolRuns"
    ) == [("running", None, None)]


def test_reinitializing_legacy_database_preserves_results(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    monkeypatch.setattr(database, "DB_FILE", str(path))
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE ToolResults (resultID INTEGER PRIMARY KEY AUTOINCREMENT, "
            "toolName TEXT NOT NULL, queryText TEXT NOT NULL, resultText TEXT, "
            "createdAt TEXT DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute("INSERT INTO ToolResults (toolName, queryText, resultText) VALUES ('holehe', 'old', 'old result')")
    database.setup_database()
    database.setup_database()

    assert database.query_database("SELECT queryText, resultText FROM ToolResults") == [
        ("old", "old result")
    ]
    assert database.query_database("SELECT * FROM ToolRuns") == []


@pytest.mark.parametrize(
    ("function", "invalid"),
    [
        (main.run_holehe_check, "bad email"),
        (main.run_maigret_check, "bad username"),
        (main.run_sherlock_check, "bad username"),
    ],
)
def test_invalid_input_is_not_recorded(initialized_db, capsys, function, invalid):
    function(invalid)
    assert "Input error:" in capsys.readouterr().out
    assert database.query_database("SELECT * FROM ToolRuns") == []


@pytest.mark.parametrize(
    ("function", "valid", "target"),
    [
        (main.run_holehe_check, "user@example.com", "run_holehe"),
        (main.run_maigret_check, "user", "run_maigret"),
    ],
)
def test_missing_database_prevents_run(tmp_path, monkeypatch, capsys, function, valid, target):
    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "missing.db"))
    runner = Mock()
    monkeypatch.setattr(main, target, runner)
    function(valid)
    runner.assert_not_called()
    assert "Could not record" in capsys.readouterr().out


def test_run_finish_rejects_repeated_completion(initialized_db):
    run_id = database.start_tool_run("holehe", "test")
    database.finish_tool_run(run_id, "failed", error_text="error")
    with pytest.raises(ValueError, match="already finished"):
        database.finish_tool_run(run_id, "success", result_text="output")


@pytest.mark.parametrize(
    ("status", "result_text", "error_text"),
    [
        ("running", None, None),
        ("success", None, None),
        ("success", "output", "error"),
        ("failed", "output", "error"),
        ("failed", None, None),
        ("timed_out", "output", None),
    ],
)
def test_finish_tool_run_rejects_invalid_terminal_states(
    initialized_db, status, result_text, error_text
):
    run_id = database.start_tool_run("holehe", "test")

    with pytest.raises(ValueError):
        database.finish_tool_run(run_id, status, result_text, error_text)

    assert database.query_database(
        "SELECT status, resultText, errorText, finishedAt FROM ToolRuns"
    ) == [("running", None, None, None)]


@pytest.mark.parametrize(
    ("function", "target", "value", "query_text", "result", "tool"),
    [
        (main.run_holehe_check, "run_holehe", " user@example.com ", "user@example.com", {"output": "email output"}, "holehe"),
        (main.run_maigret_check, "run_maigret", " user ", "user", "username output", "maigret"),
        (main.run_helix_check, "run_helix", " @user ", "user", "helix output", "helix"),
        (main.run_sherlock_check, "run_sherlock", " user ", "user", "sherlock output", "sherlock"),
    ],
)
def test_cli_saves_success(
    initialized_db, monkeypatch, capsys, function, target, value, query_text, result, tool
):
    runner = Mock(return_value=result)
    monkeypatch.setattr(main, target, runner)
    function(value)
    runner.assert_called_once_with(query_text)
    assert f"{tool.capitalize()} check saved to the database." in capsys.readouterr().out
    assert database.query_database("SELECT toolName, queryText, status FROM ToolRuns") == [
        (tool, "[redacted]", "success")
    ]


def test_cli_reports_and_records_failure(initialized_db, monkeypatch, capsys):
    monkeypatch.setattr(main, "run_maigret", Mock(side_effect=RuntimeError("dependency missing")))
    main.run_maigret_check("user")
    assert "Maigret could not run: dependency missing" in capsys.readouterr().out
    assert database.query_database("SELECT status, errorText FROM ToolRuns") == [
        ("failed", "dependency missing")
    ]


def test_database_failure_after_execution_is_reported(initialized_db, monkeypatch, capsys):
    monkeypatch.setattr(main, "run_maigret", Mock(return_value="output"))
    monkeypatch.setattr(database, "finish_tool_run", Mock(side_effect=sqlite3.OperationalError("disk full")))
    main.run_maigret_check("user")
    output = capsys.readouterr().out
    assert "Could not record maigret run: disk full" in output
    assert "saved" not in output
    assert database.query_database("SELECT status FROM ToolRuns") == [("running",)]


def test_cli_reports_timeout_without_claiming_success(initialized_db, monkeypatch, capsys):
    monkeypatch.setattr(
        main,
        "run_maigret",
        Mock(side_effect=TimeoutError("Maigret exceeded its time limit")),
    )

    main.run_maigret_check("user")

    assert capsys.readouterr().out == (
        "Maigret timed out: Maigret exceeded its time limit\n"
    )
    assert database.query_database(
        "SELECT status, resultText, errorText FROM ToolRuns"
    ) == [("timed_out", None, "Maigret exceeded its time limit")]
    assert database.list_tool_results() == []


def test_maigret_menu_action_dispatches_username(monkeypatch):
    class FakeMenu:
        def __init__(self, options, title):
            assert "Run Maigret Check" in options

        def show(self):
            return 6

    runner = Mock()
    monkeypatch.setattr(main, "TerminalMenu", FakeMenu)
    monkeypatch.setattr("builtins.input", lambda prompt: " user ")
    monkeypatch.setattr(main, "run_maigret_check", runner)
    main.main_menu()
    runner.assert_called_once_with("user")
