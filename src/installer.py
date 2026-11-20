import subprocess
import sys
from pathlib import Path

from downloader import download_github_repo as _download_github_repo
from tool_registry import get_reviewed_repo_url, get_tool_metadata


def _find_install_source(tool_path):
    """Resolve the project directory in a downloaded repository folder."""
    root = Path(tool_path).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Tool directory does not exist: {root}")

    def is_python_project(path):
        return (path / "pyproject.toml").is_file() or (path / "setup.py").is_file()

    if is_python_project(root):
        return root

    projects = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        resolved_child = child.resolve()
        if root not in resolved_child.parents:
            raise ValueError("Tool project path escapes the designated directory.")
        if is_python_project(child):
            projects.append(child)
    projects.sort()
    if len(projects) == 1:
        return projects[0]
    if len(projects) > 1:
        raise ValueError(f"Multiple Python projects found in tool directory: {root}")
    raise ValueError(f"No Python project metadata found in tool directory: {root}")


def install_tool(tool_name, tool_path):
    """Install a registered Python tool from its local project directory."""
    tool = get_tool_metadata(tool_name)
    if tool is None:
        raise ValueError(f"Tool '{tool_name}' is not supported.")
    if tool["install_method"] != "pip":
        raise ValueError(
            f"Automated installation is not supported for "
            f"'{tool_name}' ({tool['install_method']} tool)."
        )

    source = _find_install_source(tool_path)
    subprocess.run(
        [sys.executable, "-m", "pip", "install", str(source)],
        check=True,
        timeout=600,
    )


def _ensure_repo_url(repo_url):
    return get_reviewed_repo_url(repo_url)


def download_github_repo(repo_url, output_dir):
    """Download a reviewed tool repository archive to the destination directory."""
    repo_url = _ensure_repo_url(repo_url)
    return _download_github_repo(repo_url, output_dir)


def download_tool(tool_name, output_dir="tools"):
    """Download a supported OSINT tool into a local folder."""
    tool = get_tool_metadata(tool_name)
    if tool is None:
        supported = ", ".join(sorted(__import__("tool_registry").TOOL_REGISTRY))
        raise ValueError(f"Tool '{tool_name}' is not supported. Available tools: {supported}")

    key = tool_name.strip().lower()
    root = Path(output_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = root / key
    target_dir = target.resolve()
    if target_dir != target or root not in target_dir.parents:
        raise ValueError("Tool download path escapes the designated directory.")
    download_github_repo(tool["repo"], target_dir)
    return str(target_dir)
