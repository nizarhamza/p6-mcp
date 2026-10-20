# Security

## What P6 MCP can change

P6 MCP reads a Primavera P6 Professional standalone (SQLite) database, or XER files, on your PC. When
you allow it, its write tools change that database **directly, without P6's own application logic**:

| Tool | Changes |
|---|---|
| `p6_update_activity` | name, original and remaining durations, constraints, actual dates, physical % of one activity |
| `p6_update_assignment(s)` | budgeted, actual and remaining units of resource assignments, with the costs, units/time and activity unit totals P6 derives from them |
| `p6_add_relationship(s)`, `p6_update_relationship(s)`, `p6_delete_relationship(s)` | relationships (type, lag); deletes are hard deletes, as in P6 |
| `p6_delete_activity` | one activity and the rows that belong to it (hard delete) |
| `p6_delete_project` | whole projects (and their baselines), as a P6 delete does |
| `p6_create_eps` | a new EPS node |
| `p6_import_xer` | a new project, a baseline, or an update of an existing project from an XER file |
| `p6_set_schedule_options` | a project's schedule options |
| `p6_restore_backup` | replaces the database with one of its backups (a safety copy is taken first) |
| `p6_backup_database` | writes a copy of the database to the backup folder |
| `p6_export_xer` | writes an XER file (refuses to overwrite an existing file unless asked) |

Planned, early and late dates and float are never written: P6 recalculates them (press F9).

## Safeguards

- Nothing is read until you select a database and confirm it after a read-only connection test.
- Writes need all of: the Full tool profile, "Allow write operations" (off by default), a P6 schema
  version P6 MCP was tested with (or one you explicitly trust), a dry run, and a second call carrying
  the single-use confirmation token of that dry run (15 minutes; refused if the database changed in
  between). Deleting a project or an activity and restoring a backup also need a confirmation text.
- A backup is taken before every write (unless you turn it off after a warning), writes are refused
  while P6 is running, and every write is recorded in the edit log with its parameters.
- Read tools use read-only SQLite connections; `p6_sql_select` authorizes reading only and stops long
  queries. XER-only mode never writes.
- The HTTP transport listens on 127.0.0.1 only and requires a bearer token (kept in the Windows
  Credential Manager) on every request; Host and Origin headers are checked against DNS rebinding.
- Registering with an MCP client edits only the `p6mcp` entry of that client's configuration file,
  after a timestamped backup, and refuses a file it cannot parse.
- The only network requests are the updates (GitHub releases of this repository), which you can turn off.
  An update is installed only after its SHA256 matches the release's `SHA256SUMS.txt`; the installer is
  not code signed yet.

Remember that the data tools return goes to the AI model your MCP client uses. For confidential
schedules, use a local model.

## Supported versions

Security fixes go to the latest release.

## Reporting a vulnerability

Please report it privately through GitHub: **Security**, **Report a vulnerability** on
https://github.com/nizarhamza/p6-mcp. Do not open a public issue, and never attach a real schedule,
database or XER file: describe the problem, or reproduce it with the synthetic test database
(`tests/fixtures/make_db.py`).
