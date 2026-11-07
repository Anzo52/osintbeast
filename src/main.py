import os
import subprocess
import sys

from tool_registry import get_tool_metadata


def install_python_package(package_path):
    """Install a Python package from a local path or repository checkout."""
    subprocess.run([sys.executable, "-m", "pip", "install", package_path], check=True)


def install_requirements(requirements_path):
    """Install requirements from a requirements.txt file."""
    if not os.path.isfile(requirements_path):
        raise FileNotFoundError(f"Requirements file not found: {requirements_path}")
    subprocess.run([sys.executable, "-m", "pip", "install", "-r", requirements_path], check=True)


def run_install_script(script_path):
    """Run a shell install script if one exists in the repository."""
    if not os.path.isfile(script_path):
        raise FileNotFoundError(f"Installation script not found at {script_path}")
    subprocess.run(["bash", script_path], check=True)


def install_tool(tool_name, tool_path=None):
    """Install a supported tool using a safe, explicit installation method."""
    tool = get_tool_metadata(tool_name)
    if tool is None:
        raise ValueError(f"Tool '{tool_name}' is not supported.")

    target = tool_path or os.path.join("tools", tool_name)
    if not os.path.isdir(target):
        raise FileNotFoundError(f"Tool path not found for '{tool_name}': {target}")

    install_method = tool.get("install_method", "pip")
    if install_method == "pip":
        requirements_path = os.path.join(target, "requirements.txt")
        if os.path.isfile(requirements_path):
            install_requirements(requirements_path)
        else:
            install_python_package(target)
        return target

    if install_method == "binary":
        install_script = os.path.join(target, "install.sh")
        if os.path.isfile(install_script):
            run_install_script(install_script)
            return target
        raise RuntimeError(f"No install script was found for '{tool_name}' in {target}.")

    raise RuntimeError(f"No supported install method is configured for '{tool_name}'.")
