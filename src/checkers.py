from main import run_holehe_check


def _run_email_check_menu():
    email = input("Enter the email to check: ").strip()
    run_holehe_check(email)


def _run_username_check_menu(check):
    username = input("Enter the username to check: ").strip()
    check(username)