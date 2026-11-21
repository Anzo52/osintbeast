import json
import logging
import os
import sqlite3
import stat
import warnings
from unittest.mock import Mock

import pytest

import checkers
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
        ("helix", KeyError("missing field"), "failed"),
        ("sherlock", RuntimeError("missing dependency"), "failed"),
        ("holehe", HoleheTimeoutError("timeout"), "timed_out"),
        ("maigret", TimeoutError(), "timed_out"),
        ("helix", TimeoutError("timed out"), "timed_out"),
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


@pytest.mark.parametrize("tool", ["holehe", "maigret", "helix", "sherlock"])
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


def test_non_text_output_is_recorded_as_a_failure(initialized_db):
    status, message = tool_runs.execute_tool_run("helix", "test", lambda _: None)

    assert status == "failed"
    assert message == "helix returned non-text output."
    assert database.query_database(
        "SELECT status, resultText, errorText, finishedAt IS NOT NULL FROM ToolRuns"
    ) == [("failed", None, message, 1)]
    assert database.list_tool_results() == []


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


def test_sensitive_values_are_redacted_from_output_and_run_records(
    initialized_db, monkeypatch, capsys
):
    sensitive_value = "person@example.com"

    def run_with_output(value):
        print(f"Starting lookup for {value}")
        logging.warning("Lookup requested for %s", value)
        return f"Found {value} ({value.replace('@', '%40')})"

    monkeypatch.setattr(
        main,
        "run_holehe",
        Mock(side_effect=lambda value: {"output": run_with_output(value)}),
    )

    main.run_holehe_check(sensitive_value)

    output = capsys.readouterr().out
    assert sensitive_value not in output
    assert "[redacted]" in output
    assert database.query_database(
        "SELECT queryText, resultText FROM ToolRuns"
    ) == [("[redacted]", "Found [redacted] ([redacted])")]
    assert database.query_database(
        "SELECT queryText, resultText FROM ToolResults"
    ) == [("[redacted]", "Found [redacted] ([redacted])")]


def test_sensitive_json_redaction_preserves_schema_and_mixed_encoding(initialized_db):
    sensitive_value = "person/name@example.com"
    result = json.dumps(
        [
            {
                "site": "Example",
                "url": "https://example.test/person/name%40example.com",
                "status": "not found",
                "http_status": 200,
            }
        ],
        indent=2,
    )

    outcome, output = tool_runs.execute_tool_run(
        "sherlock",
        sensitive_value,
        lambda _: result,
        sensitive_input=True,
    )

    record = json.loads(output)[0]
    assert outcome == "success"
    assert record == {
        "site": "Example",
        "url": "https://example.test/[redacted]",
        "status": "not found",
        "http_status": 200,
    }


def test_short_username_redaction_preserves_numeric_json_fields(initialized_db):
    result = json.dumps(
        [
            {
                "site": "Example",
                "url": "https://example.test/2",
                "status": "not found",
                "http_status": 200,
            }
        ],
        indent=2,
    )

    outcome, output = tool_runs.execute_tool_run(
        "sherlock",
        "2",
        lambda _: result,
        sensitive_input=True,
    )

    record = json.loads(output)[0]
    assert outcome == "success"
    assert record == {
        "site": "Example",
        "url": "https://example.test/[redacted]",
        "status": "not found",
        "http_status": 200,
    }


@pytest.mark.parametrize("error_type", [RuntimeError, TypeError])
def test_sensitive_values_are_redacted_from_errors(
    initialized_db, monkeypatch, capsys, error_type
):
    sensitive_value = "person@example.com"
    monkeypatch.setattr(
        main,
        "run_holehe",
        Mock(side_effect=error_type(f"request failed for {sensitive_value}")),
    )

    main.run_holehe_check(sensitive_value)

    output = capsys.readouterr().out
    assert sensitive_value not in output
    assert database.query_database(
        "SELECT queryText, errorText FROM ToolRuns"
    ) == [("[redacted]", "request failed for [redacted]")]


@pytest.mark.parametrize(
    ("menu", "prompt", "value"),
    [
        (
            checkers._run_email_check_menu,
            "Enter the email to check: ",
            "person@example.com",
        ),
        (
            checkers._run_username_check_menu,
            "Enter the username to check: ",
            "private_user",
        ),
    ],
)
def test_sensitive_inputs_use_non_echoing_prompts(
    monkeypatch, capsys, menu, prompt, value
):
    seen = []
    monkeypatch.setattr(
        checkers,
        "getpass",
        lambda actual_prompt: seen.append(actual_prompt) or value,
    )
    runner = Mock()

    menu(runner)

    assert seen == [prompt]
    runner.assert_called_once_with(value)
    assert value not in capsys.readouterr().out


@pytest.mark.parametrize(
    ("menu", "value"),
    [
        (checkers._run_email_check_menu, "person@example.com"),
        (checkers._run_username_check_menu, "private_user"),
    ],
)
def test_sensitive_prompt_refuses_echoing_fallback(monkeypatch, capsys, menu, value):
    input_calls = []

    def insecure_getpass(_prompt):
        warnings.warn("terminal echo is enabled", checkers.GetPassWarning)
        return input("fallback: ")

    def fallback_input(_prompt):
        input_calls.append(True)
        return value

    monkeypatch.setattr(checkers, "getpass", insecure_getpass)
    monkeypatch.setattr("builtins.input", fallback_input)
    runner = Mock()

    menu(runner)

    output = capsys.readouterr().out
    assert "Unable to securely read sensitive input" in output
    assert value not in output
    assert input_calls == []
    runner.assert_not_called()


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


def test_cli_can_start_another_run_after_timeout(initialized_db, monkeypatch, capsys):
    monkeypatch.setattr(
        main,
        "run_maigret",
        Mock(side_effect=[TimeoutError("Maigret exceeded its time limit"), "output"]),
    )

    main.run_maigret_check("user")
    main.run_maigret_check("user")

    assert capsys.readouterr().out == (
        "Maigret timed out: Maigret exceeded its time limit\n"
        "output\n"
        "Maigret check saved to the database.\n"
    )
    assert database.query_database(
        "SELECT status FROM ToolRuns ORDER BY runID"
    ) == [("timed_out",), ("success",)]


def test_maigret_menu_action_dispatches_username(monkeypatch):
    class FakeMenu:
        def __init__(self, options, title):
            assert "Run Maigret Check" in options

        def show(self):
            return 6

    runner = Mock()
    monkeypatch.setattr(main, "TerminalMenu", FakeMenu)
    monkeypatch.setattr(checkers, "getpass", lambda prompt: " user ")
    monkeypatch.setattr(main, "run_maigret_check", runner)
    main.main_menu()
    runner.assert_called_once_with("user")


def test_history_filter_renders_filtered_page(initialized_db, monkeypatch, capsys):
    matching_id = database.start_tool_run("holehe", "private@example.test")
    database.finish_tool_run(matching_id, "success", result_text="match")
    other_id = database.start_tool_run("maigret", "different")
    database.finish_tool_run(other_id, "failed", error_text="error")
    choices = iter((1, 6))
    inputs = iter(("holehe", "success"))
    monkeypatch.setattr(main, "_choose_option", lambda options, title: next(choices))
    monkeypatch.setattr("builtins.input", lambda prompt: next(inputs))

    main.run_history_menu()

    output = capsys.readouterr().out
    assert output.count(f"Run {matching_id} | holehe") == 2
    assert output.count(f"Run {other_id} | maigret") == 1


@pytest.mark.parametrize(
    ("consent", "expected_input", "expected_result"),
    [
        ("", "[REDACTED]", "[REDACTED]"),
        ("no", "[REDACTED]", "[REDACTED]"),
        ("yes", "private@example.test", "sensitive result"),
    ],
)
def test_export_selected_runs_obeys_sensitive_consent(
    initialized_db, monkeypatch, tmp_path, capsys,
    consent, expected_input, expected_result,
):
    run_id = database.start_tool_run("holehe", "private@example.test")
    database.finish_tool_run(run_id, "success", result_text="sensitive result")
    output_path = tmp_path / "runs.json"
    inputs = iter((str(run_id), str(output_path), consent))
    monkeypatch.setattr("builtins.input", lambda prompt: next(inputs))

    main.export_selected_runs()

    record = json.loads(output_path.read_text(encoding="utf-8"))[0]
    assert record["input"] == expected_input
    assert record["result"] == expected_result
    if os.name == "posix":
        assert stat.S_IMODE(output_path.stat().st_mode) == 0o600
    assert "Exported 1 run(s)" in capsys.readouterr().out


def test_export_does_not_overwrite_existing_file(initialized_db, tmp_path):
    destination = tmp_path / "runs.json"
    destination.write_text("existing", encoding="utf-8")

    with pytest.raises(FileExistsError):
        main._write_export_file(str(destination), "replacement")

    assert destination.read_text(encoding="utf-8") == "existing"
    assert {path.name for path in tmp_path.iterdir()} == {"runs.db", "runs.json"}


def test_failed_export_cleans_up_temporary_file(tmp_path, monkeypatch):
    destination = tmp_path / "runs.json"

    def fail_link(*args, **kwargs):
        raise OSError("cannot publish")

    monkeypatch.setattr(main.os, "link", fail_link)
    with pytest.raises(OSError, match="cannot publish"):
        main._write_export_file(str(destination), "{}")

    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


def test_history_and_inspection_report_database_path_errors(monkeypatch, capsys):
    monkeypatch.setattr(
        main, "list_tool_runs", Mock(side_effect=OSError("database path denied"))
    )
    monkeypatch.setattr(
        main, "get_tool_run", Mock(side_effect=OSError("database path denied"))
    )

    assert main.show_run_history() == []
    main.inspect_tool_run(1)

    assert capsys.readouterr().out.count("database path denied") == 2


def test_history_filter_reports_database_errors(monkeypatch, capsys):
    monkeypatch.setattr(
        main,
        "list_tool_runs",
        Mock(side_effect=sqlite3.OperationalError("database unavailable")),
    )
    choices = iter((1, 6))
    inputs = iter(("holehe", "success"))
    monkeypatch.setattr(main, "_choose_option", lambda options, title: next(choices))
    monkeypatch.setattr("builtins.input", lambda prompt: next(inputs))

    main.run_history_menu()

    assert capsys.readouterr().out.count(
        "Run history query failed: database unavailable"
    ) == 2
