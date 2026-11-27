# P6 EPPM backend: implementation plan

Status (2026-11-27): built and released. 3.0.0 shipped milestones 1 to 6 (configuration and Test connection,
SQL and REST pull, local edits, push, server scheduling), 3.1.0 added `p6_eppm_discard` and
`p6_update_activities`. Not built from this plan: `schedule=True` on the push (`p6_eppm_schedule` is the only
way to schedule), proxy settings in a server profile (a custom CA bundle exists), a pull by REST object reads
alone (the REST route needs the XER export), UDF edits in EPPM mode, and anything that waits for a real server
(D1, OAuth on OCI, DACS prefix). The sections below keep the plan as agreed; where they differ from the code,
the code and the milestone notes in section 9 win.

Plan agreed on 2026-11-24 (decisions D2 to D7 in section 10; D1 waits for a real server). Milestone 0 ran on a private test
server (P6 EPPM 25.12, SQL Server 2022, WebLogic 14.1.2, synthetic project DEMO-SS225) on 2026-11-24: the
measured facts are in section 13 and the sections below are corrected from them.

## 1. Goal

Let MCP for Primavera P6 work on projects of a live **P6 EPPM** server (on premises or hosted by Oracle on OCI),
with the same tools as for a P6 Professional standalone database, and with edits that reach EPPM only after the
user approves them.

User decisions behind this plan (2026-11-24):

- Hosting is not known in advance: the backend must work on premises and on OCI.
- Access: a REST API account, and a read-only SQL account when one can be obtained.
- Version 1 reads and writes.
- Writes are made **locally first**; an approved push sends them to the EPPM server.
- Implemented directly in this repository (the product), not in the MCP_P6 dev server first.

## 2. Model: pull, edit locally, push

```
EPPM server                         this PC (P6 MCP)

PMDB (Oracle / SQL Server)  ==> SQL, read only ==>  pull ==> working copy (SQLite, P6 tables)
REST API /p6ws/restapi      ==> REST read      ==>            |  every existing read tool
                                                              |  existing write tools (dry run + token)
                                                              v
                                                    pending changes = edit log entries
REST API                    <== push (dry run, conflict check, apply)
REST job service            <== schedule (optional, after a push)
```

1. **Pull** copies one project (and optionally its baseline) into a local SQLite *working copy* with P6's table
   names, exactly like XER mode builds its store (`p6mcp/xerstore.py`). Every read tool then runs unchanged.
2. **Local edits** go through the existing write tools and their two-step flow. They change the working copy
   only. Each confirmed write already produces edit log entries (`operation`, `table`, `keys`, `before`, `after`):
   these are the pending changes.
3. **Push** turns the pending changes into REST calls, checks each one against the live server value
   (conflict), shows the plan, and applies it only after the user's explicit yes (same dry run and
   `confirmation_token` flow, plus a `confirmation_text`).
4. **Schedule** (optional) runs EPPM's own scheduler on the project through the REST job service, then a new
   pull brings back the dates. This replaces "press F9 in P6" in EPPM mode.

Why not write SQL into the PMDB: EPPM's business rules, security and application caches are bypassed and Oracle
does not support it. The SQL account is used for reads only.

## 3. Read routes

Two routes fill the same working copy; the pull picks SQL when a SQL connection is configured and tested, else
REST.

### 3.1 SQL route (preferred when available)

- The PMDB uses the same tables and columns as the standalone database (TASK, TASKPRED, PROJWBS, CALENDAR,
  TASKRSRC, RSRC, ROLES, ACTVTYPE, ACTVCODE, TASKACTV, UDFTYPE, UDFVALUE, UDFCODE, PROJPROP, PREFER...), so
  the copy is a row copy per table, filtered by project, with type conversion (dates to P6's text format,
  numbers, BLOB/CLOB text through `_blob_text`).
- On premises: a read-only database account created by the DBA. On OCI: DACS, read-only SQL meant for
  integrations, reached through an IPSec tunnel and Oracle Net with an integration account from Oracle; the
  tables are synonyms named `T6_<table>` (a `table_prefix` setting), and PREFER, SETTINGS, ADMIN_CONFIG,
  PUBUSER are not granted (PREFER then gets the XER mode defaults). OCI also offers the Primavera Data Service,
  JSON SELECT queries over HTTPS (`/pds/rest-service/dataservice/...`, Basic auth): a possible SQL route
  without a tunnel, to evaluate in the spike.
- Drivers: `oracledb` (thin mode, pure Python, no Oracle client install) for Oracle; `pymssql` for SQL Server
  (a wheel, no ODBC driver; measured on the test server, decision D3).
- The copy is exact for every column the tools read, so the CPM engine sees what EPPM stores. Measured: the
  EPPM 25.12 TASK table has every column of the standalone TASK plus 19 EPPM columns (risk dates, work package,
  review status), and `task_id` equals the REST Activity ObjectId.
- **Schedule options are not in the PMDB**: PROJPROP has no rows and there is no SCHEDOPTIONS table. Both
  routes read them from REST `GET /scheduleOptions` (32 fields) and write them to PROJPROP 'scheduling'.
- Security: SQL ignores EPPM's OBS. A project may be pulled only when the REST user can read it (the pull
  asks the REST API first), so the MCP never shows more than the user sees in EPPM.

### 3.2 REST route (always available)

Two ways, the first preferred when the server offers it:

- **XER export over REST** (documented in Release 25: `POST /export/exportXERProject` with
  `{FileType: "XER", ProjectObjectId: [..]}`, then `/export/downloadFiles`; older releases list only the XML
  export, so this is version dependent). The downloaded XER goes through the code XER mode already uses
  (`_xer_read_meta`, `xerstore` table build, SCHEDOPTIONS to PROJPROP), so calendars, schedule options, float
  paths and every column come exactly as P6 exports them, with no field mapping. Oracle also recommends Export
  for large volumes, because plain reads have no paging. Measured on 25.12: the answer is the XER itself in
  the POST response (no download step), labelled `text/xml` although it starts with `ERMHDR`, 178 KB in 0.3 s
  for 214 activities; `task_id` equals the ObjectId; it has **no SCHEDOPTIONS table** (schedule options come
  from REST, as for the SQL route) and no activity code or UDF table when the project has none.
- **Object reads**: the pull reads the REST objects (project, WBS, activity, relationship, calendar, resource, resource
  assignment, activity codes, UDF types and values) with explicit `Fields=` lists and writes them into the P6
  tables through a mapping (`p6mcp/eppm/mapping.py`: REST field to table column, units and enum text to P6
  codes, for example "Finish to Start" to `PR_FS`).
- Facts from the Release 25 REST reference: `Fields=` must list every field; `Filter=` uses `:eq:`, `:gt:`,
  `:like:`, `:and:`, `:or:` (for example `ProjectObjectId :eq: 4711`); there is no limit, offset or page
  parameter, so the pull reads per project (and per WBS for very large projects) with only the needed fields,
  and sorts on the client (Oracle warns that `OrderBy` may misbehave). Enums are readable text ("Finish to
  Start", "Start Milestone", "Fixed Units/Time", "As Late As Possible"), mapped to P6 codes.
- Settled by the spike: durations and lags are in **hours** (equal to the stored hour columns on every pair);
  activity dates are the wall clock of the PMDB without an offset (`2026-10-26T08:00:00`), while
  `LastUpdateDate`, `LastScheduleDate` and `CreateDate` carry the server's offset (`-08:00`); numbers arrive as
  JSON strings (`"80.0"`) and are converted; `Lag` comes as `"0.0"`.
- Calendars: the 25.12 `calendar` object does not list `StandardWorkWeek` or `HolidayOrExceptions`, so an
  object-read pull cannot rebuild working time. Calendars come from the XER export (CALENDAR with
  `clndr_data`) or SQL; a pull by object reads alone is not enough for the engine.
- Still open: `ActualUnits` = regular + overtime (P6 stores `act_reg_qty` and `act_ot_qty` apart), whether
  `/scheduleOptions` differs per user (one row came back for the project, with `UserObjectId`).

### 3.3 Working copy

- File: `%LOCALAPPDATA%\P6MCP\eppm\<profile>\<project id>.db`, plus `pull.json` (server, route, pull time,
  server `LastUpdateDate` per pulled object, the pulled value of every column a push may change).
- Ids are the EPPM ObjectIds (SQL `task_id` = REST Activity ObjectId), so a pending change names the server
  object directly. Rows created locally (a new relationship) get ids from a local range that cannot collide
  with server ObjectIds: the pull seeds NEXTKEY at `LOCAL_ID_BASE` for every table a local write may insert
  into, so `_next_key` (which never goes below MAX(id)+1) hands out local ids above it. The push maps each
  local id to the ObjectId the server returns.
- PREFER gets the minimal row XER mode writes (`xerstore._support_tables`), or the server's PREFER values by
  SQL.
- The schema gate: the working copy has its own fingerprint, recognised as writable by an `eppm` flag in the
  store (no `trusted_schemas` step for the user).
- PM.exe check, P6 triggers and REFRDEL do not apply to the working copy. Local backups (before each local
  write) and `p6_restore_backup` still work on the working copy.
- A new pull of a project with pending changes is refused unless `discard_pending=True` (the tool is marked
  destructive; there is no confirmation text, the client asks the user first).

## 4. Push

### 4.1 Pending changes

- Source: the confirmed edit log entries of the working copy made after its pull and not yet pushed, minus
  the ones a local restore undid (`_undone_fn`).
- Net change per server object and field: the first `before`, the last `after`; a field set back to its
  pulled value drops out; an object created then deleted locally drops out.
- Supported in version 1: activity fields written by `p6_update_activity(ies)`, relationships (add, update,
  delete), assignment units (`p6_update_assignment(s)`). Activity UDF values are not: the UDF tools are outside
  `server.EPPM_EDIT_TOOLS` until their REST calls are checked on a server.
- Refused in EPPM mode (the tools stay listed only for the standalone database): `p6_delete_activity` (D5),
  `p6_import_xer`,
  `p6_create_eps`, `p6_delete_project`, `p6_set_schedule_options` (until the REST schedule options object is
  confirmed), `p6_backup_database` of the server (local backups only).

### 4.2 Conflict check

For each pending change, read the live server object (REST, the same `Fields=`):

- live value equals the pulled value: safe to apply;
- live value already equals the local `after`: nothing to send (reported as already done);
- anything else: **conflict**, reported with the pulled, live and local values; the push is refused while any
  conflict remains (the user re-pulls, or drops the local change).

`LastUpdateDate` (on Activity, Relationship, ResourceAssignment, UDFValue, ScheduleOptions, and filterable)
is the fast first test: one read per object type with `LastUpdateDate:gt:'<pull stamp>'` finds the objects
changed on the server since the pull; the field comparison decides. Measured filter forms: `'2026-11-24'`,
`'2026-11-24T08:02:03'` and `'2026-11-24 08:02:03'` work; a value with an offset is refused (400), so the
pull keeps the largest `LastUpdateDate` it read with its offset removed (server wall clock, whole seconds).
**The server has no optimistic lock**: a PUT carrying an out-of-date `LastUpdateDate` is accepted without a
word (measured), so the conflict check is entirely client side. A change made on the server between the
check and the apply cannot be excluded; the apply re-reads each object just before its call to keep that
window short.

### 4.3 Apply

- Verbs (measured on 25.12): POST creates (JSON array of objects; 201 with the new ObjectIds as strings,
  `["2233"]`), PUT updates (array of objects with ObjectId and the changed fields only; answer `true`),
  DELETE takes `ObjectId=1,2,3` in the query string (answer `true`). A PUT of RemainingDuration on a not
  started activity also moves PlannedDuration (P6's rule) and moves no date until a schedule run. Relationships need
  `PredecessorActivityObjectId` and `SuccessorActivityObjectId`; a new activity would need `ProjectObjectId`
  and `WBSObjectId` (not in version 1).
- REST has no transaction across calls. So the dry run checks everything first (mapping, conflicts, user
  rights by a read of each object), and the apply sends the calls in a fixed order (relationship deletes,
  activity updates, relationship creates and updates, assignments, UDF values), grouped per object type and
  verb where the API takes arrays.
- The first failed call stops the push. A push journal (`push_<ts>.jsonl` beside the working copy) records
  each applied change, so the result says exactly what reached the server and a new push resumes with the rest.
- After an apply: the pushed entries are marked in the journal, the edit log gets a `push_eppm` entry, and the
  result tells the user that EPPM must be scheduled (or offers `schedule=True`).

### 4.4 Schedule on the server

- Optional `p6_eppm_schedule(project)` (and `schedule=True` on the push): `POST /job/schedule` with
  `{ProjectObjectId: [..], NewDataDate, Timeout}` (Timeout in seconds; 0 returns at once with `JobId` and
  `JobStatus`), then `GET /job/readJobStatus?JobId=` until the job ends, then a new pull so the tools read
  EPPM's own dates. `NewDataDate` is passed only when the user asks for a new data date. Measured: the POST
  answers a list, `[{"JobId": "1:21", "JobStatus": "Pending"}]`; `readJobStatus` answers
  `{"JobStatus": "Complete"}` (5 s for 214 activities); `POST /job/readJobLog` with `[{"JobId": ..}]` returns
  P6's scheduling log as text (settings, open ends, loops, out of sequence, critical count, finish), which the
  tool returns to the user. After a server schedule, P6's early and late dates, total and free float equalled
  the dates of this server's engine on every activity of the test project (0 differences on 214).
- The `stale` and `edited_since_last_schedule` logic keeps working on the working copy; after a server
  schedule and a pull, the project is no longer stale.

## 5. New and changed tools

| Tool | Kind | What it does |
|---|---|---|
| `p6_eppm_projects` | read, open world | Projects the REST user can read on the server (Project ID, name, EPS, data date, last schedule, pulled or not). |
| `p6_eppm_pull` | write (local), open world | Pulls one project (and its baseline when asked) into the working copy; refuses over pending changes unless `discard_pending=True`. |
| `p6_eppm_status` | read | Working copy of each pulled project: pull time, route, pending changes (net), last push, whether the server changed since the pull (one cheap REST read). |
| `p6_eppm_push` | destructive, open world | Dry run: the REST calls, conflicts, already done items; apply after the token and `confirmation_text` `PUSH TO <server>` (the server profile name). |
| `p6_eppm_schedule` | write, open world | Schedules the project on the server, then pulls. |

Existing tools: unchanged signatures. `p6_diagnose` reports the EPPM profile, routes and their last test,
working copies. `p6_list_projects` lists the pulled projects (the server list is `p6_eppm_projects`).
`policy.TOOL_KIND` gets the new tools; the EPPM tools are Full profile only and need `allow_writes` for pull,
push and schedule. `INSTRUCTIONS` (ASCII, under 1 KB) gets one line on pull, edit, push.

## 6. Configuration, secrets, Settings

- `config.DATA_SOURCES` = ("database", "xer", "eppm"); new keys `eppm_profiles` ({name, base_url, context,
  database, user, auth: auto, oauth or login, ca_bundle, timeout_s, sql: {driver, host, port, service, user,
  schema, table_prefix} or null, confirmed, tested_at, test_digest, projects, sql_ok}) and `eppm_active`.
  A profile is confirmed by the user after a successful test of its current connection settings
  (`eppm.digest`); any change of them clears the confirmation. Config schema 4 (`_migrate_3_to_4`). Built
  in milestone 1.
- Secrets in the Credential Manager only: `eppm:<profile>` (REST password), `eppm-sql:<profile>` (SQL
  password). OAuth tokens are kept in memory for their lifetime, never written.
- Settings, Database page: a third data source "P6 EPPM server" with an `EppmPanel` (URL, database, user,
  password, auth, optional SQL connection, Test connection on the thread pool: REST login + project count,
  SQL connect + schema fingerprint + row count of PROJECT), then the project list with Pull. All strings in
  `locales/*.json` (straight quotes, no long dashes).
- HTTP: the standard library (`urllib`, as `updates.py` and `shop.py`: httpx is not installed with every
  `mcp` version), TLS verification on, optional custom CA bundle path for company certificates, timeouts,
  retries only on idempotent reads.

## 7. Code layout

- `p6mcp/eppm/__init__.py`
- `p6mcp/eppm/rest.py`: session, `get(entity, fields, filter)`, `create`, `update`, `delete`, XER export
  and download, job start and poll; error mapping to plain ValueError messages. Authentication:
  - OCI: `POST /p6ws/oauth/token` (header `authToken` = base64 of user:password, `token_exp` = lifetime),
    then `Authorization: Bearer <token>` on every call; renewed before it expires.
  - On premises: `POST /p6ws/restapi/login?DatabaseName=<name>` (the database is chosen by name), then the
    session cookie, or credentials on every call (the reference contradicts itself on cookies: spike);
    `POST /logout` at the end. OAuth on premises only when the administrator enabled it.
- `p6mcp/eppm/sql.py`: driver selection, read-only connection, `project_rows(table, proj_id)`.
- `p6mcp/eppm/mapping.py`: REST field <-> P6 column tables, enum maps, unit conversions, calendar rebuild.
- `p6mcp/eppm/store.py`: working copy build (shares the table creation and SCHEDOPTIONS handling of
  `xerstore.py`; the common part moves to a small helper module), `pull.json`.
- `p6mcp/eppm/push.py`: pending changes from the edit log, net change, conflict check, ordered apply, journal.
- `p6mcp/server.py`: `_refresh` points `DB_PATH` at the active working copy (as for XER mode), the write gate
  accepts EPPM working copies, the five tools, refusals of section 4.1.
- `p6mcp/settings_app/eppm_panel.py`, `settings_logic.py` additions.

## 8. Tests

- Fake EPPM REST server in pytest (`http.server` on 127.0.0.1, `tests/test_eppm_connection.py`): its objects are generated from the synthetic
  database of `tests/fixtures/make_db.py`, so a REST pull must rebuild that database (every column the tools
  read), and pushes are checked against the fake's state.
- SQL route tested with a SQLite file standing in for the PMDB (same tables) through the same DB-API code;
  Oracle and SQL Server type conversions tested on recorded value samples.
- Oracle: `p6_cpm_validate` match rate 1.0 and identical results of the read tools between the working copy
  and the XER mode store built from an XER export of the same project (synthetic data in CI; real data only in
  the private check, never in this repository).
- Push: net change rules, conflicts (each case), stop at the first failure and resume from the journal,
  refusal of unsupported operations, confirmation text, token bound to the plan.
- Settings panel and config migration tests as for the XER panel.
- Private manual check on the real EPPM (read only first, then one push on a test project agreed with the
  EPPM administrator).

## 9. Milestones

0. **Spike**: done on the private test server (section 13), reads and writes. Left for the first real server:
   hosting and release (D1), OAuth on OCI, DACS table prefix, per-user schedule options, size and time of a
   full read of the largest project, write rights agreed with the EPPM administrator. The original checklist: hosting and release; which login works (`authToken`
   header or `username` / `password` headers, OAuth token) and whether the session cookie is needed; one
   project read by object reads, by `exportXERProject` (if the release has it) and by SQL; units of
   durations and lags and the time zone of dates (compared with the XER export of the same project, the
   oracle); the POST 201 body; DELETE with several ids; the `readJobStatus` answer and status values;
   whether `/scheduleOptions` is per user; size and time of a full read of the largest project; write rights
   on a test project agreed with the EPPM administrator. Output: a short findings note (private, no data in
   the repository) that fixes decisions D1 to D4.
1. Configuration, credentials, Settings panel, Test connection. Done (`p6mcp/eppm/rest.py`, `sql.py`,
   `settings_app/eppm_panel.py`); checked against the test server. The on-demand install of the SQL drivers
   moves to milestone 2, with the SQL pull that needs them.
   Milestone 2 done (`eppm/drivers.py`, `eppm/store.py`, the three tools in `server.py`): checked against the
   test server, DEMO-SS225 and its baseline pulled in about 1 s, `p6_cpm_validate` match rate 1.0 on the
   working copy. Two choices made while building it: one working copy per server profile
   (`%LOCALAPPDATA%\P6MCP\eppm\<profile>\working.db`, every pulled project in it, rebuilt as a whole by each
   pull) instead of one file per project, so the tools that compare projects keep working; and relationships to
   projects that were not pulled are left out with a warning (pull both projects to keep them).
   Milestone 3 done (REST pull, `store.pull_rest`, `route` auto / sql / rest on `p6_eppm_pull`): the projects
   come from one XER export (`/export/exportXERProject`, ids = ObjectIds) built by `xerstore.build`, then the
   REST schedule options. Measured on the test server: P6 EPPM 25.12 refuses to export a baseline ("Could not
   load Project with id"), but reads its objects, so each baseline is rebuilt from REST object reads
   (`p6mcp/eppm/mapping.py`: baselineProject, WBS, activity, relationship, resourceAssignment to PROJECT,
   PROJWBS, TASK, TASKPRED, TASKRSRC with P6 codes; its copied calendars take the working time of the calendar
   of the same name). On DEMO-SS225 the REST route gives the same results as the SQL route (match rate 1.0,
   baseline finish, 36 working days of variance, planned units 54.5059 %, SPI 0.8473) in under 1 s. A pull by
   object reads alone (a server without the XER export) is not built: the export exists on every release this
   product targets.
   Milestone 4 done (local edits, `p6mcp/eppm/pending.py`): in P6 EPPM mode the edits the push supports
   (`server.EPPM_EDIT_TOOLS`: activity, relationship and assignment edits, local backup and restore) write
   the working copy through the usual two-step flow, with no schema gate and no PM.exe check; the others are
   refused (`p6_delete_activity` per D5). The working copy is named `eppm_<profile>.db` so the edit log keeps
   servers apart; NEXTKEY gives new relationships ids from `LOCAL_ID_BASE` (2,000,000,000). Pending changes =
   the log entries since the pull (`pulled_ts`, to the millisecond), not undone, not pushed, netted per object
   and field (`pending.net`: create, update, delete; a field back to its pulled value, a link created then
   deleted, a UDF set then cleared where none was pulled all drop out; assignment rollups `TASK.*` left out;
   `refs` keep the ids a creation needs). `p6_eppm_status` lists them; `p6_eppm_pull` refuses over them unless
   `discard_pending=True` (so the tool is marked destructive). Checked on the test server: an edit and a new link
   on DEMO-SS225 are pending locally while the server keeps its values.
   Milestone 5 done (`p6mcp/eppm/push.py`, tool `p6_eppm_push`, destructive, confirmation text
   `PUSH TO <server>`): `push.plan` reads every changed object live and classifies each change (sent, already
   done, conflict; unsupported tables refuse), the token is bound to the call list through `_plan_gate`; `apply`
   sends one object per call in the order of 4.3 and stops at the first failure, a journal line per call in
   `push_<stamp>.jsonl`; the edit log entries sent or already done go into `pull.json` `pushed_entries` (so they
   are no longer pending and a new push resumes), new relationships take the server's ObjectId in the working
   copy, and a `push_eppm` edit log entry records the push. Sent fields: activity name, durations, primary
   constraint, actual dates, physical %, status; relationship type and lag; assignment budgeted, actual and
   remaining units (P6 recomputes costs and rollups). UDF edits are not allowed in P6 EPPM mode yet: the UDF
   REST calls were not checked on a server. Checked on the test server: a duration change and a new link pushed,
   read back over REST, then the reverse changes pushed, leaving DEMO-SS225 as it was. After a push the
   working copy keeps the local values while P6 may derive others (a not started activity's original duration
   follows its remaining one): pull again after scheduling on the server.
   Milestone 6 done (`p6mcp/eppm/jobs.py`, tool `p6_eppm_schedule`, a write tool with the two-step flow):
   `jobs.schedule` posts `/job/schedule` (`NewDataDate` only when asked), follows `readJobStatus` until a final
   status, reads the log (`readJobLog`, `log_facts`: activities, critical count, data date, finish, loops),
   then the tool pulls the projects again (same route) unless `pull_after=False`; it is refused while local
   changes are pending (the pull would drop them), never runs after a push by itself (D6), and records a
   `schedule_eppm` edit log entry. Checked on the test server: DEMO-SS225 scheduled in 12 s, pulled again, match
   rate 1.0. Found there: the pull wrote the schedule options with the pull time, which the staleness check read
   as an edit after the last F9; they are now stamped with the project's last schedule date.
   Milestone 7 prepared on the branch: README (P6 EPPM server section, tool counts, FAQ), site source and pages,
   CHANGELOG 3.0.0 (Unreleased), version 3.0.0 in `p6mcp/__init__.py` and `server.json`, the release checklist,
   and an instruction line sent to clients in P6 EPPM mode only (`instructions.EPPM_LINE`: push and schedule where
   a tool or skill says press F9 in P6; `data_source` joined the restart keys). The core skills themselves live
   in the skill shop catalog and are unchanged. Not done: the release (tag, build, publication), which follows
   the merge to main and the user's decision.
2. SQL pull, working copy, read tools on it, `p6_eppm_projects`, `p6_eppm_status`.
3. REST pull (calendar rebuild, schedule options).
4. Local writes on the working copy, pending changes in `p6_eppm_status`.
5. Push: dry run, conflicts, apply, journal, resume.
6. Server schedule and re-pull.
7. README tool tables (`tools/gen_docs.py`), site pages, CHANGELOG, skills that mention F9 adapted for EPPM,
   release.

## 10. Decisions

Open:

- D1. Which EPPM version and hosting the first customer uses (a real server answers it).

Decided by the user on 2026-11-24, after the spike:

- D2. The pull takes the project **and its current baseline** (the BaselineProject named by
  `CurrentBaselineProjectObjectId`; none when the field is empty), so baseline variance, BEI and milestones
  vs baseline work. The XER export of a baseline ObjectId is still to try (milestone 3).
- D3. SQL Server through **`pymssql`**, Oracle through `oracledb`, both **installed on demand**: not in every
  install, only when the user configures a SQL connection (Settings installs the wheel into the product's
  environment, with the user's yes, then Test connection).
- D4. A push **stops at the first failed call**; the journal records what reached the server and a new push
  resumes with the rest. No automatic undo.
- D5. **No activity deletes in version 1**: `p6_delete_activity` is refused in EPPM mode (deletes are made in
  EPPM itself). Relationship deletes are pushed.
- D6. **No automatic schedule after a push**: the push result says the project needs scheduling;
  `schedule=True` on the push or `p6_eppm_schedule` runs it when the user asks.
- D7. The EPPM backend ships as **3.0.0**.

## 11. Risks

- REST field coverage differs by EPPM version: fields missing on the customer's version make the REST route
  less exact than SQL; the spike lists them and the pull warns.
- Calendars are not readable as working time through the 25.12 REST objects: the REST route needs the XER
  export (or SQL), and a server without either cannot feed the engine.
- No transaction across REST calls: mitigated by the dry run checks, ordered apply and the journal.
- No paging in plain REST reads: a very large project can time out; mitigated by the XER export route,
  per-WBS reads and small field lists.
- Schedule options may be stored per user on EPPM (the REST key includes UserObjectId): the working copy must
  take the options the server scheduler will use, to be confirmed in the spike.
- OCI session lifetimes: token refresh in `rest.py`.
- Company proxies and TLS inspection: custom CA bundle and proxy settings.

## 12. Sources

- P6 EPPM REST API, Release 25: [endpoints](https://docs.oracle.com/cd/G18294_01/English/Integration_Documentation/rest_api/rest-endpoints.html),
  [OAuth](https://docs.oracle.com/cd/G18294_01/English/Integration_Documentation/rest_api/D99930.html),
  [login](https://docs.oracle.com/cd/G18294_01/English/Integration_Documentation/rest_api/op-login-post.html),
  [filters](https://docs.oracle.com/cd/G18294_01/English/Integration_Documentation/rest_api/D99716.html),
  [large reads](https://docs.oracle.com/cd/G18294_01/English/Integration_Documentation/rest_api/D102457.html),
  [activity](https://docs.oracle.com/cd/G18294_01/English/Integration_Documentation/rest_api/op-activity-get.html),
  [schedule job](https://docs.oracle.com/cd/G18294_01/English/Integration_Documentation/rest_api/op-job-schedule-post.html),
  [XER export](https://docs.oracle.com/cd/G18294_01/English/Integration_Documentation/rest_api/op-export-exportxerproject-post.html),
  [schedule options](https://docs.oracle.com/cd/G18294_01/English/Integration_Documentation/rest_api/op-scheduleoptions-get.html).
- [DACS, read-only SQL on OCI](https://docs.oracle.com/cd/F25602_01/English/dacs/141414.htm);
  [Primavera Data Service](https://docs.oracle.com/en/industries/construction-engineering/primavera-administration/25/data-service/D141674.html).
- Release 26 REST reference (writes, jobs, baselines):
  [PUT activity](https://docs.oracle.com/en/industries/construction-engineering/primavera-p6-project/26/rest-api/op-activity-put.html),
  [POST relationship](https://docs.oracle.com/en/industries/construction-engineering/primavera-p6-project/26/rest-api/op-relationship-post.html),
  [readJobStatus](https://docs.oracle.com/en/industries/construction-engineering/primavera-p6-project/26/rest-api/op-job-readjobstatus-get.html),
  [assignProjectAsBaseline](https://docs.oracle.com/en/industries/construction-engineering/primavera-p6-project/26/rest-api/op-project-assignprojectasbaseline-post.html).

## 13. Spike results (milestone 0, test server, 2026-11-24)

Server: P6 EPPM 25.12 (build 80561) on Windows Server 2022, SQL Server 2022 Developer (case-insensitive
collation), WebLogic 14.1.2 with JDK 17, in a private Hyper-V VM. Data: the synthetic DEMO-SS225 and
DEMO-SS225_BL XERs (214 activities, 332 relationships, 251 assignments), imported through P6 web. Tool: the
private spike script (`spike.py`, `--write` limited to the test project). Every write was restored and
checked.

| Topic | Result |
|---|---|
| Login | `POST /restapi/login?DatabaseName=PMDB` with the `authToken` header (base64 user:password): 200 and a `JSESSIONID` cookie, which carries the session; without it, or with Basic alone, 401. `POST /logout` at the end. OAuth not tried (on premises). |
| Database name | `PMDB`, the name in P6's login list. On this server it equals the SQL database name, so which of the two REST expects when they differ is not measured. |
| Object reads | 214 activities in 0.12 s (200 KB), 332 relationships 0.07 s, 251 assignments 0.05 s. Counts equal the XER. |
| Units and dates | Hours for durations and lags; activity dates without offset, equal to the stored wall clock; update and schedule stamps with the server offset; numbers as JSON strings. |
| Enums | Text: "Task Dependent", "Start Milestone", "Finish Milestone", "Not Started", "In Progress", "Completed", "Finish to Start", "Fixed Duration and Units", "Physical". |
| Calendars | REST `calendar` lists 18 fields, without work week or exceptions; working time only from the XER export or SQL. |
| XER export | Works, in the POST answer; no SCHEDOPTIONS table. |
| SQL | `pymssql` with a `db_datareader` login: TASK = standalone columns + 19; TASK and TASKPRED counts equal REST; PREFER, NEXTKEY, CALENDAR, REFRDEL readable; PROJPROP empty, no SCHEDOPTIONS. |
| Schedule options | Only through `GET /scheduleOptions` (32 fields, one row for the project). |
| Changed since | `LastUpdateDate:gt:'YYYY-MM-DD[ T]HH:MM:SS'` works; with an offset, 400. |
| Writes | PUT activity, POST and DELETE relationship as in 4.3; no optimistic lock (stale stamp accepted). |
| Schedule job | As in 4.4: Pending, then Complete after 5 s; scheduling log readable; P6 dates = this server's engine dates (0 differences on 214 activities, early and late dates, total and free float). |
| Baseline | `POST /project/assignProjectAsBaseline?OriginalProjectObjectId=<baseline project>&TargetProjectObjectId=<project>` turns the first project into a baseline of the second (it leaves the project list); `CurrentBaselineProjectObjectId` stays empty until `PUT /project` sets it. P6 web's Define Baselines Add created a copy even though the dialog was cancelled. |
