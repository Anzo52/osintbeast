import getpass
from unittest.mock import Mock

import pytest

import checkers


@pytest.mark.parametrize(
    ("menu", "prompt"),
    [
        (checkers._run_email_check_menu, "Enter the email to check: "),
        (checkers._run_username_check_menu, "Enter the username to check: "),
    ],
)
def test_checker_reads_hidden_input(monkeypatch, menu, prompt):
    reader = Mock(return_value=" value ")
    check = Mock()
    monkeypatch.setattr(checkers, "getpass", reader)
    monkeypatch.setattr("builtins.input", Mock(side_effect=AssertionError("unexpected echo")))

    menu(check)

    reader.assert_called_once_with(prompt)
    check.assert_called_once_with("value")


@pytest.mark.parametrize(
    ("menu", "prompt"),
    [
        (checkers._run_email_check_menu, "Enter the email to check: "),
        (checkers._run_username_check_menu, "Enter the username to check: "),
    ],
)
def test_checker_reads_visible_input_only_when_requested(monkeypatch, menu, prompt):
    reader = Mock(return_value=" value ")
    check = Mock()
    monkeypatch.setattr("builtins.input", reader)
    monkeypatch.setattr(checkers, "getpass", Mock(side_effect=AssertionError("unexpected getpass")))

    menu(check, show_input=True)

    reader.assert_called_once_with(prompt)
    check.assert_called_once_with("value")


@pytest.mark.parametrize("error", [EOFError(), OSError()])
@pytest.mark.parametrize(
    "menu", [checkers._run_email_check_menu, checkers._run_username_check_menu]
)
def test_checker_handles_unavailable_visible_input(monkeypatch, capsys, menu, error):
    check = Mock()
    monkeypatch.setattr("builtins.input", Mock(side_effect=error))

    menu(check, show_input=True)

    check.assert_not_called()
    assert "Cannot read input" in capsys.readouterr().out


@pytest.mark.parametrize(
    "menu", [checkers._run_email_check_menu, checkers._run_username_check_menu]
)
def test_checker_refuses_getpass_fallback_before_reading(monkeypatch, capsys, menu):
    check = Mock()
    stdin = Mock()
    monkeypatch.setattr("sys.stdin", stdin)
    monkeypatch.setattr(checkers, "getpass", getpass.fallback_getpass)

    menu(check)

    stdin.readline.assert_not_called()
    check.assert_not_called()
    assert capsys.readouterr().out == (
        "Unable to securely read sensitive input; terminal echo is enabled.\n"
    )


@pytest.mark.parametrize("error", [EOFError(), OSError()])
@pytest.mark.parametrize(
    "menu", [checkers._run_email_check_menu, checkers._run_username_check_menu]
)
def test_checker_handles_unavailable_input(monkeypatch, capsys, menu, error):
    check = Mock()
    monkeypatch.setattr(checkers, "getpass", Mock(side_effect=error))

    menu(check)

    check.assert_not_called()
    assert "Cannot read input securely" in capsys.readouterr().out
