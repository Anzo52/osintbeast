# Development roadmap

This roadmap is ordered by priority and prerequisite: earlier sections establish
the safety and shared-service foundations needed by later work. It describes
planned work, not a promise or release schedule, and priorities may change as
the project develops.

## UI direction

Keep the CLI as the current local and automation interface, and prioritize a
browser-based UI as the next graphical interface. Do not build separate native
desktop or mobile applications. The existing browser interface is local-only;
further web UI work should use shared application services rather than
duplicating CLI behavior.

## 1. Stabilize the foundation

- [x] Keep dependency manifests, source modules, schemas, and documentation in their correct files.
- [x] Declare Python 3.10+ support and bounded dependency ranges in `pyproject.toml`.
- [x] Make database initialization reliable and repeatable across supported environments.
- [x] Expand automated coverage for database operations, CLI behavior, validation, timeouts, and failures.
- [x] Add continuous integration to run tests and basic static checks on changes.

## 2. Harden tool execution and data handling

- [ ] Keep downloads and installation constrained to reviewed registry entries and safe paths.
- [ ] Prefer supported in-process integrations or private input mechanisms for sensitive values.
- [ ] Ensure long-running operations time out gracefully and return control to the CLI.
- [ ] Standardize run status, timestamps, errors, and result persistence.
- [ ] Parse tool output into normalized findings while preserving raw output and reporting malformed or unsupported formats explicitly.
- [ ] Document retention, local database permissions, and sensitive-output handling.
- [ ] Keep arbitrary SQL unavailable to any future web-facing interface.

## 3. Improve the CLI and service boundaries

- [ ] Separate reusable application services from menu/input/output code.
- [ ] Provide clear help, input validation, and actionable error messages.
- [x] Add structured result-history views and safe export options.

## 4. Build a web UI

- [ ] Add authentication and authorization before exposing any network-accessible interface.
- [ ] Run long tasks outside request handlers and enforce resource limits.
- [ ] Define a small backend API around the shared application services.
- [ ] Add a simple UI for launching authorized runs and viewing their status and results.
- [ ] Add web-route tests with mocked tool execution and security checks.

## 5. Expand persistence and integrations

- [x] Complete and test MySQL schema and database support behind a storage interface.
- [x] Add a first-party run integration for Sherlock; Sublist3r and Amass remain candidates, with documented inputs, outputs, and safety requirements.
- [x] Document deployment, backup, upgrade, and migration procedures.
