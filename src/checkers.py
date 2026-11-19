def _run_email_check_menu(check):
    email = input("Enter the email to check: ").strip()
    check(email)


def _run_username_check_menu(check):
    username = input("Enter the username to check: ").strip()
    check(username)