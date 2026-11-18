# OSINT Beast

OSINT Beast is a Python command-line framework for organizing authorized open-source intelligence (OSINT) workflows. It provides a curated registry of tools and uses SQLite to store structured records and tool results. The project is under active development; see the [development roadmap](#development-roadmap) and [development notes](doc/NOTES.md).

## Current capabilities

- Browse the tool registry and check whether each tool is installed.
- Download registered tools and install supported Python packages through the CLI. Binary tools such as Amass require manual installation.
- Initialize a local SQLite database for OSINT records and tool metadata.
- Run Holehe email checks and optional Maigret and Helix OSINT username checks, with recorded outcomes.
- Query stored database records from the CLI.

### Integrated tools

These tools have a first-party run action in the menu, input validation, and
recorded outcomes:

| Tool | Input | Availability |
| --- | --- | --- |
| Holehe | Email address | Included in the standard installation |
| Maigret | Username | Optional: `python -m pip install -e ".[maigret]"` |
| Helix OSINT | Username | Optional: `python -m pip install -e ".[helix]"` |

### Registered tools planned for integration

The registry also supports discovering and downloading these tools, but they
do not yet have first-party run actions in OSINT Beast. They are candidates for
future integrations, not implemented features:

| Tool | Registry support | Planned status |
| --- | --- | --- |
| Sherlock | Download and Python-package installation | Run integration not implemented |
| Sublist3r | Download and Python-package installation | Run integration not implemented |
| Amass | Download only; requires manual binary installation | Run integration not implemented |

Being registered does not mean a tool can be run from the OSINT Beast menu.
See the [development roadmap](#development-roadmap) for broader planned work.

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

From the menu, initialize the database before saving or querying data. The
supported-tools option lists all registered tools. It reports whether each Python tool's distribution is installed in the
current interpreter, or whether the Amass executable is available on `PATH`.
This indicates presence, not whether the tool or all its dependencies work.
Use the arrow keys and Enter to choose a tool to download or install; installing
uses the default `tools/<name>` download folder. A custom installation folder is
available as an alternate menu choice. Database views include recent runs and
saved results, alongside a custom SQL option. Type only the information that
cannot be selected in advance, such as a username, email address, custom folder,
or custom SQL query. Errors are reported in the menu so you can correct the
input or environment and try again.

### Running checks

Holehe is installed with the standard dependencies. To enable Maigret in a
source checkout, install its optional dependencies:

```bash
python -m pip install -e ".[maigret]"
```

To enable Helix OSINT:

```bash
python -m pip install -e ".[helix]"
```

Select **Run Holehe Check**, **Run Maigret Check**, or **Run Helix Check** in the menu. Helix
uses its built-in site list through the in-process API; the broader optional
source collections are not enabled by this menu integration. Helix accepts up to
50 ASCII letters, digits, dots, underscores, and hyphens (with an optional leading `@`).
The scan has a 300-second overall timeout.
Maigret accepts
up to 100 ASCII letters, digits, dots, underscores, and hyphens. It checks all
enabled username sites in its bundled database, without a proxy or profile
enrichment. This can contact thousands of services and consume considerable
network resources; use it only for authorized targets. Each Maigret request
has a 30-second timeout, and the overall search has a 300-second timeout.
Partial results are not saved when the overall search times out. Individual
site failures appear as `Unknown` results; a successful run means the search
completed, not that every site was reachable or every match is confirmed.

The CLI validates input before recording a run. Valid attempts are stored in
`ToolRuns` with the tool, input, status (`running`, `success`, `failed`, or
`timed_out`), UTC start and finish timestamps, and either output or an error.
A `running` record can remain after interruption or a database-write failure.
Successful outputs are also saved to the existing `ToolResults` table.
Existing `ToolResults` rows are preserved but do not gain historical run
statuses. Use **Query Database** to inspect either table.

Inputs, results, and errors can contain sensitive data. They are stored in
plain text in the local database; restrict its permissions and delete records
when no longer needed. Searches send the supplied input to third-party sites.

### Database initialization

Select **Initialize Database** from the menu. In a source checkout, the default
database is `osintbeast.db` in the repository root, regardless of the current
working directory. Initialization creates missing tables and indexes in one
transaction; running it again preserves existing records and recreates missing
schema objects, including the new run-history table in existing databases. It
does not migrate or repair incompatible existing tables.

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
The Maigret API-contract tests require the optional extra; without it those
tests are skipped.

GitHub Actions runs the test suite and Ruff checks on Python 3.10–3.13 for pushes and pull requests.
It also installs the Maigret extra so its offline API-contract tests run.

## Responsible use and privacy

Use OSINT Beast only with data and targets you are authorized to assess, and follow applicable laws, provider terms, and organizational policies. Tool output may contain personal or sensitive information. Protect the local database, restrict access to it, and remove data when it is no longer needed. Validate inputs and avoid exposing sensitive values in logs, process arguments, or shared environments.

## Project layout

```text
.
├── data/
│   └── schemas/  MySQL and SQLite schema files
├── doc/        Design documentation
├── src/        CLI, database, and tool integrations
├── tests/      Automated tests
├── README.md
├── pyproject.toml
└── osintbeast.db (created at runtime)
```

## Development roadmap

This roadmap describes planned work, not a promise or release schedule. Priorities may change as the project develops.

### UI direction

Keep the CLI as the current local and automation interface, and prioritize a
browser-based UI as the next graphical interface. Do not build separate native
desktop or mobile applications. The web UI remains planned work and should use
shared application services rather than duplicating CLI behavior.

### 1. Stabilize the foundation

- [x] Keep dependency manifests, source modules, schemas, and documentation in their correct files.
- [x] Declare Python 3.10+ support and bounded dependency ranges in `pyproject.toml`.
- [x] Make database initialization reliable and repeatable across supported environments.
- [ ] Expand automated coverage for database operations, CLI behavior, validation, timeouts, and failures.
- [x] Add continuous integration to run tests and basic static checks on changes.

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
- [ ] Add first-party run integrations for the registered Sherlock, Sublist3r, and Amass tools, with documented inputs, outputs, and safety requirements.
- [ ] Document deployment, backup, upgrade, and migration procedures.

## License

This project is distributed under the GNU General Public License v3.0; see [LICENSE](LICENSE).
