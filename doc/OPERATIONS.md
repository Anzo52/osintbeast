# Deployment and database operations

OSINT Beast currently supports local, interactive use as a Python CLI. It is
not a network service, and the MySQL schema files are not a supported runtime
backend. Do not expose it as a service or assume it is safe for concurrent
multi-user access.

## Deployment modes

Use Python 3.10 or later. Run OSINT Beast either from a source checkout or as
an installed Python distribution. Both modes use a local SQLite database and
the same CLI; choose one environment and use it consistently for updates and
database operations.

### Source checkout

Follow the [getting-started instructions](../README.md#getting-started), then
launch the CLI from the checkout:

```bash
python src/main.py
```

By default, the database is `<checkout>/osintbeast.db`, even if the command is
run from another working directory. The downloaded tool repositories are
placed in `tools/` under the current working directory unless a different
download folder is selected in the menu.

### Installed distribution

From a trusted checkout or a built wheel, install the package in a dedicated
virtual environment. For example, from a checkout:

```bash
python -m pip install .
python -m main
```

The package includes the SQLite schema files. Unless overridden, its database
is stored in the user's data directory:

- Windows: `%APPDATA%\osintbeast\osintbeast.db` (or
  `~/AppData/Roaming/osintbeast/osintbeast.db` if `APPDATA` is unset)
- macOS: `~/Library/Application Support/osintbeast/osintbeast.db`
- Linux: `$XDG_DATA_HOME/osintbeast/osintbeast.db`, falling back to
  `~/.local/share/osintbeast/osintbeast.db`

Set `OSINTBEAST_DB_PATH` before launching the CLI to use a specific database
path in either mode. A relative override is resolved from the working
directory at startup. The containing directory is created when the database
is opened. On Linux, `XDG_DATA_HOME` and on Windows, `APPDATA` select the
installed package's default data root; these variables do not change the
source-checkout default.

Optional integrations require their corresponding extras in the same Python
environment (for example, `python -m pip install ".[maigret]"` from a
checkout). Reinstall the extras you use when rebuilding an environment.

## Back up and restore

The SQLite database is the authoritative store for records, tool results, run
history, and tool metadata. It may contain sensitive inputs, outputs, and
errors in plain text. Keep backups encrypted or otherwise access-controlled,
and protect any downloaded tool repositories you need to retain. The default
download location is `tools/` under the working directory; custom download
folders must be backed up separately. A virtual environment can generally be
recreated from the package and its optional extras.

To make a consistent backup, stop OSINT Beast and run the following command
from the same Python environment used to launch it. If you set
`OSINTBEAST_DB_PATH`, set it to the same value for this command. The backup
file is written to the current directory; move it to protected storage:

```bash
python -c "import sqlite3; from data.utils.database import DB_FILE; source=sqlite3.connect(DB_FILE); destination=sqlite3.connect('osintbeast-backup.db'); source.backup(destination); destination.close(); source.close()"
```

Check the backup before relying on it:

```bash
python -c "import sqlite3; print(sqlite3.connect('osintbeast-backup.db').execute('PRAGMA integrity_check').fetchone()[0])"
```

The expected result is `ok`. Keep more than one backup when the data matters,
and periodically confirm that a backup can be opened.

To restore, stop OSINT Beast and use the same Python environment and database
path configuration as the backup. First preserve the current database, then
restore the backup in place:

```bash
python -c "import shutil; from pathlib import Path; from data.utils.database import DB_FILE; path=Path(DB_FILE); shutil.copy2(path, str(path) + '.before-restore') if path.exists() else None"
python -c "import sqlite3; from pathlib import Path; from data.utils.database import DB_FILE; Path(DB_FILE).parent.mkdir(parents=True, exist_ok=True); source=sqlite3.connect('osintbeast-backup.db'); destination=sqlite3.connect(DB_FILE); source.backup(destination); destination.close(); source.close()"
```

Run the integrity check against the restored database before resuming use. If
the database directory or backup is not in the current directory, use its
absolute path in the commands. Restore any separately backed-up tool folders
to the location you intend to use.

## Upgrade and schema changes

Before upgrading, make and verify a database backup. For a source checkout,
update the checkout and reinstall into its virtual environment:

```bash
git pull --ff-only
python -m pip install -e .
```

For an installed distribution, install the new trusted wheel or distribution
in the same environment. Preserve the existing `OSINTBEAST_DB_PATH` setting
and optional integrations. Start OSINT Beast and select **Initialize
Database** before using the upgraded version with existing data.

Initialization is repeatable and transactional. It creates missing tables
and indexes and, once per database, redacts legacy tool queries and their
direct and URL-encoded occurrences in results and errors. It preserves other
record fields and does not sanitize unrelated sensitive values or new records.
Existing backups and SQLite journals may still contain the original data.
It is not a general-purpose
schema migration: it does not alter or repair existing tables whose columns
or definitions are incompatible with the current code. In particular, do not
assume that every database from every prior or future version is compatible.
The application has no general-purpose versioned migration framework or
automatic downgrade support.

Keep the verified backup until you have checked that the upgraded CLI can
read the data you need. If initialization or normal operations report a schema
error, stop using that database and preserve it unchanged. Restore the
pre-upgrade backup and return to the prior application version while you
investigate; do not experiment on the only copy. If the schema change is not
covered by initialization, wait for a documented migration path rather than
assuming that rerunning initialization will fix it.
