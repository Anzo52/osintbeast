# OSINT Beast

OSINT Beast is a Python-based CLI for Open Source Intelligence gathering. It combines a small set of vetted OSINT tools into one framework, stores structured data in SQLite, and is designed to grow toward broader database and workflow support.

## Supported tools

The project currently includes the following tool integrations through `src/tool_registry.py`:

- `holehe` — email registration and username-related checks on public sites
- `sherlock` — username lookups across many social platforms
- `maigret` — deeper username discovery and profile enrichment
- `amass` — passive domain and subdomain enumeration
- `sublist3r` — subdomain discovery using public OSINT sources

## Features

- Download supported OSINT tools from GitHub
- Install tools using explicit and safe installation logic
- Initialize a SQLite schema for target and tool metadata
- Manage a curated list of supported tools in one registry

## Installation

```bash
git clone https://github.com/Anzo52/osintbeast.git
cd osintbeast
python -m pip install -r requirements.txt
