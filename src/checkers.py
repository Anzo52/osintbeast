import warnings
from getpass import GetPassWarning, getpass


def _read_sensitive_input(prompt, *, show_input=False):
    if show_input:
        try:
            return input(prompt).strip()
        except (EOFError, OSError):
            print("Cannot read input. Please use an interactive terminal.")
            return None

    with warnings.catch_warnings():
        warnings.simplefilter("error", GetPassWarning)
        try:
            return getpass(prompt).strip()
        except GetPassWarning:
            print("Unable to securely read sensitive input; terminal echo is enabled.")
            return None
        except (EOFError, OSError):
            print("Cannot read input securely. Please use an interactive terminal.")
            return None


def _run_email_check_menu(check, *, show_input=False):
    email = _read_sensitive_input("Enter the email to check: ", show_input=show_input)
    if email is not None:
        check(email)


def _run_username_check_menu(check, *, show_input=False):
    username = _read_sensitive_input("Enter the username to check: ", show_input=show_input)
    if username is not None:
        check(username)
