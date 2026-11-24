# Local data retention and privacy

OSINT Beast uses a local SQLite database. It does not encrypt or automatically
expire database contents. Tool-run services redact configured sensitive
inputs and matching output or errors before storage; the CLI enables this for
all checks, while API and browser runs enable it for Holehe email checks.
Direct database writes may still store values as supplied. Treat the database
and any copies as containing sensitive information.

## What is stored

Before a validated check starts, OSINT Beast records its tool name and input in
`ToolRuns`. When the check finishes, the row also contains its status, UTC
timestamps, and either the result or an error message. A process interruption
or database-write failure can leave a row with status `running`; browser
run-history requests mark runs older than ten minutes as failed when their
outcome could not be persisted. Successful
results are stored a second time in `ToolResults`, including the tool name,
input, output, and creation timestamp. Failed and timed-out checks store their
error in `ToolRuns`, not `ToolResults`. Other structured records saved through
the database are retained there as well.

Values remaining after any configured redaction are stored as plain text.
Output is displayed in the terminal and database views. The saved-results
database view displays up to 20 rows and truncates displayed result text at
16,384 characters; this does not change the stored output. Direct database
writes can bypass application redaction. Run history can be exported to JSON;
exports redact input, result, and error fields by default and require explicit
consent to include their stored values. On POSIX, export files are created with
owner-only permissions. Sensitive exports are disabled on Windows because
file permissions inherit from the destination directory. Exports do not
overwrite existing files. OSINT Beast does not provide a built-in backup or
cleanup feature.

## Database location and retention

In a source checkout, the default database is `osintbeast.db` in the
repository root. For an installed package, it is in the per-user data
directory: `%APPDATA%\osintbeast` on Windows,
`~/Library/Application Support/osintbeast` on macOS, or
`$XDG_DATA_HOME/osintbeast` on Linux (falling back to
`~/.local/share/osintbeast`). Set `OSINTBEAST_DB_PATH` to choose another
location. Custom relative paths are resolved from the working directory when
the application starts. See [Database initialization](../README.md#database-initialization)
for details.

There is no automatic expiration or pruning: records remain until you remove
them or delete the database. To remove run history and saved tool results,
close OSINT Beast and use a SQLite client that commits changes to the database:

```sql
BEGIN;
DELETE FROM ToolRuns;
DELETE FROM ToolResults;
COMMIT;
VACUUM;
```

These statements logically clear only run history and tool results, not other
structured records. `COMMIT;` makes the deletions permanent; it is not secure
erasure. With SQLite's `secure_delete` setting off, sensitive values may remain
recoverable in unused database pages. The following `VACUUM;` rebuilds the live
database after the commit to remove deleted content, but it does not securely
erase storage, sidecar files, snapshots, or backups. Continue protecting the
database and its copies after cleanup.

To remove all local data in this database, close the application and
delete the database file and any associated `-wal`, `-shm`, or `-journal`
sidecar files. Do not remove SQLite files while the application is running.
Deleting a file may not securely erase its contents from the storage device,
filesystem snapshots, or other backups.

## Restrict database and backup access

On POSIX, OSINT Beast restricts SQLite database and sidecar file permissions
to owner-only when opening the database. Keep the database in a private
directory on an encrypted device, and protect backup copies separately.

- **Linux and macOS:** Restrict the containing directory (substitute the
  actual path):

  ```sh
  chmod 700 /path/to/private-osintbeast-directory
  ```

  Also restrict backup copies. Check permissions again after moving or copying
  files. **Windows:** In File Explorer, open the database's and backup
  directory's **Properties → Security** settings. Remove broad access where
  appropriate and grant access only to the intended Windows account(s); review
  inherited permissions on the containing directory as well.

OSINT Beast does not create backups. If you make one, treat it as equally
sensitive, store it in a similarly restricted and encrypted location, and
protect any cloud-synced or removable copies. For a consistent backup, use
SQLite's backup facility, or stop the application before copying the database
and any sidecar files that exist. Protect all resulting copies.

## Viewing, exporting, and sharing output safely

- View results only in a private terminal on a trusted device. Be aware that
  terminal recording, screen sharing, shell/session logging, or screenshots
  may capture displayed input and results.
- Avoid running checks with sensitive inputs on shared accounts, shared
  machines, or environments that retain terminal output.
- JSON exports redact sensitive fields unless you explicitly opt in. Opted-in
  exports may contain sensitive output or error text even when the input was
  redacted before storage; store them with the same access controls as the
  database and securely transfer or dispose of them under your organization's
  policy.
- If you extract data with another SQLite client, export only the fields needed
  and apply the same access controls as the database.
- Before sharing output in an issue, report, screenshot, or message, review it
  and remove personal data, credentials, identifiers, and other information
  that recipients do not need. Do not assume output or errors have been
  redacted.
- Checks contact third-party services as described in the
  [running checks documentation](../README.md#running-checks). Do not submit
  inputs unless you are authorized to do so and understand that external
  requests may disclose them.
