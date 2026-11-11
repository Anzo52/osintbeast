# OSINT Beast

OSINT Beast is a Python command-line framework for organizing authorized open-source intelligence (OSINT) workflows. It provides a curated registry of tools and uses SQLite to store structured records and tool results. The project is under active development; see the [development roadmap](#development-roadmap) and [development notes](doc/NOTES.md).

## Current capabilities

- Browse a curated list of supported tools.
- Download registered tools and install supported Python packages through the CLI. Binary tools such as Amass require manual installation.
- Initialize a local SQLite database for OSINT records and tool metadata.
- Run the Holehe email-check integration and save its output.
- Query stored database records from the CLI.

The current registry includes Holehe, Sherlock, Maigret, Amass, and Sublist3r. Availability in the registry does not necessarily mean every tool has a complete first-party integration; consult the code and each tool's own documentation before use.

## Requirements

- Python 3.10 or later (Python 3.10–3.13 are currently declared in package classifiers).
- Git is needed for repository checkout and some tool workflows.
- Third-party integrations may have their own system and Python dependencies.

The runtime and development dependency ranges are declared in `pyproject.toml`. Direct dependencies use bounded ranges to limit unexpected major-version changes; this is a compatibility policy, not a fully locked, hash-verified environment.

## Getting started

```bash
git clone https://github.com/Anzo52/osintbeast.git
cd osintbeast
python -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python src/main.py
```

From the menu, initialize the database before saving or querying data. Use the supported-tools option to see the registered integrations.

### Database initialization

Select **Initialize Database** from the menu. In a source checkout, the default
database is `osintbeast.db` in the repository root, regardless of the current
working directory. Initialization creates missing tables and indexes in one
transaction; running it again preserves existing records and recreates missing
schema objects. It does not migrate or repair incompatible existing tables.

To use a different location, set `OSINTBEAST_DB_PATH` before starting the
application. The parent directory must already exist and be writable. For an
installed package, set this variable to a writable location outside the
installation directory. Relative paths are resolved against the working
directory at startup. Initialization errors are reported in the menu; check
the path and permissions before retrying.

```bash
# Linux/macOS, for example
export OSINTBEAST_DB_PATH="$HOME/osintbeast.db"
# Windows PowerShell: $env:OSINTBEAST_DB_PATH = "$HOME\osintbeast.db"
```

## Development and tests

Install development dependencies and run tests from the repository root:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m ruff check --select E4,E7,E9,F src tests
```

Tests should mock network requests and external tool execution; normal test runs should not contact third-party services or perform real scans. Keep database and subprocess behavior covered by focused tests.

GitHub Actions runs the test suite and Ruff checks on Python 3.10–3.13 for pushes and pull requests.

## Responsible use and privacy

Use OSINT Beast only with data and targets you are authorized to assess, and follow applicable laws, provider terms, and organizational policies. Tool output may contain personal or sensitive information. Protect the local database, restrict access to it, and remove data when it is no longer needed. Validate inputs and avoid exposing sensitive values in logs, process arguments, or shared environments.

## Project layout

```text
.
├── data/       MySQL schema files
├── doc/        Design documentation
├── src/        CLI, database, SQLite schemas, and tool integrations
├── tests/      Automated tests
├── README.md
├── pyproject.toml
└── osintbeast.db (created at runtime)
```

## Development roadmap

This roadmap describes planned work, not a promise or release schedule. Priorities may change as the project develops.

### 1. Stabilize the foundation

- [x] Keep dependency manifests, source modules, schemas, and documentation in their correct files.
- [x] Declare Python 3.10+ support and bounded dependency ranges in `pyproject.toml`.
- [x] Make database initialization reliable and repeatable across supported environments.
- [ ] Expand automated coverage for database operations, CLI behavior, validation, timeouts, and failures.
- [ ] Add continuous integration to run tests and basic static checks on changes.

### 2. Harden tool execution and data handling

- [ ] Prefer supported in-process integrations or private input mechanisms for sensitive values.
- [ ] Ensure long-running operations time out gracefully and return control to the CLI.
- [ ] Standardize run status, timestamps, errors, and result persistence.
- [ ] Document retention, local database permissions, and sensitive-output handling.
- [ ] Keep downloads and installation constrained to reviewed registry entries and safe paths.

### 3. Improve the CLI and service boundaries

- [ ] Separate reusable application services from menu/input/output code.
- [ ] Provide clear help, input validation, and actionable error messages.
- [ ] Add structured result-history views and safe export options.
- [ ] Keep arbitrary SQL unavailable to any future web-facing interface.

### 4. Build a web UI

- [ ] Define a small backend API around the shared application services.
- [ ] Add authentication and authorization before exposing any network-accessible interface.
- [ ] Add a simple UI for launching authorized runs and viewing their status and results.
- [ ] Run long tasks outside request handlers and enforce resource limits.
- [ ] Add web-route tests with mocked tool execution and security checks.

### 5. Expand persistence and integrations

- [ ] Complete and test MySQL schema and database support behind a storage interface.
- [ ] Add further tool integrations one at a time, with documented inputs, outputs, and safety requirements.
- [ ] Document deployment, backup, upgrade, and migration procedures.

## License

This project is distributed under the GNU General Public License v3.0; see [LICENSE](LICENSE).
