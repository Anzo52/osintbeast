from __future__ import annotations

from typing import TypedDict


class ToolMetadata(TypedDict):
    repo: str
    category: str
    install_method: str
    notes: str


TOOL_REGISTRY: dict[str, ToolMetadata] = {
    "holehe": {
        "repo": "https://github.com/megadose/holehe",
        "category": "email",
        "install_method": "pip",
        "notes": "Checks whether an email is registered on public sites.",
    },
    "sherlock": {
        "repo": "https://github.com/sherlock-project/sherlock",
        "category": "username",
        "install_method": "pip",
        "notes": "Searches for usernames across social networks.",
    },
    "maigret": {
        "repo": "https://github.com/soxoj/maigret",
        "category": "username",
        "install_method": "pip",
        "notes": "Searches for usernames and enriches public profiles.",
    },
    "amass": {
        "repo": "https://github.com/owasp-amass/amass",
        "category": "domain",
        "install_method": "binary",
        "notes": "Performs passive subdomain and attack-surface discovery.",
    },
    "sublist3r": {
        "repo": "https://github.com/aboul3la/Sublist3r",
        "category": "domain",
        "install_method": "pip",
        "notes": "Enumerates subdomains using public sources.",
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
