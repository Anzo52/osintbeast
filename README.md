# OSINT Beast

OSINT Beast is a Python command-line framework for organizing authorized open-source intelligence (OSINT) workflows. It provides a curated registry of tools and uses SQLite to store structured records and tool results. The project is under active development; see the [development roadmap](#development-roadmap) and [development notes](doc/NOTES.md).

## Current capabilities

- Browse the tool registry and check whether each tool is installed.
- Download registered tools and install supported Python packages through the CLI. Binary tools such as Amass require manual installation.
- Initialize a local SQLite database for OSINT records and tool metadata.
- Run Holehe email checks and optional Maigret, Helix OSINT, and Sherlock username checks, with recorded outcomes.
- Query stored database records from the CLI.

### Integrated tools

These tools have a first-party run action in the menu, input validation, and
recorded outcomes:

| Tool | Input | Availability |
| --- | --- | --- |
| Holehe | Sensitive email address; in-process Python API | Included in the standard installation |
| Maigret | Sensitive username; in-process Python API | Optional: `python -m pip install -e ".[maigret]"` |
| Helix OSINT | Sensitive username; in-process Python API | Optional: `python -m pip install -e ".[helix]"` |
| Sherlock | Sensitive username; in-process Python API | Optional: `python -m pip install -e ".[sherlock]"` |

### Registered tools planned for integration

The registry also supports discovering and downloading these tools, but they
do not yet have first-party run actions in OSINT Beast. They are candidates for
future integrations, not implemented features:

| Tool | Registry support | Planned status |
| --- | --- | --- |
| Sublist3r | Download and Python-package installation | Run integration not implemented; future runs must use stdin/private pipe |
| Amass | Download only; requires manual binary installation | Run integration not implemented; future runs must use stdin/private pipe |

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
saved results. Type only the information that cannot be selected in advance,
such as a username, email address, or custom folder. Errors are reported in the
menu so you can correct the input or environment and try again.

### Command-line commands

The installed `osintbeast` command also accepts direct commands. Run
`osintbeast --help` or `osintbeast <command> --help` for complete usage. With
no command, it opens the interactive menu.

```text
osintbeast init-db
osintbeast list-tools
osintbeast download TOOL
osintbeast install TOOL [--path DIRECTORY]
osintbeast check-email EMAIL
osintbeast check-username {maigret,helix,sherlock} USERNAME
osintbeast query {runs,results}
```

Email checks accept a practical email address such as `person@example.com`.
Username checks accept ASCII letters, digits, dots, underscores, and hyphens;
Maigret and Sherlock allow up to 100 characters, while Helix allows up to 50
and optionally accepts a leading `@`. The `query` command displays recent
runs or saved results; it does not accept arbitrary SQL. Invalid inputs and
failed operations print a corrective message and return a nonzero status.

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

To enable Sherlock:

```bash
python -m pip install -e ".[sherlock]"
```

Select **Run Holehe Check**, **Run Maigret Check**, **Run Helix Check**, or
**Run Sherlock Check** in the menu. Sherlock uses its bundled site list and
skips sites marked NSFW. It sends a username to many public sites; each request
has a 15-second timeout, and the full scan has a 300-second overall timeout.
Use it only for authorized targets. Helix uses its built-in site list through
the in-process API; the broader optional source collections are not enabled by
this menu integration. Helix accepts up to
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

The CLI validates input before recording a run and uses a non-echoing prompt.
Holehe, Maigret, Helix, and Sherlock receive sensitive input through their
in-process Python APIs, not process arguments. Their run records use a
`[redacted]` query; direct and URL-encoded occurrences of the input are also
redacted from saved and displayed results and errors. A `running` record can
remain after interruption or a database-write failure. Existing
`ToolResults` rows are preserved but do not gain historical run statuses.

Inputs, results, and errors can contain sensitive data. They are stored in
plain text in the local database; restrict its permissions and delete records
when no longer needed. See the [local data retention and privacy guide](doc/data-retention-and-privacy.md)
for exactly what is retained, how to restrict database and backup access, and
safe practices for viewing, removing, and sharing sensitive output.

Valid attempts are stored in `ToolRuns` with the tool, input, status (`running`,
`success`, `failed`, or `timed_out`), and a UTC `startedAt` timestamp. A run
starts as `running`, with `finishedAt`, `resultText`, and `errorText` unset.
Every terminal run has a UTC `finishedAt`; `success` requires saved output in
`resultText` and no `errorText`, while `failed` and `timed_out` require an
`errorText` and no `resultText`. Successful outputs are also saved to the
existing `ToolResults` table; failures and timeouts are never stored there.
Use **Query Database** to inspect either table.

Sublist3r and Amass do not yet have run integrations. Any future integration
that needs an external process must pass sensitive input through standard input
(for example, `subprocess.run(..., input=value, text=True)`), never an argument,
environment variable, log, or echoed prompt. If a tool cannot accept a private
input channel, it must not be integrated until a safe mechanism is available.
Searches send the supplied input to third-party sites.

### Local HTTP API

The experimental HTTP API is separate from the CLI and does not provide a
browser UI. It requires configured bearer credentials before starting and binds
only to `127.0.0.1`; remote binding is not supported. Keep it on a trusted
machine and do not expose it through a proxy or port forward.

Set `OSINTBEAST_API_TOKENS` to a JSON object whose keys are unique,
cryptographically random URL-safe tokens of at least 32 characters and whose
values are roles. Configure the value through a secret manager or another
private environment mechanism; do not commit it, place it in a URL, or include
it in logs:

```text
{"<32+ character random operator token>":"operator","<32+ character random viewer token>":"viewer"}
```

Replace the example token placeholders with private random tokens, then set the
JSON as `OSINTBEAST_API_TOKENS` in the server's environment and start the API:

```bash
python -m web_api
```

An `operator` may launch checks and view runs and results. A `viewer` may view
runs and results but cannot launch checks. Every API route requires a bearer
token; missing or invalid credentials receive `401`, and insufficient
permissions receive `403`. The API exposes only fixed operations, not custom
SQL:

| Method | Path | Access |
| --- | --- | --- |
| `POST` | `/api/runs` | Operator; JSON body contains `tool` and `query` strings |
| `GET` | `/api/runs` | Operator or viewer |
| `GET` | `/api/results` | Operator or viewer |

This server uses plain HTTP and is intentionally loopback-only. Do not make it
remotely accessible; a future remote deployment must add HTTPS and retain
server-side authentication and authorization.

### Database initialization

Select **Initialize Database** from the menu. In a source checkout, the default
database is `osintbeast.db` in the repository root, regardless of the current
working directory. Initialization creates missing tables and indexes in one
transaction; running it again preserves existing records and recreates missing
schema objects, including the new run-history table in existing databases. It
does not migrate or repair incompatible existing tables.

To use a different location, set `OSINTBEAST_DB_PATH` before starting the
application. For a source checkout, the default remains the repository-root
`osintbeast.db`. For an installed package, the default is a per-user data
directory: `%APPDATA%\osintbeast` on Windows,
`~/Library/Application Support/osintbeast` on macOS, or
`$XDG_DATA_HOME/osintbeast` on Linux (falling back to
`~/.local/share/osintbeast`). The database directory is created as needed.
Custom relative paths are resolved against the working directory at startup.
Initialization errors are reported in the menu; check the path and permissions
before retrying.

For deployment, backups, upgrades, and schema-migration expectations, see the
[operations guide](doc/OPERATIONS.md).

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
python -m ruff check --select E4,E7,E9,F src data tests
```

Tests should mock network requests and external tool execution; normal test runs should not contact third-party services or perform real scans. Keep database and subprocess behavior covered by focused tests.
The Maigret API-contract tests require the optional extra; without it those
tests are skipped.

GitHub Actions runs the test suite and Ruff checks on Python 3.10–3.13 for pushes and pull requests.
It also installs the Maigret extra so its offline API-contract tests run.

### Background run service

`background_runs.BackgroundRuns` is a shared service for web handlers, not a
web server. It is not yet connected to the browser interface below. Initialize
the database first and create one service per application
process. `submit(tool_name, query)` validates the input, records a run and
returns its integer ID without waiting for tool execution; `status(run_id)`
returns the persisted record (or `None`), and `cancel(run_id)` requests
cancellation and returns whether the run was active. Poll status for the
terminal outcome. Cancellation is recorded as `failed` with the error
`Run cancelled.` for compatibility with existing databases.

Configure `RunLimits(max_concurrent=2, timeout_seconds=300,
max_query_chars=254, max_output_bytes=1000000)` when constructing the service.
There is no waiting queue: `CapacityError` explicitly rejects submissions when
all slots are occupied. Execution occurs in child processes, which are stopped
on timeout or cancellation; tool failures and unexpected worker exits are
recorded as failures. Call `close()` during orderly application shutdown.
If saving an outcome fails, the service retains it in memory and keeps its
capacity slot reserved. Calling `status(run_id)` retries the write and raises
`RuntimeError` if it still cannot be saved; retry after restoring database
access. Pending outcomes are lost if the application process exits.

These limits apply per service instance, not across web server processes or
hosts. Use a single application process or an external coordinator to enforce
a deployment-wide limit. An abrupt application crash can leave `running`
records behind; this in-process service is not a durable job queue. A web
interface must enforce authentication, authorization, CSRF protection as
appropriate, and access controls on run records before exposing these methods.
Tool output is limited when returned to the parent; tools may still consume
additional memory internally while producing it.
## Browser interface

Start the local browser interface with:

```bash
python -m web_ui
```

Open `http://127.0.0.1:8000`. The interface initializes the local database and
supports authorized runs with Holehe, Maigret, Helix, and Sherlock; tools in the
registry without a runnable integration are not offered. Confirm authorization
before each run. Runs execute through the existing validators, integrations,
and persistence service, and their status and saved output are available in
the recent-runs view. Keep the interface bound to loopback: it has no login or
remote-access authentication, and its database and results may contain
sensitive information.

## Responsible use and privacy

Use OSINT Beast only with data and targets you are authorized to assess, and
follow applicable laws, provider terms, and organizational policies. Tool
output may contain personal or sensitive information; follow the
[local data retention and privacy guide](doc/data-retention-and-privacy.md).
Validate inputs and avoid exposing sensitive values in logs, process
arguments, or shared environments.

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

This roadmap is ordered by priority and prerequisite: earlier sections establish
the safety and shared-service foundations needed by later work. It describes
planned work, not a promise or release schedule, and priorities may change as
the project develops.

### UI direction

Keep the CLI as the current local and automation interface, and prioritize a
browser-based UI as the next graphical interface. Do not build separate native
desktop or mobile applications. The web UI remains planned work and should use
shared application services rather than duplicating CLI behavior.

### 1. Stabilize the foundation

- [x] Keep dependency manifests, source modules, schemas, and documentation in their correct files.
- [x] Declare Python 3.10+ support and bounded dependency ranges in `pyproject.toml`.
- [x] Make database initialization reliable and repeatable across supported environments.
- [x] Expand automated coverage for database operations, CLI behavior, validation, timeouts, and failures.
- [x] Add continuous integration to run tests and basic static checks on changes.

### 2. Harden tool execution and data handling

- [ ] Keep downloads and installation constrained to reviewed registry entries and safe paths.
- [ ] Prefer supported in-process integrations or private input mechanisms for sensitive values.
- [ ] Ensure long-running operations time out gracefully and return control to the CLI.
- [ ] Standardize run status, timestamps, errors, and result persistence.
- [ ] Parse tool output into normalized findings while preserving raw output and reporting malformed or unsupported formats explicitly.
- [ ] Document retention, local database permissions, and sensitive-output handling.
- [ ] Keep arbitrary SQL unavailable to any future web-facing interface.

### 3. Improve the CLI and service boundaries

- [ ] Separate reusable application services from menu/input/output code.
- [ ] Provide clear help, input validation, and actionable error messages.
- [ ] Add structured result-history views and safe export options.

### 4. Build a web UI

- [ ] Add authentication and authorization before exposing any network-accessible interface.
- [ ] Run long tasks outside request handlers and enforce resource limits.
- [ ] Define a small backend API around the shared application services.
- [ ] Add a simple UI for launching authorized runs and viewing their status and results.
- [ ] Add web-route tests with mocked tool execution and security checks.

### 5. Expand persistence and integrations

- [ ] Complete and test MySQL schema and database support behind a storage interface.
- [x] Add a first-party run integration for Sherlock; Sublist3r and Amass remain candidates, with documented inputs, outputs, and safety requirements.
- [x] Document deployment, backup, upgrade, and migration procedures.

## License

This project is distributed under the GNU General Public License v3.0; see [LICENSE](LICENSE).
