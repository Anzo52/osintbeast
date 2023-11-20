from simple_term_menu import TerminalMenu
from downloader import download_tool
from installer import install_tool
from database import setup_database, query_database

def main_menu():
    options = ["Initialize Database", "Download Tool", "Install Tool", "Query Database", "Exit"]
    terminal_menu = TerminalMenu(options, title="OSINT Beast Main Menu")
    choice = terminal_menu.show()

    if choice == 0:
        init_db()
    elif choice == 1:
        tool_name = input("Enter the name of the tool to download: ")
        download(tool_name)
    elif choice == 2:
        tool_name = input("Enter the name of the tool to install: ")
        install(tool_name)
    elif choice == 3:
        query_str = input("Enter your query: ")
        query(query_str)
    elif choice == 4:
        exit()

def init_db():
    setup_database()
    print("Database initialized successfully.")

def download(tool_name):
    download_tool(tool_name)
    print(f"{tool_name} downloaded successfully.")

def install(tool_name):
    install_tool(tool_name)
    print(f"{tool_name} installed successfully.")

def query(query):
    result = query_database(query)
    print(f"Query result: {result}")

if __name__ == "__main__":
    while True:
        main_menu()
