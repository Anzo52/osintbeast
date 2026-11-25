# OSINT Beast

OSINT Beast is a Python command-line framework for organizing authorized open-source intelligence (OSINT) workflows. It provides a curated registry of tools and uses SQLite to store structured records and tool results. The project is under active development; see the [documentation](doc/wiki/Home.md), [development roadmap](#development-roadmap), and [development notes](doc/NOTES.md).

## Current capabilities

- Browse the tool registry and check whether each tool is installed.
- Download registered tools and install supported Python packages through the CLI. Binary tools such as Amass require manual installation.
- Initialize a local SQLite database for OSINT records and tool metadata.
- Run Holehe email checks and optional Maigret, Helix OSINT, and Sherlock username checks, with recorded outcomes.
- Review, filter, and page through prior runs; inspect results and export selected runs as JSON.
- Query stored database records from the CLI.
- Launch authorized checks and retrieve their status and results through a local API.

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
saved results; saved-result browsing is limited to 20 rows with result text
truncated at 16,384 characters. Type only the information that cannot be
selected in advance, such as a username, email address, or custom folder.
Errors are reported in the menu so you can correct the input or environment
and try again.

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

Select a supported check from the menu. Typed target input is visible in the
terminal, so be aware of people who can see your screen; checks send it to
third-party sites. See the
[running checks guide](doc/wiki/Running-Checks.md) for optional installations,
tool limits, run history, exports, and privacy details.

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

### Database configuration and initialization

Select **Initialize Database** from the menu. In a source checkout, the default
database is `osintbeast.db` in the repository root, regardless of the current
working directory. SQLite initialization creates missing tables and indexes in one
transaction; running it again preserves existing records and recreates missing
schema objects, including the new run-history table in existing databases. The
initialization does not rewrite legacy records. Saved run and result views
redact queries and matching result or error text at read time; the database
values remain unchanged. This does not repair incompatible existing tables.

To use SQLite at a different location, set `OSINTBEAST_DB_PATH` before starting
the application. For a source checkout, the default remains the repository-root
`osintbeast.db`. For an installed package, the default is a per-user data
directory: `%APPDATA%\osintbeast` on Windows,
`~/Library/Application Support/osintbeast` on macOS, or
`$XDG_DATA_HOME/osintbeast` on Linux (falling back to
`~/.local/share/osintbeast`). The database directory is created as needed.
Custom relative paths are resolved against the working directory at startup.

For deployment, backups, upgrades, and schema-migration expectations, see the
[operations guide](doc/OPERATIONS.md).

```bash
# Linux/macOS, for example
export OSINTBEAST_DB_PATH="$HOME/osintbeast.db"
# Windows PowerShell: $env:OSINTBEAST_DB_PATH = "$HOME\osintbeast.db"
```

MySQL is also supported through the same persistence operations. Install the
optional driver and set `OSINTBEAST_DATABASE_URL`; this setting takes precedence
over `OSINTBEAST_DB_PATH`.

```bash
python -m pip install -e ".[mysql]"
export OSINTBEAST_DATABASE_URL="mysql://${MYSQL_USER}:${MYSQL_PASSWORD}@db.example:3306/osintbeast?charset=utf8mb4"
```

The target database must already exist, and the configured account needs
permission to create tables and indexes and to read/write the application
tables. Use a protected secret source rather than committing credentials or
putting a production password in shell history; URL-encode special characters
in credentials. `mysql://` URLs are accepted and use PyMySQL automatically.
Supported MySQL servers are MySQL 8.0.16 or
newer (the schema uses enforced `CHECK` constraints); MariaDB and older MySQL
releases are not currently covered. The MySQL schema uses InnoDB, foreign keys,
uniqueness constraints, and indexes for relation and recent-run queries.

Run **Initialize Database** after configuring the URL. MySQL initialization is
idempotent: it creates absent tables with their declared indexes and preserves existing rows,
including by adding the `ToolRuns` table to a pre-run-history installation.
MySQL commits DDL implicitly, so initialization cannot roll back already
created tables if a later statement fails. It does not alter incompatible
existing columns, add constraints to tables that already existed without them,
or migrate data between SQLite and MySQL. Back up the database before applying
schema changes and use a MySQL-compatible client for custom SQL queries.

Initialization errors are reported in the menu; check the path and permissions
before retrying.

## Local API

Install the package with `python -m pip install -e .` and any desired tool
extras as described above. Set a strong, secret `OSINTBEAST_API_TOKEN` (for example, generate one with
`python -c "import secrets; print(secrets.token_urlsafe(32))"`), then run:

```bash
python -m api
```

The server initializes the SQLite schema and listens on `127.0.0.1:8000`.
It is a minimal local, single-operator server, not a production internet-facing
deployment. Every request must include an `Authorization` header using the
`Bearer` scheme and the configured token. Anyone with the token can
start runs and read **all** stored runs, including those
created by the CLI. Keep the token and database private. Do not expose this
server directly to an untrusted network; a deployment needs HTTPS, access
controls, resource limits, and an appropriate production WSGI server.
The WSGI application factory is `api.create_app(token)`.

All responses are JSON with `Cache-Control: no-store`. The API does not expose
SQL, command execution, installation, or configurable tool options.

### `POST /runs`

Send `Content-Type: application/json` with a body of at most 4096 bytes:

```json
{"tool": "maigret", "query": "example_user", "authorized": true}
```

`authorized` must be the JSON boolean `true`, confirming that the caller is
authorized to assess the target. `tool` must be one of `holehe`, `maigret`,
`helix`, or `sherlock` (case and surrounding whitespace are normalized).
`query` is an email for Holehe or a username for the other tools, validated
and normalized by the same integration as the CLI. No additional fields or
options are accepted. Optional integrations must be installed separately.

Execution is **synchronous**: the request waits for the tool and persistence
to finish. The bundled single-threaded server cannot handle other requests
while a scan is running. On a recorded outcome, including a tool failure or timeout, it
returns `201 Created`, `Location: /runs/<run_id>`, and, for example:

```json
{"run_id": 1, "status": "success"}
```

Statuses are `running`, `success`, `failed`, or `timed_out`. Interruption or a
storage failure can leave a `running` record; a storage error does not imply
that a scan did not run. Clients should not automatically retry such requests.

### `GET /runs/<run_id>`

The positive integer ID identifies the same persisted run returned by POST.
Example `200 OK` response:

```json
{"run_id": 1, "tool": "maigret", "query": "example_user",
 "status": "success", "result": "tool output", "error": null,
 "started_at": "2026-01-01 12:00:00", "finished_at": "2026-01-01 12:01:00"}
```

Timestamps are UTC. `result` is a string on success and otherwise null;
`error` is a string for failed or timed-out runs and otherwise null.
`finished_at` is null while running. Outputs and errors may contain sensitive
data, as can the query itself.

Errors use `{"error": {"code": "...", "message": "..."}}`: `400` for invalid
requests, JSON, tools, or queries; `401` for a missing or invalid token; `403`
when authorization is not confirmed; `404` for an unknown endpoint or run;
`413` for an oversized body; `415` for an unsupported content type; and `500`
for storage or unexpected internal failures. Validation errors do not create a run.

## Development and tests

Install development dependencies and run tests from the repository root:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m ruff check --select E4,E7,E9,F src data tests
```

Tests should mock network requests and external tool execution; normal test runs should not contact third-party services or perform real scans. Keep database and subprocess behavior covered by focused tests.
MySQL storage behavior is tested with a mocked SQLAlchemy engine, so CI does
not require a live MySQL service.
The Maigret API-contract tests require the optional extra; without it those
tests are skipped.

GitHub Actions runs the test suite and Ruff checks on Python 3.10–3.13 for pushes and pull requests.
It also installs the Maigret extra so its offline API-contract tests run.

### Flask web API

The optional Flask API requires an `OSINTBEAST_API_TOKEN`; it rejects all API
requests when no token is configured. Start it with
`OSINTBEAST_API_TOKEN=<token> python -m flask --app web:create_app run`.
Authenticated clients can launch a run with `POST /api/runs` and retrieve
status and saved output from `GET /api/runs/<run_id>` and
`GET /api/results/<result_id>`. Requests use a `Bearer` authorization token.

### Background run service

`background_runs.BackgroundRuns` is a shared service for web handlers, not a
web server. The browser interface below uses one instance. Initialize
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
on timeout or cancellation; each worker also monitors its deadline and its
parent process so it exits if the parent dies. On POSIX, the worker's process
group is killed as well; on Windows, this watchdog stops only the worker.
Tool failures and unexpected worker exits are recorded as failures. Call
`close()` during orderly application shutdown.
If saving an outcome fails, the service retains it in memory and keeps its
capacity slot reserved while retrying the write independently of status
requests. Calling `status(run_id)` also retries immediately and raises
`RuntimeError` if the outcome still cannot be saved. Pending outcomes are lost
if the application process exits.
If starting the supervisor fails and recording that failure also fails,
`submit` raises `StartupPersistenceError` with a `run_id` attribute; retry
`status(run_id)` after database access is restored. The browser API includes
that ID in its 503 response.

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
the recent-runs view. At most two browser runs can be active at once; additional
requests receive HTTP 503 until capacity is available. Holehe input is redacted
in stored records and output. Submissions accept an `Idempotency-Key`; retries
with the same key and input return the existing run, while reusing a key for a
different run returns HTTP 409. Keys are retained for the lifetime of the
service.

Configure separate operator and viewer tokens before starting the server:

```bash
export OSINTBEAST_OPERATOR_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export OSINTBEAST_VIEWER_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
python -m web_ui
```

Send a configured token using HTTP bearer authentication. Operators may launch
runs and read results; viewers may only read results. Missing, invalid, or
unauthorized credentials are rejected. The browser prompts for an access token
and keeps it only in session storage for the current tab. The server refuses
to start unless both tokens are configured and refuses any bind address outside
IPv4 or IPv6 loopback. Do not expose the server through a tunnel or reverse
proxy; keep its database and results restricted to authorized local users.

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
├── doc/        Documentation, including wiki pages
├── src/        CLI, database, and tool integrations
├── tests/      Automated tests
├── README.md
├── pyproject.toml
└── osintbeast.db (created at runtime)
```

## Development roadmap

See the [development roadmap](doc/wiki/Roadmap.md) for priorities and planned work.

## License

This project is distributed under the GNU General Public License v3.0; see [LICENSE](LICENSE).
