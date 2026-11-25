# Running checks

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
**Run Sherlock Check** in the menu. Email and username entry is visible in the
terminal.
Sherlock uses its bundled site list and
skips sites marked NSFW. It sends a username to many public sites; each request
has a 15-second timeout, with a 300-second overall limit. At most two Sherlock
scans run concurrently; additional scans are rejected while both workers are
busy. Use it only for authorized targets. Helix uses its built-in site list
through the in-process API; the broader optional source collections are not
enabled by this menu integration. Helix accepts up to
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

## Run records and results

The CLI validates input before recording a run. Interactive menu prompts echo
the target in the terminal. The direct `check-email` and `check-username`
commands take targets as command-line arguments, which may be visible in shell
history and process listings. Holehe, Maigret, Helix, and Sherlock receive
sensitive input through their in-process Python APIs, not separate process
arguments. Their run records use a
`[redacted]` query; direct and URL-encoded occurrences of the input are also
redacted from saved and displayed results and errors. Ctrl+C during a check marks
the run as failed before exiting. A `running` record can remain after abrupt
termination or a database-write failure. Successful outputs are
also saved to the existing `ToolResults` table. Existing `ToolResults` rows
are preserved but do not gain historical run statuses.

Callers of the shared run helpers must explicitly declare whether an input is
sensitive using a boolean. Set it to `False` only when unredacted storage is
appropriate. Unexpected runner defects are recorded as failures and propagated
instead of being returned as ordinary tool errors.

Use **Query Database → Run History** to browse `ToolRuns`. History pages are
ordered newest first by stable run ID and contain the tool, input summary,
status, and timestamps. Filter by tool or status, move between 20-run pages,
and inspect a run's stored result or error; empty results are shown as empty
rather than parsed as structured data.

The history menu can export selected run IDs as a UTF-8 JSON array. JSON string
escaping preserves quotes, backslashes, newlines, and malformed/non-JSON tool
output as text. By default, the `input`, `result`, and `error` fields are
replaced with `[REDACTED]`; explicitly answer yes to include the values stored
in the database. Sensitive input is redacted before storage, so opted-in
exports may still contain other sensitive output or error text; store and share
them accordingly. On POSIX, exports are created with owner-only permissions.
Sensitive exports are disabled on Windows because file permissions inherit
from the destination directory; redacted exports remain available. Exports will
not overwrite an existing file.

Inputs, results, and errors can contain sensitive data. They are stored in
plain text in the local database; restrict its permissions and delete records
when no longer needed. See the [local data retention and privacy guide](../data-retention-and-privacy.md)
for exactly what is retained, how to restrict database and backup access, and
safe practices for viewing, removing, and sharing sensitive output.
Saved run and result views redact queries and matching result or error text
at read time, without changing stored values. Other sensitive information may
remain in those texts.

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
