import subprocess
import os

def install_python_package(package_path):
    """
    Install a Python package using pip.

    :param package_path: Path to the Python package directory or wheel file.
    """
    try:
        subprocess.run(["pip", "install", package_path], check=True)
        print(f"Python package at {package_path} installed successfully.")
    except subprocess.CalledProcessError as e:
        print(f"Error installing Python package: {e}")

def run_install_script(script_path):
    """
    Run a shell script to install a tool.

    :param script_path: Path to the installation shell script.
    """
    if not os.path.isfile(script_path):
        print(f"Installation script not found at {script_path}")
        return

    try:
        subprocess.run(["bash", script_path], check=True)
        print(f"Installation script at {script_path} executed successfully.")
    except subprocess.CalledProcessError as e:
        print(f"Error running installation script: {e}")

def install_tool(tool_name, tool_path):
    """
    Install an OSINT tool. This function can be expanded to handle different installation methods based on the tool.

    :param tool_name: Name of the tool to install.
    :param tool_path: Path to the tool's installation files.
    """
    # Example logic, should be expanded based on your tool requirements
    if tool_name == "example_python_tool":
        install_python_package(tool_path)
    elif tool_name == "example_script_tool":
        run_install_script(os.path.join(tool_path, "install.sh"))
    else:
        print(f"Installation method for {tool_name} not defined.")
