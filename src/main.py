from simple_term_menu import TerminalMenu

from database import setup_database, query_database
from downloader import download_tool
from installer import install_tool
from holehe_integration import run_holehe, save_tool_result
from tool_registry import get_supported_tools


def main_menu():
    options = [
        "Initialize Database",
        "List Supported Tools",
        "Download Tool",
        "Install Tool",
        "Run Holehe Check",
        "Query Database",
        "Exit",
    ]
    terminal_menu = TerminalMenu(options, title="OSINT Beast Main Menu")
    choice = terminal_menu.show()

    if choice == 0:
        init_db()
    elif choice == 1:
        list_supported_tools()
    elif choice == 2:
        tool_name = input("Enter the name of the tool to download: ").strip()
        download(tool_name)
    elif choice == 3:
        tool_name = input("Enter the name of the tool to install: ").strip()
        install(tool_name)
    elif choice == 4:
        email = input("Enter the email to check: ").strip()
        run_holehe_check(email)
    elif choice == 5:
        query_str = input("Enter your query: ").strip()
        query(query_str)
    elif choice == 6:
        raise SystemExit(0)


def init_db():
    setup_database()
    print("Database initialized successfully.")


def list_supported_tools():
    tools = get_supported_tools()
    print("Supported tools:")
    for tool_name in tools:
        print(f"- {tool_name}")


def download(tool_name):
    target_dir = download_tool(tool_name, output_dir="tools")
    print(f"{tool_name} downloaded successfully to {target_dir}.")


def install(tool_name):
    tool_path = input("Enter the local tool path or press Enter to use the default download directory: ").strip()
    install_path = tool_path or f"tools/{tool_name}"
    install_tool(tool_name, install_path)
    print(f"{tool_name} installed successfully.")


def run_holehe_check(email):
    try:
        result = run_holehe(email)
        print(f"Return code: {result['returncode']}")
        print(result["output"])
        save_tool_result("holehe", email, result["output"])
        print("Holehe check saved to the database.")
    except ValueError as exc:
        print(f"Input error: {exc}")


def query(query_text):
    result = query_database(query_text)
    print(f"Query result: {result}")


if __name__ == "__main__":
    while True:
        main_menu()
