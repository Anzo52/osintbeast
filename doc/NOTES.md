# Development notes

The development roadmap and current design constraints are maintained in the
[roadmap](wiki/Roadmap.md). This file remains the location for
additional design notes that do not belong in the user-facing overview.

## Backend API persistence boundary

Backend and future web-facing services must use the narrow application
operations in `backend_services` and parameterized persistence methods such as
the bounded tool-run and saved-result listings. Request data may provide
validated values (for example, a page size), but must never choose SQL text,
table or column names, or query fragments. Do not expose
`data.utils.database.query_database`, `insert_data`, or a database connection
to web routes; those are lower-level local/maintenance interfaces, not API
operations.
