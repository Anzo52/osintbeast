# Development notes

This file is the canonical home for development priorities, design constraints, database schema notes, and integration ideas. For setup and user-facing instructions, see [README.md](README.md).

## Project direction

The goal is to automate authorized OSINT workflows while keeping integrations, data handling, and persistence testable and safe. Prioritize correctness, privacy, and maintainability over adding integrations quickly.

## Roadmap

Items are not commitments to specific dates; priorities may change as the project develops.

### Phase 1 — Reliable baseline

- [x] Keep requirements files, Python modules, SQL schemas, and documentation correctly separated; restore installable dependency entries and the tool registry module.
- [x] Declare Python 3.10+ support, setuptools build metadata, and bounded direct dependency ranges in `pyproject.toml`.
- [ ] Make SQLite setup idempotent and test it using temporary databases.
- [ ] Add CI for the test suite and basic formatting/linting checks.
- [ ] Add tests for CLI flows and error paths; mock external tools and HTTP.

### Phase 2 — Shared application services

- [ ] Move business logic out of CLI functions into reusable services.
- [ ] Define clear interfaces for tool execution and persistence.
- [ ] Normalize tool results and record execution status, timestamps, and errors.
- [ ] Ensure invalid input is rejected before any external work begins.
- [ ] Handle timeouts, cancellation, and execution failures without crashing the menu.

### Phase 3 — Security and privacy

- [ ] Do not put sensitive input in process arguments where a supported in-process integration is available.
- [ ] If subprocesses remain necessary, document platform limitations and provide private execution boundaries where possible.
- [ ] Restrict table and column selection to known schemas; bind values rather than concatenating SQL.
- [ ] Keep downloads restricted to curated registry sources, validate archive paths, and avoid arbitrary install scripts.
- [ ] Minimize stored personal data, secure database files and backups, and define a retention/deletion approach.
- [ ] Add a clear authorization warning and safe defaults for potentially sensitive operations.

### Phase 4 — Web interface

- [ ] Design an API that calls the same services as the CLI.
- [ ] Require authentication and authorization before exposing a network-accessible interface.
- [ ] Use background jobs for long-running scans; expose status and bounded result retrieval.
- [ ] Apply input validation, rate/resource limits, CSRF protections for browser forms, and output escaping.
- [ ] Never expose arbitrary SQL execution through the API or UI.
- [ ] Test routes and job behavior with mocked integrations; avoid live scans in CI.

### Phase 5 — Database and integration growth

- [ ] Define a storage abstraction so SQLite and MySQL implementations can share behavior.
- [ ] Complete MySQL schemas, migrations, connection management, and integration tests.
- [ ] Add tools incrementally, each with a reviewed registry entry, validation, normalized output, and focused tests.
- [ ] Document deployment, configuration, backups, upgrades, and data migrations.

## Database schema notes

The SQLite information/target schema currently contains these entities:

| Table | Fields | Key/index notes |
| --- | --- | --- |
| `Platforms` | `platformID`, `platform` | `platformID`; platform value is unique |
| `Names` | `nameID`, `firstName`, `middleName`, `lastName`, `other` | `nameID` |
| `Locations` | `locationID`, `country`, `usState`, `county`, `city`, `street`, `addressNum`, `apt`, `zip` | `locationID` |
| `Domains` | `domainID`, `domain` | `domainID`; domain value is unique |
| `Services` | `serviceID`, `serviceName`, `domainID`, `platformID` | `serviceID`; references `Domains` and `Platforms` |
| `Urls` | `urlID`, `urlAddress`, `domainID` | `urlID`; references `Domains` |
| `Usernames` | `usernameID`, `username`, `nameID`, `serviceID` | `usernameID`; username unique; references `Names` and `Services` |

SQLite DDL is maintained in `data/sqlite-infoschema.sql` and `data/sqlite-toolschema.sql`. MySQL schema drafts are in `data/mysql-infoschema.sql` and `data/mysql-toolschema.sql`; they still need validation against a real MySQL server and a consistent migration strategy.

Tool discovery should eventually support finding tools by the information sought, the information available, or a combination of both to build a more complete target profile. Keep catalog metadata and execution results distinct, and add indexes based on measured query patterns rather than speculation.

## Integration backlog

### Current registry

The registry in `src/tool_registry.py` currently lists Holehe, Sherlock, Maigret, Amass, and Sublist3r. Registry presence does not necessarily mean a complete first-party integration exists; each integration needs its own validation, normalized output, and tests.

### Ideas and follow-up

- [ ] Assess additional libraries, command-line tools, and graphical tools individually before adding them to the registry.
- [ ] Review the [Dorker project](https://github.com/Anzo52/dorker) and its safety and maintenance status before considering integration.
- [ ] Track which integrations are fully implemented versus registry-only, and reflect that distinction in user-facing documentation.

## Design notes

- Keep the CLI thin: it should collect input, call application services, and present results.
- Keep external side effects behind small, testable functions that can be replaced with mocks.
- Prefer explicit allowlists and parameterized queries. SQL parameters protect values; they do not parameterize identifiers, so identifiers must come from trusted schema metadata or fixed allowlists.
- Treat tool output and stored target data as sensitive. Do not log full inputs or results unnecessarily.
- Do not claim a scan result is valid merely because a tool returned output; preserve exit status and report failures distinctly.
- Future web deployment should not be enabled until authentication, isolation, job limits, and data-protection decisions are implemented.

## Current follow-up

- [x] Declare project metadata, Python >=3.10, and bounded direct dependency ranges; `requirements.txt` installs runtime dependencies and `requirements-dev.txt` adds test tools.
- [ ] Run the full test suite from a clean environment and document the supported command.
- [ ] Review the current CLI/database behavior against the roadmap before starting the web UI.
