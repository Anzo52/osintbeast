from __future__ import annotations

from importlib.metadata import PackageNotFoundError, distribution
from shutil import which
from typing import TypedDict


class ToolMetadata(TypedDict):
    repo: str
    category: str
    install_method: str
    install_target: str
    notes: str


TOOL_REGISTRY: dict[str, ToolMetadata] = {
    "holehe": {
        "repo": "https://github.com/megadose/holehe",
        "category": "email",
        "install_method": "pip",
        "install_target": "holehe",
        "notes": "Checks whether an email is registered on public sites.",
    },
    "sherlock": {
        "repo": "https://github.com/sherlock-project/sherlock",
        "category": "username",
        "install_method": "pip",
        "install_target": "sherlock-project",
        "notes": "Searches for usernames across social networks.",
    },
    "maigret": {
        "repo": "https://github.com/soxoj/maigret",
        "category": "username",
        "install_method": "pip",
        "install_target": "maigret",
        "notes": "Searches for usernames and enriches public profiles.",
    },
    "amass": {
        "repo": "https://github.com/owasp-amass/amass",
        "category": "domain",
        "install_method": "binary",
        "install_target": "amass",
        "notes": "Performs passive subdomain and attack-surface discovery.",
    },
    "sublist3r": {
        "repo": "https://github.com/aboul3la/Sublist3r",
        "category": "domain",
        "install_method": "pip",
        "install_target": "Sublist3r",
        "notes": "Enumerates subdomains using public sources.",
    },
    "helix": {
        "repo": "https://github.com/thalha-a9/helix",
        "category": "username",
        "install_method": "pip",
        "install_target": "helix-osint",
        "notes": "Searches for usernames across social networks.",
    },
}


def get_supported_tools() -> list[str]:
    """Return the alphabetized list of supported tool names."""
    return sorted(TOOL_REGISTRY)


def get_tool_metadata(name: str) -> ToolMetadata | None:
    """Return metadata for a supported tool, ignoring surrounding whitespace and case."""
    if not isinstance(name, str):
        return None
    return TOOL_REGISTRY.get(name.strip().lower())


def is_tool_installed(name: str) -> bool:
    """Check the current interpreter's distributions or PATH for binary tools."""
    metadata = get_tool_metadata(name)
    if metadata is None:
        raise ValueError(f"Unknown tool: {name}")
    if metadata["install_method"] == "binary":
        return which(metadata["install_target"]) is not None
    if metadata["install_method"] != "pip":
        raise ValueError(f"Unsupported install method: {metadata['install_method']}")
    try:
        distribution(metadata["install_target"])
    except PackageNotFoundError:
        return False
    return True
