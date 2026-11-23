import json
import logging
import sqlite3
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

    outcome, message = tool_runs.execute_tool_run(
        tool, "test", fail, sensitive_input=False
    )

    assert outcome == status
    assert message
    assert database.query_database(
        "SELECT toolName, queryText, status, resultText, errorText, "
        "startedAt IS NOT NULL, finishedAt IS NOT NULL FROM ToolRuns"
    ) == [(tool, "test", status, None, message, 1, 1)]
    assert database.list_tool_results() == []


def test_keyboard_interrupt_is_preserved_if_failure_persistence_fails(
    initialized_db, monkeypatch
):
    run_id = database.start_tool_run("maigret", "test")
    monkeypatch.setattr(
        database,
        "finish_tool_run",
        Mock(side_effect=sqlite3.OperationalError("database is locked")),
    )

    with pytest.raises(KeyboardInterrupt, match="stop"):
        tool_runs.execute_tool_run(
            "maigret",
            "test",
            Mock(side_effect=KeyboardInterrupt("stop")),
            run_id=run_id,
            sensitive_input=False,
        )


def test_keyboard_interrupt_during_result_redaction_marks_run_failed(
    initialized_db, monkeypatch
):
    monkeypatch.setattr(
        tool_runs,
        "redact_sensitive_output",
        Mock(side_effect=KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        tool_runs.execute_tool_run(
            "holehe", "private", lambda _: "output", sensitive_input=True
        )

    assert database.query_database("SELECT status, errorText FROM ToolRuns") == [
        ("failed", "Tool run interrupted.")
    ]


def test_keyboard_interrupt_after_run_creation_marks_run_failed(
    initialized_db, monkeypatch
):
    monkeypatch.setattr(
        tool_runs, "_execute_tool_run", Mock(side_effect=KeyboardInterrupt())
    )

    with pytest.raises(KeyboardInterrupt):
        tool_runs.execute_tool_run(
            "maigret", "test", lambda _: "output", sensitive_input=False
        )

    assert database.query_database("SELECT status, errorText FROM ToolRuns") == [
        ("failed", "Tool run interrupted.")
    ]


@pytest.mark.parametrize("tool", ["holehe", "maigret", "helix", "sherlock"])
def test_success_run_preserves_legacy_results(initialized_db, tool):
    assert tool_runs.execute_tool_run(
        tool, "test", lambda _: "output", sensitive_input=False
    ) == (
        "success", "output"
    )
    assert database.query_database(
        "SELECT toolName, queryText, status, resultText, errorText, "
        "startedAt IS NOT NULL, finishedAt IS NOT NULL FROM ToolRuns"
    ) == [(tool, "test", "success", "output", None, 1, 1)]
    assert database.query_database(
        "SELECT toolName, queryText, resultText FROM ToolResults"
    ) == [(tool, "test", "output")]


@pytest.mark.parametrize("sensitive_input", [None, 0, "false"])
def test_sensitivity_must_be_boolean(initialized_db, sensitive_input):
    runner = Mock()
    with pytest.raises(TypeError):
        tool_runs.execute_tool_run("holehe", "private", runner, sensitive_input=sensitive_input)
    with pytest.raises(TypeError):
        tool_runs.execute_tool_run_with_id(
            "holehe", "private", runner, sensitive_input=sensitive_input
        )
    with pytest.raises(TypeError):
        tool_runs.start_tool_run("holehe", "private", sensitive_input=sensitive_input)
    runner.assert_not_called()
    assert database.list_tool_runs() == []


def test_sensitivity_is_required(initialized_db):
    runner = Mock()
    with pytest.raises(TypeError):
        tool_runs.execute_tool_run("holehe", "private", runner)
    runner.assert_not_called()
    assert database.list_tool_runs() == []


@pytest.mark.parametrize(
    ("outcome", "error"),
    [("success", None), ("failed", RuntimeError("private input error")),
     ("timed_out", TimeoutError("private input timeout"))],
)
def test_sensitive_run_redacts_persistence(initialized_db, outcome, error):
    runner = Mock(return_value="private input result", side_effect=error)
    status, value = tool_runs.execute_tool_run(
        "holehe", "private input", runner, sensitive_input=True
    )
    runner.assert_called_once_with("private input")
    assert status == outcome
    assert value == (
        "[redacted] result" if error is None else str(error).replace("private input", "[redacted]")
    )
    assert database.query_database(
        "SELECT queryText, status, resultText, errorText FROM ToolRuns"
    ) == [(
        tool_runs.REDACTED, outcome,
        value if error is None else None,
        value if error is not None else None,
    )]
    assert database.query_database(
        "SELECT queryText, resultText FROM ToolResults"
    ) == ([(tool_runs.REDACTED, value)] if error is None else [])


def test_sensitive_existing_run_must_already_be_redacted(initialized_db):
    run_id = database.start_tool_run("holehe", "private input")
    runner = Mock()
    with pytest.raises(ValueError, match="redacted running record"):
        tool_runs.execute_tool_run(
            "holehe", "private input", runner, run_id=run_id, sensitive_input=True
        )
    runner.assert_not_called()
    assert database.get_tool_run(run_id)[3] == "running"


def test_sensitive_existing_redacted_run_is_completed(initialized_db):
    run_id = database.start_tool_run("holehe", tool_runs.REDACTED)
    assert tool_runs.execute_tool_run(
        "holehe", "private input", lambda value: f"result: {value}",
        run_id=run_id, sensitive_input=True,
    ) == ("success", "result: [redacted]")
    assert database.get_tool_run(run_id)[2:6] == (
        tool_runs.REDACTED, "success", "result: [redacted]", None
    )
    assert database.query_database(
        "SELECT queryText, resultText FROM ToolResults"
    ) == [(tool_runs.REDACTED, "result: [redacted]")]


def test_non_text_output_is_recorded_as_a_failure(initialized_db):
    status, message = tool_runs.execute_tool_run(
        "helix", "test", lambda _: None, sensitive_input=False
    )

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


def test_reinitializing_legacy_database_redacts_results(tmp_path, monkeypatch):
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
        ("[redacted]", "[redacted] result")
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


def test_list_tool_runs_filters_orders_and_paginates(initialized_db):
    oldest = database.start_tool_run("holehe", "old")
    database.finish_tool_run(oldest, "success", result_text="old result")
    database.start_tool_run("maigret", "middle")
    newest = database.start_tool_run("holehe", "new")
    database.finish_tool_run(newest, "failed", error_text="failed")

    assert [row[0] for row in database.list_tool_runs(limit=2)] == [newest, newest - 1]
    assert [row[2] for row in database.list_tool_runs(
        tool_name="holehe", status="success"
    )] == ["old"]
    assert database.list_tool_runs(tool_name="holehe' OR 1=1 --") == []
    assert [row[2] for row in database.list_tool_runs(offset=2)] == ["old"]
    assert database.list_tool_runs(offset=3) == []
    with pytest.raises(ValueError):
        database.list_tool_runs(status="unknown")


def test_export_tool_runs_json_escapes_content_and_handles_empty_results(
    initialized_db,
):
    malformed = '{"broken":\n"value"'
    malformed_id = database.start_tool_run("maigret", 'user,"example"')
    database.finish_tool_run(malformed_id, "success", result_text=malformed)
    empty_id = database.start_tool_run("holehe", "empty")
    database.finish_tool_run(empty_id, "success", result_text="")

    exported = json.loads(
        database.export_tool_runs_json([malformed_id, empty_id])
    )

    assert exported[0]["input"] == "[REDACTED]"
    assert exported[0]["result"] == "[REDACTED]"
    assert exported[1]["result"] == "[REDACTED]"
    included = json.loads(
        database.export_tool_runs_json([malformed_id, empty_id], include_sensitive=True)
    )
    assert included[0]["input"] == 'user,"example"'
    assert included[0]["result"] == malformed
    assert included[1]["result"] == ""


def test_export_tool_runs_json_rejects_missing_or_invalid_ids(initialized_db):
    with pytest.raises(ValueError):
        database.export_tool_runs_json([])
    with pytest.raises(ValueError):
        database.export_tool_runs_json([0])
    with pytest.raises(ValueError, match="do not exist"):
        database.export_tool_runs_json([99])


def test_history_menu_filters_and_inspects_results(
    initialized_db, monkeypatch, capsys
):
    first_id = database.start_tool_run("maigret", "first")
    failed_id = database.start_tool_run("holehe", "target")
    database.finish_tool_run(failed_id, "failed", error_text="broken output")
    choices = iter([1, 2, 6])
    answers = iter(["holehe", "failed", str(failed_id)])

    class FakeMenu:
        def __init__(self, options, title):
            assert title == "Run History"

        def show(self):
            return next(choices)

    monkeypatch.setattr("menu_utils.TerminalMenu", FakeMenu)
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))

    main.run_history_menu()

    output = capsys.readouterr().out
    assert f"Run {failed_id} | holehe | input='target' | failed" in output
    assert "Error: 'broken output'" in output
    assert output.count(f"Run {first_id} |") == 1


def test_export_selected_runs_creates_json_with_explicit_sensitive_opt_in(
    initialized_db, monkeypatch, tmp_path, capsys
):
    result = '{"matched":\nfalse}'
    run_id = database.start_tool_run("holehe", "person@example.com")
    database.finish_tool_run(run_id, "success", result_text=result)
    output_path = tmp_path / "selected.json"
    answers = iter([str(run_id), str(output_path), "yes"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))

    main.export_selected_runs()

    exported = json.loads(output_path.read_text(encoding="utf-8"))
    assert exported[0]["input"] == "person@example.com"
    assert exported[0]["result"] == result
    assert output_path.stat().st_mode & 0o077 == 0
    assert f"Exported 1 run(s) to {output_path}." in capsys.readouterr().out

    original_content = output_path.read_bytes()
    answers = iter([str(run_id), str(output_path), "yes"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    main.export_selected_runs()

    assert output_path.read_bytes() == original_content
    assert "Could not export runs:" in capsys.readouterr().out


def test_history_menu_filter_handles_sqlite_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "missing.db"))
    choices = iter([1, 6])
    answers = iter(["holehe", "success"])

    class FakeMenu:
        def __init__(self, options, title):
            assert title == "Run History"

        def show(self):
            return next(choices)

    monkeypatch.setattr("menu_utils.TerminalMenu", FakeMenu)
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))

    main.run_history_menu()

    output = capsys.readouterr().out
    assert "Invalid history filter:" not in output
    assert output.count("Run history query failed: no such table: ToolRuns") == 2


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
    output = capsys.readouterr().out
    assert (result["output"] if isinstance(result, dict) else result) in output
    assert f"{tool.capitalize()} check saved to the database." in output
    assert database.query_database("SELECT toolName, queryText, status FROM ToolRuns") == [
        (tool, tool_runs.REDACTED, "success")
    ]


def test_cli_reports_and_records_failure(initialized_db, monkeypatch, capsys):
    monkeypatch.setattr(main, "run_maigret", Mock(side_effect=RuntimeError("dependency missing")))
    main.run_maigret_check("user")
    assert "Maigret could not run: dependency missing" in capsys.readouterr().out
    assert database.query_database("SELECT status, errorText FROM ToolRuns") == [
        ("failed", "dependency missing")
    ]


def test_sensitive_values_are_redacted_from_output_and_run_records(
    initialized_db, monkeypatch, capsys, caplog
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
    assert sensitive_value not in caplog.text
    assert all(sensitive_value not in record.getMessage() for record in caplog.records)
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


@pytest.mark.parametrize("query", ["a", "2"])
def test_short_username_redaction_does_not_replace_substrings(query):
    result = json.dumps({
        "status": "data and 200",
        "url": f"https://example.test/{query}",
        "message": f"found {query}, not data or 200",
    })

    record = json.loads(tool_runs.redact_sensitive_output(result, query))

    assert record["status"] == "data and 200"
    assert record["url"] == "https://example.test/[redacted]"
    assert record["message"] == "found [redacted], not data or 200"


def test_sensitive_values_are_redacted_from_errors(
    initialized_db, monkeypatch, capsys
):
    sensitive_value = "person@example.com"
    monkeypatch.setattr(
        main,
        "run_holehe",
        Mock(side_effect=RuntimeError(f"request failed for {sensitive_value}")),
    )

    main.run_holehe_check(sensitive_value)

    output = capsys.readouterr().out
    assert sensitive_value not in output
    assert database.query_database(
        "SELECT queryText, errorText FROM ToolRuns"
    ) == [("[redacted]", "request failed for [redacted]")]


def test_sensitive_unexpected_failure_is_recorded_and_propagated(initialized_db):
    sensitive_value = "person@example.com"

    def fail(value):
        raise TypeError(f"unexpected value: {value}")

    with pytest.raises(tool_runs.UnexpectedToolError) as error:
        tool_runs.execute_tool_run(
            "holehe", sensitive_value, fail, sensitive_input=True
        )

    assert "TypeError" in str(error.value)
    assert sensitive_value not in str(error.value)
    assert any(frame.name == "fail" for frame in error.traceback)
    assert database.query_database(
        "SELECT queryText, status, errorText FROM ToolRuns"
    ) == [("[redacted]", "failed", "holehe failed unexpectedly (TypeError).")]


def test_sensitivity_must_be_explicit():
    with pytest.raises(TypeError, match="sensitive_input"):
        tool_runs.execute_tool_run("holehe", "private", lambda _: "output")
    with pytest.raises(TypeError, match="sensitive_input"):
        tool_runs.execute_tool_run_with_id("holehe", "private", lambda _: "output")
    with pytest.raises(TypeError, match="sensitive_input"):
        tool_runs.start_tool_run("holehe", "private")


def test_execute_tool_run_with_id_redacts_sensitive_values(initialized_db):
    sensitive_value = "person@example.com"
    run_id, status, output = tool_runs.execute_tool_run_with_id(
        "holehe",
        sensitive_value,
        lambda value: f"Found {value}",
        sensitive_input=True,
    )

    assert status == "success"
    assert output == "Found [redacted]"
    assert database.get_tool_run(run_id)[2:6] == (
        "[redacted]", "success", "Found [redacted]", None
    )
    assert database.query_database(
        "SELECT queryText, resultText FROM ToolResults"
    ) == [("[redacted]", "Found [redacted]") ]

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
