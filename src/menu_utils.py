from simple_term_menu import TerminalMenu

from main import download, install
from tool_registry import TOOL_REGISTRY, get_supported_tools


def _choose_option(options, title):
    choice = TerminalMenu(options, title=title).show()
    if not isinstance(choice, int) or not 0 <= choice < len(options):
        return None
    return choice


def _choose_tool(title, tool_names):
    if not tool_names:
        print("No tools are available for this action.")
        return None
    choice = _choose_option(tool_names, title)
    return tool_names[choice] if choice is not None else None


def _download_tool_menu():
    tool_name = _choose_tool("Select a tool to download", get_supported_tools())
    if tool_name:
        download(tool_name)


def _install_tool_menu():
    installable_tools = [
        name for name in get_supported_tools()
        if TOOL_REGISTRY[name]["install_method"] == "pip"
    ]
    tool_name = _choose_tool("Select a Python tool to install", installable_tools)
    if not tool_name:
        return

    install_source = _choose_option(
        ["Use downloaded copy", "Choose another folder", "Back"],
        f"Install {tool_name}",
    )
    if install_source == 0:
        install(tool_name)
    elif install_source == 1:
        tool_path = input("Enter the local tool folder: ").strip()
        if tool_path:
            install(tool_name, tool_path)