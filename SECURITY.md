# Security

## What MCP for Primavera P6 can change

MCP for Primavera P6 reads a Primavera P6 Professional standalone (SQLite) database, or XER files, on your PC. When
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
| `p6_export_dashboard` | writes an Excel (.xlsx) dashboard file (refuses to overwrite an existing file unless asked) |

Planned, early and late dates and float are never written: P6 recalculates them (press F9).

## Safeguards

- Nothing is read until you select a database and confirm it after a read-only connection test.
- Writes need all of: the Full tool profile, "Allow write operations" (off by default), a P6 schema
  version MCP for Primavera P6 was tested with (or one you explicitly trust), a dry run, and a second call carrying
  the single-use confirmation token of that dry run (15 minutes; refused if the database changed in
  between). Deleting a project or an activity and restoring a backup also need a confirmation text.
- A backup is taken before every write (unless you turn it off after a warning), writes are refused
  while P6 is running, and every write is recorded in the edit log with its parameters.
- Read tools use read-only SQLite connections; `p6_sql_select` authorizes reading only and stops long
  queries. XER-only mode never writes.
- The HTTP transport listens on 127.0.0.1 only and requires a bearer token (kept in the Windows
  Credential Manager) on every request; Host and Origin headers are checked against DNS rebinding.
- Remote access (claude.ai and ChatGPT in the browser, 3.2, off by default) goes through the user's own
  Cloudflare account: a relay Worker adds a secret the local remote access server requires on every request (a
  local process or a web page reaching `127.0.0.1` directly is refused), and a tunnel this PC opens (no inbound
  port). Clients sign in with OAuth 2.1 (PKCE S256, client metadata documents fetched only from claude.ai,
  claude.com, chatgpt.com and openai.com, redirect addresses on an allowlist, tokens bound to the connector
  address, stored as SHA-256 hashes, refresh tokens rotated and a reused one ending the connection). Each
  connection is approved on the PC: the user types the code the browser shows, or clicks Connect with this PC on
  the connection page (3.3), which posts the code to a listener of the notification area app on `127.0.0.1` only
  (setting `remote_pair_port`, default 39219, running while remote access is on) that accepts it only from a page
  of the user's own connector address (Origin and Host checked) and from the browser that started the request (a
  key kept in a cookie); the user still clicks Approve. Remote clients never get delete,
  restore, file import or export, or shop tools; they edit only with the remote edit switch and a connection
  approved with edits, and the confirmation token of a dry run works only for the connection that asked for it.
  Password columns read as empty in their `p6_sql_select`, local paths are reduced to file names, request rates
  are limited, and every request, sign-in and tool call goes to an audit log (no values, tokens or query
  strings). The Cloudflare API token is used once during setup and never stored; the tunnel token and relay
  secret are in the Windows Credential Manager; `cloudflared` is shipped unchanged and pinned by its published
  SHA256.
- Registering with an MCP client edits only the `p6mcp` entry of that client's configuration file,
  after a timestamped backup, and refuses a file it cannot parse.
- Without remote access, the only network requests go to GitHub, and both can be turned off: the updates (releases of this
  repository; an update is installed only after its SHA256 matches the release's `SHA256SUMS.txt`, the
  installer is not code signed yet) and the skill shop (anonymous reads of the public catalog
  nizarhamza/p6-mcp-shop under raw.githubusercontent.com, when the Shop page or a `p6_shop_*` tool asks, at
  the first start of the Settings app and on Restore core skills for the core set, and with automatic updates
  on for newer versions of the shop skills not edited locally; each skill file is checked against the
  catalog index's SHA256; with the shop off, the core set comes from the copy shipped with MCP for Primavera P6). MCP for Primavera P6
  never sends anything to the
  catalog: sharing a skill is a GitHub issue the user creates in the browser.
- P6 EPPM mode connects only to the EPPM server the user configures (REST over HTTPS, or the SQL route), and
  through a proxy when the Windows or environment settings or the profile name one (3.4); a server on this PC is
  never proxied. The EPPM, SQL and proxy passwords are kept in the Windows Credential Manager, never in
  config.json (a proxy address holding a user name or password is refused), and are stored only after a
  connection test that worked.
- Skills are text only: SKILL.md and up to 5 text reference files (.md, .txt, .csv, .json); scripts are
  refused on import. A skill names tools; it gets no permission of its own: write tools keep the dry run and
  confirmation token, and `p6_shop_install` shows the whole skill and needs a confirmation too.

Remember that the data tools return goes to the AI model your MCP client uses. For confidential
schedules, use a local model.

## Supported versions

Security fixes go to the latest release.

## Reporting a vulnerability

Please report it privately through GitHub: **Security**, **Report a vulnerability** on
https://github.com/nizarhamza/p6-mcp. Do not open a public issue, and never attach a real schedule,
database or XER file: describe the problem, or reproduce it with the synthetic test database
(`tests/fixtures/make_db.py`).
