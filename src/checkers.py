import getpass
import warnings


def _read_sensitive_input(prompt):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            return getpass.getpass(prompt).strip()
    except (getpass.GetPassWarning, EOFError, OSError):
        print("Cannot read input securely. Please use an interactive terminal.")
        return None


def _run_email_check_menu(check):
    email = _read_sensitive_input("Enter the email to check: ")
    if email is not None:
        check(email)


def _run_username_check_menu(check):
    username = _read_sensitive_input("Enter the username to check: ")
    if username is not None:
        check(username)