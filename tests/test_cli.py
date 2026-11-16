import subprocess
import zipfile
import zlib

import pytest

import database
import main


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (ValueError("Unsupported tool: unknown"), "Unsupported tool: unknown"),
        (RuntimeError("Unable to download repository archive"), "Unable to download repository archive"),
        (PermissionError("permission denied"), "permission denied"),
        (zipfile.BadZipFile("File is not a zip file"), "File is not a zip file"),
        (zlib.error("invalid compressed data"), "invalid compressed data"),
        (EOFError("truncated archive"), "truncated archive"),
        (NotImplementedError("unsupported compression"), "unsupported compression"),
    ],
)
def test_download_reports_errors(monkeypatch, capsys, error, message):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(main, "download_tool", fail)

    main.download("unknown")

    assert capsys.readouterr().out == f"Download failed: {message}\n"


@pytest.mark.parametrize(
    "error",
    [
        ValueError("Tool is not supported"),
        FileNotFoundError("Tool directory does not exist"),
        PermissionError("permission denied"),
        subprocess.CalledProcessError(1, ["pip"]),
        subprocess.TimeoutExpired(["pip"], 600),
    ],
)
def test_install_reports_errors(monkeypatch, capsys, error):
    monkeypatch.setattr("builtins.input", lambda prompt: "")

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(main, "install_tool", fail)

    main.install("holehe")

    assert capsys.readouterr().out == f"Installation failed: {error}\n"


def test_query_reports_sqlite_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "test.db"))
    database.setup_database()

    main.query("SELECT * FROM missing_table")

    assert capsys.readouterr().out == (
        "Database query failed: no such table: missing_table\n"
    )


@pytest.mark.parametrize(
    ("choice", "inputs", "failure"),
    [
        (2, ["unknown"], "Download failed: Unsupported tool: unknown"),
        (3, ["unknown", ""], "Installation failed: Tool 'unknown' is not supported."),
        (5, ["SELECT * FROM missing_table"], "Database query failed: no such table: missing_table"),
    ],
)
def test_menu_remains_usable_after_failure(
    tmp_path, monkeypatch, capsys, choice, inputs, failure
):
    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "test.db"))
    database.setup_database()
    choices = iter([choice, 1])
    answers = iter(inputs)

    class FakeMenu:
        def __init__(self, options, title):
            pass

        def show(self):
            return next(choices)

    monkeypatch.setattr(main, "TerminalMenu", FakeMenu)
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))

    main.main_menu()
    main.main_menu()

    output = capsys.readouterr().out
    assert failure in output
    assert "- holehe" in output


def test_successful_actions_keep_their_output(monkeypatch, capsys):
    monkeypatch.setattr(main, "download_tool", lambda *args, **kwargs: "tools/holehe")
    monkeypatch.setattr(main, "install_tool", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "query_database", lambda query: [(1,)])
    monkeypatch.setattr("builtins.input", lambda prompt: "")

    main.download("holehe")
    main.install("holehe")
    main.query("SELECT 1")

    assert capsys.readouterr().out == (
        "holehe downloaded successfully to tools/holehe.\n"
        "holehe installed successfully.\n"
        "Query result: [(1,)]\n"
    )
