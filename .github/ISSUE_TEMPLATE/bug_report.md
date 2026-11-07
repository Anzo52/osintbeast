# OSINT Beast

OSINT Beast is a Python-based CLI for gathering, storing, and managing OSINT workflow data. The project is intentionally lightweight: it downloads supported tool repositories, exposes a safe install path, and stores results in SQLite with a schema that can be extended to MySQL.

## Current status

This repository is still in development. The goal is to move from the placeholder database and example tooling to a real registry of vetted OSINT tools, with explicit safety checks before downloads or installation.

## Features

- Download supported OSINT tools from GitHub
- Install tools using explicit and safe installation logic
- Initialize a SQLite schema for target and tool data
- Track supported tools through a central registry

## Supported tools

The project currently recognizes a small, curated set of tools:

- `holehe`
- `sherlock`
- `maigret`
- `amass`
- `sublist3r`
- `dorker`

## Installation

```bash
git clone https://github.com/Anzo52/osintbeast.git
cd osintbeast
python -m pip install -r requirements.txt
```

## Running the CLI

```bash
python src/main.py
```

## Safety note

Only run this project against targets and data sources you are authorized to assess. OSINT research should always comply with applicable laws, access policies, and platform rules.

## Repository layout

```text
.
├── data/                  Project SQL schema definitions
├── doc/                   Design and notes
├── src/                   CLI and tooling logic
├── LICENSE
├── README.md
├── requirements.txt
└── osintbeast.db         SQLite database created at runtime
```

## Planned work

- Replace placeholder database logic with the real schema
- Add explicit tool metadata and source validation
- Add safer install flows and repository checks
- Complete MySQL schema support
- Add issue and pull request templates
- Expand tests for CLI and database behavior
