import io
import os
import zipfile

import requests

from tool_registry import get_tool_metadata


def _ensure_repo_url(repo_url):
    if not repo_url:
        raise ValueError("A GitHub repository URL is required.")
    if not repo_url.startswith(("http://", "https://")):
        repo_url = f"https://{repo_url}"
    return repo_url.rstrip("/")


def download_github_repo(repo_url, output_dir):
    """Download a GitHub repository archive and extract it to the destination directory."""
    repo_url = _ensure_repo_url(repo_url)
    os.makedirs(output_dir, exist_ok=True)

    last_error = None
    for branch in ("main", "master"):
        zip_url = f"{repo_url}/archive/refs/heads/{branch}.zip"
        try:
            response = requests.get(zip_url, timeout=30)
            response.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                archive.extractall(output_dir)
            return output_dir
        except requests.RequestException as exc:
            last_error = exc

    if last_error is not None:
        raise RuntimeError(f"Unable to download repository archive from {repo_url}: {last_error}")
    raise RuntimeError(f"No supported default branch was found for repository {repo_url}.")


def download_tool(tool_name, output_dir="tools"):
    """Download a supported OSINT tool into a local folder."""
    tool = get_tool_metadata(tool_name)
    if tool is None:
        supported = ", ".join(sorted(__import__("tool_registry").TOOL_REGISTRY))
        raise ValueError(f"Tool '{tool_name}' is not supported. Available tools: {supported}")

    target_dir = os.path.join(output_dir, tool_name)
    download_github_repo(tool["repo"], target_dir)
    return target_dir
