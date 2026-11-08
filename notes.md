# Development notes

This file records engineering priorities and design constraints. For setup and user-facing instructions, see [README.md](README.md).

## Roadmap

The roadmap is a living plan. Items are not commitments to specific dates; prioritize correctness, safety, and maintainability over adding integrations quickly.

### Phase 1 — Reliable baseline

- [x] Keep requirements files, Python modules, SQL schemas, and documentation correctly separated; restore installable dependency entries and the tool registry module.
- [ ] Establish supported Python versions and keep dependency installation reproducible with a lockfile or bounded dependency policy.
- [ ] Make SQLite setup idempotent and test it using temporary databases.
- [ ] Add CI for the test suite and basic formatting/linting checks.
- [ ] Add tests for CLI flows and error paths; mock external tools and HTTP.

### Phase 2 — Shared application services

- [ ] Move business logic out of CLI functions into reusable services.
- [ ] Define clear interfaces for tool execution and persistence.
- [ ] Normalize tool results and record execution status, timestamps, and errors.
- [ ] Ensure invalid input is rejected before any external work begins.
- [ ] Handle timeouts, cancellation, and subprocess failures without crashing the menu.

### Phase 3 — Security and privacy

- [ ] Do not put sensitive input in process arguments where a supported in-process integration is available.
- [ ] If subprocesses remain necessary, document platform limitations and provide private execution boundaries where possible.
- [ ] Restrict table and column selection to known schemas; bind values rather than concatenating SQL.
- [ ] Keep downloads restricted to curated registry sources, validate archive paths, and avoid arbitrary install scripts.
- [ ] Minimize stored personal data, secure database files and backups, and define a retention/deletion approach.
- [ ] Add a clear authorization warning and safe defaults for potentially sensitive operations.

### Phase 4 — Web interface

- [ ] Design an API that calls the same services as the CLI.
- [ ] Require authentication and authorization before network access is enabled.
- [ ] Use a background job mechanism for long-running scans; expose status and bounded result retrieval.
- [ ] Apply input validation, rate/resource limits, CSRF protections for browser forms, and output escaping.
- [ ] Never expose arbitrary SQL execution through the API or UI.
- [ ] Test routes and job behavior with mocked integrations; avoid live scans in CI.

### Phase 5 — Database and integration growth

- [ ] Define a storage abstraction so SQLite and MySQL implementations can share behavior.
- [ ] Complete MySQL schemas, migrations, connection management, and integration tests.
- [ ] Add tools incrementally, each with a reviewed registry entry, validation, normalized output, and focused tests.
- [ ] Document deployment, configuration, backups, upgrades, and data migrations.

## Design notes

- Keep the CLI thin: it should collect input, call application services, and present results.
- Keep external side effects behind small, testable functions that can be replaced with mocks.
- Prefer explicit allowlists and parameterized queries. SQL parameters protect values; they do not parameterize identifiers, so identifiers must come from trusted schema metadata or fixed allowlists.
- Treat tool output and stored target data as sensitive. Do not log full inputs or results unnecessarily.
- Do not claim a scan result is valid merely because a tool returned output; preserve exit status and report failures distinctly.
- Future web deployment should not be enabled until authentication, isolation, job limits, and data-protection decisions are implemented.

## Current follow-up

- [x] Verify the dependency manifest contains installable dependency specifications and keep the supported-tools registry in `src/tool_registry.py`.
- [ ] Run the full test suite from a clean environment and document the supported command.
- [ ] Review the current CLI/database behavior against the roadmap before starting the web UI.
